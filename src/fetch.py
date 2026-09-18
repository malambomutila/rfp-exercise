"""
Aggregation layer for the daily RFP digest.

This module sits directly above src/sources.py and does three jobs:

  1. fetch_all    run every registered source fetcher concurrently, isolate
                  failures, and merge whatever came back
  2. dedupe       collapse the same opportunity when it appears more than once,
                  either byte for byte or as a near duplicate posted to two
                  portals
  3. filter_fresh keep only opportunities that are recent and still open

Python standard library only, so the GitHub Actions run needs no pip install
step and a reviewer can run it with no setup.

Every record handled here follows the canonical schema produced by
src/sources.py and is passed through unchanged apart from the merges described
below:

    id, title, source, url, published, deadline, countries, sectors,
    funder, value_usd, summary

This module never adds keys to a record. Scoring keys are added later by the
scorer, so anything downstream can rely on the schema staying predictable.
"""

import concurrent.futures
import datetime
import pathlib
import re
import sys
import time

# Wall clock budget in seconds for a single source. One slow portal must not
# hold up the whole daily run, so we abandon it and carry on with the rest.
#
# Sized against what the sources actually need, not guessed. These were 45 and
# 150, set when the pipeline read four JSON APIs inside a CI job with its own
# time limit. Both are now far too tight and were silently discarding real
# work: UNGM issues around 27 requests spaced 1.5 seconds apart for politeness,
# so it cannot finish inside 45 seconds, and it was being cancelled after
# having already found 98 matching notices. The web sources each wait on a
# model call that is itself allowed 150 seconds.
#
# There is no external time limit any more: the pipeline runs once a day in a
# container on our own server, so the budget only needs to be short enough that
# a genuinely hung source cannot stall the report indefinitely.
SOURCE_TIMEOUT_SECONDS = 240

# Budget for the entire concurrent fetch. Sources run in parallel, so this is
# not the sum of the per-source budgets, it is the backstop for the whole
# stage.
TOTAL_FETCH_TIMEOUT_SECONDS = 600

# Fixed by the agreed architecture.
MAX_WORKERS = 8

# A record is unusable without these. Anything missing one of them cannot be
# scored or linked to, so it is discarded rather than rendered half empty.
REQUIRED_KEYS = ("id", "title", "source", "url", "published")

# Optional keys and the value we fall back to when a source omits them. This
# keeps the schema uniform for the scorer and the renderer.
OPTIONAL_KEY_DEFAULTS = {
    "deadline": None,
    "countries": [],
    "sectors": [],
    "funder": None,
    "value_usd": None,
    "summary": "",
}

# Matches the contract in the brief. Enforced defensively here because a source
# fetcher that forgets to trim would otherwise bloat the rendered page.
SUMMARY_MAX_CHARS = 1200

# Generic procurement boilerplate. Dropping it before comparing titles is what
# lets "Request for Proposals: Endline Evaluation of the Nutrition Programme"
# and "Endline evaluation, nutrition programme (tender notice)" collapse into
# one entry for the director.
TITLE_STOPWORDS = frozenset(
    [
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
        "by", "with", "from", "into", "under", "re",
        "rfp", "rfq", "rfi", "eoi", "itb", "itt",
        "request", "requests", "proposal", "proposals", "quotation",
        "quotations", "tender", "tenders", "bid", "bids", "bidding",
        "notice", "notices", "invitation", "invitations", "expression",
        "expressions", "interest", "announcement", "opportunity",
        "procurement", "provision", "consultancy", "consultant",
        "consultants", "consulting", "firm", "firms", "individual",
        "international", "national", "terms", "reference", "tor", "tors",
        "hiring", "recruitment", "engagement", "assignment", "contract",
        "call", "solicitation", "advert", "advertisement", "vacancy",
    ]
)

# A near duplicate match needs at least this many meaningful words. Below the
# threshold the key is too generic ("evaluation services" would swallow
# unrelated tenders), so those records are left alone.
MIN_TITLE_KEY_TOKENS = 4

_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")
_ISO_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _log(message):
    """Send a progress or warning line to stderr.

    stdout is reserved for anything a caller might want to pipe, and the
    GitHub Actions log captures both streams, so stderr is where the operator
    looks when a source goes quiet.
    """
    print(message, file=sys.stderr, flush=True)


def _load_sources():
    """Return sources.SOURCES, or raise a message that explains the problem.

    src/sources.py is written by a separate agent, so this module has to cope
    with it being absent or half finished without dumping a bare ImportError
    traceback on whoever runs the tool.
    """
    src_dir = pathlib.Path(__file__).resolve().parent
    if str(src_dir) not in sys.path:
        # Supports both "python src/main.py" and importing src.fetch as part of
        # a package, without needing an __init__.py.
        sys.path.insert(0, str(src_dir))
    try:
        import sources  # noqa: PLC0415, imported lazily on purpose
    except ImportError as exc:
        raise RuntimeError(
            "Cannot import the source fetchers. Expected a module at "
            + str(src_dir / "sources.py")
            + " exposing SOURCES as a list of (name, fetch_callable) tuples, "
            "where each callable takes a datetime.date and returns canonical "
            "records. Underlying import error: " + str(exc)
        ) from exc
    registry = getattr(sources, "SOURCES", None)
    if registry is None:
        raise RuntimeError(
            "src/sources.py was imported but does not define SOURCES. It must "
            "be a list of (name, fetch_callable) tuples."
        )
    if not isinstance(registry, (list, tuple)):
        raise RuntimeError(
            "src/sources.py defines SOURCES as "
            + type(registry).__name__
            + ", but it must be a list of (name, fetch_callable) tuples."
        )
    return list(registry)


def _parse_iso_date(value):
    """Return a datetime.date, or None when the value cannot be trusted.

    Sources vary: some give "2026-09-14", some give a full ISO timestamp such
    as "2026-09-14T08:30:00Z". Both carry the date we need, so we read the date
    prefix and ignore the time. Assumption: any timezone offset is close enough
    to irrelevant for a seven day freshness window, so we do not convert.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        return None
    match = _ISO_DATE_PREFIX.match(value.strip())
    if not match:
        return None
    try:
        return datetime.date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
    except ValueError:
        # Catches impossible dates such as 2026-02-31.
        return None


def _coerce_record(item, source_name):
    """Validate one record and fill in optional keys, or return None.

    Defensive on purpose: sources.py is written against the same contract, but
    a portal changing its JSON shape should cost us one notice, not the run.
    """
    if not isinstance(item, dict):
        return None
    for key in REQUIRED_KEYS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    record = dict(item)
    record["title"] = record["title"].strip()
    record["url"] = record["url"].strip()
    for key, default in OPTIONAL_KEY_DEFAULTS.items():
        if key not in record or record[key] is None:
            # A list default must not be shared between records, hence the copy.
            record[key] = list(default) if isinstance(default, list) else default
    for key in ("countries", "sectors"):
        if not isinstance(record[key], (list, tuple)):
            record[key] = []
        else:
            record[key] = [str(v).strip() for v in record[key] if str(v).strip()]
    if not isinstance(record["summary"], str):
        record["summary"] = ""
    if len(record["summary"]) > SUMMARY_MAX_CHARS:
        record["summary"] = record["summary"][:SUMMARY_MAX_CHARS].rstrip()
    if record.get("source") != source_name and source_name:
        # Trust the registry name over the fetcher, so the report never shows a
        # source label the director cannot match to the list of portals.
        record["source"] = source_name
    return record


def _call_source(name, fetcher, since):
    """Run one fetcher and return only the records that match the contract."""
    raw = fetcher(since)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise TypeError(
            "source returned " + type(raw).__name__ + ", expected a list of dicts"
        )
    cleaned = []
    malformed = 0
    for item in raw:
        record = _coerce_record(item, name)
        if record is None:
            malformed += 1
        else:
            cleaned.append(record)
    if malformed:
        _log(
            "fetch_all: " + name + " returned " + str(malformed)
            + " malformed record(s), skipped."
        )
    return cleaned


def fetch_all(days=7, source_list=None):
    """Fetch from every source concurrently and return the merged records.

    days        how far back to ask each source to look. Converted to a
                datetime.date because that is what each fetcher expects.
    source_list optional registry override, used by the smoke test. Production
                callers leave it out and get sources.SOURCES.

    Failures are isolated per source: a fetcher that raises, returns rubbish or
    times out is logged to stderr and contributes nothing. A partial digest is
    far more useful to the director than no digest.
    """
    registry = _load_sources() if source_list is None else list(source_list)
    if not registry:
        _log("fetch_all: no sources registered, nothing to fetch.")
        return []

    since = datetime.date.today() - datetime.timedelta(days=days)
    merged = []
    ok_sources = 0
    failed_sources = 0
    budget_ends = time.monotonic() + TOTAL_FETCH_TIMEOUT_SECONDS

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        pending = []
        for entry in registry:
            try:
                name, fetcher = entry
            except (TypeError, ValueError):
                _log("fetch_all: skipping malformed SOURCES entry " + repr(entry))
                failed_sources += 1
                continue
            if not callable(fetcher):
                _log("fetch_all: skipping " + str(name) + ", fetcher is not callable.")
                failed_sources += 1
                continue
            pending.append((str(name), pool.submit(_call_source, str(name), fetcher, since)))

        for name, future in pending:
            # The sources run in parallel, so waiting on them in submission
            # order costs at most one source timeout overall, not one per
            # source. The total budget is the backstop.
            remaining = min(
                SOURCE_TIMEOUT_SECONDS, max(0.0, budget_ends - time.monotonic())
            )
            try:
                records = future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                future.cancel()
                failed_sources += 1
                _log(
                    "fetch_all: " + name + " timed out after "
                    + str(int(remaining)) + "s, skipped."
                )
                continue
            except Exception as exc:
                # Deliberately broad: any single source is expendable.
                failed_sources += 1
                _log(
                    "fetch_all: " + name + " failed ("
                    + type(exc).__name__ + ": " + str(exc) + "), skipped."
                )
                continue
            ok_sources += 1
            merged.extend(records)
            _log("fetch_all: " + name + " returned " + str(len(records)) + " record(s).")
    finally:
        # Do not block on a thread that is still stuck in a slow HTTP call.
        # Assumption: every fetcher passes a timeout to urllib, otherwise a
        # hung socket would still delay interpreter shutdown.
        pool.shutdown(wait=False, cancel_futures=True)

    _log(
        "fetch_all: " + str(len(merged)) + " record(s) from "
        + str(ok_sources) + " source(s), " + str(failed_sources) + " unavailable."
    )
    return merged


def _title_key(title):
    """Build a normalised comparison key, or "" when the title is too generic.

    Lowercase, strip punctuation, collapse whitespace, drop procurement
    stopwords and single characters, then sort the remaining words so that a
    reordered title still matches. Sorting is safe here only because we insist
    on at least MIN_TITLE_KEY_TOKENS meaningful words.
    """
    lowered = (title or "").lower()
    words = _WHITESPACE.sub(" ", _NON_WORD.sub(" ", lowered)).strip().split(" ")
    meaningful = [w for w in words if len(w) > 1 and w not in TITLE_STOPWORDS]
    if len(meaningful) < MIN_TITLE_KEY_TOKENS:
        return ""
    return " ".join(sorted(set(meaningful)))


def _summary_length(record):
    """Length of a record's summary, used to decide which copy is richer."""
    summary = record.get("summary")
    return len(summary) if isinstance(summary, str) else 0


def _merge_string_lists(first, second):
    """Union of two lists, order preserving, case insensitive on duplicates."""
    out = []
    seen = set()
    for source in (first, second):
        if not isinstance(source, (list, tuple)):
            continue
        for value in source:
            text = str(value).strip()
            marker = text.lower()
            if text and marker not in seen:
                seen.add(marker)
                out.append(text)
    return out


def _merge_pair(left, right):
    """Combine two records describing the same opportunity.

    The copy with the richer summary wins, because that is the one that tells
    the director what the work actually is. Country and sector lists are
    merged, since one portal often tags the country and the other does not.
    Assumption: filling an empty deadline, funder or value from the other copy
    is an improvement rather than a risk, as the two records are the same
    notice. No new keys are introduced.
    """
    keep, other = (
        (left, right) if _summary_length(left) >= _summary_length(right) else (right, left)
    )
    merged = dict(keep)
    merged["countries"] = _merge_string_lists(keep.get("countries"), other.get("countries"))
    merged["sectors"] = _merge_string_lists(keep.get("sectors"), other.get("sectors"))
    for field in ("deadline", "funder", "value_usd"):
        if merged.get(field) in (None, "") and other.get(field) not in (None, ""):
            merged[field] = other[field]
    return merged


def dedupe(records):
    """Collapse repeated opportunities. Two passes, order preserving.

    Pass one matches on the stable "id" field, which catches the same notice
    picked up twice from the same portal. Pass two matches on a normalised
    title key, which catches the same tender cross posted to two portals under
    slightly different wording.
    """
    if not records:
        return []

    def collapse(items, key_for, label):
        buckets = {}
        order = []
        unique_counter = 0
        for item in items:
            key = key_for(item)
            if key is None:
                # No usable key, so the record cannot be compared. Keep it under
                # a key of its own rather than risk a false merge.
                unique_counter += 1
                key = ("__unkeyed__", unique_counter)
            if key in buckets:
                buckets[key] = _merge_pair(buckets[key], item)
            else:
                buckets[key] = item
                order.append(key)
        collapsed = len(items) - len(order)
        if collapsed:
            _log("dedupe: " + label + " pass merged " + str(collapsed) + " record(s).")
        return [buckets[key] for key in order]

    def id_key(record):
        identifier = record.get("id") or record.get("url")
        if isinstance(identifier, str) and identifier.strip():
            return ("id", identifier.strip())
        return None

    def title_key(record):
        key = _title_key(record.get("title"))
        return ("title", key) if key else None

    first_pass = collapse(list(records), id_key, "exact id")
    second_pass = collapse(first_pass, title_key, "near duplicate title")
    _log(
        "dedupe: " + str(len(records)) + " record(s) in, "
        + str(len(second_pass)) + " out."
    )
    return second_pass


def filter_fresh(records, days=7, today=None):
    """Keep only opportunities that are recent and still open.

    Three reasons to drop a record:

      1. published more than "days" days ago, so it is not new to the director
      2. deadline already passed, because an expired tender is pure noise
      3. published date cannot be parsed

    Assumption on point three: an unparseable published date is treated as a
    failure and the record is DROPPED, not kept. The brief rewards freshness,
    and a notice we cannot date might be years old, so letting it through would
    undermine the one promise the page makes. The count is logged to stderr so
    a source that starts emitting bad dates is visible in the Actions log.

    Assumption on point two: an unparseable deadline is treated as "no deadline
    given" and the record is kept, since a missing deadline is common and is
    not evidence that the opportunity has closed.

    today is injectable for testing. Production callers leave it out.
    """
    reference = today or datetime.date.today()
    cutoff = reference - datetime.timedelta(days=days)

    kept = []
    dropped_unparseable = 0
    dropped_stale = 0
    dropped_expired = 0

    for record in records or []:
        published = _parse_iso_date(record.get("published"))
        if published is None:
            dropped_unparseable += 1
            continue
        if published < cutoff:
            dropped_stale += 1
            continue
        deadline = _parse_iso_date(record.get("deadline"))
        if deadline is not None and deadline < reference:
            dropped_expired += 1
            continue
        kept.append(record)

    if dropped_unparseable:
        _log(
            "filter_fresh: dropped " + str(dropped_unparseable)
            + " record(s) with an unparseable published date."
        )
    _log(
        "filter_fresh: kept " + str(len(kept)) + " of "
        + str(len(records or [])) + " record(s). Dropped "
        + str(dropped_stale) + " published before " + cutoff.isoformat()
        + ", " + str(dropped_expired) + " past deadline, "
        + str(dropped_unparseable) + " undated."
    )
    return kept


if __name__ == "__main__":
    # Smoke test. Runs with no network and no sources.py, so a reviewer can
    # check the aggregation logic on its own: python src/fetch.py
    import hashlib

    def _stub(source, title, published, deadline=None, countries=None, summary=""):
        """Build a canonical record the same way a real fetcher would."""
        url = "https://example.org/" + hashlib.sha1(title.encode()).hexdigest()[:8]
        return {
            "id": hashlib.sha1((source + url).encode()).hexdigest()[:12],
            "title": title,
            "source": source,
            "url": url,
            "published": published,
            "deadline": deadline,
            "countries": countries or [],
            "sectors": [],
            "funder": None,
            "value_usd": None,
            "summary": summary,
        }

    today = datetime.date(2026, 9, 18)
    recent = (today - datetime.timedelta(days=2)).isoformat()
    old = (today - datetime.timedelta(days=40)).isoformat()

    cross_posted_a = _stub(
        "ReliefWeb",
        "Request for Proposals: Endline Evaluation of the Maternal Health Programme",
        recent,
        countries=["Kenya"],
        summary="Short version of the notice.",
    )
    cross_posted_b = _stub(
        "UNGM",
        "Endline evaluation, maternal health programme (tender notice)",
        recent,
        countries=["Kenya", "Uganda"],
        summary="A much longer description of the same endline evaluation, "
        "including the scope of work and the reporting requirements.",
    )

    sample = [
        cross_posted_a,
        cross_posted_b,
        dict(cross_posted_a),  # exact duplicate, same id
        _stub("ReliefWeb", "Baseline survey for a social protection pilot", recent),
        _stub("UNGM", "Impact evaluation of a school feeding programme", old),
        _stub(
            "DevBusiness",
            "Third party monitoring of a nutrition programme",
            recent,
            deadline=(today - datetime.timedelta(days=1)).isoformat(),
        ),
        _stub("UNGM", "Supply of office furniture", "not-a-date"),
    ]

    print("smoke test: " + str(len(sample)) + " sample record(s) in")

    deduped = dedupe(sample)
    print("smoke test: " + str(len(deduped)) + " record(s) after dedupe")
    assert len(deduped) == 5, deduped

    merged_pair = [r for r in deduped if "maternal" in r["title"].lower()]
    assert len(merged_pair) == 1, merged_pair
    assert merged_pair[0]["source"] == "UNGM", "richer summary should win"
    assert merged_pair[0]["countries"] == ["Kenya", "Uganda"], merged_pair[0]["countries"]

    fresh = filter_fresh(deduped, days=7, today=today)
    print("smoke test: " + str(len(fresh)) + " record(s) after freshness filter")
    assert len(fresh) == 2, [r["title"] for r in fresh]
    assert all(_parse_iso_date(r["published"]) >= today - datetime.timedelta(days=7) for r in fresh)

    # Every record must still carry exactly the canonical keys.
    expected_keys = set(REQUIRED_KEYS) | set(OPTIONAL_KEY_DEFAULTS)
    for record in fresh:
        assert set(record) == expected_keys, set(record) ^ expected_keys

    # Failure isolation: a source that raises must not take the run down.
    def _good(since):
        return [_stub("Good", "Data systems assessment for a health ministry", recent)]

    def _broken(since):
        raise RuntimeError("portal returned 503")

    def _wrong_shape(since):
        return "not a list"

    fetched = fetch_all(
        days=7,
        source_list=[("Good", _good), ("Broken", _broken), ("WrongShape", _wrong_shape)],
    )
    assert len(fetched) == 1, fetched
    assert fetched[0]["source"] == "Good"

    print("smoke test: all checks passed")
