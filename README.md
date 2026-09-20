# Daily RFP radar

Every morning this tool puts one page in front of IDinsight's fundraising
director listing the tender notices and funding calls published in the last
seven days that are worth chasing, ranked by how well each one fits
IDinsight's geographies, sectors and services. It runs itself on a schedule,
so there is nothing to install, log into or configure. Open the link, read the
High tier first, and click straight through to the original notice.

**Live report: <https://rfp.malambomutila.com>**

Sign in with the credentials given in the submission email. The gate keeps the
page off the open web; it is a courtesy gate over public tender notices rather
than a real access control, and a production version would use IDinsight's own
single sign-on.

> **For the IDinsight review team.** This deployment is temporary and will be
> taken down once the exercise has been reviewed. The tool does not depend on
> it: everything needed to run the pipeline yourself is in this repository, it
> needs nothing installed beyond Python 3.12 or later, and the two-minute
> version is directly below. If the link is already down by the time you read
> this, that is expected, and running it locally gives you the same report
> built from live data.

## Try it in two minutes

No dependencies, no API key, no account. This is the whole thing:

```
git clone https://github.com/malambomutila/rfp-exercise.git
cd rfp-exercise
python3 src/main.py
```

It queries eight live sources, so give it three to five minutes, most of which
is waiting on the slower portals. It prints a running commentary of each stage
to the terminal, then writes `site/index.html`. Open that file in a browser:
it is entirely self contained, with no external requests, so it works offline
and from a file:// URL.

A run with no key looks like this, and the numbers will differ on the day you
run it because the sources are live:

```
stage fetch:   552 records from 8 sources
stage dedupe:  536 after removing duplicates
stage fresh:   509 published within 7 days
score: OPENROUTER_API_KEY not set, using baseline scores only.
stage score:   509 scored, 2 high, 3 medium, 504 low
stage floor:   321 at or above score 20, 188 lower-scoring notices omitted
stage render:  site/index.html and site/data.json
done: 321 opportunities rendered in the report
```

That run is the deterministic keyword layer on its own, which is the honest
floor of what the tool does. To see the full thing, including the model
re-scoring the top 25 and writing the one-line rationale under each
opportunity, supply a key for OpenRouter or any endpoint exposing the same
chat completions API:

```
OPENROUTER_API_KEY=[OPENROUTER_API_KEY] python3 src/main.py
```

The page states which layer scored each result, so you can always tell the two
apart.

## How it works

A small scheduler container runs `python3 src/main.py` once a day at 05:30 UTC,
which is before the working day starts in Lusaka and Nairobi. It writes the
rendered page straight into the directory the web server container serves, and
keeps a dated copy so the history is available.

The schedule runs on your own server rather than on a hosted CI service, so
the project is self-contained: clone it, configure it, start it, and it keeps
producing a daily report with no external account, runner or deploy key
involved.

```
 fetch          eight APIs, portals and web pages, queried in parallel
   |            (a source that fails contributes nothing and is skipped)
   v
 deduplicate    collapse the same notice posted to two portals, by URL
   |            and by near duplicate title
   v
 filter         keep only notices published in the last 7 days
   |            with a title, a link and a date
   v
 score          rules layer always runs, language model refines the top 25
   |            when a key is present; each record gets 0 to 100 and a tier
   v
 render         one self contained index.html plus data.json, no CDN,
   |            no web fonts, no external requests
   v
 publish        written straight into the directory nginx serves, behind
                TLS and a login gate. No copy step, nothing to go wrong
                between building and publishing
```

## Data sources

| Source | What it covers | How it is read |
| --- | --- | --- |
| World Bank | Procurement notices from Bank financed operations worldwide: consulting services, non consulting services, goods and civil works. Roughly 150 new notices a day, each with a country, a project name and a submission deadline. | JSON API, `search.worldbank.org/api/v2/procnotices` |
| UNDP | Procurement notices from UNDP country offices, around 570 open at any time, with heavy coverage of the African and Asian countries IDinsight works in. | No API exists, so the server rendered notice table at `procurement-notices.undp.org` is parsed with a regular expression |
| Grants.gov | US federal funding opportunities, which is where USAID, CDC, NIH and MCC calls appear. Queried with nine narrow keyword passes, for example "impact evaluation" and "monitoring and evaluation", because the index scores one keyword string at a time. | JSON API, `api.grants.gov/v1/api/search2`, plus one detail call per record for the description and the award ceiling |
| TED (EU) | EU and EEA public contract notices, filtered to nine CPV codes covering evaluation consultancy, social research, survey and data analysis services. EU external action contracts for evaluation work in Africa and Asia surface here. | JSON API, `api.ted.europa.eu/v3/notices/search` |
| UNGM | The United Nations Global Marketplace, the widest single source of multilateral consultancy work: notices from WHO, UNICEF, UNDP, ILO, IOM, FAO, UNFPA and others. Queried with nine single service keywords rather than enumerated by date, which returns around a hundred already relevant notices instead of several hundred mostly irrelevant ones. | Undocumented POST endpoint, `ungm.org/Public/Notice/Search`, returning an HTML fragment that is parsed deterministically. No model call. The exact payload and its several traps are documented in `src/websearch.py` |
| Gavi | Gavi's open requests for proposals, expressions of interest and consulting opportunities, covering immunisation programme support, evidence and surveillance work. | Web page, fetched then read by the model |
| Global Fund | Business opportunities plus the independent evaluation pipeline, which pre-announces forthcoming evaluation requests for proposals. | Web page, fetched then read by the model. Currently returns nothing, see Limitations |
| IDRC | Open research funding calls from Canada's International Development Research Centre. | Web page, fetched then read by the model |

Three other sources were tried and dropped, with the reason recorded in
`src/sources.py` so nobody retries them blindly: ReliefWeb, whose v1 API is
decommissioned and whose v2 API rejects every unregistered app name; the World
Bank projects API, which is live but returns 2024 as its newest record and so
carries no freshness signal; and the finances.worldbank.org datasets, which no
longer serve JSON.

## How the scoring works

Every notice is scored out of 100 by adding four positive components and
subtracting a penalty. The weights say what matters: what the work actually is
counts for more than where it is, and where it is counts for more than which
sector it sits in.

| Component | Maximum | What earns the points |
| --- | --- | --- |
| Service fit | 35 | The notice names work IDinsight sells. A strong term such as impact evaluation, RCT, MEL, learning partner, third party monitoring, baseline/endline survey, management information system or machine learning scores 22 for the first match. A weaker supporting term such as "research" or "evidence" scores 12. Each further distinct match adds 6. |
| Geography | 25 | A priority country such as Kenya, India, Zambia or Nigeria scores 18 for the first match. A regional term such as Sub-Saharan Africa, South Asia or multi-country scores 12. Each further distinct match adds 7. |
| Sector | 20 | A priority sector such as health, education, agriculture, nutrition, social protection, WASH, gender or governance scores 12 for the first match, then 4 for each further one. |
| Freshness | 20 | 20 points for a notice published today, decaying in a straight line to 5 points at seven days old. |
| Negative signals | minus 40 | Terms that mark work IDinsight does not do: construction, civil works, equipment supply, vehicle hire, catering, printing, security or cleaning services, fuel, drilling, road rehabilitation. The first match costs 20, each further one 10. The penalty is halved when a strong service term also fired, so that "third party monitoring of a road rehabilitation programme" is not buried. |

The total is clamped to the 0 to 100 range and mapped to a tier: **High** at 45
and above, **Medium** from 30 to 44, **Low** below 30. Those thresholds were set
against the live feeds rather than guessed: the components rarely all fire at
once, so a notice in the middle forties is already a strong fit. Each notice
also carries a one line rationale naming the terms that fired, and the report
shows the component breakdown, so any ranking can be audited without reading
the code.

Two gates then override the arithmetic, because on a real day of tender data
the additive score alone promotes the wrong things:

- **Service gate.** A notice naming none of IDinsight's services cannot leave
  the Low tier, however good the country and sector look. This stops an office
  furniture tender in Kenya outranking an evaluation.
- **Geography gate.** A notice naming no priority country and no priority
  region cannot reach High. This keeps domestic European research tenders,
  which the EU feed supplies in volume, out of the lead position. "Global" and
  "multi-country" count as priority regions, so a genuinely global call is
  unaffected.

Two further filters cut noise before the page is written. Contract **award**
notices are dropped at the source, because an award records a contract already
placed: on 18 September 2026 they were 538 of the 733 World Bank notices in the
window. Anything scoring below 20 is then left out of the report, which on a
typical day is roughly three quarters of the raw feed.

Two layers, in this order:

1. **The rules layer always runs.** It is deterministic, free, offline after
   the fetch, and produces the same answer twice for the same input. It is
   what the score, tier and rationale come from by default.
2. **The language model layer only refines the top results.** When an
   `OPENROUTER_API_KEY` is present in the environment, the 25 highest scoring
   notices are sent in a single batched call and the model returns a revised
   score and a rationale written for the director. Everything else keeps its
   rules score. The tier is always recomputed from the final score, so the
   number and the label can never disagree. If the key is absent, or the call
   times out, errors or returns unusable JSON, the run logs it and keeps the
   rules scores. The report is never blocked on the model.

Records show which layer scored them, so it is always clear whether a ranking
came from the keyword rules or from the model.

## Running it: the options

The quick start above covers the common case. The rest of the surface:

| Command | What it does |
| --- | --- |
| `python3 src/main.py` | The full pipeline, 7-day window, writes `site/` |
| `python3 src/main.py --days 14` | Widen the freshness window |
| `python3 src/main.py --out /tmp/report` | Write somewhere other than `site/` |
| `python3 src/main.py --no-llm` | Force keyword scoring even when a key is set, useful for comparing the two layers |
| `python3 src/main.py --archive ""` | Skip writing the dated archive copy |

Each module also runs on its own, which is the quickest way to inspect one
part without waiting for the whole pipeline:

| Command | What it does |
| --- | --- |
| `python3 src/sources.py` | Record count per API source |
| `python3 src/websearch.py` | Same for the web-page sources |
| `python3 src/score.py` | Scores built-in fixtures, no network, no key |
| `python3 src/render.py` | Renders a fixture report, no network |

Requirements are Python 3.12 or later and nothing else. There is no
`requirements.txt` and no `pip install` step, here or in the scheduler
container, which is deliberate: it removes dependency resolution as a failure
mode and means this repository runs as-is on any machine with a recent Python.
Tested on 3.12 and 3.14.

Two things worth knowing when you run it. The sources are live, so two runs an
hour apart will not return identical numbers, and a portal being slow or down
on the day shows up as that source contributing nothing rather than as a
crash. And `data/` fills with cached API responses and page extractions so a
repeated run within six hours does not re-fetch or re-pay for the same work;
delete it to force a clean run.

## Deploying it

The whole thing is three small containers and nothing installed on the host
beyond Docker. On any server:

```
git clone https://github.com/malambomutila/rfp-exercise.git
cd rfp-exercise/deploy/server
cp .env.example .env
chmod 600 .env          # then edit it
docker compose up -d
docker logs -f rfp_cron
```

| Container | Image | What it does |
| --- | --- | --- |
| `rfp_cron` | `python:3.12-alpine` | Builds the report on a daily schedule |
| `rfp_site` | `nginx:1.27-alpine` | Serves the report and enforces the login gate |
| `rfp_auth` | `python:3.12-alpine` | Validates the login form |

It builds once immediately on start, so you are not waiting a day to see
output. Everything is configured in `.env`: the login credentials, the build
time (`RFP_SCHEDULE_UTC`, default 05:30 UTC), the freshness window, and the
optional `OPENROUTER_API_KEY`. Without that key the report still builds, scored
by the keyword layer alone. `deploy/server/.env.example` documents every
setting.

Two assumptions worth knowing. The stack does not bind ports 80 or 443,
because it expects a reverse proxy in front of it to terminate TLS; that is how
the original host works, where another container already owned those ports. If
you have no proxy, publish `rfp_site` on a port of your choice and point your
own web server at it. And `RFP_NETWORK` must name an existing Docker network
that your proxy is also on, so it can reach `rfp_site` by name.

`docs/server-deployment.md` is the full walkthrough, including the DNS record,
certificate renewal, routine operations and rollback.

## Repository layout

```
README.md                         this file
src/sources.py                    one fetcher per API source, all returning
                                  the same record shape
src/websearch.py                  the web-page sources: fetch the page, then
                                  have the model read it
src/fetch.py                      runs the fetchers in parallel, deduplicates,
                                  filters to the last 7 days
src/score.py                      rules scorer, then the optional model
                                  refinement of the top 25
src/render.py                     builds index.html and data.json
src/main.py                       the pipeline the scheduler runs
deploy/server/docker-compose.yml  the three containers
deploy/server/run-scheduler.sh    the daily schedule, POSIX sh
deploy/server/.env.example        every setting, documented
docs/server-deployment.md         the server, the login gate, DNS, TLS and
                                  rollback
docs/reflection-notes.md          notes on tradeoffs and next steps
deploy/server/auth/app.py         the login service
deploy/server/nginx-site.conf     serving rules and the login gate
site/                             generated output when run locally, not
                                  tracked
data/                             cached API responses and page extractions,
                                  not tracked
```

## Limitations

These are real and worth knowing before trusting the ranking.

- **Coverage stops where open access stops.** The eight sources here are the
  ones reachable without a key or a subscription. The
  aggregators that development organisations actually pay for, Devex and
  DevelopmentAid, are behind paywalls and are absent, so anything they carry
  exclusively will not appear in this report.
- **Foundation and multilateral web pages are partly covered now, not fully.**
  Opportunities published as ordinary web pages rather than through an API used
  to be invisible. Four such sources are now read: UNGM, Gavi, the Global Fund
  and IDRC. Rather than a brittle scraper per funder, each page is fetched and
  then read by the model, so a site restyle does not break the parse the way a
  CSS selector would. What is still missing, and why: the Gates Foundation
  solicitation portal renders entirely in JavaScript and serves no listing to a
  plain HTTP client; the Global Fund's live tenders sit behind an Oracle Fusion
  portal that returns only a loader script, so that source contributes nothing
  today and is kept only for its evaluation pipeline page; Hewlett is largely
  invitation based, so there is often no open call to find; and the Asian and
  African Development Banks block scripted requests outright. Reaching those
  needs a headless browser in the pipeline, which would end the promise that
  this tool installs nothing.
- **Keyword scoring cannot read a tender's technical requirements.** It sees
  the title, the summary and the tagged country and sector fields. It cannot
  tell a serious impact evaluation from a notice that merely uses the phrase,
  and it cannot judge whether the required team, budget or registration
  conditions are ones IDinsight could meet. A high score means "worth two
  minutes of your attention", not "worth bidding".
- **No human feedback loop.** Nothing records which leads the team pursued or
  ignored, so the weights cannot learn. They are informed judgement, set by
  hand, and they will stay exactly as good as that judgement until someone
  feeds real outcomes back in.
- **The web sources depend on a model reading a page correctly.** Extraction is
  instructed to return null rather than guess, and no fabricated notice has
  been found in spot checks against live pages, but this is a real trust
  boundary that the API sources do not have. One known ambiguity is recorded in
  the `fetch_gavi` docstring: Gavi shows one unlabelled date per listing, the
  markup implies a posting date, yet most of those dates are in the future, so
  the extraction reads them as closing dates. If a Gavi deadline is ever wrong,
  that is why.
- **Source fragility is uneven.** The UNDP fetcher reads a rendered HTML table.
  If UNDP restyle that page the parse returns nothing and the report quietly
  loses one source rather than failing loudly. The same applies, less
  severely, to any of the four APIs changing shape.
- **Contract values are indicative.** Non USD amounts are converted with
  static rates held in the code, because there is no FX feed in the standard
  library. Treat them as an order of magnitude, not a figure to plan against.
