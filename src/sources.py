"""Source registry for the IDinsight RFP radar.

Each source exposes a function fetch_<name>(since_date) that returns a list of
canonical records. since_date is a datetime.date and only notices published on
or after that date are returned.

Canonical record keys (exactly these, in this order):
    id, title, source, url, published, deadline, countries, sectors, funder,
    value_usd, summary

Design rules that this module follows:
  - Python standard library only. No requests, no pandas, no parsers.
  - Every fetcher is defensive. Network problems, schema drift and unparseable
    dates return [] or skip the record, they never raise.
  - Every endpoint below was hit with curl and the real JSON or HTML shape was
    inspected before the fetcher was written.

Endpoints confirmed live (checked 2026-09-18):
  1. World Bank procurement notices, search.worldbank.org/api/v2/procnotices
  2. UNDP procurement notices, procurement-notices.undp.org (server rendered HTML)
  3. Grants.gov search2, api.grants.gov/v1/api/search2 (POST JSON, no key)
  4. TED, api.ted.europa.eu/v3/notices/search (POST JSON, no key)

Candidates dropped, with the reason, so nobody retries them blindly:
  - ReliefWeb. api.reliefweb.int/v1/* now returns HTTP 410 "version v1 has been
    decommissioned". The replacement /v2/* returns HTTP 403 "You are not using
    an approved appname" for every appname tried, so it is key gated in practice
    and cannot run unattended in CI. Dropped.
  - World Bank projects API, /api/v2/projects. Live, but stale: sorting by
    board approval date descending returns December 2024 as the newest record,
    so it carries no freshness signal. /api/v3/wbprojects and /api/v3/procnotices
    both return HTTP 404. Dropped.
  - finances.worldbank.org Socrata datasets. The /resource/*.json path now
    302-redirects into the HTML data portal, so it serves no JSON. Dropped.
  - Asian Development Bank (www.adb.org/projects/tenders) and African
    Development Bank procurement pages. Both return HTTP 403 to non-browser
    clients, a WAF block that would need a headless browser. Dropped.
  - SAM.gov opportunities. Requires an API key. Excluded by the no-key rule.
  - IATI datastore. Returns HTTP 401 without a subscription key. Dropped.
"""

import concurrent.futures
import datetime
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# Polite identification. Some of these hosts rate limit anonymous clients, and
# a contactable User-Agent is the minimum courtesy when polling daily.
USER_AGENT = "IDinsight-RFP-Radar/1.0 (+https://rfp.malambomutila.com)"

# Hard network timeout in seconds, applied to every request in this module.
HTTP_TIMEOUT = 20

# Canonical summary length, per the record schema.
SUMMARY_LIMIT = 1200

# ASSUMPTION: we have no FX feed in the standard library, so non-USD contract
# values are converted with these static rates. They are indicative only and are
# used solely to give the reader an order of magnitude. Rates as of Q3 2026.
FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CHF": 1.12,
    "SEK": 0.095,
    "DKK": 0.145,
    "NOK": 0.094,
    "PLN": 0.25,
    "CZK": 0.043,
    "RON": 0.22,
    "HUF": 0.0028,
    "BGN": 0.55,
}

# ISO3 to country name for the codes TED actually returns, plus the IDinsight
# priority countries so that a non-EU buyer is named rather than left as a code.
ISO3_NAMES = {
    "AUT": "Austria", "BEL": "Belgium", "BGR": "Bulgaria", "CHE": "Switzerland",
    "CYP": "Cyprus", "CZE": "Czechia", "DEU": "Germany", "DNK": "Denmark",
    "ESP": "Spain", "EST": "Estonia", "FIN": "Finland", "FRA": "France",
    "GBR": "United Kingdom", "GRC": "Greece", "HRV": "Croatia", "HUN": "Hungary",
    "IRL": "Ireland", "ISL": "Iceland", "ITA": "Italy", "LIE": "Liechtenstein",
    "LTU": "Lithuania", "LUX": "Luxembourg", "LVA": "Latvia", "MLT": "Malta",
    "NLD": "Netherlands", "NOR": "Norway", "POL": "Poland", "PRT": "Portugal",
    "ROU": "Romania", "SVK": "Slovakia", "SVN": "Slovenia", "SWE": "Sweden",
    "BGD": "Bangladesh", "BFA": "Burkina Faso", "ETH": "Ethiopia",
    "GHA": "Ghana", "IDN": "Indonesia", "IND": "India", "KEN": "Kenya",
    "KHM": "Cambodia", "LBR": "Liberia", "MAR": "Morocco", "MLI": "Mali",
    "MOZ": "Mozambique", "MWI": "Malawi", "NER": "Niger", "NGA": "Nigeria",
    "NPL": "Nepal", "PAK": "Pakistan", "PHL": "Philippines", "RWA": "Rwanda",
    "SEN": "Senegal", "SLE": "Sierra Leone", "TZA": "Tanzania", "UGA": "Uganda",
    "VNM": "Vietnam", "ZMB": "Zambia",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fetch_bytes(url, payload=None, content_type=None):
    """Fetch a URL and return the raw body, or None on any failure.

    payload, when given, is sent as the request body which makes the call a POST.
    Every exception is swallowed on purpose: one unreachable source must never
    stop the daily report from being produced.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
    }
    if content_type:
        headers["Content-Type"] = content_type
    try:
        request = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _get_json(url):
    """GET a URL and decode JSON, returning None if either step fails."""
    raw = _fetch_bytes(url)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _post_json(url, body):
    """POST a JSON body and decode the JSON response, or None on failure."""
    try:
        payload = json.dumps(body).encode("utf-8")
    except (TypeError, ValueError):
        return None
    raw = _fetch_bytes(url, payload=payload, content_type="application/json")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _get_text(url):
    """GET a URL and decode it as text, or None on failure."""
    raw = _fetch_bytes(url)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


_BLOCK_TAGS = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>")
_SCRIPTS = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_ANY_TAG = re.compile(r"<[^>]{0,400}>")
_WHITESPACE = re.compile(r"\s+")


def _strip_html(raw):
    """Turn a source HTML fragment into flat plain text.

    Tags are removed with a regex rather than a parser because the project is
    restricted to the standard library. Block level tags become spaces first so
    that words do not run together, then entities are unescaped, then any tag
    revealed by unescaping is removed in a second pass.
    """
    if not raw:
        return ""
    text = _SCRIPTS.sub(" ", str(raw))
    text = _BLOCK_TAGS.sub(" ", text)
    text = _ANY_TAG.sub(" ", text)
    text = html.unescape(text)
    text = _ANY_TAG.sub(" ", text)
    text = text.replace("\xa0", " ")
    return _WHITESPACE.sub(" ", text).strip()


def _trim(text, limit=SUMMARY_LIMIT):
    """Trim plain text to the schema limit, cutting on a word boundary."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "..."


def _make_id(source, url):
    """Stable record id, per the agreed schema."""
    return hashlib.sha1((source + url).encode("utf-8")).hexdigest()[:12]


def _iso(value):
    """Return the date part of an ISO style timestamp, or None.

    Accepts "2026-10-29T00:00:00Z", "2026-09-11+02:00" and "2026-09-11".
    """
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return match.group(0)


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_day_month_year(value):
    """Parse "16-Sep-2026" and "17-Sep-26" into a date, or None.

    Both forms appear: the World Bank uses four digit years, UNDP uses two.
    ASSUMPTION: a two digit year is 2000 plus that number, which is safe for
    procurement notices.
    """
    if not value:
        return None
    match = re.match(r"(\d{1,2})[-\s]([A-Za-z]{3})[a-z]*[-\s](\d{2,4})", str(value).strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if not month:
        return None
    year = int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime.date(year, month, int(match.group(1)))
    except ValueError:
        return None


def _parse_us_slash_date(value):
    """Parse the Grants.gov "09/15/2026" month/day/year form, or None."""
    if not value:
        return None
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value).strip())
    if not match:
        return None
    try:
        return datetime.date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _parse_long_us_date(value):
    """Parse the Grants.gov detail form "Jul 02, 2026 12:00:00 AM EDT", or None."""
    if not value:
        return None
    match = re.match(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})", str(value).strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return datetime.date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def _to_float(value):
    """Best effort numeric conversion, returning None rather than raising."""
    if value in (None, "", "None"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _convert_to_usd(amount, currency):
    """Convert an amount to USD using the static FX table, or None."""
    number = _to_float(amount)
    if number is None:
        return None
    rate = FX_TO_USD.get((currency or "USD").upper())
    if rate is None:
        return None
    return round(number * rate, 2)


def _clean_list(values):
    """De-duplicate and tidy a list of strings, preserving order."""
    seen = []
    for value in values or []:
        text = _strip_html(value)
        if text and text not in seen:
            seen.append(text)
    return seen


def _record(source, title, url, published, deadline=None, countries=None,
            sectors=None, funder=None, value_usd=None, summary=""):
    """Build a canonical record with exactly the agreed keys.

    Returns None when the record lacks a title, a URL or a publication date,
    because a record missing any of those is not actionable for the reader.
    """
    title = _trim(_strip_html(title), 300)
    url = (url or "").strip()
    if not title or not url or not published:
        return None
    return {
        "id": _make_id(source, url),
        "title": title,
        "source": source,
        "url": url,
        "published": published,
        "deadline": deadline,
        "countries": _clean_list(countries),
        "sectors": _clean_list(sectors),
        "funder": funder or None,
        "value_usd": value_usd,
        "summary": _trim(_strip_html(summary)),
    }


def _dedupe(records):
    """Drop repeats within a single source, keeping the first occurrence."""
    seen = set()
    unique = []
    for record in records:
        if record is None or record["id"] in seen:
            continue
        seen.add(record["id"])
        unique.append(record)
    return unique


def _days_back(since_date):
    """Whole days from since_date to today, floored at zero."""
    if not isinstance(since_date, datetime.date):
        return 7
    return max(0, (datetime.date.today() - since_date).days)


# ---------------------------------------------------------------------------
# 1. World Bank procurement notices
# ---------------------------------------------------------------------------
# Verified shape: {"rows":..,"total":"419278","procnotices":[{...}]}
# Useful fields: id, notice_type, noticedate ("16-Sep-2026"), notice_status,
# submission_deadline_date (ISO), project_ctry_name, project_id, project_name,
# bid_description, procurement_group, procurement_method_name, notice_lang_name.
# notice_text also exists but is a full HTML notice of tens of kilobytes, so it
# is deliberately not requested: bid_description plus project_name give the
# scorer enough keyword signal at a fraction of the payload.

WORLDBANK_URL = "https://search.worldbank.org/api/v2/procnotices"

WORLDBANK_FIELDS = ",".join([
    "id", "notice_type", "noticedate", "notice_status",
    "submission_deadline_date", "project_ctry_name", "project_id",
    "project_name", "bid_description", "procurement_group",
    "procurement_method_name", "contact_organization",
])

# The feed carries roughly 150 notices a day, so 200 rows per page over at most
# 12 pages comfortably covers a 7 day window with headroom.
WORLDBANK_PAGE_SIZE = 200
WORLDBANK_MAX_PAGES = 12

# Procurement group codes. CS is consulting services, which is where IDinsight
# work sits. GO (goods) and CW (civil works) are kept rather than filtered out
# because the scorer penalises them, and dropping them here would hide the odd
# mislabelled research contract.
WORLDBANK_GROUP_NAMES = {
    "CS": "Consulting services",
    "GO": "Goods",
    "CW": "Civil works",
    "NC": "Non consulting services",
    "SE": "Services",
}


def fetch_worldbank(since_date):
    """World Bank procurement notices published on or after since_date."""
    records = []
    for page in range(WORLDBANK_MAX_PAGES):
        query = urllib.parse.urlencode({
            "format": "json",
            "rows": WORLDBANK_PAGE_SIZE,
            "os": page * WORLDBANK_PAGE_SIZE,
            "srt": "noticedate",
            "order": "desc",
            "fl": WORLDBANK_FIELDS,
        })
        payload = _get_json(WORLDBANK_URL + "?" + query)
        if not isinstance(payload, dict):
            break
        notices = payload.get("procnotices")
        if not isinstance(notices, list) or not notices:
            break

        # Results are sorted newest first, so the first notice older than the
        # window tells us we can stop paging.
        reached_window_end = False
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            published = _parse_day_month_year(notice.get("noticedate"))
            if published is None:
                continue
            if published < since_date:
                reached_window_end = True
                continue
            if str(notice.get("notice_status", "")).lower() == "cancelled":
                continue

            notice_id = str(notice.get("id") or "").strip()
            if not notice_id:
                continue
            url = ("https://projects.worldbank.org/en/projects-operations/"
                   "procurement-detail/" + urllib.parse.quote(notice_id))

            group = WORLDBANK_GROUP_NAMES.get(
                str(notice.get("procurement_group") or "").upper(), "")
            summary_parts = [
                notice.get("bid_description"),
                "Project: " + str(notice.get("project_name")) if notice.get("project_name") else "",
                "Notice type: " + str(notice.get("notice_type")) if notice.get("notice_type") else "",
                "Procurement category: " + group if group else "",
                "Method: " + str(notice.get("procurement_method_name")) if notice.get("procurement_method_name") else "",
            ]
            summary = ". ".join(part for part in summary_parts if part)

            # The bid description is the contract title. Where it is missing,
            # fall back to the project name so the row is still readable.
            title = notice.get("bid_description") or notice.get("project_name")

            records.append(_record(
                source="World Bank",
                title=title,
                url=url,
                published=published.isoformat(),
                deadline=_iso(notice.get("submission_deadline_date")),
                countries=[notice.get("project_ctry_name")],
                sectors=[],
                funder="World Bank",
                value_usd=None,  # The notices feed carries no contract value.
                summary=summary,
            ))

        if reached_window_end or len(notices) < WORLDBANK_PAGE_SIZE:
            break
    return _dedupe(records)


# ---------------------------------------------------------------------------
# 2. UNDP procurement notices
# ---------------------------------------------------------------------------
# There is no JSON or RSS endpoint. Every /api/* and /rss* path tried returns
# HTTP 404. The landing page, however, renders the whole notice table server
# side in one 1 MB response, roughly 570 rows, each an anchor containing
# labelled cells: Title, Ref No, UNDP Office/Country, Process, Deadline, Posted.
# That is stable enough to read with a regex, and it is the only open route in.
# FRAGILITY NOTE: this is an HTML scrape. If UNDP restyle the table the parse
# yields zero rows and the fetcher returns [], which degrades the report by one
# source but never breaks it.

UNDP_BASE = "https://procurement-notices.undp.org/"

_UNDP_ROW = re.compile(
    r'<a\s+href="(view_notice\.cfm\?notice_id=\d+|view_negotiation\.cfm\?nego_id=\d+)"'
    r'(.*?)</a>',
    re.S,
)
_UNDP_CELL = re.compile(
    r'__cell__label">\s*(.*?)\s*</div>\s*<span>(.*?)</span>', re.S)


def fetch_undp(since_date):
    """UNDP procurement notices posted on or after since_date."""
    page = _get_text(UNDP_BASE)
    if not page:
        return []

    records = []
    for href, block in _UNDP_ROW.findall(page):
        cells = {
            _strip_html(label): _strip_html(value)
            for label, value in _UNDP_CELL.findall(block)
        }
        published = _parse_day_month_year(cells.get("Posted"))
        if published is None or published < since_date:
            continue

        title = cells.get("Title")
        if not title:
            continue

        # "PNUD ARGENTINA/ARGENTINA" and "UNDP-ZWE/ZIMBABWE" both put the
        # country after the final slash.
        office = cells.get("UNDP Office/Country", "")
        country = office.split("/")[-1].strip().title() if "/" in office else ""

        process = cells.get("Process", "")
        reference = cells.get("Ref No", "")
        deadline_date = _parse_day_month_year(cells.get("Deadline"))
        summary_parts = [
            title,
            "UNDP office: " + office if office else "",
            "Process: " + process if process else "",
            "Reference: " + reference if reference else "",
        ]

        records.append(_record(
            source="UNDP",
            title=title,
            url=urllib.parse.urljoin(UNDP_BASE, href),
            published=published.isoformat(),
            deadline=deadline_date.isoformat() if deadline_date else None,
            countries=[country],
            sectors=[],
            funder="UNDP",
            value_usd=None,  # Not shown on the listing page.
            summary=". ".join(part for part in summary_parts if part),
        ))
    return _dedupe(records)


# ---------------------------------------------------------------------------
# 3. Grants.gov
# ---------------------------------------------------------------------------
# POST https://api.grants.gov/v1/api/search2 with a JSON body. Open, no key.
# Response: {"errorcode":0,"data":{"hitCount":N,"oppHits":[{id,number,title,
# agencyCode,agency,openDate:"09/15/2026",closeDate,oppStatus,docType}]}}
# The listing carries no description, so the full text comes from a second call,
# POST /v1/api/fetchOpportunity with {"opportunityId": <int>}, whose data has a
# "synopsis" or "forecast" block holding synopsisDesc/forecastDesc, awardCeiling,
# estimatedFunding and postingDate.

GRANTS_SEARCH_URL = "https://api.grants.gov/v1/api/search2"
GRANTS_DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"

# Keyword passes. The API scores a single keyword string, so several narrow
# passes recall far more relevant work than one broad query.
GRANTS_KEYWORDS = [
    "impact evaluation",
    "monitoring and evaluation",
    "international development",
    "global health research",
    "survey research",
    "data systems",
    # Both spellings are kept on purpose: this index is US English, so the
    # US form is what matches, while the UK form catches partner language.
    "randomized controlled trial",
    "randomised controlled trial",
]

# The API only accepts its own date range buckets, seen in dateRangeOptions.
GRANTS_DATE_BUCKETS = [3, 7, 14, 21, 28]

# Detail lookups cost one request each, so cap them. Records beyond the cap keep
# a title based summary rather than being dropped.
GRANTS_DETAIL_CAP = 40
GRANTS_DETAIL_WORKERS = 6


def _grants_date_bucket(days):
    """Smallest supported bucket that covers the requested window."""
    for bucket in GRANTS_DATE_BUCKETS:
        if bucket >= days:
            return str(bucket)
    return str(GRANTS_DATE_BUCKETS[-1])


def _grants_detail(opportunity_id):
    """Fetch one opportunity detail, returning (description, value_usd)."""
    payload = _post_json(GRANTS_DETAIL_URL, {"opportunityId": opportunity_id})
    if not isinstance(payload, dict):
        return "", None
    data = payload.get("data")
    if not isinstance(data, dict):
        return "", None
    block = data.get("synopsis") or data.get("forecast") or {}
    if not isinstance(block, dict):
        return "", None
    description = block.get("synopsisDesc") or block.get("forecastDesc") or ""
    # Prefer the per-award ceiling: total programme funding overstates what a
    # single applicant could win.
    value = _to_float(block.get("awardCeiling")) or _to_float(block.get("estimatedFunding"))
    return description, value


def fetch_grants_gov(since_date):
    """US federal funding opportunities posted on or after since_date."""
    date_bucket = _grants_date_bucket(_days_back(since_date) or 7)

    hits = {}
    for keyword in GRANTS_KEYWORDS:
        payload = _post_json(GRANTS_SEARCH_URL, {
            "keyword": keyword,
            "rows": 100,
            "oppStatuses": "posted|forecasted",
            "dateRange": date_bucket,
        })
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        for hit in data.get("oppHits") or []:
            if not isinstance(hit, dict):
                continue
            key = str(hit.get("id") or "").strip()
            if key and key not in hits:
                hits[key] = hit

    # Keep only hits inside the window before spending any detail requests.
    in_window = []
    for key, hit in hits.items():
        published = _parse_us_slash_date(hit.get("openDate"))
        if published is None or published < since_date:
            continue
        in_window.append((key, hit, published))

    # Newest first, so the detail budget is spent on the freshest notices.
    in_window.sort(key=lambda item: item[2], reverse=True)

    details = {}
    if in_window:
        targets = [key for key, _hit, _pub in in_window[:GRANTS_DETAIL_CAP]]
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=GRANTS_DETAIL_WORKERS) as pool:
            futures = {pool.submit(_grants_detail, key): key for key in targets}
            for future in concurrent.futures.as_completed(futures, timeout=HTTP_TIMEOUT * 3):
                key = futures[future]
                try:
                    details[key] = future.result()
                except Exception:  # noqa: BLE001 - a dead detail call is not fatal
                    details[key] = ("", None)

    records = []
    for key, hit, published in in_window:
        description, value = details.get(key, ("", None))
        agency = hit.get("agency") or hit.get("agencyCode") or ""
        summary_parts = [
            description,
            "Agency: " + str(agency) if agency else "",
            "Opportunity number: " + str(hit.get("number")) if hit.get("number") else "",
            "Status: " + str(hit.get("oppStatus")) if hit.get("oppStatus") else "",
        ]
        close_date = _parse_us_slash_date(hit.get("closeDate"))
        records.append(_record(
            source="Grants.gov",
            title=hit.get("title"),
            url="https://www.grants.gov/search-results-detail/" + urllib.parse.quote(key),
            published=published.isoformat(),
            deadline=close_date.isoformat() if close_date else None,
            # Grants.gov does not tag geography, so countries stays empty and
            # the scorer picks up place names from title and summary text.
            countries=[],
            sectors=[],
            funder=str(agency) or "US Federal Government",
            value_usd=value,
            summary=". ".join(part for part in summary_parts if part),
        ))
    return _dedupe(records)


# ---------------------------------------------------------------------------
# 4. TED, the EU tenders journal
# ---------------------------------------------------------------------------
# POST https://api.ted.europa.eu/v3/notices/search with a JSON body. Open, no key.
# Body: {"query": <expert query>, "limit": 100, "page": N, "fields": [...]}
# Response: {"notices":[...], "totalNoticeCount": N, "iterationNextToken": ...}
# Verified field shapes: publication-date "2026-09-11+02:00", notice-title is a
# dict keyed by three letter language code, buyer-name is {"lit":[...]},
# buyer-country is a list of ISO3 codes, total-value a number with
# total-value-cur a list of currency codes, deadline-receipt-request a list of
# timestamps, one per lot.
#
# The CPV filter is what makes this source usable: unfiltered TED is mostly EU
# construction and supplies. Even filtered it is EU heavy, which is why the
# scorer, not this fetcher, decides relevance. EU external action contracts for
# evaluation and research in Africa and Asia do surface here, and those are the
# ones worth the noise.

TED_URL = "https://api.ted.europa.eu/v3/notices/search"

# CPV codes for evaluation, research, survey and data services.
TED_CPV_CODES = [
    "79419000",  # Evaluation consultancy services
    "79315000",  # Social research services
    "79311000",  # Survey services
    "79311200",  # Survey conduction services
    "79311300",  # Survey analysis services
    "79313000",  # Performance review services
    "73210000",  # Research consultancy services
    "73200000",  # Research and development consultancy services
    "72316000",  # Data analysis services
]

TED_PAGE_SIZE = 100
TED_MAX_PAGES = 5

TED_FIELDS = [
    "publication-number",
    "notice-title",
    "publication-date",
    "deadline-receipt-request",
    "buyer-name",
    "buyer-country",
    "total-value",
    "total-value-cur",
    "notice-type",
    "classification-cpv",
]

# Notice types to skip: contract award notices and voluntary ex ante
# transparency notices report work already given out, so they are not leads.
TED_SKIP_PREFIXES = ("can", "veat", "cm-", "cofin")


def _ted_text(value, prefer="eng"):
    """Flatten a TED multilingual field to one string, preferring English."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_ted_text(item, prefer) for item in value if item)
    if isinstance(value, dict):
        if prefer in value:
            return _ted_text(value[prefer], prefer)
        # "lit" holds the original language text when no translation exists.
        if "lit" in value:
            return _ted_text(value["lit"], prefer)
        for item in value.values():
            text = _ted_text(item, prefer)
            if text:
                return text
    return ""


def _ted_first(value):
    """First entry of a TED list field, or the value itself."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def fetch_ted(since_date):
    """TED notices for evaluation and research services within the window."""
    days = _days_back(since_date) or 7
    query = ("publication-date>=today(-%d) AND classification-cpv IN (%s)"
             % (days, " ".join(TED_CPV_CODES)))

    records = []
    for page in range(1, TED_MAX_PAGES + 1):
        payload = _post_json(TED_URL, {
            "query": query,
            "limit": TED_PAGE_SIZE,
            "page": page,
            "fields": TED_FIELDS,
        })
        if not isinstance(payload, dict):
            break
        notices = payload.get("notices")
        if not isinstance(notices, list) or not notices:
            break

        for notice in notices:
            if not isinstance(notice, dict):
                continue
            published = _iso(notice.get("publication-date"))
            if not published:
                continue
            if datetime.date.fromisoformat(published) < since_date:
                continue

            notice_type = str(notice.get("notice-type") or "").lower()
            if notice_type.startswith(TED_SKIP_PREFIXES):
                continue

            number = str(notice.get("publication-number") or "").strip()
            if not number:
                continue

            # Earliest lot deadline is the one the reader must act on.
            deadlines = [d for d in (_iso(item) for item in
                                     (notice.get("deadline-receipt-request") or []))
                         if d]
            deadline = min(deadlines) if deadlines else None

            countries = [ISO3_NAMES.get(str(code).upper(), str(code))
                         for code in (notice.get("buyer-country") or [])]
            buyer = _ted_text(notice.get("buyer-name")).strip()
            title = _ted_text(notice.get("notice-title"))

            cpv_codes = sorted({str(code) for code in
                                (notice.get("classification-cpv") or [])})
            summary_parts = [
                title,
                "Buyer: " + buyer if buyer else "",
                "Notice type: " + notice_type if notice_type else "",
                "CPV: " + ", ".join(cpv_codes) if cpv_codes else "",
            ]

            records.append(_record(
                source="TED (EU)",
                title=title,
                url="https://ted.europa.eu/en/notice/-/detail/" + urllib.parse.quote(number),
                published=published,
                deadline=deadline,
                countries=countries,
                sectors=[],
                funder=buyer or "European Union",
                value_usd=_convert_to_usd(notice.get("total-value"),
                                          _ted_first(notice.get("total-value-cur"))),
                summary=". ".join(part for part in summary_parts if part),
            ))

        total = payload.get("totalNoticeCount")
        if len(notices) < TED_PAGE_SIZE:
            break
        if isinstance(total, int) and page * TED_PAGE_SIZE >= total:
            break
    return _dedupe(records)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES = [
    ("World Bank", fetch_worldbank),
    ("UNDP", fetch_undp),
    ("Grants.gov", fetch_grants_gov),
    ("TED (EU)", fetch_ted),
]


def fetch_all(since_date, max_workers=4):
    """Run every source and return one combined list of canonical records.

    Sources run in parallel because they are independent and network bound.
    A source that fails contributes nothing and is not allowed to propagate.
    """
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetcher, since_date): name
                   for name, fetcher in SOURCES}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 - one dead source must not break the run
                result = []
            if isinstance(result, list):
                records.extend(record for record in result if isinstance(record, dict))
    return records


if __name__ == "__main__":
    # Smoke test: one line per source with a count and a sample record, so the
    # module can be checked on its own without running the full pipeline.
    WINDOW_DAYS = 7
    window_start = datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)
    print("Window: %s to %s" % (window_start.isoformat(),
                                datetime.date.today().isoformat()))
    grand_total = 0
    for source_name, source_fetcher in SOURCES:
        try:
            source_records = source_fetcher(window_start)
        except Exception as error:  # noqa: BLE001 - report it, do not crash
            print("%-14s FAILED: %s" % (source_name, error))
            continue
        grand_total += len(source_records)
        print("%-14s %4d records" % (source_name, len(source_records)))
        if source_records:
            sample = source_records[0]
            print("               keys: %s" % ", ".join(sample.keys()))
            print("               %s | %s | %s" % (
                sample["published"], sample["countries"], sample["title"][:70]))
    print("%-14s %4d records" % ("TOTAL", grand_total))
