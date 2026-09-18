# Daily RFP radar

Every morning this tool puts one page in front of IDinsight's fundraising
director listing the tender notices and funding calls published in the last
seven days that are worth chasing, ranked by how well each one fits
IDinsight's geographies, sectors and services. It runs itself on a schedule,
so there is nothing to install, log into or configure. Open the link, read the
High tier first, and click straight through to the original notice.

**Live report: <https://rfp.malambomutila.com>**

Until the DNS record has propagated and the TLS certificate has been issued,
the same report is served at
<https://malambomutila.github.io/rfp-exercise/>.

## How it works

A GitHub Actions workflow runs `python src/main.py` on a daily cron at 05:30
UTC, which is before the working day starts in Lusaka and Nairobi. The run
publishes the rendered page to GitHub Pages and commits a dated copy to
`reports/` so the history is kept.

```
 fetch          four open APIs and portals, queried in parallel
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
 publish        GitHub Pages serves it at rfp.malambomutila.com
```

## Data sources

| Source | What it covers | How it is read |
| --- | --- | --- |
| World Bank | Procurement notices from Bank financed operations worldwide: consulting services, non consulting services, goods and civil works. Roughly 150 new notices a day, each with a country, a project name and a submission deadline. | JSON API, `search.worldbank.org/api/v2/procnotices` |
| UNDP | Procurement notices from UNDP country offices, around 570 open at any time, with heavy coverage of the African and Asian countries IDinsight works in. | No API exists, so the server rendered notice table at `procurement-notices.undp.org` is parsed with a regular expression |
| Grants.gov | US federal funding opportunities, which is where USAID, CDC, NIH and MCC calls appear. Queried with nine narrow keyword passes, for example "impact evaluation" and "monitoring and evaluation", because the index scores one keyword string at a time. | JSON API, `api.grants.gov/v1/api/search2`, plus one detail call per record for the description and the award ceiling |
| TED (EU) | EU and EEA public contract notices, filtered to nine CPV codes covering evaluation consultancy, social research, survey and data analysis services. EU external action contracts for evaluation work in Africa and Asia surface here. | JSON API, `api.ted.europa.eu/v3/notices/search` |

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

The total is clamped to the 0 to 100 range and mapped to a tier: **High** at 65
and above, **Medium** from 40 to 64, **Low** below 40. Each notice also carries
a one line rationale naming the terms that fired, and the report shows the
component breakdown, so any ranking can be audited without reading the code.

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

## Running it locally

Python 3.12 or later. There are no dependencies: the whole tool is standard
library, so there is no `requirements.txt` and no `pip install` step, here or
in the workflow.

```
git clone https://github.com/malambomutila/rfp-exercise.git
cd rfp-exercise
python src/main.py
```

The run takes a couple of minutes, most of it waiting on the four sources, and
writes `site/index.html` and `site/data.json`. Open the HTML file directly in
a browser: it is self contained, with no external requests. The key is
optional, and the run works without it:

```
OPENROUTER_API_KEY=[OPENROUTER_API_KEY] python src/main.py
```

Each module also runs on its own for debugging: `python src/sources.py` prints
a record count per source, and `python src/render.py` renders a fixture report
without touching the network.

## Repository layout

```
CNAME                             custom domain for GitHub Pages
README.md                         this file
src/sources.py                    one fetcher per source, all returning the
                                  same record shape
src/fetch.py                      runs the fetchers in parallel, deduplicates,
                                  filters to the last 7 days
src/score.py                      rules scorer, then the optional model refinement
src/render.py                     builds index.html and data.json
src/main.py                       the pipeline the workflow runs
.github/workflows/daily-report.yml  daily cron, build, archive, deploy
docs/deployment.md                setting up Pages, the secret, DNS and TLS
docs/reflection-notes.md          notes on tradeoffs and next steps
reports/                          dated archive, one HTML file per day,
                                  committed by the workflow
site/                             generated output, rebuilt on every run,
                                  not tracked
data/                             cached API responses, not tracked
deploy/server/                    static file serving stack, for the case where
                                  the report is self hosted behind nginx
```

## Limitations

These are real and worth knowing before trusting the ranking.

- **Coverage stops where open APIs stop.** The four sources here are the ones
  that serve machine readable data without a key or a subscription. The
  aggregators that development organisations actually pay for, Devex and
  DevelopmentAid, are behind paywalls and are absent, so anything they carry
  exclusively will not appear in this report.
- **Foundation calls are mostly invisible.** Gates Foundation, Hewlett, Rockefeller
  and most bilateral donors publish calls as ordinary HTML pages with no feed
  and no API. Reaching them needs a scraper per funder, which is real
  maintenance, not a configuration change.
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
- **Source fragility is uneven.** The UNDP fetcher reads a rendered HTML table.
  If UNDP restyle that page the parse returns nothing and the report quietly
  loses one source rather than failing loudly. The same applies, less
  severely, to any of the four APIs changing shape.
- **Contract values are indicative.** Non USD amounts are converted with
  static rates held in the code, because there is no FX feed in the standard
  library. Treat them as an order of magnitude, not a figure to plan against.
