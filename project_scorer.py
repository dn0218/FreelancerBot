"""
=============================================================================
FreelancerClaw — Module 2: project_scorer.py
Scores Freelancer search results against the user's profile (0-100).
=============================================================================

Inputs
------
* project dict EXACTLY as returned by freelancer_api.search_projects().
  Verified field names used here (search results):
      id, title, seo_url, preview_description,
      budget{minimum,maximum}, currency{code,exchange_rate},
      bid_stats{bid_count,bid_avg}, submitdate (unix seconds)
  NOTE: description/jobs/owner_* are NULL in search results — this module
  deliberately never touches them (the drafter fetches full details).
* resume text via portfolio_reader.get_resume() (or passed in).
* GitHub portfolio summary via portfolio_reader.get_portfolio() — used ONLY
  to enrich the skill-token set (repo languages + names); the portfolio
  dict itself is optional and never required.

Scoring (defaults; weights configurable via WEIGHTS at module top)
------------------------------------------------------------------
  skill_match   0.35  |resume-skill tokens  &  project tokens| / |project tokens|
                      (lowercase, tokens len >= 3, stopwords removed, cap 1.0)
  budget_fit    0.20  USD-converted budget MIDPOINT ((min+max)/2) vs
                      MIN_BUDGET: >= 2x -> 1.0 ; < 1x -> 0.0 ; linear between.
                      (Wide ranges like USD 30-250 make minimum a floor
                      probe, not a real signal; midpoint estimates better.)
  competition   0.25  bid_count <= 10 -> 1.0 ; >= 150 -> 0.0 ; linear between
  recency       0.20  age <= 6h -> 1.0 ; >= 72h -> 0.0 ; linear between
  exclusion_filter    boolean GATE: any EXCLUDED_KEYWORDS substring in
                      title/preview_description -> discarded, total 0

Missing data policy (hard constraint)
-------------------------------------
If a dimension's input field is None/missing/unusable, the dimension is
EXCLUDED from the weighted sum, the remaining weights are renormalized,
and the dimension name is recorded in excluded_dimensions. A dimension is
never silently zero-scored for missing data (a legitimate raw of 0.0 from
real data — e.g. 5-day-old project — is kept and disclosed).

Config (env vars, matching env.example; OpenWebUI Valves override later)
------------------------------------------------------------------------
  MIN_BUDGET        default 100   (USD)
  SCORE_THRESHOLD   default 70
  EXCLUDED_KEYWORDS default: crypto,adult,captcha bypass,guaranteed returns,
                    profit guarantee,whatsapp business api,linkedin automation,
                    cold email

CLI
---
  python project_scorer.py --test     run mock unit tests T1-T7
  python project_scorer.py --sample   score the hardcoded sample project
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# =============================================================================
# SECTION 1: Constants & configuration
# =============================================================================

# Dimension weights (sum to 1.0). Override per-call via score_project(weights=)
WEIGHTS: Dict[str, float] = {
    "skill_match": 0.35,
    "budget_fit": 0.20,
    "competition": 0.25,
    "recency": 0.20,
}

DEFAULT_MIN_BUDGET = 100
DEFAULT_SCORE_THRESHOLD = 70
DEFAULT_EXCLUDED_KEYWORDS = (
    "crypto,adult,captcha bypass,guaranteed returns,profit guarantee,"
    "whatsapp business api,linkedin automation,cold email"
)

# Common English stopwords (plus marketplace filler like "looking"/"need").
# Applied AFTER the length >= 3 filter, so only len >= 3 entries matter.
STOPWORDS = frozenset(
    """
    the and for are was were been being have has had does did doing not but
    with this that these those from will would shall should can could may
    might must your yours our ours their theirs its his her they them then
    than there here when where why how what which who whom whose all any
    both each few more most other some such only own same too very just
    also because while during before after above below between through
    against about into over under again once out off down up please help
    need needs needed want wants wanted looking seeking like know make
    makes made get gets got let lets using use used uses new one two etc
    via per within without able give given take takes taken see seen say
    said tell told well best good better lot bit way ways thing things
    stuff really much many ever never always often sometimes now soon
    later first last long short small large big great
    """.split()
)

# Token charset: letters, digits, and the symbols that matter in tech
# skills (c++, c#, node.js, .net). Everything else is a separator.
_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")

PROJECT_URL_TEMPLATE = "https://www.freelancer.com/projects/{seo_url}"

# Short labels for the one-line rationale
_LABELS = {
    "skill_match": "skill",
    "budget_fit": "budget",
    "competition": "comp",
    "recency": "recency",
}


def _env_int(name: str, default: int) -> int:
    """Read an int env var; fall back to the default if unset/unparseable."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _parse_keywords(excluded: Optional[Any]) -> List[str]:
    """
    Normalize the excluded-keyword list from (a) an explicit argument,
    (b) the EXCLUDED_KEYWORDS env var, or (c) the default. Always returns a
    list of lowercase, stripped, non-empty keywords.
    """
    if excluded is None:
        excluded = os.environ.get("EXCLUDED_KEYWORDS", DEFAULT_EXCLUDED_KEYWORDS)
    if isinstance(excluded, str):
        items = excluded.split(",")
    else:
        items = list(excluded)
    return [str(kw).strip().lower() for kw in items if str(kw).strip()]


def get_min_budget() -> int:
    return _env_int("MIN_BUDGET", DEFAULT_MIN_BUDGET)


def get_score_threshold() -> int:
    return _env_int("SCORE_THRESHOLD", DEFAULT_SCORE_THRESHOLD)


# =============================================================================
# SECTION 2: Tokenization & skill extraction
# =============================================================================


def _tokenize(text: str) -> Set[str]:
    """
    Lowercase, split on anything outside [a-z0-9+#.], drop tokens shorter
    than 3 chars and stopwords. Returns a SET of tokens.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


_skills_warning_done = False


def extract_skill_tokens(resume_text: str) -> Set[str]:
    """
    Skill tokens from resume.md — Skills-section ONLY (hard requirement).

    The '## Skills' heading is located and ONLY the lines under it (up to
    the next '## ' heading) are parsed as the authoritative skill list.
    The rest of the document (experience prose, employer names, addresses)
    is deliberately NOT tokenized: those words inflate skill_match when
    unrelated project tokens match prose. The full document stays available
    to proposal_drafter as citable evidence.

    Placeholder lines (containing << or >>) are skipped so an unfilled
    template does not inject marker junk.

    Missing '## Skills' heading -> mandated warning (printed once per
    process) and an EMPTY set: no skills are declared, so none are claimed.
    skill_match can still score via portfolio-derived tokens passed in as
    extra_skills. Full-document fallback was removed on review — it was
    the source of score inflation (prose/company/address tokens matching
    project text).
    """
    global _skills_warning_done

    match = re.search(r"^##\s*Skills\s*$(.*?)(?=^##\s|\Z)", resume_text or "", re.M | re.S)
    if match:
        section = match.group(1)
        lines = [ln for ln in section.splitlines() if "<<" not in ln and ">>" not in ln]
        return _tokenize(" ".join(lines))

    if not _skills_warning_done:
        _skills_warning_done = True
        print(
            "resume.md is missing a ## Skills section; no declared skills found "
            "(skill_match will rely on portfolio tokens only).",
            file=sys.stderr,
        )
    return set()


def portfolio_skill_tokens(portfolio: Optional[dict]) -> Set[str]:
    """
    Extra skill tokens from the GitHub portfolio summary: repo languages
    plus repo-name tokens. Optional input — missing portfolio -> empty set.
    """
    tokens: Set[str] = set()
    for repo in (portfolio or {}).get("repos", []):
        lang = repo.get("language")
        if lang:
            tokens.add(str(lang).lower())
        tokens |= _tokenize(str(repo.get("name") or ""))
    return tokens


def _default_resume() -> str:
    """Best-effort resume load via portfolio_reader; empty string if absent."""
    try:
        from portfolio_reader import get_resume

        return get_resume()
    except Exception:  # noqa: BLE001 — resume is optional at import time
        return ""


# =============================================================================
# SECTION 3: Dimension scorers (pure functions)
# Each returns (raw 0..1, note) or (None, reason) when data is MISSING.
# =============================================================================


def _dim_skill_match(project: dict, skill_tokens: Set[str]) -> Tuple[Optional[float], Optional[str]]:
    title = project.get("title") or ""
    preview = project.get("preview_description") or ""
    text_tokens = _tokenize(f"{title} {preview}")
    if not text_tokens:
        return None, "no usable tokens in title/preview_description"
    overlap = skill_tokens & text_tokens
    raw = min(1.0, len(overlap) / len(text_tokens))
    note = f"{len(overlap)}/{len(text_tokens)} tokens overlap"
    return raw, note


def _dim_budget_fit(project: dict, min_budget: float) -> Tuple[Optional[float], Optional[str]]:
    """
    budget_fit on the USD-converted MIDPOINT of the budget range.

    Reference = (budget.minimum + budget.maximum) / 2, converted to USD
    via currency.exchange_rate when currency.code != "USD". Rationale:
    clients post wide ranges (e.g. USD 30-250) where minimum is a floor
    probe, not a real signal. Both bounds are REQUIRED — if either is
    missing the dimension is excluded (weights renormalize) rather than
    guessed from one bound.
    """
    budget = project.get("budget")
    if not isinstance(budget, dict):
        return None, "budget missing"
    minimum = budget.get("minimum")
    maximum = budget.get("maximum")
    if minimum is None or maximum is None:
        return None, "budget bound missing (midpoint needs both minimum and maximum)"

    try:
        minimum = float(minimum)
        maximum = float(maximum)
    except (TypeError, ValueError):
        return None, "budget bounds not numeric"

    currency = project.get("currency")
    currency = currency if isinstance(currency, dict) else {}
    code = currency.get("code")
    if code is None:
        # Nulls stay null: without a currency we cannot convert — exclude.
        return None, "currency.code missing"

    midpoint = (minimum + maximum) / 2.0
    if code == "USD":
        ref_usd = midpoint
        note = f"USD {minimum:.0f}-{maximum:.0f} mid {midpoint:.0f}"
    else:
        rate = currency.get("exchange_rate")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0:
            return None, f"cannot convert {code}: exchange_rate missing or invalid"
        ref_usd = midpoint * rate
        note = f"{code} {minimum:.0f}-{maximum:.0f} mid {midpoint:.0f} -> USD {ref_usd:.2f} (rate {rate})"

    if ref_usd >= 2 * min_budget:
        raw = 1.0
    elif ref_usd < min_budget:
        raw = 0.0
    else:
        raw = (ref_usd - min_budget) / min_budget  # linear ramp 1x..2x
    return raw, note


def _dim_competition(project: dict) -> Tuple[Optional[float], Optional[str]]:
    stats = project.get("bid_stats")
    if not isinstance(stats, dict):
        return None, "bid_stats missing"
    bid_count = stats.get("bid_count")
    if bid_count is None:
        return None, "bid_count missing"
    try:
        bid_count = float(bid_count)
    except (TypeError, ValueError):
        return None, "bid_count not numeric"
    if bid_count < 0:
        return None, "bid_count invalid (negative)"

    if bid_count <= 10:
        raw = 1.0
    elif bid_count >= 150:
        raw = 0.0
    else:
        raw = 1.0 - (bid_count - 10.0) / 140.0
    return raw, f"{bid_count:.0f} bids"


def _dim_recency(project: dict, now: float) -> Tuple[Optional[float], Optional[str]]:
    submitdate = project.get("submitdate")
    if submitdate is None:
        return None, "submitdate missing"
    try:
        submitdate = float(submitdate)
    except (TypeError, ValueError):
        return None, "submitdate not numeric"

    hours = max(0.0, (now - submitdate) / 3600.0)
    if hours <= 6:
        raw = 1.0
    elif hours >= 72:
        raw = 0.0
    else:
        raw = 1.0 - (hours - 6.0) / 66.0
    return raw, f"{hours:.1f}h old"


# =============================================================================
# SECTION 4: ScoredProject output type
# =============================================================================


@dataclass
class ScoredProject:
    """Result of score_project(); see module docstring for semantics."""

    project_id: Any
    title: str
    url: Optional[str]                       # None when seo_url is null
    total_score: float                       # 0-100, 1 decimal
    breakdown: Dict[str, Dict[str, Any]]     # {dim: {"raw","weighted"[,"note"]}}
    excluded_dimensions: List[str]           # missing-data disclosures
    discarded: bool                          # exclusion-gate hit
    reason: Optional[str]                    # discard reason or None
    rationale: str                           # one-line human summary


def meets_threshold(scored: ScoredProject, threshold: Optional[int] = None) -> bool:
    """True when the project is not discarded and score >= threshold."""
    if scored.discarded:
        return False
    threshold = get_score_threshold() if threshold is None else threshold
    return scored.total_score >= threshold


# =============================================================================
# SECTION 5: The scorer
# =============================================================================


def score_project(
    project: dict,
    *,
    resume_text: Optional[str] = None,
    extra_skills: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    min_budget: Optional[float] = None,
    excluded_keywords: Optional[Any] = None,
    weights: Optional[Dict[str, float]] = None,
) -> ScoredProject:
    """
    Score one search-result project. Raises ValueError for structurally
    invalid input (e.g. missing title) — never a silent score.
    """
    if not isinstance(project, dict):
        raise ValueError(f"project must be a dict, got {type(project).__name__}")

    title = project.get("title")
    if not title or not str(title).strip():
        # Hard requirement (T7): a missing title is an error, not a zero.
        raise ValueError(
            f"project dict is missing required field 'title' "
            f"(id={project.get('id', 'unknown')}) - cannot score"
        )
    title = str(title)

    now = time.time() if now is None else float(now)
    min_budget = float(get_min_budget()) if min_budget is None else float(min_budget)
    keywords = _parse_keywords(excluded_keywords)
    w = dict(WEIGHTS)
    if weights:
        w.update(weights)

    if resume_text is None:
        resume_text = _default_resume()
    skill_tokens = extract_skill_tokens(resume_text) | set(extra_skills or ())

    project_id = project.get("id")  # may be None -> stays None (unknown)
    seo_url = project.get("seo_url")
    url = PROJECT_URL_TEMPLATE.format(seo_url=seo_url) if seo_url else None

    # ---- exclusion gate: checked FIRST, wins over everything --------------
    haystack = f"{title} {(project.get('preview_description') or '')}".lower()
    for keyword in keywords:
        if keyword in haystack:
            reason = f"matched excluded keyword: {keyword}"
            return ScoredProject(
                project_id=project_id,
                title=title,
                url=url,
                total_score=0.0,
                breakdown={},
                excluded_dimensions=[],
                discarded=True,
                reason=reason,
                rationale=f"discarded - {reason}",
            )

    # ---- dimensions --------------------------------------------------------
    results: Dict[str, Tuple[Optional[float], Optional[str]]] = {
        "skill_match": _dim_skill_match(project, skill_tokens),
        "budget_fit": _dim_budget_fit(project, min_budget),
        "competition": _dim_competition(project),
        "recency": _dim_recency(project, now),
    }

    breakdown: Dict[str, Dict[str, Any]] = {}
    excluded: List[str] = []
    for name, (raw, note) in results.items():
        if raw is None:
            excluded.append(name)  # missing data: disclose, renormalize
            continue
        entry: Dict[str, Any] = {
            "raw": round(float(raw), 4),
            "weighted": round(float(raw) * w.get(name, 0.0), 4),
        }
        if note:
            entry["note"] = note
        breakdown[name] = entry

    weight_sum = sum(w.get(name, 0.0) for name in breakdown)
    if breakdown and weight_sum > 0:
        total = round(sum(e["weighted"] for e in breakdown.values()) / weight_sum * 100, 1)
    else:
        total = 0.0  # every dimension missing — disclosed via excluded list

    # ---- one-line rationale -------------------------------------------------
    parts = []
    for name in ("skill_match", "budget_fit", "competition", "recency"):
        label = _LABELS[name]
        if name in breakdown:
            parts.append(f"{label} {round(breakdown[name]['raw'], 2)}")
        elif name in excluded:
            parts.append(f"{label} n/a")
    rationale = " | ".join(parts) + f" -> {total}"

    return ScoredProject(
        project_id=project_id,
        title=title,
        url=url,
        total_score=total,
        breakdown=breakdown,
        excluded_dimensions=excluded,
        discarded=False,
        reason=None,
        rationale=rationale,
    )


def score_many(
    projects: Iterable[dict],
    *,
    resume_text: Optional[str] = None,
    extra_skills: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    **kwargs,
) -> List[ScoredProject]:
    """Score a batch; invalid entries raise ValueError (fail loud)."""
    return [
        score_project(p, resume_text=resume_text, extra_skills=extra_skills, now=now, **kwargs)
        for p in projects
    ]


# =============================================================================
# SECTION 6: Mock unit tests (T1-T7)
# =============================================================================

_TEST_NOW = 1_700_000_000.0

_TEST_RESUME = """
# Test User

## Professional summary

Python automation developer.

## Skills

python, web scraping, automation, telegram bot, pdf extraction, llm integration, data analysis, api integration, fastapi, pandas, sql

## Selected experience

### 1. Automation lead
- Built scraping pipelines.
"""


def _base_project(**overrides) -> dict:
    """A near-perfect project; tests override specific fields."""
    project = {
        "id": 1001,
        "title": "Python automation and web scraping expert",
        "seo_url": "Python-automation-and-web-scraping-expert",
        "preview_description": (
            "Need python automation for web scraping, telegram bot integration "
            "and pdf extraction with data analysis."
        ),
        "budget": {"minimum": 250, "maximum": 750},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": 5, "bid_avg": 200},
        "submitdate": _TEST_NOW - 3600,  # 1h old
        "type": "fixed",
        "language": "en",
        "status": "active",
    }
    project.update(overrides)
    return project


def _t1_perfect_project():
    scored = score_project(
        _base_project(), resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert not scored.discarded, "T1: perfect project must not be discarded"
    assert scored.total_score >= 90, f"T1: expected >= 90, got {scored.total_score}"
    assert scored.excluded_dimensions == [], f"T1: unexpected exclusions {scored.excluded_dimensions}"
    assert scored.url == "https://www.freelancer.com/projects/Python-automation-and-web-scraping-expert"
    assert 0 <= scored.total_score <= 100


def _t2_excluded_keyword():
    scored = score_project(
        _base_project(title="Crypto trading bot development"),
        resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert scored.discarded, "T2: crypto project must be discarded"
    assert scored.total_score == 0.0, "T2: discarded project must score 0"
    assert scored.reason and "crypto" in scored.reason, f"T2: bad reason {scored.reason!r}"
    assert scored.rationale.startswith("discarded")


def _t3_missing_bid_stats_renormalizes():
    project = _base_project()
    del project["bid_stats"]  # entire field gone, like a null from the API
    scored = score_project(
        project, resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert "competition" in scored.excluded_dimensions, "T3: competition must be excluded"
    assert "competition" not in scored.breakdown, "T3: excluded dim must not be scored"
    assert scored.total_score > 0, "T3: missing data must NOT zero-score the project"
    # renormalization: present dims are skill/budget/recency with weights .35/.20/.20
    expected = (
        scored.breakdown["skill_match"]["weighted"]
        + scored.breakdown["budget_fit"]["weighted"]
        + scored.breakdown["recency"]["weighted"]
    ) / (0.35 + 0.20 + 0.20) * 100
    assert abs(scored.total_score - round(expected, 1)) < 0.05, "T3: renormalization math wrong"
    assert "comp n/a" in scored.rationale, f"T3: rationale must disclose n/a: {scored.rationale}"


def _t4_inr_conversion():
    # (a) INR 12500-37500 @ 0.012 -> midpoint 25000 INR = USD 300 -> >= 2x MIN_BUDGET -> 1.0
    project = _base_project(
        budget={"minimum": 12500, "maximum": 37500},
        currency={"code": "INR", "exchange_rate": 0.012},
    )
    scored = score_project(
        project, resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert "budget_fit" in scored.breakdown, "T4: budget_fit must be scored"
    note = scored.breakdown["budget_fit"].get("note", "")
    assert "mid 25000" in note and "USD 300" in note, f"T4: conversion note wrong: {note!r}"
    assert abs(scored.breakdown["budget_fit"]["raw"] - 1.0) < 1e-9, "T4: mid USD 300 >= 2x100 must be raw 1.0"

    # (b) ramp: INR 5000-20000 @ 0.012 -> midpoint 12500 INR = USD 150 -> exactly 0.5
    project_ramp = _base_project(
        budget={"minimum": 5000, "maximum": 20000},
        currency={"code": "INR", "exchange_rate": 0.012},
    )
    scored_ramp = score_project(
        project_ramp, resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    note_ramp = scored_ramp.breakdown["budget_fit"].get("note", "")
    assert "mid 12500" in note_ramp and "USD 150" in note_ramp, f"T4: ramp note wrong: {note_ramp!r}"
    assert abs(scored_ramp.breakdown["budget_fit"]["raw"] - 0.5) < 1e-9, "T4: midpoint ramp math wrong"

    # (c) a missing maximum excludes the dimension instead of guessing from minimum
    project_nomax = _base_project(budget={"minimum": 250, "maximum": None})
    scored_nomax = score_project(
        project_nomax, resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert "budget_fit" in scored_nomax.excluded_dimensions, "T4: missing maximum must exclude budget_fit"
    assert "budget_fit" not in scored_nomax.breakdown


def _t5_bid_count_ordering():
    low = score_project(
        _base_project(), resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    high = score_project(
        _base_project(bid_stats={"bid_count": 200, "bid_avg": 50}),
        resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert low.breakdown["competition"]["raw"] == 1.0, "T5: 0 bids should be raw 1.0"
    assert high.breakdown["competition"]["raw"] == 0.0, "T5: 200 bids should be raw 0.0"
    assert low.total_score > high.total_score, "T5: fewer bids must score higher"


def _t6_stale_project():
    scored = score_project(
        _base_project(submitdate=_TEST_NOW - 5 * 24 * 3600),  # 5 days old
        resume_text=_TEST_RESUME, now=_TEST_NOW,
        min_budget=100, excluded_keywords="crypto,adult",
    )
    assert "recency" in scored.breakdown, "T6: old submitdate is valid data, not missing"
    assert scored.breakdown["recency"]["raw"] == 0.0, "T6: 5-day-old project must score 0 on recency"
    assert "recency" not in scored.excluded_dimensions, "T6: raw 0.0 must not be treated as missing"


def _t7_missing_title_raises():
    project = _base_project()
    del project["title"]
    try:
        score_project(
            project, resume_text=_TEST_RESUME, now=_TEST_NOW,
            min_budget=100, excluded_keywords="crypto,adult",
        )
    except ValueError as exc:
        assert "title" in str(exc), f"T7: error must mention 'title': {exc}"
        return
    raise AssertionError("T7: missing title must raise ValueError, not score silently")


_TESTS = [
    ("T1 perfect project scores >= 90", _t1_perfect_project),
    ("T2 crypto project discarded with reason", _t2_excluded_keyword),
    ("T3 missing bid_stats renormalizes, not zero", _t3_missing_bid_stats_renormalizes),
    ("T4 INR midpoint converts to USD before budget scoring", _t4_inr_conversion),
    ("T5 bid_count 0 outranks bid_count 200", _t5_bid_count_ordering),
    ("T6 5-day-old project scores 0 on recency", _t6_stale_project),
    ("T7 missing title raises clear error", _t7_missing_title_raises),
]


def run_tests() -> int:
    """Run T1-T7; print PASS/FAIL per test and a summary line. 0 iff all pass."""
    passed = 0
    failed = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {name}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001 — a crash is a failure
            print(f"FAIL  {name}: unexpected {type(exc).__name__}: {exc}")
            failed += 1
    print()
    print(f"SUMMARY: {passed}/{len(_TESTS)} passed" + ("" if failed == 0 else f", {failed} FAILED"))
    return 0 if failed == 0 else 1


# =============================================================================
# SECTION 7: Sample project + CLI
# =============================================================================

# Hardcoded sample (id / title / budget / bid_count as seen in live search;
# preview_description and seo_url reconstructed for the demo).
_SAMPLE_ID = 40754444
_SAMPLE_TITLE = "Automated Trading System Development in Python"


def _sample_project(now: float) -> dict:
    return {
        "id": _SAMPLE_ID,
        "title": _SAMPLE_TITLE,
        "seo_url": "Automated-Trading-System-Development-in-Python",
        "preview_description": (
            "Looking for an experienced Python developer to build an automated "
            "trading system. The bot should connect to broker APIs, implement "
            "indicator-based strategies, run backtests and execute orders with "
            "risk controls."
        ),
        "budget": {"minimum": 30, "maximum": 250},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": 81, "bid_avg": 140},
        "submitdate": int(now - 3 * 3600),  # 3h old at run time
        "type": "fixed",
        "language": "en",
        "status": "active",
    }


def _print_scored(scored: ScoredProject, threshold: int, extras: Dict[str, Any]) -> None:
    print("ScoredProject:")
    print(f"  project_id:         {scored.project_id}")
    print(f"  title:              {scored.title}")
    print(f"  url:                {scored.url if scored.url else 'unknown (seo_url is null)'}")
    print(f"  total_score:        {scored.total_score}")
    print(f"  threshold:          {threshold} -> "
          f"{'QUALIFIES' if meets_threshold(scored, threshold) else 'does not qualify'}")
    print("  breakdown:")
    for name in ("skill_match", "budget_fit", "competition", "recency"):
        if name in scored.breakdown:
            e = scored.breakdown[name]
            note = f"   [{e['note']}]" if "note" in e else ""
            print(f"    {name:<12} raw {e['raw']:<7} weighted {e['weighted']}{note}")
    print(f"  excluded_dimensions: {scored.excluded_dimensions or '[]'}"
          + ("  (weights renormalized)" if scored.excluded_dimensions else ""))
    print(f"  discarded:          {scored.discarded}")
    print(f"  reason:             {scored.reason if scored.reason else 'None'}")
    print(f"  rationale:          {scored.rationale}")
    if extras:
        for k, v in extras.items():
            print(f"  {k:<21}{v}")


def run_sample() -> int:
    """Score the hardcoded sample with best-effort real profile data."""
    # Best-effort .env load (override=False): mirrors the other CLIs so a
    # locally-set GITHUB_PAT/portfolio cache can enrich the demo. Tests
    # never reach the network regardless.
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]

        load_dotenv(override=False)
    except ImportError:
        pass

    now = time.time()
    threshold = get_score_threshold()

    resume_text, resume_src = None, "(embedded fallback)"
    portfolio: Optional[dict] = None
    extras: Set[str] = set()
    portfolio_note = "not available"
    try:
        from portfolio_reader import get_portfolio, get_resume

        resume_text = get_resume()
        resume_src = "resume.md"
        try:
            portfolio, _ = get_portfolio()
            extras = portfolio_skill_tokens(portfolio)
            portfolio_note = f"{len(extras)} tokens from {portfolio.get('login', 'unknown')}'s {len(portfolio.get('repos', []))} repos"
        except Exception as exc:  # noqa: BLE001 — portfolio is optional here
            portfolio_note = f"unavailable ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 — resume is optional here
        resume_src = f"unavailable ({type(exc).__name__}), using embedded fallback"
        resume_text = _TEST_RESUME

    scored = score_project(
        _sample_project(now),
        resume_text=resume_text,
        extra_skills=extras,
        now=now,
    )
    _print_scored(
        scored,
        threshold,
        {
            "resume source:": resume_src,
            "portfolio extras:": portfolio_note,
            "skill tokens:": len(extract_skill_tokens(resume_text or "")) + len(extras),
        },
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="project_scorer.py",
        description="FreelancerClaw scoring engine - mock tests and sample scoring.",
    )
    parser.add_argument("--test", action="store_true", help="run mock unit tests T1-T7")
    parser.add_argument("--sample", action="store_true", help="score the hardcoded sample project")
    args = parser.parse_args(argv)

    if args.test:
        return run_tests()
    if args.sample:
        return run_sample()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
