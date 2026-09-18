"""
Report renderer for the daily IDinsight opportunity digest.

render(records, out_dir, generated_at) writes two files into out_dir:

    index.html   one self contained page for the fundraising director. All CSS
                 sits in a single <style> block and the only JavaScript is a
                 short vanilla filter script. No CDN, no web fonts, no external
                 request of any kind, so the page renders identically from a
                 web server, a local file or an email attachment.
    data.json    the scored records plus a small run header, so the digest is
                 reusable and auditable after the fact.

Input records follow the canonical schema from src/sources.py with the keys
added by src/score.py:

    id, title, source, url, published, deadline, countries, sectors, funder,
    value_usd, summary, score, tier, rationale, signals, scored_by

Design rules this module follows:
  - Python standard library only. The HTML is assembled with string.Template
    and list joins, never jinja2.
  - Every value that reaches the page goes through html.escape first. These
    strings come from third party APIs and routinely contain quotes, ampersands
    and angle brackets.
  - The renderer never raises on one bad record. A record missing a field is
    rendered with that field omitted, because a partial digest is worth far
    more to the director than a traceback.
  - Nothing here imports the rest of the pipeline, so the page can be rendered
    from a saved data.json or from fixtures:  python src/render.py

Run it on its own to write a fixture report:

    python src/render.py [out_dir]
"""

import datetime
import html
import json
import os
import pathlib
import re
import string
import sys

# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

PAGE_TITLE = "IDinsight Opportunity Finder"

# The freshness window the brief cares about. Passed in by the caller so the
# page text and the pipeline can never disagree; 7 is only the fallback.
DEFAULT_WINDOW_DAYS = 7

# A deadline this close is called out in red. Assumption: a week is the point
# at which the director has to decide today whether to bid.
DEADLINE_WARNING_DAYS = 7

# Tier order on the page, with the display label and whether the section starts
# expanded. Low is collapsed so the page stays scannable, which is the whole
# point of the layout.
TIER_SECTIONS = [
    ("High", "High priority", True),
    ("Medium", "Medium priority", True),
    ("Low", "Lower priority", False),
]

# Countries are listed in full up to this many, then summarised, so a
# multi-country regional notice cannot swamp a card.
MAX_COUNTRIES_SHOWN = 6

_SLUG = re.compile(r"[^a-z0-9]+")


def _log(message):
    """Write progress to stderr so stdout stays clean for piping."""
    sys.stderr.write(message + "\n")


# ---------------------------------------------------------------------------
# Small formatting helpers. Each one is defensive: bad input yields "" or None
# rather than an exception.
# ---------------------------------------------------------------------------


def _esc(value):
    """Escape any value for use in HTML text or in a quoted attribute."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _slug(value):
    """Build a safe id fragment from a human readable label."""
    cleaned = _SLUG.sub("-", str(value or "").lower()).strip("-")
    return cleaned or "item"


def _parse_iso_date(value):
    """Parse a 'YYYY-MM-DD' string, tolerating a full timestamp. None on failure."""
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (ValueError, IndexError):
        return None


def _as_list(value):
    """Coerce a field that should be a list of strings into exactly that."""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _generated_at_text(generated_at):
    """Format the run time as, for example, '18 September 2026 at 06:12 UTC'.

    Assumption: a naive datetime is UTC. GitHub Actions runners are UTC, so
    this matches production, and labelling the zone is better than printing a
    bare time the reader cannot place.
    """
    if generated_at is None:
        generated_at = datetime.datetime.now(datetime.timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=datetime.timezone.utc)
    zone = generated_at.strftime("%Z") or "UTC"
    # %-d is not portable, so strip the leading zero by hand.
    day = str(generated_at.day)
    return day + generated_at.strftime(" %B %Y at %H:%M ") + zone


def _relative_day_phrase(days_old):
    """Phrase an age in days the way a person would say it."""
    if days_old is None:
        return "publication date not given"
    if days_old < 0:
        # A source occasionally posts a date a day ahead of the runner's clock.
        return "published today"
    if days_old == 0:
        return "published today"
    if days_old == 1:
        return "published yesterday"
    return "published " + str(days_old) + " days ago"


def _deadline_phrase(deadline, today):
    """Return (text, is_urgent) for the deadline, or (None, False) if unknown."""
    parsed = _parse_iso_date(deadline)
    if parsed is None:
        return (None, False)
    days_left = (parsed - today).days
    stamp = parsed.isoformat()
    if days_left < 0:
        return ("deadline passed on " + stamp, False)
    if days_left == 0:
        return ("closes today, " + stamp, True)
    if days_left == 1:
        return ("closes tomorrow, " + stamp, True)
    text = "closes in " + str(days_left) + " days, " + stamp
    return (text, days_left <= DEADLINE_WARNING_DAYS)


def _format_value(value_usd):
    """Format an estimated contract value, or None when it is unusable."""
    if value_usd is None:
        return None
    try:
        amount = float(value_usd)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    if amount >= 1000000:
        millions = amount / 1000000.0
        digits = 1 if millions < 100 else 0
        text = ("%." + str(digits) + "f") % millions
        if text.endswith(".0"):
            text = text[:-2]
        return "estimated USD " + text + " million"
    return "estimated USD " + format(int(round(amount)), ",d")


def _safe_url(value):
    """Return the URL only if it is a plain http or https link, else "".

    Source URLs arrive from third party APIs. Anything else, notably a
    javascript: or data: URL, is dropped and the title renders as plain text,
    so a malformed feed cannot put an executable link in front of the reader.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return text
    _log("render: dropped a link with an unsupported scheme: " + text[:80])
    return ""


def _countries_phrase(countries):
    """List the countries, summarising once the list gets long."""
    items = _as_list(countries)
    if not items:
        return None
    if len(items) <= MAX_COUNTRIES_SHOWN:
        return ", ".join(items)
    shown = ", ".join(items[:MAX_COUNTRIES_SHOWN])
    return shown + " and " + str(len(items) - MAX_COUNTRIES_SHOWN) + " more"


def _plural(count, singular, plural=None):
    """Return 'N thing' or 'N things', so the summary line reads naturally."""
    word = singular if count == 1 else (plural or singular + "s")
    return str(count) + " " + word


def _sort_key(record):
    """Rank by score, then freshness, then title.

    The page is regenerated daily and committed to git, so two runs over the
    same data must produce byte identical output. Reimplemented here rather
    than imported, to keep the renderer independent of the scorer.
    """
    published = _parse_iso_date(record.get("published"))
    return (
        -int(record.get("score") or 0),
        -(published.toordinal() if published else 0),
        str(record.get("title") or "").lower(),
    )


def _search_blob(record):
    """Build the lowercase text the filter box searches.

    Everything the director might type goes in: title, source, funder,
    countries, sectors, the rationale and the notice summary. The blob lives in
    a data attribute, so filtering touches only the already rendered DOM and
    needs no data copy in JavaScript.
    """
    parts = [
        str(record.get("title") or ""),
        str(record.get("source") or ""),
        str(record.get("funder") or ""),
        str(record.get("tier") or ""),
        " ".join(_as_list(record.get("countries"))),
        " ".join(_as_list(record.get("sectors"))),
        str(record.get("rationale") or ""),
        str(record.get("summary") or ""),
    ]
    return " ".join(part for part in parts if part).lower()


# ---------------------------------------------------------------------------
# Stylesheet. Colour carries meaning only: priority tier and deadline urgency.
# Tokens are defined on :root and redefined for dark mode, and body always
# states its own background so no viewer gets a white flash or black text on a
# dark canvas.
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f5f6f8;
  --surface: #ffffff;
  --surface-alt: #eef0f3;
  --text: #15181c;
  --muted: #58636f;
  --border: #d5dae0;
  --link: #14507d;
  --high-fg: #0e6249;
  --high-bg: #e2efea;
  --medium-fg: #74500a;
  --medium-bg: #f5eedd;
  --low-fg: #47515c;
  --low-bg: #e9ecef;
  --urgent-fg: #97291f;
  --urgent-bg: #fbe9e7;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b;
    --surface: #1c2126;
    --surface-alt: #242a31;
    --text: #e7eaee;
    --muted: #a3adb8;
    --border: #333b44;
    --link: #8ec1ea;
    --high-fg: #77d3ae;
    --high-bg: #16332a;
    --medium-fg: #e0bd72;
    --medium-bg: #352d18;
    --low-fg: #b3bcc6;
    --low-bg: #262c33;
    --urgent-fg: #f0a29a;
    --urgent-bg: #3a1f1c;
  }
}

* { box-sizing: border-box; }

[hidden] { display: none !important; }

body {
  margin: 0;
  padding: 0 16px 56px;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 16px;
  line-height: 1.55;
  -webkit-text-size-adjust: 100%;
}

.wrap { max-width: 860px; margin: 0 auto; }

a { color: var(--link); }
a:focus-visible, summary:focus-visible, input:focus-visible, button:focus-visible {
  outline: 2px solid var(--link);
  outline-offset: 2px;
}

header.masthead { padding: 28px 0 18px; }

h1 {
  margin: 0 0 6px;
  font-size: 25px;
  line-height: 1.25;
  letter-spacing: -0.01em;
}

.runline { margin: 0; color: var(--muted); font-size: 14px; }

.standfirst {
  margin: 14px 0 0;
  font-size: 17px;
  max-width: 60ch;
}

/* The filter bar stays in reach while she scrolls a long list. */
.controls {
  position: sticky;
  top: 0;
  z-index: 5;
  padding: 12px 0;
  background: var(--bg);
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.controls label.searchlabel {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  margin-bottom: 5px;
}

.searchrow { display: flex; gap: 8px; flex-wrap: wrap; }

#q {
  flex: 1 1 220px;
  min-width: 0;
  padding: 9px 11px;
  font: inherit;
  font-size: 15px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
}

#reset {
  padding: 9px 13px;
  font: inherit;
  font-size: 14px;
  color: var(--text);
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}

.sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 10px 0 0;
  font-size: 14px;
}

.sources span.srclabel {
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
  width: 100%;
}

.sources label { display: inline-flex; align-items: center; gap: 6px; }

.tier { margin: 26px 0 0; }

.tier > summary {
  cursor: pointer;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  padding: 6px 0;
  border-bottom: 1px solid var(--border);
}

.tier > summary .count { font-weight: 400; letter-spacing: 0; text-transform: none; }

.cards { margin-top: 12px; }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 12px;
}

.card.tier-high { border-left-color: var(--high-fg); }
.card.tier-medium { border-left-color: var(--medium-fg); }
.card.tier-low { border-left-color: var(--low-fg); }

.cardhead {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  justify-content: space-between;
}

.card h3 {
  margin: 0;
  font-size: 17px;
  line-height: 1.35;
  font-weight: 600;
  overflow-wrap: anywhere;
}

/* A page of underlined headings reads as clutter, so the underline arrives on
   interaction instead. */
.card h3 a { text-decoration: none; }
.card h3 a:hover, .card h3 a:focus-visible { text-decoration: underline; }

.badge {
  flex: 0 0 auto;
  font-size: 13px;
  font-weight: 700;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}

.badge.tier-high { color: var(--high-fg); background: var(--high-bg); }
.badge.tier-medium { color: var(--medium-fg); background: var(--medium-bg); }
.badge.tier-low { color: var(--low-fg); background: var(--low-bg); }

.meta {
  margin: 8px 0 0;
  font-size: 14px;
  color: var(--muted);
}

.meta span + span::before {
  content: "\\00B7";
  margin: 0 8px;
}

.meta .urgent strong {
  color: var(--urgent-fg);
  background: var(--urgent-bg);
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 4px;
  /* Keep the warning on one line so the highlight never breaks mid-phrase on
     a narrow screen. */
  white-space: nowrap;
}

/* The rationale is why the page saves her time, so it is the loudest text. */
.rationale { margin: 11px 0 0; font-size: 16px; }

.notice { margin: 10px 0 0; }

.notice > summary {
  cursor: pointer;
  font-size: 13px;
  color: var(--muted);
}

.notice p {
  margin: 8px 0 0;
  font-size: 14px;
  color: var(--muted);
  background: var(--surface-alt);
  border-radius: 6px;
  padding: 10px 12px;
  overflow-wrap: anywhere;
}

.empty { color: var(--muted); font-size: 15px; margin: 12px 0 0; }

footer {
  margin: 40px 0 0;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  font-size: 13.5px;
  color: var(--muted);
}

footer p { margin: 0 0 7px; }

@media (max-width: 520px) {
  h1 { font-size: 22px; }
  .cardhead { flex-direction: column; gap: 6px; }
  .badge { align-self: flex-start; }
}
"""


# ---------------------------------------------------------------------------
# Filter script. Vanilla JavaScript over the rendered DOM: no data is duplicated
# into JavaScript, so the page works with the script removed and degrades to a
# plain list if it is blocked.
# ---------------------------------------------------------------------------

SCRIPT = """
(function () {
  var box = document.getElementById("q");
  var reset = document.getElementById("reset");
  var boxes = Array.prototype.slice.call(
    document.querySelectorAll("input[data-source]")
  );
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var sections = Array.prototype.slice.call(document.querySelectorAll(".tier"));
  var none = document.getElementById("noresults");
  if (!box || !cards.length) { return; }

  function apply() {
    var query = box.value.trim().toLowerCase();
    var allowed = {};
    var anyBox = false;
    boxes.forEach(function (input) {
      if (input.checked) {
        allowed[input.getAttribute("data-source")] = true;
        anyBox = true;
      }
    });

    var shownTotal = 0;
    cards.forEach(function (card) {
      // An unchecked-everything state is treated as no source filter at all,
      // because an empty page is never what the reader meant.
      var sourceOk = !anyBox || allowed[card.getAttribute("data-source")] === true;
      var haystack = card.getAttribute("data-search") || "";
      var textOk = query === "" || haystack.indexOf(query) !== -1;
      var show = sourceOk && textOk;
      card.hidden = !show;
      if (show) { shownTotal += 1; }
    });

    sections.forEach(function (section) {
      var total = parseInt(section.getAttribute("data-total"), 10) || 0;
      var shown = section.querySelectorAll(".card:not([hidden])").length;
      var label = section.querySelector(".count");
      if (label) {
        label.textContent = shown === total
          ? "(" + total + ")"
          : "(" + shown + " of " + total + ")";
      }
      // Hide a section that has nothing left to show, and open a collapsed one
      // that does, so a search result is never buried behind a closed section.
      section.hidden = total > 0 && shown === 0;
      if (query !== "" && shown > 0) {
        section.open = true;
      } else if (query === "") {
        section.open = section.getAttribute("data-open") === "true";
      }
    });

    if (none) { none.hidden = shownTotal !== 0; }
  }

  box.addEventListener("input", apply);
  boxes.forEach(function (input) { input.addEventListener("change", apply); });
  if (reset) {
    reset.addEventListener("click", function () {
      box.value = "";
      boxes.forEach(function (input) { input.checked = true; });
      apply();
    });
  }
  apply();
})();
"""


PAGE = string.Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>$page_title</title>
<style>
$css
</style>
</head>
<body>
<div class="wrap">
$header
$controls
$sections
$footer
</div>
<script>
$script
</script>
</body>
</html>
"""
)


# ---------------------------------------------------------------------------
# HTML fragments
# ---------------------------------------------------------------------------


def _render_card(record, today):
    """Render one opportunity card. Missing fields are simply left out."""
    tier = str(record.get("tier") or "Low")
    tier_class = "tier-" + _slug(tier)
    source = str(record.get("source") or "Unknown source")
    title = str(record.get("title") or "Untitled notice")
    url = _safe_url(record.get("url"))

    published = _parse_iso_date(record.get("published"))
    days_old = (today - published).days if published else None

    parts = ['<article class="card ' + tier_class + '"']
    parts.append(' data-source="' + _esc(source) + '"')
    parts.append(' data-search="' + _esc(_search_blob(record)) + '">')

    # Head: the title is the link, because that is what she clicks, and the
    # score badge sits beside it so the tier reads at a glance.
    parts.append('<div class="cardhead"><h3>')
    if url:
        parts.append(
            '<a href="' + _esc(url) + '" rel="noopener noreferrer">'
            + _esc(title) + "</a>"
        )
    else:
        parts.append(_esc(title))
    parts.append("</h3>")
    score = record.get("score")
    score_text = str(int(score)) if isinstance(score, (int, float)) else "n/a"
    parts.append(
        '<span class="badge ' + tier_class + '" title="Fit score out of 100">'
        + _esc(tier) + " " + _esc(score_text) + "</span></div>"
    )

    # Meta line: source, freshness, deadline, countries, value, funder.
    meta = ['<span>' + _esc(source) + "</span>"]
    meta.append("<span>" + _esc(_relative_day_phrase(days_old)) + "</span>")

    deadline_text, urgent = _deadline_phrase(record.get("deadline"), today)
    if deadline_text:
        if urgent:
            # strong, not colour alone, so the urgency survives a greyscale
            # print and reaches a screen reader.
            meta.append(
                '<span class="urgent"><strong>' + _esc(deadline_text)
                + "</strong></span>"
            )
        else:
            meta.append("<span>" + _esc(deadline_text) + "</span>")
    else:
        meta.append("<span>no deadline given</span>")

    countries = _countries_phrase(record.get("countries"))
    if countries:
        meta.append("<span>" + _esc(countries) + "</span>")

    value_text = _format_value(record.get("value_usd"))
    if value_text:
        meta.append("<span>" + _esc(value_text) + "</span>")

    funder = str(record.get("funder") or "").strip()
    if funder and funder.lower() != source.lower():
        meta.append("<span>" + _esc(funder) + "</span>")

    parts.append('<p class="meta">' + "".join(meta) + "</p>")

    rationale = str(record.get("rationale") or "").strip()
    if rationale:
        parts.append('<p class="rationale">' + _esc(rationale) + "</p>")

    # The notice text is kept collapsed. It is useful once she is interested,
    # and noise before that.
    summary = str(record.get("summary") or "").strip()
    if summary:
        parts.append(
            '<details class="notice"><summary>Notice text as published</summary><p>'
            + _esc(summary)
            + "</p></details>"
        )

    parts.append("</article>")
    return "".join(parts)


def _render_sections(by_tier, today):
    """Render the three tier sections in priority order."""
    blocks = []
    for tier, label, start_open in TIER_SECTIONS:
        items = by_tier.get(tier, [])
        if not items and tier != "High":
            # Nothing to say about an empty Medium or Low section. High is kept
            # even when empty, because "nothing urgent today" is real news.
            continue
        section_id = "sec-" + _slug(tier)
        open_attr = " open" if start_open else ""
        blocks.append(
            '<details class="tier" id="' + section_id + '" data-total="'
            + str(len(items)) + '" data-open="' + ("true" if start_open else "false")
            + '"' + open_attr + ">"
        )
        blocks.append(
            "<summary>" + _esc(label)
            + ' <span class="count">(' + str(len(items)) + ")</span></summary>"
        )
        blocks.append('<div class="cards">')
        if items:
            for record in items:
                try:
                    blocks.append(_render_card(record, today))
                except Exception as exc:
                    # Deliberately broad. One malformed record must not cost the
                    # director the whole page.
                    _log(
                        "render: skipped a record ("
                        + type(exc).__name__ + ": " + str(exc) + ")."
                    )
        else:
            blocks.append(
                '<p class="empty">No high priority opportunities in this window. '
                "The medium list below is worth a scan.</p>"
            )
        blocks.append("</div></details>")
    return "\n".join(blocks)


def _render_header(count, source_names, tier_counts, generated_at, window_days):
    """Header block: what this is, when it ran, and the honest headline."""
    window_text = "published in the last " + _plural(window_days, "day")
    runline = (
        "Generated " + _generated_at_text(generated_at) + ". "
        + _plural(count, "opportunity", "opportunities") + ", " + window_text + "."
    )

    if count == 0:
        standfirst = (
            "No opportunities matched the filters in this window. "
            "The sources were queried and returned nothing that fits."
        )
    else:
        high = tier_counts.get("High", 0)
        standfirst = (
            _plural(count, "fresh opportunity", "fresh opportunities") + " from "
            + _plural(len(source_names) or 1, "source") + ". "
        )
        if high:
            standfirst += str(high) + " rated high priority."
        else:
            standfirst += "None rated high priority."

    return (
        '<header class="masthead"><h1>' + _esc(PAGE_TITLE) + "</h1>"
        + '<p class="runline">' + _esc(runline) + "</p>"
        + '<p class="standfirst">' + _esc(standfirst) + "</p></header>"
    )


def _render_controls(source_names):
    """Search box and one checkbox per source that actually appears on the page."""
    rows = ['<section class="controls" aria-label="Filter opportunities">']
    rows.append('<label class="searchlabel" for="q">Search these opportunities</label>')
    rows.append('<div class="searchrow">')
    rows.append(
        '<input id="q" type="search" autocomplete="off" '
        'placeholder="Country, sector, funder or keyword">'
    )
    rows.append('<button id="reset" type="button">Clear</button>')
    rows.append("</div>")

    if source_names:
        rows.append('<div class="sources"><span class="srclabel">Sources</span>')
        for name in source_names:
            box_id = "src-" + _slug(name)
            rows.append(
                '<label for="' + _esc(box_id) + '"><input type="checkbox" id="'
                + _esc(box_id) + '" data-source="' + _esc(name)
                + '" checked>' + _esc(name) + "</label>"
            )
        rows.append("</div>")

    rows.append(
        '<p class="empty" id="noresults" hidden>Nothing matches that filter. '
        "Clear the search to see the full list.</p>"
    )
    rows.append("</section>")
    return "".join(rows)


def _render_footer(source_names, queried_names, scored_by_llm):
    """Footer: provenance, refresh cadence and how the scores were produced."""
    listed = queried_names or source_names
    if listed:
        sources_line = "Sources queried: " + ", ".join(listed) + "."
    else:
        # Reached only when the caller passed no source list and no record
        # carried a source name, so the honest statement is that we do not know.
        sources_line = "The source list was not recorded for this run."

    if scored_by_llm:
        scoring_line = (
            "Scoring: a deterministic keyword model ranks every notice, then a "
            "language model reviews the strongest candidates and rewrites their "
            "rationale. If that review is unavailable the keyword scores stand."
        )
    else:
        scoring_line = (
            "Scoring: deterministic keyword rules only on this run. No language "
            "model was used, so every score can be traced to the matched terms "
            "in data.json."
        )

    return (
        "<footer><p>" + _esc(sources_line) + "</p>"
        + "<p>This page regenerates every day from a GitHub Actions schedule and "
        "is deployed to rfp.malambomutila.com. No action is needed to refresh it.</p>"
        + "<p>" + _esc(scoring_line) + "</p>"
        + "<p>Scores are a shortlisting aid, not a bid decision. Always open the "
        "original notice before committing effort.</p></footer>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render(records, out_dir, generated_at=None, window_days=DEFAULT_WINDOW_DAYS,
           sources_queried=None):
    """Write index.html and data.json into out_dir and return both paths.

    records         scored records, in any order. They are re-sorted here so the
                    output is identical for identical input.
    out_dir         directory to write into. Created if it does not exist.
    generated_at    datetime for the header. Defaults to now in UTC. A naive
                    value is assumed to be UTC, which matches the Actions runner.
    window_days     the freshness window the records were filtered to, used for
                    the header wording only.
    sources_queried optional list of every source the pipeline asked, including
                    ones that returned nothing. Falls back to the sources that
                    appear in the records, so the footer is never empty.
    """
    if generated_at is None:
        generated_at = datetime.datetime.now(datetime.timezone.utc)
    today = generated_at.date()

    clean = [record for record in (records or []) if isinstance(record, dict)]
    clean.sort(key=_sort_key)

    # Group by tier, defaulting anything unrecognised to Low rather than
    # dropping it, so a scorer change can never silently hide a record.
    known_tiers = set(tier for tier, _label, _open in TIER_SECTIONS)
    by_tier = {}
    for record in clean:
        tier = str(record.get("tier") or "Low")
        if tier not in known_tiers:
            tier = "Low"
        by_tier.setdefault(tier, []).append(record)

    tier_counts = dict((tier, len(items)) for tier, items in by_tier.items())

    # Source names in the order they first appear, which is score order, so the
    # checkbox row leads with the source carrying the best opportunities.
    source_names = []
    for record in clean:
        name = str(record.get("source") or "").strip()
        if name and name not in source_names:
            source_names.append(name)

    queried_names = [str(name) for name in (sources_queried or []) if str(name).strip()]
    scored_by_llm = any(
        str(record.get("scored_by") or "").lower() == "llm" for record in clean
    )

    page = PAGE.substitute(
        page_title=_esc(PAGE_TITLE),
        css=CSS,
        script=SCRIPT,
        header=_render_header(
            len(clean), source_names, tier_counts, generated_at, window_days
        ),
        controls=_render_controls(source_names),
        sections=_render_sections(by_tier, today),
        footer=_render_footer(source_names, queried_names, scored_by_llm),
    )

    out_path = pathlib.Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    html_file = out_path / "index.html"
    json_file = out_path / "data.json"

    html_file.write_text(page, encoding="utf-8")

    # data.json carries a small run header alongside the records, so a later
    # reader can tell when the digest ran and what it covered without having to
    # parse the HTML.
    payload = {
        "generated_at": generated_at.isoformat(),
        "window_days": window_days,
        "count": len(clean),
        "tier_counts": tier_counts,
        "sources_in_report": source_names,
        "sources_queried": queried_names or source_names,
        "scoring": "llm_refined" if scored_by_llm else "rules",
        "records": clean,
    }
    json_file.write_text(
        # default=str keeps the write from failing on an unexpected type such as
        # a date object that slipped through a fetcher.
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False, default=str),
        encoding="utf-8",
    )

    _log(
        "render: wrote " + str(html_file) + " (" + str(len(clean))
        + " record(s)) and " + str(json_file) + "."
    )
    return (str(html_file), str(json_file))


if __name__ == "__main__":
    # Smoke test. Renders fixture records so the HTML can be opened and checked
    # without the fetchers, the scorer, a network connection or credentials:
    #
    #     python src/render.py            writes to /tmp/idinsight-rfp-preview
    #     python src/render.py some/dir   writes there instead
    import hashlib

    TARGET = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        "/tmp", "idinsight-rfp-preview"
    )
    GENERATED_AT = datetime.datetime(2026, 9, 18, 6, 12, tzinfo=datetime.timezone.utc)
    TODAY = GENERATED_AT.date()

    def fixture(source, title, days_ago, score, tier, rationale, deadline_in=None,
                countries=None, sectors=None, funder=None, value_usd=None,
                summary="", scored_by="rules"):
        """Build a record shaped exactly as the scorer hands it over."""
        url = "https://example.org/notice/" + _slug(title)[:40]
        published = (TODAY - datetime.timedelta(days=days_ago)).isoformat()
        deadline = None
        if deadline_in is not None:
            deadline = (TODAY + datetime.timedelta(days=deadline_in)).isoformat()
        return {
            "id": hashlib.sha1((source + url).encode()).hexdigest()[:12],
            "title": title,
            "source": source,
            "url": url,
            "published": published,
            "deadline": deadline,
            "countries": countries or [],
            "sectors": sectors or [],
            "funder": funder,
            "value_usd": value_usd,
            "summary": summary,
            "score": score,
            "tier": tier,
            "rationale": rationale,
            "signals": {
                "geography": 18, "sector": 12, "service": 22,
                "recency": 18, "penalty": 0,
            },
            "scored_by": scored_by,
        }

    FIXTURES = [
        fixture(
            "World Bank",
            'Impact evaluation of the "Shishu Poshan" nutrition programme, Bihar',
            1, 88, "High",
            "A named impact evaluation in India covering nutrition, which is core "
            "IDinsight work and a geography where we already have a team.",
            deadline_in=5,
            countries=["India"], sectors=["nutrition", "health"],
            funder="World Bank", value_usd=1450000,
            summary="The Bihar state government seeks a firm to design and deliver "
                    "a randomised evaluation of a child nutrition programme, "
                    "including baseline and endline surveys of 12,000 households. "
                    "Bids must include a survey plan and a data management plan.",
            scored_by="llm",
        ),
        fixture(
            "Grants.gov",
            "Learning partner for adolescent health & wellbeing, Kenya and Uganda",
            0, 76, "High",
            "A learning partner role across two priority countries in health, "
            "which maps directly onto our MEL and data systems offer.",
            deadline_in=21,
            countries=["Kenya", "Uganda"], sectors=["health", "gender"],
            funder="USAID",
            summary="Five year cooperative agreement for monitoring, evaluation and "
                    "learning support to an adolescent health portfolio.",
        ),
        fixture(
            "UNDP",
            "Third party monitoring of social protection cash transfers, Zambia",
            3, 58, "Medium",
            "Third party monitoring in Zambia in social protection. Good fit on "
            "service and geography, but the scope reads as routine spot checks "
            "rather than evaluation.",
            deadline_in=2,
            countries=["Zambia"], sectors=["social protection"],
            funder="UNDP", value_usd=320000,
            summary="Verification visits to district pay points, with quarterly "
                    "reporting to the Ministry of Community Development.",
        ),
        fixture(
            "TED (EU)",
            "Framework contract for survey research services, Sub-Saharan Africa",
            5, 47, "Medium",
            "A regional survey research framework. Relevant service, but no named "
            "country and no sector, so the value to us is unclear until the first "
            "call-off.",
            deadline_in=34,
            countries=["Sub-Saharan Africa"], sectors=[],
            funder="European Commission",
            summary="Multi-lot framework for quantitative and qualitative survey "
                    "fieldwork, with call-offs issued over four years.",
        ),
        fixture(
            "World Bank",
            "Supply and installation of laboratory equipment, Nigeria",
            2, 12, "Low",
            "An equipment supply contract in a priority country. The geography "
            "fits but the work does not: this is procurement of goods.",
            deadline_in=14,
            countries=["Nigeria"], sectors=["health"],
            funder="World Bank", value_usd=780000,
            summary="Procurement of laboratory analysers, consumables and a two "
                    "year maintenance agreement for six referral hospitals.",
        ),
        fixture(
            "UNDP",
            "Road rehabilitation works supervision, Mozambique",
            6, 8, "Low",
            "Civil works supervision in Mozambique. Not an IDinsight service.",
            deadline_in=9,
            countries=["Mozambique"], sectors=[],
            funder="UNDP",
            summary="Engineering supervision of 40km of rural road rehabilitation.",
        ),
    ]

    render(
        FIXTURES,
        TARGET,
        generated_at=GENERATED_AT,
        window_days=7,
        sources_queried=["World Bank", "UNDP", "Grants.gov", "TED (EU)"],
    )
    print("Open file://" + os.path.abspath(os.path.join(TARGET, "index.html")))
