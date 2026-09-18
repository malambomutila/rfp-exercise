# Workflows

`daily-report.yml` is the only workflow. On a 05:30 UTC cron, on manual
dispatch, and on any push to `main` that touches `src/`, it builds the report
with `python src/main.py`, commits a dated copy to `reports/`, rsyncs the
rendered site to the server behind `rfp.malambomutila.com`, and then checks the
live URL responds as expected.

It needs two repository secrets. `SERVER_SSH_KEY` is required for the deploy
step. `OPENROUTER_API_KEY` is optional: without it the report still publishes,
scored by the deterministic keyword layer alone.

Reading a failed run: the step name tells you which half broke. A failure at
"Build the report" is a pipeline or source problem, and the stage counts printed
to stderr show how far it got. A failure at "Verify the build produced a site"
means the build produced nothing usable, and the deploy was deliberately
skipped so the server kept serving the previous report. A failure at "Deploy the
report to the server over rsync" is usually a missing or rotated
`SERVER_SSH_KEY`. A failure at "Verify the live site responds" means the deploy
itself worked but nginx, the login gate or the TLS certificate did not, so check
the containers on the server before re-running.
