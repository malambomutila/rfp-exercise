"""
Relevance scorer for the daily RFP digest.

Two layers, in this order:

  Layer 1, baseline   deterministic weighted keyword scoring. Always runs,
                      needs no credentials and no network.
  Layer 2, refinement optional. Only runs when OPENROUTER_API_KEY is present
                      in the environment. Sends the strongest baseline records
                      to a language model in ONE batched request so it can
                      correct obvious keyword mistakes and write a sharper
                      rationale for the fundraising director.

The guiding rule for layer 2 is that it is a bonus, never a dependency. If the
key is absent, the call fails, the response times out or the model returns
something that is not valid JSON, every record keeps its baseline score and the
run continues. The digest must never fail because the model step failed.

Python standard library only, so the GitHub Actions run needs no pip install
step and a reviewer can run this file with no setup:

    python src/score.py

Input records follow the canonical schema produced by src/sources.py and
filtered by src/fetch.py:

    id, title, source, url, published, deadline, countries, sectors,
    funder, value_usd, summary

This module ADDS keys and removes none:

    score      int, 0 to 100
    tier       str, "High", "Medium" or "Low"
    rationale  str, one or two plain English sentences for the director
    signals    dict, the points contributed by each signal
    scored_by  str, "rules" or "llm", so the report can be honest about it
"""

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Weights. These are the numbers agreed in the brief, kept together so the
# scoring policy can be tuned in one place.
# ---------------------------------------------------------------------------

GEOGRAPHY_MAX = 25
SECTOR_MAX = 20
SERVICE_MAX = 35
RECENCY_MAX = 20
PENALTY_MAX = 40

# Points for the first match in a group, then for each further distinct match.
# A second or third match adds less than the first, because one clear signal is
# most of the evidence and repetition adds little.
GEOGRAPHY_COUNTRY_FIRST = 18
GEOGRAPHY_REGION_FIRST = 12
GEOGRAPHY_EXTRA = 7

SECTOR_FIRST = 12
SECTOR_EXTRA = 4

# Service terms are split in two. A "strong" term names the work IDinsight
# actually sells, for example an impact evaluation. A "supporting" term is
# suggestive but weak on its own, for example the word "research".
SERVICE_STRONG_FIRST = 22
SERVICE_SUPPORT_FIRST = 12
SERVICE_EXTRA = 6

PENALTY_FIRST = 20
PENALTY_EXTRA = 10

# Recency decays linearly from RECENCY_MAX at published today to
# RECENCY_FLOOR at RECENCY_WINDOW_DAYS old.
RECENCY_WINDOW_DAYS = 7
RECENCY_FLOOR = 5

# Tier thresholds, inclusive lower bounds.
TIER_HIGH_MIN = 45
TIER_MEDIUM_MIN = 30

# ---------------------------------------------------------------------------
# Layer 2 configuration.
# ---------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Fixed by the agreed architecture.
LLM_MODEL = "anthropic/claude-opus-5"

# One request for the whole batch, so the Actions run stays fast and cheap.
LLM_BATCH_SIZE = 25

# Generous enough for one batched call, tight enough that a hanging endpoint
# cannot stall the daily run.
LLM_TIMEOUT_SECONDS = 90

# The model only needs enough of the notice to judge it. Trimming keeps the
# prompt small, which keeps the call cheap and fast.
LLM_SUMMARY_CHARS = 700

# ---------------------------------------------------------------------------
# IDinsight fit vocabulary.
# ---------------------------------------------------------------------------

# Priority countries. The label is what the rationale shows the director, so it
# is written the way she would write it.
PRIORITY_COUNTRIES = [
    "India", "Kenya", "Nigeria", "Zambia", "Ethiopia", "Rwanda", "Tanzania",
    "Uganda", "Ghana", "Malawi", "Senegal", "Morocco", "Pakistan",
    "Bangladesh", "Philippines", "Indonesia", "Burkina Faso", "Niger", "Mali",
    "Mozambique", "Sierra Leone", "Liberia", "Nepal", "Cambodia", "Vietnam",
]

# Regional and multi-country terms. Weaker than a named priority country,
# because "Africa" could mean anywhere on the continent.
PRIORITY_REGIONS = [
    "Africa", "Sub-Saharan Africa", "Sub Saharan Africa", "East Africa",
    "West Africa", "Southern Africa", "South Asia", "Southeast Asia",
    "South East Asia", "Asia Pacific", "global", "multi-country",
    "multi country",
]

PRIORITY_SECTORS = [
    "health", "public health", "education", "agriculture", "nutrition",
    "social protection", "WASH", "water and sanitation", "sanitation",
    "gender", "financial inclusion", "governance", "climate adaptation",
    "child development", "early childhood", "immunisation", "immunization",
    "vaccination", "maternal health", "livelihoods",
]

# The heaviest signal in the model. A monitoring or evaluation tender in a
# country IDinsight does not yet work in is still a better lead than a
# construction tender in Kenya, which is why SERVICE_MAX is the largest weight.
SERVICE_TERMS_STRONG = [
    "impact evaluation", "randomised controlled trial",
    "randomized controlled trial", "RCT", "monitoring and evaluation",
    "monitoring evaluation", "m and e", "MEL", "MERL", "learning partner",
    "third party monitoring", "baseline survey", "endline survey",
    "midline survey", "midline", "endline", "data systems", "data system",
    "management information system", "MIS", "data science",
    "machine learning", "cost effectiveness", "cost effective analysis",
    "process evaluation", "performance evaluation", "formative evaluation",
    "summative evaluation", "data quality assessment",
    "monitoring and learning", "evaluation partner",
]

SERVICE_TERMS_SUPPORTING = [
    "data platform", "predictive model", "predictive modelling",
    "survey design", "survey firm", "survey research", "research",
    "evidence", "needs assessment", "dashboard", "baseline",
    "data collection", "data analysis", "evaluation", "monitoring",
]

# Work IDinsight does not do. These are penalised hard, because a keyword
# scorer that ranks a road contract above an evaluation wastes the director's
# time and costs the tool her trust.
NEGATIVE_TERMS = [
    "construction", "civil works", "supply of equipment", "vehicle hire",
    "vehicle supply", "catering", "furniture", "printing", "stationery",
    "security guard", "security guards", "cleaning services", "cleaning",
    "insurance brokerage", "fuel supply", "supply of fuel",
    "medical supplies", "pharmaceutical supply", "drilling", "borehole",
    "road rehabilitation", "road construction", "generator", "generators",
    "renovation", "rehabilitation works", "landscaping", "uniforms",
    # Goods and works procurement. These dominate the multilateral feeds and
    # are never IDinsight work, so they must be penalised explicitly.
    "supply and delivery", "supply and installation", "supply of goods",
    "procurement of", "purchase of", "delivery of equipment", "spare parts",
    "laptops", "computers", "vehicles", "motorcycles", "tyres", "toner",
    "kits", "tanks", "pipes", "cement", "solar panels", "air conditioning",
    "hire of", "rental of", "lease of", "maintenance of",
    # French language equivalents, because the World Bank feed carries notices
    # from francophone west Africa untranslated.
    "acquisition de", "fourniture de", "fournitures", "materiel", "materiels",
    "mobilier", "mobiliers", "travaux de", "achat de", "location de",
    # Equipment supply notices that reach the feed with service-sounding words
    # attached, for example "supply, delivery, installation and calibration".
    "supply, delivery", "installation and calibration", "calibration",
    "installation of", "commissioning of",
    # Biomedical and laboratory science. Grants.gov carries a large volume of
    # NIH calls whose research language scores well on the service terms but
    # which are nothing like IDinsight's applied development analytics work.
    "somatic", "mosaicism", "genomic", "genome", "molecular", "in vitro",
    "preclinical", "biomarker", "psychotropic", "neurobiology", "cell line",
    "animal model", "biological materials", "assay", "pathogenesis",
    "clinical trial network", "drug discovery", "vaccine development",
    # Academic award mechanisms. The remaining Grants.gov false positives are
    # United States research training and career grants for universities, for
    # example "Emerging Global Leader Award (K43)". They score well because
    # they are global and use the word research, but IDinsight cannot bid for
    # them: the recipient must be an academic institution. The bare activity
    # codes are matched on word boundaries, so they cannot fire inside a
    # longer word.
    "research training", "training grant", "career development award",
    "fellowship", "postdoctoral", "mentored", "k43", "r01", "u01", "r21",
    "notice of special interest", "notice of intent to publish",
    # Posts advertised for one named individual. IDinsight bids as an
    # organisation, so an individual consultant post is not a project
    # opportunity however well the subject matter fits. ASSUMPTION: "hiring
    # the" is deliberately NOT in this list, because "hiring the services of a
    # firm" is a legitimate tender opening.
    "individual consultant", "individual contractor", "vacancy",
    "internship", "roster of consultants",
]


def _log(message):
    """Send one progress or warning line to stderr.

    stdout stays clean for anything a caller wants to pipe. The Actions log
    captures both streams, so stderr is where the operator looks when the
    model step goes quiet.
    """
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Text normalisation and term matching.
# ---------------------------------------------------------------------------

_AMPERSAND = re.compile(r"&")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_ISO_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _normalise(text):
    """Flatten text to lowercase words separated by single spaces.

    Both the notice text and every search term go through this, so a term
    matches regardless of punctuation, hyphens or casing. "Third-party
    monitoring", "third party monitoring" and "THIRD PARTY MONITORING" all
    reduce to the same string.

    The ampersand is expanded to " and " first, so "M&E" becomes "m and e" and
    "monitoring & evaluation" becomes "monitoring and evaluation". That is why
    "m and e" appears in the strong service list.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    lowered = _AMPERSAND.sub(" and ", text.lower())
    return " " + _NON_WORD.sub(" ", lowered).strip() + " "


def _compile_terms(terms):
    """Return [(label, compiled_pattern)] for a vocabulary list.

    The label is the original spelling, used in the rationale. The pattern
    matches the normalised term on a word boundary, so "Niger" does not fire
    inside "Nigeria" and "Mali" does not fire inside "Malawi".
    """
    compiled = []
    for term in terms:
        normalised = _normalise(term).strip()
        if not normalised:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(normalised) + r"(?!\w)")
        compiled.append((term, pattern))
    return compiled


_COUNTRY_PATTERNS = _compile_terms(PRIORITY_COUNTRIES)
_REGION_PATTERNS = _compile_terms(PRIORITY_REGIONS)
_SECTOR_PATTERNS = _compile_terms(PRIORITY_SECTORS)
_SERVICE_STRONG_PATTERNS = _compile_terms(SERVICE_TERMS_STRONG)
_SERVICE_SUPPORT_PATTERNS = _compile_terms(SERVICE_TERMS_SUPPORTING)
_NEGATIVE_PATTERNS = _compile_terms(NEGATIVE_TERMS)


def _find_terms(text, patterns):
    """Return the labels whose pattern appears in the already normalised text.

    Order follows the vocabulary list, which keeps the rationale wording stable
    from one day to the next.
    """
    return [label for label, pattern in patterns if pattern.search(text)]


def _dedupe_labels(labels):
    """Drop repeated labels that differ only by spelling variant.

    "immunisation" and "immunization" are the same sector, and showing both in
    a rationale would read as a bug. Comparison strips the vowel-neutral
    spelling difference by normalising "z" to "s".
    """
    out = []
    seen = set()
    for label in labels:
        marker = label.lower().replace("z", "s").replace("-", " ")
        if marker not in seen:
            seen.add(marker)
            out.append(label)
    return out


def _drop_subsumed(labels, stronger=None):
    """Remove labels that are already contained in a longer matched label.

    "Africa" always fires inside "West Africa", and "health" always fires
    inside "maternal health". Counting both would inflate the score and would
    read as a bug in the rationale, so the shorter label is dropped whenever a
    longer matched label contains it as a whole phrase.

    When `stronger` is given, labels are compared against that list instead of
    against themselves. That is how a vague supporting service term such as
    "monitoring" is discarded once "third party monitoring" has already fired.
    """
    haystacks = [_normalise(l).strip() for l in (stronger if stronger is not None else labels)]
    kept = []
    for label in labels:
        needle = _normalise(label).strip()
        pattern = re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
        swallowed = any(
            other != needle and pattern.search(other) for other in haystacks
        )
        if not swallowed:
            kept.append(label)
    return kept


def _searchable_text(record):
    """Build the one string every keyword group is matched against.

    Title, summary, countries and sectors are concatenated. The title is
    repeated once, which is a deliberate simplification rather than a separate
    field weight: a term in the title is usually the subject of the notice,
    whereas a term buried in the summary is often incidental. Repeating the
    title does not change whether a term fires, but it keeps the intent of the
    design visible if someone later adds frequency weighting.
    """
    parts = [
        record.get("title") or "",
        record.get("title") or "",
        record.get("summary") or "",
        " ".join(str(c) for c in (record.get("countries") or [])),
        " ".join(str(s) for s in (record.get("sectors") or [])),
        record.get("funder") or "",
    ]
    return _normalise(" ".join(parts))


# ---------------------------------------------------------------------------
# Dates and small helpers.
# ---------------------------------------------------------------------------


def _parse_iso_date(value):
    """Return a datetime.date, or None when the value cannot be trusted.

    Accepts "2026-09-14" and full timestamps such as "2026-09-14T08:30:00Z",
    since both carry the date. Assumption: a timezone offset is irrelevant
    inside a seven day freshness window, so no conversion is attempted.
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


def _tiered_points(count, first, extra, cap):
    """Points for a group: `first` for one match, `extra` for each further one."""
    if count <= 0:
        return 0
    return min(cap, first + extra * (count - 1))


def tier_for(score):
    """Map a 0 to 100 score onto the three tiers the director sees."""
    if score >= TIER_HIGH_MIN:
        return "High"
    if score >= TIER_MEDIUM_MIN:
        return "Medium"
    return "Low"


def _join_english(items):
    """Join labels the way a person writes a list: "a, b and c"."""
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _age_phrase(days):
    """Plain English age of the notice, for the end of the rationale."""
    if days is None:
        return "Published date not given by the source."
    if days <= 0:
        return "Published today."
    if days == 1:
        return "Published yesterday."
    return "Published " + str(days) + " days ago."


# ---------------------------------------------------------------------------
# Layer 1, deterministic baseline.
# ---------------------------------------------------------------------------


def _build_rationale(hits, days_old):
    """Write one or two sentences from the signals that actually fired.

    Written for a senior reader who is not an AI expert: no scores, no jargon,
    no mention of keywords. It says what the notice asks for, where it is, and
    how fresh it is, then flags anything that looks outside IDinsight's work.
    """
    sentences = []

    # score_record hands over already collapsed lists. Collapsing again is
    # harmless and keeps this function usable on raw hits in isolation.
    strong = _dedupe_labels(hits["service_strong"])
    support = _drop_subsumed(_dedupe_labels(hits["service_support"]), stronger=strong)
    countries = _dedupe_labels(hits["countries"])
    regions = _drop_subsumed(_dedupe_labels(hits["regions"]))
    sectors = _drop_subsumed(_dedupe_labels(hits["sectors"]))
    negatives = _drop_subsumed(_dedupe_labels(hits["negatives"]))

    # Service first, because it is the strongest evidence of a real fit.
    if strong:
        sentences.append(
            "Strong service fit, the notice asks for "
            + _join_english([s.lower() for s in strong[:3]])
            + "."
        )
    elif support:
        sentences.append(
            "Possible service fit, the notice mentions "
            + _join_english([s.lower() for s in support[:3]])
            + ", but does not clearly ask for evaluation or data work."
        )
    else:
        sentences.append(
            "No evaluation, monitoring or data services are named in the notice."
        )

    # Then geography and sector, combined into one sentence where both fired.
    place = ""
    if countries:
        place = (
            _join_english(countries)
            + (" is a core IDinsight country" if len(countries) == 1
               else " are core IDinsight countries")
        )
    elif regions:
        place = "It covers " + _join_english(regions) + ", a priority region"

    if sectors and place:
        sentences.append(
            place + ", and it sits in " + _join_english([s.lower() for s in sectors[:3]])
            + "."
        )
    elif place:
        sentences.append(place + ".")
    elif sectors:
        sentences.append(
            "It sits in " + _join_english([s.lower() for s in sectors[:3]])
            + ", a priority sector, though no priority country is named."
        )
    else:
        sentences.append("No priority country or sector is named.")

    sentences.append(_age_phrase(days_old))

    if negatives:
        # The matched phrase is quoted rather than dropped into the sentence
        # bare. The negative vocabulary now holds contract forms as well as
        # trades, for example "individual consultant", and "the notice also
        # involves individual consultant" does not read as English. Quoting
        # keeps the sentence correct whatever the vocabulary grows to hold.
        sentences.append(
            "Caution, the notice also mentions "
            + _join_english(['"' + n.lower() + '"' for n in negatives[:3]])
            + ", which points away from IDinsight's work."
        )

    return " ".join(sentences)


def score_record(record, today=None):
    """Score one record with the deterministic rules and return a new dict.

    The input is never mutated, so a caller can compare before and after. The
    returned dict carries every original key plus score, tier, rationale,
    signals and scored_by.
    """
    reference = today or datetime.date.today()
    text = _searchable_text(record)

    hits = {
        "countries": _find_terms(text, _COUNTRY_PATTERNS),
        "regions": _find_terms(text, _REGION_PATTERNS),
        "sectors": _find_terms(text, _SECTOR_PATTERNS),
        "service_strong": _find_terms(text, _SERVICE_STRONG_PATTERNS),
        "service_support": _find_terms(text, _SERVICE_SUPPORT_PATTERNS),
        "negatives": _find_terms(text, _NEGATIVE_PATTERNS),
    }

    # Collapse spelling variants, then drop labels that a longer matched label
    # already covers, so nothing is counted twice.
    countries = _dedupe_labels(hits["countries"])
    regions = _drop_subsumed(_dedupe_labels(hits["regions"]))
    sectors = _drop_subsumed(_dedupe_labels(hits["sectors"]))
    strong = _dedupe_labels(hits["service_strong"])
    support = _drop_subsumed(_dedupe_labels(hits["service_support"]), stronger=strong)
    negatives = _drop_subsumed(_dedupe_labels(hits["negatives"]))

    # The rationale must describe exactly the signals that were scored.
    hits = dict(hits)
    hits["countries"] = countries
    hits["regions"] = regions
    hits["sectors"] = sectors
    hits["service_strong"] = strong
    hits["service_support"] = support
    hits["negatives"] = negatives

    # Geography. A named priority country outranks a regional term, and extra
    # matches of either kind add a little more.
    if countries:
        extras = (len(countries) - 1) + len(regions)
        geography = min(
            GEOGRAPHY_MAX, GEOGRAPHY_COUNTRY_FIRST + GEOGRAPHY_EXTRA * extras
        )
    elif regions:
        geography = _tiered_points(
            len(regions), GEOGRAPHY_REGION_FIRST, GEOGRAPHY_EXTRA, GEOGRAPHY_MAX
        )
    else:
        geography = 0

    sector = _tiered_points(len(sectors), SECTOR_FIRST, SECTOR_EXTRA, SECTOR_MAX)

    # Service. One strong term is worth far more than several vague ones, so a
    # strong hit sets the base and everything else counts as an extra.
    if strong:
        extras = (len(strong) - 1) + len(support)
        service = min(SERVICE_MAX, SERVICE_STRONG_FIRST + SERVICE_EXTRA * extras)
    elif support:
        service = _tiered_points(
            len(support), SERVICE_SUPPORT_FIRST, SERVICE_EXTRA, SERVICE_MAX
        )
    else:
        service = 0

    # Recency, decaying linearly from RECENCY_MAX today to RECENCY_FLOOR at
    # RECENCY_WINDOW_DAYS old.
    published = _parse_iso_date(record.get("published"))
    if published is None:
        # Assumption: an undated notice earns no freshness points. It should
        # already have been dropped by src/fetch.py, so this is a backstop.
        days_old = None
        recency = 0
    else:
        days_old = (reference - published).days
        if days_old < 0:
            # A future publication date is almost always a source quirk rather
            # than a real embargo, so treat it as published today.
            days_old = 0
        if days_old <= RECENCY_WINDOW_DAYS:
            slope = (RECENCY_MAX - RECENCY_FLOOR) / float(RECENCY_WINDOW_DAYS)
            recency = int(round(RECENCY_MAX - slope * days_old))
        else:
            # Outside the seven day window the brief cares about. Kept rather
            # than dropped, because dropping is src/fetch.py's job.
            recency = 0

    # Negative signals. Halved when a strong service term also fired, because
    # "third party monitoring of a road rehabilitation programme" is genuine
    # IDinsight work and should not be buried. Assumption, and the reason the
    # penalty is recorded separately in signals so it can be audited.
    raw_penalty = _tiered_points(
        len(negatives), PENALTY_FIRST, PENALTY_EXTRA, PENALTY_MAX
    )
    if raw_penalty and strong:
        raw_penalty = int(round(raw_penalty / 2.0))
    penalty = -raw_penalty

    total = geography + sector + service + recency + penalty
    total = max(0, min(100, int(total)))

    # Service-fit gate. IDinsight sells evaluation, monitoring, data and
    # research work. If a notice contains none of that language, geography and
    # sector alone must not be able to promote it out of the Low tier.
    if service == 0:
        total = min(total, TIER_MEDIUM_MIN - 1)

    # Geography gate. IDinsight works in Africa and Asia, so a notice that
    # names no priority country and no priority region is not a High priority
    # call on the director's time even when the subject matter fits well. The
    # clearest case is the EU feed: a domestic Irish or Belgian research
    # tender can score well on sector, service and freshness alone. Such a
    # notice is still shown, and can still reach Medium, because a funder
    # sometimes runs a global call from a European buyer, but it cannot lead
    # the page ahead of work in an IDinsight country. Note that the regional
    # vocabulary includes "global" and "multi-country", so a genuinely global
    # call does clear this gate.
    if geography == 0:
        total = min(total, TIER_HIGH_MIN - 1)

    # High tier gate. A notice only reaches High when it names work IDinsight
    # actually sells, which means at least one strong service term. Supporting
    # words on their own are not enough: "research" plus "health" plus "global"
    # was lifting United States academic research grants to the top of the
    # page, and the director should not have to discover for herself that the
    # best looking item is something IDinsight cannot bid for. This is a
    # structural backstop rather than another keyword, so it keeps holding as
    # the feeds change and the negative vocabulary falls behind them.
    if not strong and total >= TIER_HIGH_MIN:
        total = TIER_HIGH_MIN - 1

    # Actionability gate. A lead has to be either somewhere IDinsight works or
    # a piece of work IDinsight sells. A notice that is neither is not a lead,
    # whatever else it mentions. This is what keeps the United States domestic
    # calls that Grants.gov carries in bulk out of the shortlist: a tribal
    # review board, an unemployment insurance centre and a quantum computing
    # competition all matched on a supporting word plus the sector word
    # "health", with no priority country and no service IDinsight offers, and
    # five of the top ten rows were notices of that kind. They are still
    # published in the Low section rather than hidden, so nothing is lost if
    # the judgement is wrong on a given day.
    if not strong and geography == 0:
        total = min(total, TIER_MEDIUM_MIN - 1)

    scored = dict(record)
    scored["score"] = total
    scored["tier"] = tier_for(total)
    scored["rationale"] = _build_rationale(hits, days_old)
    scored["signals"] = {
        "geography": geography,
        "sector": sector,
        "service": service,
        "recency": recency,
        "penalty": penalty,
    }
    scored["scored_by"] = "rules"
    return scored


def score_baseline(records, today=None):
    """Apply the deterministic rules to every record. Never fails on one record."""
    scored = []
    for record in records or []:
        try:
            scored.append(score_record(record, today=today))
        except Exception as exc:
            # Deliberately broad. One malformed record must not cost the digest.
            _log(
                "score: skipped a record ("
                + type(exc).__name__ + ": " + str(exc) + ")."
            )
    return scored


# ---------------------------------------------------------------------------
# Layer 2, optional model refinement.
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = (
    "You screen tender notices for IDinsight, a global development analytics "
    "organisation that works with governments, foundations and NGOs across "
    "Africa and Asia. IDinsight sells impact evaluation, monitoring and "
    "evaluation, data systems, data science and machine learning, and "
    "programme diagnosis. It does not sell construction, equipment supply, "
    "logistics, catering or other procurement of goods.\n\n"
    "You are given notices that a keyword scorer has already ranked. Your job "
    "is to correct the keyword scorer's obvious mistakes. Lower the score when "
    "a word such as evaluation appears only in boilerplate, when the research "
    "is in a discipline IDinsight does not serve, or when the notice is really "
    "a goods or works contract. Raise the score when the notice is clearly "
    "analytical work that the keyword scorer under-rated.\n\n"
    "For each notice return a score from 0 to 100, a tier of High for 65 and "
    "above, Medium for 40 to 64 and Low below 40, and a rationale of one "
    "sentence written for a senior fundraising director who is not a "
    "technical specialist. Say what the work is and why it fits or does not. "
    "Do not mention scores, keywords or models in the rationale. Use UK "
    "English. Do not use emojis or em dashes.\n\n"
    "Reply with JSON only, in the form "
    '{"results": [{"id": "...", "score": 0, "tier": "Low", '
    '"rationale": "..."}]}. Return one entry for every notice given, using the '
    "id exactly as supplied. No prose outside the JSON."
)


def _llm_payload(records):
    """Build the compact notice list the model is asked to judge."""
    items = []
    for record in records:
        summary = record.get("summary") or ""
        if len(summary) > LLM_SUMMARY_CHARS:
            summary = summary[:LLM_SUMMARY_CHARS].rstrip()
        items.append(
            {
                "id": record.get("id"),
                "title": record.get("title"),
                "source": record.get("source"),
                "published": record.get("published"),
                "countries": record.get("countries") or [],
                "sectors": record.get("sectors") or [],
                "funder": record.get("funder"),
                "baseline_score": record.get("score"),
                "summary": summary,
            }
        )
    return items


def _extract_json(content):
    """Pull a JSON value out of the model's reply, or return None.

    Models sometimes wrap JSON in a code fence or add a sentence of preamble
    despite being told not to, so the first balanced object or array in the
    text is located rather than trusting the whole string.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        # Strip a fenced block, with or without a language tag.
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Try the outermost structure first, which is the one that starts earliest.
    # Without that ordering, a reply such as 'Here you go: [{"id": ...}]' would
    # yield the inner object instead of the list.
    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append((start, text[start:end + 1]))
    for _, snippet in sorted(candidates):
        try:
            return json.loads(snippet)
        except ValueError:
            continue
    return None


def _call_openrouter(records, api_key):
    """Make the single batched request and return the parsed JSON, or None.

    Every failure path returns None rather than raising, because the caller's
    contract is to fall back silently to the baseline.
    """
    body = json.dumps(
        {
            "model": LLM_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Notices to judge:\n"
                    + json.dumps(_llm_payload(records), ensure_ascii=False),
                },
            ],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        method="POST",
        headers={
            # The key is read from the environment only and is never logged.
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "X-Title": "IDinsight RFP digest",
        },
    )
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")
    envelope = json.loads(raw)
    choices = envelope.get("choices") or []
    if not choices:
        _log("score: model returned no choices, keeping baseline scores.")
        return None
    content = (choices[0].get("message") or {}).get("content")
    return _extract_json(content)


def _coerce_results(parsed):
    """Normalise the model's reply into a list of dicts, or return []."""
    if isinstance(parsed, dict):
        for key in ("results", "records", "notices", "data"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def refine_with_llm(scored, api_key=None, batch_size=LLM_BATCH_SIZE):
    """Optionally sharpen the top baseline records with one batched model call.

    Returns a new list. Records the model did not or could not judge keep their
    baseline score, tier and rationale, and keep scored_by "rules". Records it
    did judge get scored_by "llm" and keep their baseline signals, so the
    report can still show which rules fired.

    Silent fallback is the whole point of this function: no key, a network
    error, a timeout, an HTTP error, a non JSON reply or a nonsense score all
    lead to the baseline being returned unchanged.
    """
    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
    if not key:
        _log("score: OPENROUTER_API_KEY not set, using baseline scores only.")
        return list(scored)
    if not scored:
        return list(scored)

    batch = scored[:batch_size]
    try:
        parsed = _call_openrouter(batch, key)
    except urllib.error.HTTPError as exc:
        # Body may carry the reason, but never the key, so it is safe to log.
        _log(
            "score: model call returned HTTP " + str(exc.code)
            + ", keeping baseline scores."
        )
        return list(scored)
    except Exception as exc:
        # Deliberately broad: timeouts, DNS failures, TLS errors, bad JSON.
        _log(
            "score: model call failed ("
            + type(exc).__name__ + ": " + str(exc) + "), keeping baseline scores."
        )
        return list(scored)

    results = _coerce_results(parsed)
    if not results:
        _log("score: model reply was not usable JSON, keeping baseline scores.")
        return list(scored)

    by_id = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier:
            by_id[identifier] = item

    refined = []
    changed = 0
    for record in scored:
        item = by_id.get(record.get("id"))
        if item is None:
            refined.append(record)
            continue
        try:
            new_score = int(round(float(item.get("score"))))
        except (TypeError, ValueError):
            # A missing or non numeric score means this one entry is unusable.
            refined.append(record)
            continue
        new_score = max(0, min(100, new_score))
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            rationale = record.get("rationale")
        updated = dict(record)
        updated["score"] = new_score
        # The tier is recomputed from the score rather than trusted, so the
        # tier and the number the director sees can never disagree.
        updated["tier"] = tier_for(new_score)
        updated["rationale"] = rationale.strip()
        updated["scored_by"] = "llm"
        refined.append(updated)
        changed += 1

    _log(
        "score: model refined " + str(changed) + " of " + str(len(batch))
        + " record(s) sent."
    )
    return refined


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def _sort_key(record):
    """Rank by score, then by freshness, then by title for a stable order.

    A stable order matters because the page is regenerated daily and committed
    to git: two runs over the same data should produce the same file.
    """
    published = _parse_iso_date(record.get("published"))
    return (
        -int(record.get("score") or 0),
        -(published.toordinal() if published else 0),
        (record.get("title") or "").lower(),
    )


def score_all(records, today=None, use_llm=True):
    """Score every record and return a new list sorted by score, highest first.

    today   injectable reference date, used by the smoke test. Production
            callers leave it out.
    use_llm set to False to force the baseline only, whatever the environment
            holds. The default respects OPENROUTER_API_KEY, so the layer is
            automatic in the Actions run and absent on a machine with no key.
    """
    baseline = score_baseline(records, today=today)
    baseline.sort(key=_sort_key)

    if use_llm:
        # refine_with_llm receives the records in baseline order, so "top 25"
        # means the 25 strongest baseline candidates.
        result = refine_with_llm(baseline)
    else:
        result = baseline

    result.sort(key=_sort_key)
    tiers = {"High": 0, "Medium": 0, "Low": 0}
    for record in result:
        tiers[record.get("tier", "Low")] = tiers.get(record.get("tier", "Low"), 0) + 1
    _log(
        "score: " + str(len(result)) + " record(s) scored. High "
        + str(tiers["High"]) + ", Medium " + str(tiers["Medium"])
        + ", Low " + str(tiers["Low"]) + "."
    )
    return result


if __name__ == "__main__":
    # Smoke test. Runs with no network and no credentials, so a reviewer can
    # check the scoring logic on its own: python src/score.py
    import hashlib

    TODAY = datetime.date(2026, 9, 18)

    def fixture(source, title, days_ago, countries=None, sectors=None, summary=""):
        """Build a canonical record the way a real fetcher would."""
        url = "https://example.org/" + hashlib.sha1(title.encode()).hexdigest()[:8]
        return {
            "id": hashlib.sha1((source + url).encode()).hexdigest()[:12],
            "title": title,
            "source": source,
            "url": url,
            "published": (TODAY - datetime.timedelta(days=days_ago)).isoformat(),
            "deadline": None,
            "countries": countries or [],
            "sectors": sectors or [],
            "funder": None,
            "value_usd": None,
            "summary": summary,
        }

    fixtures = [
        fixture(
            "ReliefWeb",
            "Request for Proposals: Baseline Survey and Impact Evaluation of a Maternal Health Programme",
            2,
            countries=["Kenya"],
            sectors=["Health"],
            summary="The ministry seeks a research firm to design and deliver a "
            "baseline survey and a randomised controlled trial of a maternal "
            "health programme, including endline data collection.",
        ),
        fixture(
            "UNGM",
            "Consultancy for monitoring and evaluation of a social protection programme",
            0,
            countries=["Zambia"],
            sectors=["Social protection"],
            summary="Third party monitoring and evaluation support to a cash "
            "transfer programme, including a management information system "
            "review and a performance dashboard.",
        ),
        fixture(
            "DevBusiness",
            "Supply and installation of generators for regional health facilities",
            1,
            countries=["Nigeria"],
            sectors=["Health"],
            summary="Procurement of generators and associated civil works for "
            "twelve health facilities, including construction of housing slabs.",
        ),
        fixture(
            "UNGM",
            "Third party monitoring of a rural road rehabilitation programme",
            3,
            countries=["Ethiopia"],
            summary="Independent third party monitoring of a road rehabilitation "
            "programme, covering data collection, data quality assessment and "
            "quarterly reporting to the funder.",
        ),
        fixture(
            "ReliefWeb",
            "Research consultancy on marine sediment transport",
            6,
            countries=["Norway"],
            summary="Research and evidence review on marine sediment transport. "
            "The contractor will report to the environment agency.",
        ),
        fixture(
            "UNGM",
            "Learning partner for a multi-country education programme",
            7,
            countries=[],
            sectors=["Education"],
            summary="A learning partner is sought for a multi-country education "
            "programme across West Africa and South Asia, covering monitoring, "
            "evaluation and learning, and a data platform for programme teams.",
        ),
    ]

    print("smoke test: " + str(len(fixtures)) + " fixture record(s) in")

    # use_llm is forced off so the test never touches the network, even on a
    # machine that happens to have OPENROUTER_API_KEY set.
    scored = score_all(fixtures, today=TODAY, use_llm=False)

    print("")
    for record in scored:
        print(
            str(record["score"]).rjust(3) + "  " + record["tier"].ljust(8)
            + record["scored_by"].ljust(7) + record["title"][:58]
        )
        print("     " + record["rationale"])
        print("     signals " + json.dumps(record["signals"], sort_keys=True))
    print("")

    by_title = {r["title"]: r for r in scored}

    # Sorted, highest first.
    assert [r["score"] for r in scored] == sorted(
        (r["score"] for r in scored), reverse=True
    ), [r["score"] for r in scored]

    # Nothing removed, the four scoring keys plus scored_by added.
    added = {"score", "tier", "rationale", "signals", "scored_by"}
    original_keys = set(fixtures[0])
    for record in scored:
        assert original_keys <= set(record), original_keys - set(record)
        assert set(record) == original_keys | added, set(record) ^ (original_keys | added)
        assert 0 <= record["score"] <= 100, record["score"]
        assert record["tier"] in ("High", "Medium", "Low"), record["tier"]
        assert record["scored_by"] == "rules", record["scored_by"]
        assert set(record["signals"]) == {
            "geography", "sector", "service", "recency", "penalty"
        }, record["signals"]
        assert record["rationale"] and "--" not in record["rationale"]

    # A baseline survey plus an impact evaluation in a core country is the
    # clearest possible fit, so it must be High and must score the full weight
    # on service. It ties with the monitoring and evaluation fixture on points,
    # so the test asserts on the record rather than on which one sorts first.
    assert scored[0]["tier"] == "High", scored[0]
    evaluation = by_title[
        "Request for Proposals: Baseline Survey and Impact Evaluation of a "
        "Maternal Health Programme"
    ]
    assert evaluation["tier"] == "High", evaluation
    assert evaluation["signals"]["service"] == SERVICE_MAX, evaluation["signals"]
    assert "Kenya" in evaluation["rationale"], evaluation["rationale"]

    # Nested labels must not be shown twice: "Africa" fires inside "West
    # Africa", and only the longer label should survive.
    multi = by_title["Learning partner for a multi-country education programme"]
    assert multi["rationale"].count("Africa") == 1, multi["rationale"]

    # A generator and civil works tender must be penalised and must not be High.
    goods = by_title[
        "Supply and installation of generators for regional health facilities"
    ]
    assert goods["signals"]["penalty"] <= -PENALTY_MAX, goods["signals"]
    assert goods["tier"] != "High", goods

    # Third party monitoring of road works is real IDinsight work, so the
    # halved penalty must leave it ranked above the pure goods tender.
    monitoring = by_title[
        "Third party monitoring of a rural road rehabilitation programme"
    ]
    assert monitoring["score"] > goods["score"], (monitoring["score"], goods["score"])
    assert -PENALTY_MAX < monitoring["signals"]["penalty"] < 0, monitoring["signals"]

    # Recency: today scores the maximum, seven days old scores the floor.
    fresh = by_title[
        "Consultancy for monitoring and evaluation of a social protection programme"
    ]
    week_old = by_title["Learning partner for a multi-country education programme"]
    assert fresh["signals"]["recency"] == RECENCY_MAX, fresh["signals"]
    assert week_old["signals"]["recency"] == RECENCY_FLOOR, week_old["signals"]

    # A regional term stands in for a country, at a lower weight.
    assert 0 < week_old["signals"]["geography"] <= GEOGRAPHY_MAX, week_old["signals"]

    # Off-sector research in a non priority country must not reach High.
    marine = by_title["Research consultancy on marine sediment transport"]
    assert marine["tier"] != "High", marine
    assert marine["signals"]["geography"] == 0, marine["signals"]

    # Word boundaries: Niger must not fire on Nigeria, Mali must not fire on Malawi.
    assert _find_terms(_normalise("Nigeria"), _COUNTRY_PATTERNS) == ["Nigeria"]
    assert _find_terms(_normalise("Malawi"), _COUNTRY_PATTERNS) == ["Malawi"]
    assert _find_terms(_normalise("Niger"), _COUNTRY_PATTERNS) == ["Niger"]

    # Punctuation and ampersand handling.
    assert "m and e" in [
        t for t in _find_terms(_normalise("M&E support services"),
                               _SERVICE_STRONG_PATTERNS)
    ], _find_terms(_normalise("M&E support services"), _SERVICE_STRONG_PATTERNS)
    assert "third party monitoring" in _find_terms(
        _normalise("Third-party monitoring agent"), _SERVICE_STRONG_PATTERNS
    )

    # Tier boundaries. Asserted against the constants rather than against
    # literal numbers, so that retuning the thresholds cannot silently break
    # this smoke test the way hardcoded values did.
    assert tier_for(TIER_HIGH_MIN) == "High"
    assert tier_for(TIER_HIGH_MIN - 1) == "Medium"
    assert tier_for(TIER_MEDIUM_MIN) == "Medium"
    assert tier_for(TIER_MEDIUM_MIN - 1) == "Low"
    assert TIER_MEDIUM_MIN < TIER_HIGH_MIN, "tier thresholds must be ordered"

    # An undated record still scores rather than crashing the run.
    undated = score_record({"title": "Impact evaluation", "published": "not-a-date"},
                           today=TODAY)
    assert undated["signals"]["recency"] == 0, undated["signals"]

    # Layer 2 fallback: no key means the baseline is returned untouched.
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        unchanged = refine_with_llm(scored)
        assert [r["score"] for r in unchanged] == [r["score"] for r in scored]
        assert all(r["scored_by"] == "rules" for r in unchanged)
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved

    # Layer 2 fallback: a broken endpoint must not change any score. The key
    # used here is a placeholder, never a real credential.
    _real_call = _call_openrouter

    def _exploding_call(records, api_key):
        raise urllib.error.URLError("simulated network failure")

    _call_openrouter = _exploding_call
    try:
        survived = refine_with_llm(scored, api_key="[OPENROUTER_API_KEY]")
        assert [r["score"] for r in survived] == [r["score"] for r in scored]
        assert all(r["scored_by"] == "rules" for r in survived)
    finally:
        _call_openrouter = _real_call

    # Layer 2 success path, with a stubbed reply so no network is needed.
    def _stub_call(records, api_key):
        return {
            "results": [
                {
                    "id": records[0]["id"],
                    "score": 91,
                    "tier": "Low",  # deliberately wrong, must be recomputed
                    "rationale": "A clear impact evaluation in a core country.",
                },
                {"id": "unknown-id", "score": 10, "rationale": "Ignore me."},
                {"id": records[1]["id"], "score": "not a number"},
            ]
        }

    _call_openrouter = _stub_call
    try:
        refined = refine_with_llm(scored, api_key="[OPENROUTER_API_KEY]")
    finally:
        _call_openrouter = _real_call

    refined_by_id = {r["id"]: r for r in refined}
    first = refined_by_id[scored[0]["id"]]
    assert first["score"] == 91 and first["tier"] == "High", first
    assert first["scored_by"] == "llm", first
    assert first["rationale"] == "A clear impact evaluation in a core country."
    assert first["signals"] == scored[0]["signals"], "signals must survive refinement"
    second = refined_by_id[scored[1]["id"]]
    assert second["scored_by"] == "rules", "an unusable score must fall back"

    # The fenced and prefixed JSON that models sometimes return.
    assert _extract_json('```json\n{"results": []}\n```') == {"results": []}
    assert _extract_json('Here you go: [{"id": "a"}]') == [{"id": "a"}]
    assert _extract_json("no json here at all") is None

    print("smoke test: all checks passed")
