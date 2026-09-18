#!/bin/sh
# Daily scheduler for the RFP opportunity report.
#
# This replaces what used to be a GitHub Actions cron. The schedule now lives
# with the project and runs on whatever server the project is deployed to, so
# anyone who clones this repository gets a working daily tool without needing a
# GitHub account, an Actions runner, a deploy key or any repository secrets.
#
# Why a sleep loop rather than crond:
#   A container running crond needs its log plumbed to stdout to be visible to
#   "docker logs", needs the environment exported into the crontab because cron
#   does not inherit it, and fails silently in ways that are tedious to debug.
#   This loop is a dozen lines, logs to stdout like every other container, and
#   recomputes the next run time from the clock on each pass, so it does not
#   drift and it survives the container being restarted at any hour.
#
# POSIX sh only: the base image is Alpine, which has no bash.

set -eu

# Where the pipeline and its output live inside the container. These are set in
# docker-compose.yml and are mount points, not paths on the host.
SRC_DIR="${RFP_SRC_DIR:-/app/src}"
OUT_DIR="${RFP_OUT_DIR:-/app/site}"
ARCHIVE_DIR="${RFP_ARCHIVE_DIR:-/app/reports}"

# The time of day to build, as UTC "HH:MM". The default of 05:30 UTC is 07:30
# in Lusaka and 08:30 in Nairobi, so the report is waiting before the working
# day starts. Override RFP_SCHEDULE_UTC in .env for a different timezone.
SCHEDULE="${RFP_SCHEDULE_UTC:-05:30}"

# How many days of notices to include. Matches the freshness promise made in
# the report itself, so changing it here changes what the page claims.
WINDOW_DAYS="${RFP_WINDOW_DAYS:-7}"

# Build immediately on first start rather than leaving the site empty until the
# next scheduled time, which could be almost 24 hours away. Set to 0 if you
# would rather the first build wait for the schedule.
RUN_ON_START="${RFP_RUN_ON_START:-1}"

log() {
    # Timestamped so "docker logs rfp_cron" reads as a history of runs.
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] scheduler: $*"
}

build_report() {
    log "starting build, window ${WINDOW_DAYS} days"
    # The pipeline is never allowed to kill the scheduler. A source outage or a
    # transient network failure should cost one day's refresh, not the whole
    # daily job, so the exit status is captured rather than propagated.
    if python3 "${SRC_DIR}/main.py" \
        --days "${WINDOW_DAYS}" \
        --out "${OUT_DIR}" \
        --archive "${ARCHIVE_DIR}"; then
        log "build finished, wrote ${OUT_DIR}/index.html"
    else
        status=$?
        log "build FAILED with exit status ${status}, keeping the previous report"
    fi
}

# Strip a leading zero from a two digit clock field. Without this, POSIX
# arithmetic reads "08" and "09" as invalid octal and the script dies, which
# would have broken the scheduler every day between 08:00 and 09:59 UTC.
strip_zero() {
    value="${1#0}"
    echo "${value:-0}"
}

seconds_until_schedule() {
    # How long to sleep until the next occurrence of SCHEDULE in UTC.
    # Computed purely from clock fields, with no date string parsing, so it
    # behaves identically under BusyBox and GNU coreutils.
    target_hour=$(strip_zero "${SCHEDULE%%:*}")
    target_min=$(strip_zero "${SCHEDULE##*:}")
    now_hour=$(strip_zero "$(date -u '+%H')")
    now_min=$(strip_zero "$(date -u '+%M')")
    now_sec=$(strip_zero "$(date -u '+%S')")

    now_secs=$(( now_hour * 3600 + now_min * 60 + now_sec ))
    target_secs=$(( target_hour * 3600 + target_min * 60 ))

    delta=$(( target_secs - now_secs ))
    # Already past today's time, so aim at the same time tomorrow.
    [ "${delta}" -le 0 ] && delta=$(( delta + 86400 ))
    echo "${delta}"
}

log "started. Schedule ${SCHEDULE} UTC, window ${WINDOW_DAYS} days."
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    # Not fatal. The scorer falls back to its deterministic keyword layer, so
    # the report still publishes, just without the model refining the top
    # results or writing their rationales.
    log "OPENROUTER_API_KEY is not set, scoring will use the keyword layer only."
fi

if [ "${RUN_ON_START}" = "1" ]; then
    build_report
fi

while true; do
    sleep_for=$(seconds_until_schedule)
    log "next build in $(( sleep_for / 3600 ))h $(( (sleep_for % 3600) / 60 ))m"
    sleep "${sleep_for}"
    build_report
    # Guard against a build that finishes within the same minute it started,
    # which would otherwise compute a zero-length sleep and run twice.
    sleep 61
done
