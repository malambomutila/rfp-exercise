# Workflows

`daily-report.yml` is the only workflow here and it is what makes the tool run
itself. Every day at 05:30 UTC, and on demand from the **Run workflow** button,
it checks out the repository, runs `python src/main.py` to fetch, filter and
score the past seven days of tender notices, publishes the rendered page to
GitHub Pages, and commits a dated copy of it to `reports/` so the daily history
stays in the repository. There is no dependency installation step because the
tool uses the Python standard library only.

To read a failed run, open the **Actions** tab, select the red run, then expand
the step marked with a red cross: the last few lines of that step's log carry
the real error, and everything above it is usually noise. The `build` job
failing points at the tool or at a source API that changed shape or went down,
and the quickest check is to run `python src/main.py` locally and compare. The
`deploy` job failing almost always points at configuration rather than code,
most often Pages not being set to the GitHub Actions source, which
`docs/deployment.md` covers. Re-running a failed run is safe: the report is
regenerated from scratch and the archive commit is skipped when nothing
changed.
