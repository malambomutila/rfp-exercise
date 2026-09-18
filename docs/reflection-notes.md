# Reflection notes

Raw material for the three reflection answers, each of which is capped at 150
words in the submission. These are arguments and specifics to draw from, not
finished prose.

## 1. The main tradeoffs of this approach

**The central tradeoff: where the automation lives.**

- The brief asks for the daily report to be generated with no user
  intervention. That requirement, read strictly, decides the architecture.
- A Claude Skill or a Claude Project would have been the closer fit to how
  IDinsight already works. Claude Enterprise is in place, the fundraising
  director could interrogate it conversationally, and it would need no engineer
  to keep it alive. It is the better product for this user.
- But a Skill or a Project cannot trigger itself. Someone has to open Claude
  and ask. That is user intervention, and it is exactly the failure mode the
  brief is guarding against: the report only exists on the days somebody
  remembers to ask for it.
- GitHub Actions on a cron does satisfy the requirement outright. It runs at
  05:30 UTC whether anyone thinks about it or not, costs nothing on a public
  repository, and leaves an auditable log of every run.
- The price of that choice: the artefact is a static page rather than
  something she can talk to, and it sits in a code repository, which makes it
  an engineer's asset rather than a fundraising team's asset. The natural
  resolution is both layers, cron for generation and a Skill for
  interrogation, which is in the "more time" list below.

**Standard library only versus a richer stack.**

- Chosen because it removes the pip install step entirely: zero dependency
  resolution in CI, nothing to pin, nothing to break on a transitive update,
  and a reviewer can clone and run it on Python 3.12 with no setup. On a two
  hour build that reliability is worth a lot.
- The cost is real. No `requests`, so retries, connection pooling and
  redirect handling are hand rolled. No `feedparser` or `bs4`, so the UNDP
  source is a regular expression over a rendered HTML table, which is the most
  fragile component in the tool. No `jinja2`, so the HTML is assembled with
  `string.Template` and joins. A richer stack would have been faster to write
  and easier to extend.

**Open APIs versus paid aggregators.**

- Free, keyless sources mean the tool runs unattended forever with no
  procurement conversation and no credential to rotate.
- But Devex and DevelopmentAid are where a lot of this market is actually
  announced, and they are paywalled. The tool therefore has a structural blind
  spot that no amount of engineering closes: it needs a subscription.
- Open sources also skew towards large multilaterals, so World Bank and UNDP
  work is over represented relative to foundation and bilateral work.

**Deterministic keyword scoring versus LLM scoring.**

- The rules layer is auditable, free, instant and reproducible. The director
  can be shown exactly why a notice ranked highly: these terms fired, these
  points were awarded. That matters for trust with a non technical user who
  has to defend how she prioritises her week.
- It is also shallow. It matches strings, not meaning. It cannot tell a real
  impact evaluation from a notice that happens to use the words.
- The model layer is better at judgement but costs money on every run, varies
  between runs, and is harder to explain. The compromise implemented: rules
  always run and set the baseline, the model refines only the top 25, the tier
  is recomputed from the final score, and any failure falls back silently to
  the rules. Quality improves when a key is present and nothing breaks when it
  is not.
- Also worth naming: the seven day freshness window is itself a tradeoff. It
  satisfies the brief and keeps the page short, but it means a notice with a
  long deadline that was published eight days ago never appears at all.

## 2. What additional content or information would strengthen performance

Ordered by how much the ranking would improve.

- **Historical win/loss record.** The single highest value input. Every
  proposal IDinsight has submitted, with funder, country, sector, service
  line, value and outcome. That turns scoring from hand set weights into
  something fitted on what IDinsight actually wins, which is a different thing
  from what it is technically capable of. It would also expose funders where
  the hit rate is poor enough that the opportunity is not worth the proposal
  cost.
- **The CRM pipeline, likely Salesforce.** Needed to suppress noise. An
  opportunity already being worked by a colleague should not be presented as
  new, and a lead that was deliberately declined should not resurface every
  morning. Without this, the director has to remember the state of the
  pipeline herself, which is the manual work the tool is supposed to remove.
- **Country office footprint and staff availability.** An opportunity in a
  country with no registered entity, or in a service line whose team is fully
  committed for the next two quarters, is not a high priority lead however
  well it matches on paper. Feasibility should be a scoring component, not
  something discovered a week later.
- **Past proposals and capability statements.** These are the corpus to match
  a tender's stated requirements against. Semantic similarity to work
  IDinsight has actually delivered is a far stronger relevance signal than
  keyword presence, and it would also let the report say which prior project
  to reuse as evidence.
- **Funder relationship history.** Who at IDinsight knows whom, which funder
  has a live contract, who has been briefed on which idea. Relationship
  strength is often the deciding factor in whether a tender is winnable, and
  it is entirely invisible to the current tool.
- **Registration, eligibility and compliance data.** Prequalification status
  with each funder, registration in each country, audit and insurance
  thresholds. A tender IDinsight cannot legally bid on should be filtered, not
  ranked.
- **Paid feed access to Devex and DevelopmentAid**, plus any donor early
  warning or forecast feeds. This is the direct fix for the coverage gap and
  is a procurement decision rather than an engineering one.

## 3. What I would do with more time

Ordered by value delivered per unit of effort.

1. **A feedback loop.** Put "pursued" and "not relevant" buttons on each entry
   in the report and store the clicks. Within a few weeks that gives labelled
   data, which is enough to refit the component weights and to learn which
   sources and funders are actually productive. Nothing else on this list
   compounds the way this does, because it turns a static rule set into
   something that improves by being used.
2. **Semantic matching against IDinsight's own capability statements.** Embed
   past proposals, project one pagers and capability statements, then score
   each notice by similarity to that corpus rather than by keyword hits. This
   is what fixes the deepest weakness in the current scoring: it reads meaning
   instead of strings, and it gives a rationale grounded in comparable past
   work.
3. **Scrapers with change detection for the funders who have no feed.** One
   small fetcher per foundation and bilateral page, storing a hash of the
   listing and reporting only what changed. This is where the coverage the
   open APIs miss actually lives. It is ongoing maintenance, so it earns its
   place only after the ranking is good enough to be worth extending.
4. **Push delivery by email and Slack.** The director should not have to
   remember to open a link. A short morning message with the High tier items
   and a link to the full page, posted to a business development channel, puts
   the report where she already is. Cheap to build, and it is what makes the
   tool part of a routine rather than a page to remember.
5. **Deadline tracking and reminders.** Store each opportunity's submission
   deadline and alert when a High tier lead is a week out with no decision
   recorded. Freshness gets an opportunity noticed, deadlines are what make it
   get done, and the current report only shows the former.
6. **A Claude Skill wrapper over the stored data.** The natural interface for
   this user is conversation, not a static page: "what came up in Zambia this
   month", "which funders have posted evaluation work since June", "show me
   everything we marked not relevant and why". The cron keeps generating the
   daily digest with no intervention, and the Skill sits on top of the same
   data for follow up questions. This is how the tool ends up owned by the
   fundraising team rather than by whoever maintains the repository.
7. **Testing and observability.** Recorded fixtures for each source so the
   parsers can be tested without the network, and an alert when a source
   returns zero records for two consecutive days. As it stands a silently
   broken fetcher degrades the report without anyone noticing.
