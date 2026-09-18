#!/usr/bin/env python3
"""Entry point for the IDinsight client opportunity finder.

Runs the whole pipeline in order: fetch from every source, remove duplicates,
keep only what is fresh, score each opportunity for IDinsight fit, then render
the report. Designed to be run unattended by GitHub Actions on a daily cron,
so it prints a one line summary per stage to stderr and never exits non-zero
for an empty result. A quiet day is not a build failure, and a red build that
means nothing would train the team to ignore real failures.

Usage:
    python3 src/main.py --days 7 --out site
"""

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the sibling modules importable whether this is run as "python3 src/main.py"
# from the repository root or from inside src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch
import render
import score

# Africa/Lusaka is UTC+2 all year and observes no daylight saving, so a fixed
# offset is correct here and avoids depending on the tz database being present
# on the runner. Assumption: if IDinsight later wants a different reporting
# timezone, this is the single place to change it.
LUSAKA = timezone(timedelta(hours=2), name="CAT")

# Minimum score a notice needs to appear in the report at all. Tuned against a
# real run: below this the notices are goods procurement, not IDinsight work.
RENDER_FLOOR = 20


def _log(message):
    """Stage logging goes to stderr so stdout stays clean for piping."""
    print(message, file=sys.stderr, flush=True)


def build(days=7, out_dir="site", archive_dir="reports", use_llm=True):
    """Run the pipeline and write the report. Returns the scored records."""
    generated_at = datetime.now(LUSAKA)

    raw = fetch.fetch_all(days=days)
    _log(f"stage fetch:   {len(raw)} records from {len(fetch_source_names())} sources")

    unique = fetch.dedupe(raw)
    _log(f"stage dedupe:  {len(unique)} after removing duplicates")

    fresh = fetch.filter_fresh(unique, days=days)
    _log(f"stage fresh:   {len(fresh)} published within {days} days")

    scored = score.score_all(fresh, use_llm=use_llm)
    tiers = {"High": 0, "Medium": 0, "Low": 0}
    for record in scored:
        tiers[record.get("tier", "Low")] = tiers.get(record.get("tier", "Low"), 0) + 1
    _log(
        f"stage score:   {len(scored)} scored, "
        f"{tiers['High']} high, {tiers['Medium']} medium, {tiers['Low']} low"
    )

    # Render floor. The multilateral feeds return roughly a thousand notices a
    # week, the great majority of them goods procurement that scored near zero.
    # Rendering all of them produced a 1.4MB page that buried the real leads, so
    # anything below the floor is dropped from the report. The count is logged
    # rather than dropped silently, and data.json keeps only what is rendered.
    floor = RENDER_FLOOR
    shortlist = [r for r in scored if r.get("score", 0) >= floor]
    if len(shortlist) < len(scored):
        _log(
            f"stage floor:   {len(shortlist)} at or above score {floor}, "
            f"{len(scored) - len(shortlist)} lower-scoring notices omitted"
        )

    index_path, data_path = render.render(
        shortlist,
        out_dir,
        generated_at=generated_at,
        window_days=days,
        sources_queried=fetch_source_names(),
    )
    _log(f"stage render:  {index_path} and {data_path}")

    # Keep a dated copy so the team can see what appeared on any given day.
    # This is committed by the workflow, which is why it lives outside out_dir.
    if archive_dir:
        archive = Path(archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        stamped = archive / f"{generated_at.strftime('%Y-%m-%d')}.html"
        shutil.copyfile(index_path, stamped)
        _log(f"stage archive: {stamped}")

    # DELIBERATELY NO CNAME STEP. rfp.malambomutila.com is served by the
    # self-hosted nginx in deploy/server, not by GitHub Pages, so this
    # directory must not carry a CNAME file: a CNAME in the Pages artifact
    # would make Pages claim the same hostname that nginx already serves, and
    # the two would fight over it. Pages stays on its github.io address as a
    # mirror. If the delivery path is ever switched back to Pages, copy the
    # repository CNAME into out_dir here and remove the nginx vhost.

    # Return what was rendered, not everything that was scored, so the caller
    # reports the number the director will actually see on the page.
    return shortlist


def fetch_source_names():
    """Every source the pipeline asks, including any that returned nothing.

    Read from the registry rather than from the results, so the report can be
    honest about a source having been queried and come back empty.
    """
    import sources

    return [name for name, _ in sources.SOURCES]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate the daily IDinsight opportunity report."
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="freshness window in days (default: 7)",
    )
    parser.add_argument(
        "--out", default="site",
        help="directory for index.html and data.json (default: site)",
    )
    parser.add_argument(
        "--archive", default="reports",
        help="directory for the dated archive copy, or empty to skip",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="skip the language model refinement layer even if a key is set",
    )
    args = parser.parse_args(argv)

    try:
        scored = build(
            days=args.days,
            out_dir=args.out,
            archive_dir=args.archive or None,
            use_llm=not args.no_llm,
        )
    except Exception as error:  # noqa: BLE001
        # A total failure still needs to be loud, because it means no report at
        # all. Anything recoverable is already handled inside the stages.
        _log(f"FAILED: {type(error).__name__}: {error}")
        return 1

    _log(f"done: {len(scored)} opportunities rendered in the report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
