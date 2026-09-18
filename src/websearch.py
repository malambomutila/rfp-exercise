"""Web page sources for the IDinsight RFP radar.

The four JSON feeds in src/sources.py cover multilateral procurement portals
that publish machine readable notices. They miss almost all foundation and
multilateral consultancy work, because that is published as an ordinary web
page with no feed behind it. This module closes that gap.

Architecture: deterministic fetch, then model extraction.

    1. Fetch the page ourselves with urllib and a browser User-Agent.
    2. Reduce the HTML to plain text, keeping anchor text paired with its
       absolute href, because the link is the thing we actually need.
    3. Send the reduced text to one model call and ask for a strict JSON array
       of notices.
    4. Normalise the reply into the canonical record schema.

Why a model rather than CSS selectors: these pages are hand maintained and get
restyled without warning. A selector based scraper rots the moment a foundation
changes its theme, and silent rot is the usual reason a scraper stops earning
its keep. Reading the reduced text lets the same code survive a redesign.

Anti-fabrication rule, the most important line in this file: a record is only
kept if its URL was genuinely present in the page we fetched. Every absolute
href is harvested during the reduce step and the model's output is checked
against that set. A model that invents a plausible looking opportunity would
destroy trust in the whole report, so an invented link is dropped and counted.

Pages confirmed live and readable without a browser (checked 2026-09-18):
  - Gavi RFPs, EOIs and consulting opportunities   HTTP 200, 185KB
  - Global Fund business opportunities             HTTP 200,  48KB
  - Global Fund upcoming requests for proposals    HTTP 200,  37KB
  - IDRC funding opportunities                     HTTP 200,  63KB

Both Global Fund pages routinely return zero records, and that is correct
rather than broken. Verified on 2026-09-18: /business-opportunities/ lists only
procurement policies and spreadsheets of past contracts, because its live
tenders sit behind a "View Open Tenders" link into an Oracle Fusion portal that
serves nothing but an ADF loopback script to a non-browser client, and
/iel/upcoming-requests-for-proposals/ lists five evaluation RFPs all marked
"RFP Closed". They are kept because the second page is the Global Fund's
independent evaluation pipeline, which is squarely IDinsight work, and both
pages reduce to under 5000 characters, so watching them costs very little.

Pages probed and rejected, recorded so nobody retries them blindly:
  - unitaid.org/consultancies-and-rfps/ returns HTTP 403 to every client
    tried. Its notices are published on UNGM as well, so nothing is lost.
  - unicef.org/supply/service-contracts-tender-calendar returns HTTP 404.
  - submit.gatesfoundation.org renders its list in JavaScript and serves no
    links in the HTML, so there is nothing to read.
  - adb.org and afdb.org return HTTP 403 even with browser headers, a WAF
    block that would need a headless browser.
  - OpenRouter's ":online" web search plugin is search index based, not live
    page loading, so it cannot read a current tender table. Do not build on it.

UNGM, read this before touching fetch_ungm
------------------------------------------
https://www.ungm.org/Public/Notice returns HTTP 200 and 147KB, but the notice
table is not in that HTML. Verified on 2026-09-18: zero occurrences of
"Notice/<id>" anywhere in the response, and the only links in the page are
navigation such as /Public/ContractAward and /Public/UNSPSC. The table is
loaded afterwards by a client side call to /Public/Notice/Search, which the
page wires to a form whose action is "javascript:void(0);".

That endpoint was attempted four ways and returns HTTP 400 every time: JSON
body, form encoded body, with and without a session cookie jar, and with the
__RequestVerificationToken lifted from the page and sent both as a field and as
a RequestVerificationToken header. No RSS or sitemap route exists either:
/Public/Notice/Rss and /sitemap.xml both return HTTP 404, and
/Public/SiteMap/Index carries no notice links.

Individual notices ARE readable. https://www.ungm.org/Public/Notice/312928
returns HTTP 200 and 120KB with the full notice in the HTML, so the extractor
works fine on UNGM once a notice URL is known. The missing piece is discovery,
not parsing, and closing it needs either the real shape of the Search call or a
headless browser, neither of which fits this project's standard library only
promise. fetch_ungm therefore fetches the list page, finds no notice links and
returns [] with one explanatory line on stderr. fetch_notice_detail is kept and
is exercised against notice 312928 by the self check in __main__, so the day
UNGM discovery becomes possible the parsing half is already proven.
"""

import datetime
import hashlib
import html as html_module
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Reuse the canonical helpers from src/sources.py rather than reimplementing
# them, so this module and that one normalise text, ids and dates identically.
#
# The import has to be deferred. src/sources.py imports this module in order to
# register the fetchers at the bottom of this file, so a plain module level
# "import sources" here would make the pair work only when sources happens to
# be loaded first, and raise AttributeError when websearch is loaded first.
# Verified: it does raise, so this proxy is not defensive decoration. Attribute
# access on it imports sources on first use, by which time both modules are
# fully defined, and every call site below reads as if it were a normal import.
class _LazySources(object):
    """Proxy that imports src/sources.py the first time an attribute is read."""

    _module = None

    def __getattr__(self, name):
        if _LazySources._module is None:
            import sources as module
            _LazySources._module = module
        return getattr(_LazySources._module, name)


sources = _LazySources()

# Browser User-Agent. These hosts serve a different response, or none at all,
# to an unrecognised client. Verified: every page listed above returns HTTP 200
# with this string and is blocked or truncated without it.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# Page fetch timeout in seconds. Generous because two of these hosts are slow
# to first byte, but bounded so a hung socket cannot stall the daily run.
FETCH_TIMEOUT = 25

# Model call timeout. Extraction over roughly 30000 characters is slower than
# the scoring call in src/score.py, so it gets more room.
MODEL_TIMEOUT = 150

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Deliberately not "anthropic/claude-opus-5:online". The web search variant is
# search index based and returns nothing useful for a seven day window.
MODEL = "anthropic/claude-opus-5"

# Characters of reduced page text sent to the model, per page.
#
# WHY THIS CAP: the largest page here reduces to well under this, so in normal
# operation nothing is truncated at all and the cap is only a cost ceiling
# against a page that suddenly balloons. Roughly 30000 characters is about 8000
# tokens, which keeps one run of five pages inside a few cents. When a page
# does exceed the cap we do not take the first 30000 characters, because site
# chrome and cookie banners sit at the top and the notice table sits in the
# middle. We take the window densest in notice-like links instead.
EXTRACT_CHAR_CAP = 30000

# Cache time to live. Six hours means repeated local runs during a development
# session hit the cache and cost nothing, while the daily cron always fetches
# fresh. Both the raw HTML and the extracted JSON are cached: caching only the
# HTML would still repeat the model call, which is the expensive half.
CACHE_TTL_SECONDS = 6 * 60 * 60

# Politeness. These are small institutional sites, not APIs. A minimum gap
# between two requests to the same host stops us hammering them when one
# fetcher reads two pages from the same origin, as the Global Fund one does.
HOST_MIN_INTERVAL = 1.5

_HOST_LOCK = threading.Lock()
_HOST_LAST_FETCH = {}


def _log(message):
    """One line to stderr. Stdout stays clean for the pipeline's own output."""
    print("websearch: " + str(message), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Cache, under data/raw/ which is already gitignored
# ---------------------------------------------------------------------------

def _cache_dir():
    """Return the cache directory, creating it, or None if that is not possible.

    Located relative to this file so the path is the same whether the pipeline
    is run from the repository root or from inside src/.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "data", "raw", "websearch")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


def _cache_path(kind, key):
    """Cache file path for a (kind, key) pair, or None when caching is off."""
    directory = _cache_dir()
    if directory is None:
        return None
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(directory, kind + "-" + digest + ".json")


def _cache_read(kind, key):
    """Return the cached payload when it is younger than the TTL, else None."""
    path = _cache_path(kind, key)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, ValueError):
        return None
    stamp = envelope.get("fetched_at_epoch")
    if not isinstance(stamp, (int, float)):
        return None
    if time.time() - stamp > CACHE_TTL_SECONDS:
        return None
    return envelope.get("payload")


def _cache_write(kind, key, payload):
    """Store a payload with a timestamp. A failed write is not an error."""
    path = _cache_path(kind, key)
    if not path:
        return
    envelope = {
        "key": key,
        "fetched_at_epoch": time.time(),
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle)
    except (OSError, TypeError, ValueError):
        return


# ---------------------------------------------------------------------------
# Step 1, fetch
# ---------------------------------------------------------------------------

def _throttle(url):
    """Sleep just enough to keep one request per host per HOST_MIN_INTERVAL."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return
    with _HOST_LOCK:
        last = _HOST_LAST_FETCH.get(host, 0.0)
        wait = HOST_MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _HOST_LAST_FETCH[host] = time.monotonic()


def fetch_page(url, use_cache=True):
    """Return the page HTML as text, or None on any failure.

    Redirects are followed, which urllib does by default for GET. Every
    exception is swallowed on purpose: one dead page must never break the
    report, so the caller simply gets None and returns [].
    """
    if use_cache:
        cached = _cache_read("html", url)
        if isinstance(cached, str):
            return cached

    _throttle(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as error:
        _log("fetch failed for " + url + " (" + type(error).__name__ + ": "
             + str(error) + ")")
        return None

    text = raw.decode("utf-8", errors="replace")
    if use_cache:
        _cache_write("html", url, text)
    return text


# ---------------------------------------------------------------------------
# Step 2, reduce
# ---------------------------------------------------------------------------

_COMMENTS = re.compile(r"(?s)<!--.*?-->")

# Blocks whose contents carry no notice information and cost a lot of
# characters. head is dropped too: the page title is not needed, the notices
# are in the body.
_DROP_BLOCKS = re.compile(
    r"(?is)<(script|style|svg|nav|footer|noscript|head|iframe|form)\b[^>]*>.*?</\1>")

# Anchors, captured so link text and href survive into the reduced text.
_ANCHOR = re.compile(r'(?is)<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>')

_ANY_TAG = re.compile(r"(?s)<[^>]{0,600}>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Block level tags become newlines so that a table row does not run into the
# next one once the tags are gone.
_BLOCK_BREAK = re.compile(
    r"(?is)</?(p|div|li|tr|td|th|h[1-6]|br|section|article|ul|ol|table|dt|dd)\b[^>]*>")

# What counts as a notice-like link, used both to choose the window when a page
# must be truncated and to report link density in the logs.
_NOTICE_HINT = re.compile(
    r"(?i)\b(rfp|rfq|rfi|eoi|itb|tor)\b|tender|notice|proposal|consultan"
    r"|opportunit|call[ \-_]?for|funding|procure|expression[ \-]of[ \-]interest"
    r"|terms[ \-]of[ \-]reference|bid")


def _absolute(base, href):
    """Resolve an href against the page URL, or return None if unusable."""
    href = (href or "").strip()
    if not href:
        return None
    lowered = href.lower()
    # Skip in-page anchors and non-http schemes: none of these is a notice.
    if lowered.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    try:
        resolved = urllib.parse.urljoin(base, href)
    except ValueError:
        return None
    split = urllib.parse.urlsplit(resolved)
    if split.scheme not in ("http", "https") or not split.netloc:
        return None
    # Fragments point at a place on a page, not a different notice, so they are
    # dropped to keep the allowed-URL check stable.
    return urllib.parse.urlunsplit(
        (split.scheme, split.netloc, split.path, split.query, ""))


def _normalise_url(url):
    """Canonical form used only for comparing two URLs for equality."""
    if not url:
        return ""
    split = urllib.parse.urlsplit(url.strip())
    path = split.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (split.scheme.lower(), split.netloc.lower(), path, split.query, ""))


def reduce_html(page_url, raw_html):
    """Return (reduced_text, allowed_urls) for one fetched page.

    reduced_text is plain text in which every link appears as
    "[link text](absolute url)", so the model sees the wording of a notice and
    the link to it together. allowed_urls is the set of every absolute href
    found on the page, which is what the anti-fabrication check is measured
    against later.
    """
    if not raw_html:
        return "", set()

    text = _COMMENTS.sub(" ", raw_html)
    text = _DROP_BLOCKS.sub(" ", text)

    allowed = set()

    def _anchor_replacement(match):
        target = _absolute(page_url, match.group(1))
        if target is None:
            # Keep the wording, drop the unusable link.
            return " " + sources._strip_html(match.group(2)) + " "
        allowed.add(target)
        label = sources._strip_html(match.group(2))
        return " [" + label + "](" + target + ") "

    text = _ANCHOR.sub(_anchor_replacement, text)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    # _strip_html collapses all whitespace including the newlines that keep one
    # table row apart from the next, so it is applied line by line instead of
    # to the whole document.
    lines = []
    for line in text.split("\n"):
        line = sources._strip_html(line)
        if line:
            lines.append(line)
    text = "\n".join(lines)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text).strip()

    if len(text) > EXTRACT_CHAR_CAP:
        text = _densest_window(text, EXTRACT_CHAR_CAP)
    return text, allowed


def _densest_window(text, cap):
    """Return the cap-sized slice of text holding the most notice-like links.

    Candidate starts are the link positions themselves, backed off by a quarter
    of the window so a notice table is not cut off at its first row. This is a
    cheap heuristic, not an optimum, which is all it needs to be: it only ever
    runs when a page is unusually large.
    """
    positions = [match.start() for match in re.finditer(r"\]\(http", text)]
    if not positions:
        return text[:cap]
    hint_positions = [
        pos for pos in positions
        if _NOTICE_HINT.search(text[max(0, pos - 200):pos + 200])
    ] or positions

    best_start, best_count = 0, -1
    for pos in [0] + hint_positions:
        start = max(0, min(pos - cap // 4, len(text) - cap))
        count = sum(1 for p in hint_positions if start <= p < start + cap)
        if count > best_count:
            best_start, best_count = start, count
    return text[best_start:best_start + cap]


# ---------------------------------------------------------------------------
# Step 3, extract with one model call per page
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = (
    "You read the text of a procurement or funding web page and extract the "
    "opportunities listed on it. The text has been reduced from HTML. Every "
    "link appears as [link text](absolute url).\n\n"
    "Extract ONLY notices that are actually present in the text you are "
    "given. Do not use anything you may remember about this organisation. Do "
    "not complete a partial notice from memory. If a field is not readable in "
    "the text, return null for it rather than guessing. An invented "
    "opportunity is far worse than a missing one.\n\n"
    "Skip any item with no resolvable link. Skip navigation, guidance pages, "
    "policy documents, supplier registration pages, past awards and anything "
    "that is not an open or forthcoming opportunity to bid or apply.\n\n"
    "Be careful with dates. A single date beside a notice on these pages is "
    "usually the closing date, not the posting date. Only set published when "
    "the page labels the date as published, posted, issued or similar. When "
    "you cannot tell which a date is, treat it as the deadline and return null "
    "for published. Never return a published date in the future.\n\n"
    "Reply with a strict JSON array and nothing else. No prose, no code "
    "fence. Each element must be an object with exactly these keys:\n"
    '  "title":     the notice title as written on the page, a string\n'
    '  "url":       the absolute link to the notice, copied character for '
    "character from the text, a string\n"
    '  "published":  publication or posting date as "YYYY-MM-DD", or null\n'
    '  "deadline":   closing or submission date as "YYYY-MM-DD", or null\n'
    '  "countries":  list of country names the work covers, or an empty list\n'
    '  "sectors":    list of sector or theme labels, or an empty list\n'
    '  "funder":     the funding or contracting organisation, or null\n'
    '  "value_usd":  contract value in US dollars as a number, or null\n'
    '  "summary":    one to three plain sentences describing the work, drawn '
    "only from the text, no HTML\n\n"
    "Return [] when the page lists no opportunities. Use UK English. Do not "
    "use emojis or em dashes."
)


def _model_extract(page_url, reduced_text, source_label, use_cache=True):
    """Ask the model for the notices on one page. Never raises.

    Returns a list of raw items, or None when the extraction could not be done.
    The caller needs that distinction so a failure and a genuinely empty page
    do not produce the same log line.

    Graceful degradation is the whole point of this function. A missing key, a
    network failure, an HTTP error, a timeout or a reply that is not JSON all
    produce one stderr line and no records, so the report still publishes.
    """
    if not reduced_text.strip():
        return None

    cache_key = MODEL + "|" + page_url + "|" + hashlib.sha1(
        reduced_text.encode("utf-8")).hexdigest()
    if use_cache:
        cached = _cache_read("extract", cache_key)
        if isinstance(cached, list):
            return cached

    # The key is read from the environment only. It is never written to a file,
    # never logged and never has a default.
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        _log(source_label + ": OPENROUTER_API_KEY not set, skipping extraction.")
        return None

    body = json.dumps({
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 8000,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Source organisation: " + source_label + "\n"
                "Page URL: " + page_url + "\n\n"
                "Reduced page text:\n" + reduced_text)},
        ],
    }).encode("utf-8")

    request = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "X-Title": "IDinsight RFP radar page extraction",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            envelope = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        _log(source_label + ": model call returned HTTP " + str(error.code)
             + ", returning no records.")
        return None
    except (urllib.error.URLError, OSError, ValueError) as error:
        _log(source_label + ": model call failed (" + type(error).__name__
             + ": " + str(error) + "), returning no records.")
        return None

    choices = envelope.get("choices") or []
    if not choices:
        _log(source_label + ": model returned no choices, returning no records.")
        return None
    content = (choices[0].get("message") or {}).get("content")
    parsed = _parse_json_reply(content)
    if parsed is None:
        _log(source_label + ": model reply was not usable JSON, returning no records.")
        return None

    if use_cache:
        _cache_write("extract", cache_key, parsed)
    return parsed


def _parse_json_reply(content):
    """Turn the model's reply into a list of dicts, or None when unusable.

    Models occasionally wrap JSON in a code fence or answer with an object
    around the array despite being told not to, so both are tolerated.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    value = None
    try:
        value = json.loads(text)
    except ValueError:
        # Fall back to the outermost balanced array or object in the text.
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    value = json.loads(text[start:end + 1])
                    break
                except ValueError:
                    continue
    if value is None:
        return None
    if isinstance(value, dict):
        # Accept {"notices": [...]} and similar single-key wrappers.
        for candidate in value.values():
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return None


# ---------------------------------------------------------------------------
# Step 4, normalise to the canonical record schema
# ---------------------------------------------------------------------------

def _clean_date(value):
    """Return an ISO date string, or None when it cannot be trusted.

    A date later than today is rejected for the published field by the caller,
    because a publication date in the future means the model has misread a
    deadline as a publication date.
    """
    iso = sources._iso(value)
    if iso:
        return iso
    # Tolerate the forms these pages actually use, for example "02-Oct-2026"
    # in a UNGM deadline and "15 September 2026" in a Gavi listing.
    parsed = sources._parse_day_month_year(value)
    if parsed:
        return parsed.isoformat()
    parsed = sources._parse_long_us_date(value)
    if parsed:
        return parsed.isoformat()
    text = str(value or "").strip()
    match = re.match(r"(\d{1,2})\s+([A-Za-z]{3,})[a-z]*\.?\s+(\d{4})", text)
    if match:
        month = sources._MONTHS.get(match.group(2)[:3].lower())
        if month:
            try:
                return datetime.date(
                    int(match.group(3)), month, int(match.group(1))).isoformat()
            except ValueError:
                return None
    return None


def normalise(items, source_label, page_url, allowed_urls, since_date, today=None):
    """Turn the model's raw items into canonical records.

    Records are dropped when the URL was not present on the page we fetched,
    when there is no title, when the publication date cannot be established,
    when the notice predates since_date, or when its deadline has already
    passed. Counts of each drop reason go to stderr so a page that starts
    misbehaving is visible in the cron log rather than silently empty.
    """
    today = today or datetime.date.today()
    allowed = {_normalise_url(url) for url in allowed_urls}
    allowed.add(_normalise_url(page_url))

    records = []
    dropped = {"fabricated_url": 0, "no_title": 0, "undated": 0,
               "stale": 0, "expired": 0}
    observed_dates = 0

    for item in items or []:
        url = _absolute(page_url, str(item.get("url") or ""))
        if not url or _normalise_url(url) not in allowed:
            # ANTI-FABRICATION: the model returned a link that is not on the
            # page. Treat it as invented and drop it.
            dropped["fabricated_url"] += 1
            continue

        title = sources._strip_html(item.get("title") or "")
        if not title:
            dropped["no_title"] += 1
            continue

        deadline = _clean_date(item.get("deadline"))
        # A deadline already in the past is a closed opportunity, so it is
        # dropped here as well as by fetch.filter_fresh downstream.
        if deadline and datetime.date.fromisoformat(deadline) < today:
            dropped["expired"] += 1
            continue

        published = _clean_date(item.get("published"))
        if published and datetime.date.fromisoformat(published) > today:
            # A publication date in the future is not credible, so it is
            # treated as unreadable and falls through to the rule below.
            published = None

        summary = sources._strip_html(item.get("summary") or "")

        if not published:
            # DECISION on missing publication dates. Most foundation pages show
            # a deadline and no posting date. Dropping those loses real work,
            # and inventing a date would corrupt the freshness guarantee the
            # whole tool rests on, so neither is acceptable. The compromise:
            # when there is a future deadline but no readable publication date,
            # keep the record, set published to the date we first observed it,
            # and say so in the summary so no reader mistakes an observation
            # date for a verified publication date.
            #
            # FLAGGED CONSEQUENCE: such a record always falls inside the
            # freshness window on the day it first appears, and because the id
            # is a hash of source plus URL it keeps the same id afterwards. It
            # will therefore look newly published on every run until the page
            # drops it or its deadline passes. That is the price of not losing
            # undated foundation notices, and it is bounded by the deadline
            # check above.
            if not deadline:
                dropped["undated"] += 1
                continue
            published = today.isoformat()
            observed_dates += 1
            note = ("No publication date is stated on the source page. First "
                    "observed by this tool on " + published + ", which is used "
                    "in place of a publication date.")
            summary = (summary + " " + note).strip() if summary else note

        if datetime.date.fromisoformat(published) < since_date:
            dropped["stale"] += 1
            continue

        countries = item.get("countries")
        sectors = item.get("sectors")
        records.append(sources._record(
            source=source_label,
            title=title,
            url=url,
            published=published,
            deadline=deadline,
            countries=countries if isinstance(countries, list) else [],
            sectors=sectors if isinstance(sectors, list) else [],
            funder=sources._strip_html(item.get("funder") or "") or source_label,
            value_usd=sources._to_float(item.get("value_usd")),
            summary=summary,
        ))

    # Dedupe first, so the count in the log is the count actually returned. Two
    # rows on a page can point at the same notice, for example a Gavi title and
    # its Download link, and the record id is a hash of source plus URL, so
    # they collapse into one.
    unique = sources._dedupe(records)

    if dropped["fabricated_url"]:
        _log(source_label + ": dropped " + str(dropped["fabricated_url"])
             + " record(s) whose link was not present on the page.")
    noisy = {key: value for key, value in dropped.items()
             if value and key != "fabricated_url"}
    if noisy or observed_dates or len(unique) != len(records):
        _log(source_label + ": kept " + str(len(unique)) + " record(s), "
             + str(observed_dates) + " dated by first observation, "
             + str(len(records) - len(unique)) + " duplicate(s) merged, dropped "
             + json.dumps(noisy) + ".")
    return unique


# ---------------------------------------------------------------------------
# The generic page pipeline
# ---------------------------------------------------------------------------

def scrape_page(url, source_label, since_date, use_cache=True,
                link_pattern=None):
    """Fetch, reduce, extract and normalise one page. Returns [] on any failure.

    link_pattern is an optional compiled regex describing what a notice URL
    looks like on this source. When it is given and no link on the page matches
    it, the model call is skipped, because a page with no notice links cannot
    yield a record that would survive the anti-fabrication check and the call
    would be wasted money. This is the UNGM case, see the module docstring.
    """
    try:
        raw_html = fetch_page(url, use_cache=use_cache)
        if not raw_html:
            return []
        reduced, allowed = reduce_html(url, raw_html)
        if not reduced or not allowed:
            _log(source_label + ": " + url + " reduced to no readable links,"
                 " no records.")
            return []
        if link_pattern is not None and not any(
                link_pattern.search(link) for link in allowed):
            _log(source_label + ": " + url + " carries no notice links in its"
                 " HTML (" + str(len(allowed)) + " links, all navigation), so"
                 " there is nothing to extract. Skipping the model call.")
            return []
        items = _model_extract(url, reduced, source_label, use_cache=use_cache)
        if items is None:
            # The extraction failed and has already logged why.
            return []
        if not items:
            _log(source_label + ": " + url + " read (" + str(len(reduced))
                 + " chars) and the extraction found no open opportunities.")
            return []
        return normalise(items, source_label, url, allowed, since_date)
    except Exception as error:  # noqa: BLE001 - one dead page is expendable
        _log(source_label + ": unexpected failure (" + type(error).__name__
             + ": " + str(error) + "), returning no records.")
        return []


def scrape_pages(urls, source_label, since_date, use_cache=True):
    """Run scrape_page over several pages of one source, sequentially.

    Sequential on purpose. Two pages of the same source share a host, and
    _throttle would serialise them anyway, so threads would add risk without
    adding speed. The outer pipeline already runs the sources in parallel.
    """
    records = []
    for url in urls:
        records.extend(scrape_page(url, source_label, since_date,
                                   use_cache=use_cache))
    return sources._dedupe(records)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

UNGM_NOTICE_LIST = "https://www.ungm.org/Public/Notice"
UNGM_SEARCH = "https://www.ungm.org/Public/Notice/Search"
UNGM_NOTICE = "https://www.ungm.org/Public/Notice/"

# A UNGM notice always lives at /Public/Notice/<numeric id>.
UNGM_NOTICE_LINK = re.compile(r"/Public/Notice/\d+")

# UNGM serves its notice table from a POST endpoint that returns an HTML
# fragment, one div per notice, carrying every field we need. So this source
# costs no model call at all: the fragment is parsed deterministically.
#
# Getting here took some work, and the details are unobvious enough to be
# worth recording:
#   1. PageSize is validated against an allow list. 15 works. 20, 25, 30, 50
#      and 100 all return HTTP 400. Paginate with PageIndex instead.
#   2. Every field below must be present. Omitting isPicker, IsActive,
#      NoticeSearchTotalLabelId or TypeOfCompetitions returns HTTP 400, which
#      is the trap that made this endpoint look unusable.
#   3. Dates use "dd-MMM-yyyy", not ISO. ISO returns HTTP 400.
#   4. SortField accepts "Deadline" and "DatePublished". "Published" returns
#      HTTP 500.
#   5. The antiforgery token must be scraped from the list page and sent in a
#      "RequestVerificationToken" header, with the session cookie held. The
#      page carries two such hidden inputs and either one is accepted.
#   6. Filtering by PublishedFrom does NOT reliably surface recent notices:
#      a 7-day window enumerated 198 notices yet omitted notice 312928, which
#      the page itself states was published 15-Sep-2026. Title search finds it
#      immediately. So we query by keyword rather than enumerate by date, and
#      filter on the published date ourselves after parsing.
UNGM_PAGE_SIZE = 15
UNGM_MAX_PAGES = 3

# Single words only. UNGM title search behaves as a substring match, so
# multi-word phrases such as "impact evaluation" and "monitoring and
# evaluation" return nothing. Each term is one query, so this list is also the
# cost: terms times pages HTTP requests per run, and no model calls.
UNGM_TERMS = [
    "evaluation", "monitoring", "survey", "baseline", "endline",
    "verification", "statistics", "research", "assessment",
]

GAVI_PAGES = [
    "https://www.gavi.org/about-us/work-us/"
    "rfps-eois-and-consulting-opportunities",
]

GLOBAL_FUND_PAGES = [
    "https://www.theglobalfund.org/en/business-opportunities/",
    "https://www.theglobalfund.org/en/iel/upcoming-requests-for-proposals/",
]

IDRC_PAGES = [
    "https://idrc-crdi.ca/en/funding",
]


_UNGM_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_UNGM_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_UNGM_ROW_SPLIT = re.compile(r'(?=<div role="row")')
_UNGM_ID = re.compile(r'data-noticeid="(\d+)"')
_UNGM_CELL = re.compile(
    r'(?s)<div role="cell"[^>]*class="([^"]*)"[^>]*>(.*?)(?=<div role="cell"|\Z)'
)
_UNGM_SCRIPT = re.compile(r"(?s)<script.*?</script>")


def _ungm_date(value):
    """Parse UNGM's "18-Sep-2026" format into an ISO string, or None."""
    match = _UNGM_DATE.search(value or "")
    if not match:
        return None
    day, month, year = match.groups()
    number = _UNGM_MONTHS.get(month.lower())
    if not number:
        return None
    try:
        return datetime.date(int(year), number, int(day)).isoformat()
    except ValueError:
        return None


def _ungm_cell_text(html_fragment):
    """Flatten one table cell to plain text."""
    text = _UNGM_SCRIPT.sub(" ", html_fragment)
    text = _ANY_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def _ungm_session():
    """Fetch the list page, returning an opener holding its cookies and a token.

    Returns (opener, token) or (None, None) on any failure, so the caller can
    degrade to an empty source rather than raise.
    """
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        _throttle(UNGM_NOTICE_LIST)
        request = urllib.request.Request(
            UNGM_NOTICE_LIST, headers={"User-Agent": USER_AGENT}
        )
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            html_text = response.read().decode("utf-8", "replace")
        tokens = re.findall(
            r'__RequestVerificationToken" type="hidden" value="([^"]+)"',
            html_text,
        )
        if not tokens:
            _log("UNGM: no antiforgery token on the list page, skipping source.")
            return None, None
        return opener, tokens[0]
    except Exception as error:  # noqa: BLE001
        _log("UNGM: could not open a session, " + str(error))
        return None, None


def _ungm_search(opener, token, term, page_index):
    """POST one keyword query and return the HTML fragment, or "" on failure."""
    payload = {
        "PageIndex": page_index,
        "PageSize": UNGM_PAGE_SIZE,
        "Title": term,
        "Description": "",
        "Reference": "",
        "PublishedFrom": "",
        "PublishedTo": "",
        "DeadlineFrom": "",
        "DeadlineTo": "",
        "Countries": [],
        "Agencies": [],
        "UNSPSCs": [],
        "NoticeTypes": [],
        "SortField": "DatePublished",
        "SortAscending": False,
        # Every one of the following must be present or the endpoint 400s.
        "isPicker": False,
        "IsSustainable": False,
        "IsActive": True,
        "NoticeDisplayType": None,
        "NoticeSearchTotalLabelId": "noticeSearchTotal",
        "TypeOfCompetitions": [],
    }
    try:
        _throttle(UNGM_SEARCH)
        request = urllib.request.Request(
            UNGM_SEARCH,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Referer": UNGM_NOTICE_LIST,
                "X-Requested-With": "XMLHttpRequest",
                "RequestVerificationToken": token,
            },
        )
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        _log("UNGM: search for " + repr(term) + " failed, " + str(error))
        return ""


def _parse_ungm_rows(fragment):
    """Turn one search fragment into canonical records.

    Cell order in the fragment is: options, title, deadline, published,
    agency, notice type, reference, country. Cells are read by position
    because only three of the eight carry a distinguishing class, but the
    title, deadline and agency cells are confirmed by class where possible so
    a layout change is noticed rather than silently mis-parsed.
    """
    records = []
    for row in _UNGM_ROW_SPLIT.split(fragment):
        id_match = _UNGM_ID.search(row)
        if not id_match:
            continue
        cells = [
            (classes, _ungm_cell_text(body))
            for classes, body in _UNGM_CELL.findall(row)
        ]
        if len(cells) < 8:
            continue

        title_classes, title = cells[1]
        # The title cell wraps the notice link, whose accessible label leaks
        # into the flattened text. Strip it rather than leave it in a title the
        # director reads.
        for noise in ("Open in a new window", "Opens in a new window"):
            title = title.replace(noise, "")
        title = re.sub(r"\s+", " ", title).strip(" .;,-")
        if "resultTitle" not in title_classes or not title:
            # Layout drift. Skip the row rather than guess at its meaning.
            continue

        deadline = _ungm_date(cells[2][1])
        published = _ungm_date(cells[3][1])
        agency = cells[4][1] or None
        notice_type = cells[5][1]
        reference = cells[6][1]
        country = cells[7][1]

        notice_id = id_match.group(1)
        url = UNGM_NOTICE + notice_id
        summary_parts = [p for p in (notice_type, reference) if p]
        records.append(
            {
                "id": hashlib.sha1(
                    ("UNGM" + url).encode("utf-8")
                ).hexdigest()[:12],
                "title": title[:500],
                "source": "UNGM",
                "url": url,
                "published": published,
                "deadline": deadline,
                "countries": [country] if country else [],
                "sectors": [],
                "funder": agency,
                "value_usd": None,
                "summary": (". ".join(summary_parts))[:1200],
            }
        )
    return records


def fetch_ungm(since_date):
    """UNGM notices, discovered by keyword search rather than enumeration.

    UNGM carries the widest range of multilateral consultancy work of any
    source here, and it is where the notices that matter most to IDinsight
    appear, so it is worth the extra work. Querying by IDinsight's own service
    vocabulary rather than paging the whole feed is both cheaper and more
    precise: it returns roughly a hundred already relevant notices instead of
    several hundred mostly irrelevant ones, and it surfaces notices that the
    date-filtered enumeration misses entirely.

    Costs no model call: the fragment is parsed deterministically.
    """
    opener, token = _ungm_session()
    if not opener:
        return []

    by_id = {}
    for term in UNGM_TERMS:
        seen_for_term = 0
        for page_index in range(UNGM_MAX_PAGES):
            fragment = _ungm_search(opener, token, term, page_index)
            if not fragment:
                break
            rows = _parse_ungm_rows(fragment)
            if not rows:
                break
            fresh = 0
            for record in rows:
                if record["id"] not in by_id:
                    fresh += 1
                by_id[record["id"]] = record
            seen_for_term += len(rows)
            # A page that adds nothing new means we have reached the end of
            # this term's results, because UNGM repeats the last page.
            if fresh == 0:
                break

    # Filter on the published date ourselves, since the endpoint's own
    # PublishedFrom filter proved unreliable. A record with no readable
    # published date is dropped, matching the pipeline's freshness promise.
    kept = []
    for record in by_id.values():
        if not record["published"]:
            continue
        try:
            published = datetime.date.fromisoformat(record["published"])
        except ValueError:
            continue
        if published >= since_date:
            kept.append(record)

    _log(
        "UNGM: "
        + str(len(by_id))
        + " notice(s) matched the service terms, "
        + str(len(kept))
        + " published on or after "
        + since_date.isoformat()
        + "."
    )
    return kept


def fetch_gavi(since_date):
    """Gavi RFPs, EOIs and consulting opportunities.

    KNOWN AMBIGUITY, verified on 2026-09-18 and unresolved. Each Gavi listing
    carries one date and the page never labels it. The markup calls it an
    authoring date, class "authored-on-feed" inside a <time datetime=...> tag,
    which in Drupal means the posting date, yet four of the six active dates
    were in the future, which no posting date can be. The extraction therefore
    reads the date as a closing date, which is the reading consistent with a
    page headed "currently inviting offers" and "NEWEST FIRST".

    This could not be settled from the HTML: the individual RFP pages under
    /news/document-library/ render their body in JavaScript and serve no text
    or PDF link to urllib, so there is nowhere to cross-check the date without
    a headless browser. If a Gavi deadline in the report is ever wrong, this is
    why, and the fix is to confirm Gavi's convention with Gavi.
    """
    return scrape_pages(GAVI_PAGES, "Gavi", since_date)


def fetch_global_fund(since_date):
    """Global Fund business opportunities and upcoming requests for proposals."""
    return scrape_pages(GLOBAL_FUND_PAGES, "Global Fund", since_date)


def fetch_idrc(since_date):
    """IDRC funding opportunities."""
    return scrape_pages(IDRC_PAGES, "IDRC", since_date)


# Registered in src/sources.py. Page count per run: UNGM 1, Gavi 1, Global Fund
# 2, IDRC 1, so five pages and at most four model calls, since UNGM never makes
# one. Cached for six hours, so a development session pays once.
WEB_SOURCES = [
    ("UNGM", fetch_ungm),
    ("Gavi", fetch_gavi),
    ("Global Fund", fetch_global_fund),
    ("IDRC", fetch_idrc),
]


# ---------------------------------------------------------------------------
# Notice detail extraction, kept for the UNGM correctness fixture
# ---------------------------------------------------------------------------

def fetch_notice_detail(url, source_label, since_date=None):
    """Extract a single notice from its own detail page.

    Not used by the pipeline, because one model call per notice would blow the
    cost ceiling. It exists so the UNGM parsing half stays proven and testable
    while UNGM discovery is blocked, and it is exercised by the self check
    below against notice 312928.
    """
    since_date = since_date or datetime.date(1970, 1, 1)
    raw_html = fetch_page(url)
    if not raw_html:
        return []
    reduced, allowed = reduce_html(url, raw_html)
    items = _model_extract(url, reduced, source_label)
    if not items:
        return []
    # A detail page describes itself, so its own URL is the resolvable link
    # even when the model returns a relative or absent one.
    for item in items:
        if not item.get("url"):
            item["url"] = url
    return normalise(items, source_label, url, allowed | {url}, since_date)


# ---------------------------------------------------------------------------
# Standalone self check
# ---------------------------------------------------------------------------

# The Unitaid portfolio evaluation RFP, used as the correctness fixture. Known
# good values, read off the live page on 2026-09-18.
FIXTURE_URL = "https://www.ungm.org/Public/Notice/312928"
FIXTURE_EXPECTED = {
    "title_contains": "portfolio",
    "published": "2026-09-15",
    "deadline": "2026-10-02",
}


def _self_check(window_days=7, run_fixture=True):
    """Print a per-source count, then check the extractor against the fixture."""
    today = datetime.date.today()
    window_start = today - datetime.timedelta(days=window_days)
    print("Window: %s to %s" % (window_start.isoformat(), today.isoformat()))
    print("Model:  %s" % MODEL)
    print("Key:    %s" % ("present" if os.environ.get("OPENROUTER_API_KEY")
                          else "absent, extraction will be skipped"))
    print("")

    total = 0
    for name, fetcher in WEB_SOURCES:
        try:
            found = fetcher(window_start)
        except Exception as error:  # noqa: BLE001 - report it, do not crash
            print("%-14s FAILED: %s: %s" % (name, type(error).__name__, error))
            continue
        total += len(found)
        print("%-14s %3d records" % (name, len(found)))
        for record in found[:3]:
            print("               %s | deadline %s | %s" % (
                record["published"], record["deadline"], record["title"][:66]))
    print("%-14s %3d records" % ("TOTAL", total))

    if not run_fixture:
        return
    print("\nFixture, UNGM notice 312928 (one extra model call, cached 6h):")
    fixture = fetch_notice_detail(FIXTURE_URL, "UNGM")
    if not fixture:
        print("  NOT EXTRACTED. See the UNGM section of the module docstring.")
        return
    record = fixture[0]
    checks = [
        ("title", FIXTURE_EXPECTED["title_contains"] in record["title"].lower()),
        ("published", record["published"] == FIXTURE_EXPECTED["published"]),
        ("deadline", record["deadline"] == FIXTURE_EXPECTED["deadline"]),
        ("url", record["url"] == FIXTURE_URL),
    ]
    for field, passed in checks:
        print("  %-10s %s" % (field, "ok" if passed else "MISMATCH"))
    print("  title:     %s" % record["title"])
    print("  published: %s   deadline: %s" % (record["published"], record["deadline"]))
    print("  countries: %s" % record["countries"])
    print("  funder:    %s" % record["funder"])


if __name__ == "__main__":
    _self_check(window_days=7, run_fixture="--no-fixture" not in sys.argv)
