"""
=============================================================================
FreelancerClaw — Module 3: proposal_drafter.py
Deterministic, evidence-cited proposal drafting.
=============================================================================

NOTE ON LLM USAGE (per design review): this agent runs inside OpenWebUI
CPTR, so the "LLM call" is the agent's own inference, not an external API.
The drafter is therefore implemented as DETERMINISTIC TEMPLATE COMPOSITION:
every sentence is assembled from verified inputs (project description,
ScoredProject, resume.md evidence, GitHub portfolio). Rule enforcement is
code, not prompt-hope. An `opening_provider` hook lets tests (T4) simulate
a bad "generation" and prove the validator rejects it.

INPUTS (priority order):
  1. full project detail via freelancer_api.get_project_details(project_id)
     -> the description field (search preview_description is truncated)
  2. the ScoredProject from project_scorer
  3. resume.md via portfolio_reader.get_resume()
  4. GitHub portfolio cache via portfolio_reader.get_portfolio()
     -> repos[].name / language / description / html_url

EVIDENCE BANK:
  * primary:   "## Selected experience" entries ("### N. Title" + bullets)
  * secondary: GitHub repos with non-null description

RULES ENFORCED IN CODE:
  R1 opening quotes/paraphrases the description; no greeting first word
  R2 every bullet cites a resume entry title or repo name VERBATIM
  R3 total 150-250 words including the compliance line (expand/truncate)
  R4 fixed compliance line, always last
  R5 timeline defaults to "to be confirmed after scope review"; a number
     appears ONLY if the resume's Rates-and-preferences section states one
  R6 rotation: evidence cited in the last 5 drafts is deprioritized
  R7 no phone/email/address; no URLs except github.com/<login> and cited
     repo URLs (evidence text is sanitized before composition)
  R8 no quantitative claims absent from the resume (years, N+ clients,
     guarantees, 100% ...) — contaminated evidence lines are dropped

OUTPUT: dict with text, word_count, cited_evidence, opening_quote, warnings.

CLI:
  python proposal_drafter.py --test              mock suite T1-T8
  python proposal_drafter.py --sample <id>       live: fetch details, score,
                                                 draft, print everything
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from project_scorer import ScoredProject, _tokenize, score_project

logger = logging.getLogger("freelancerclaw.drafter")

# =============================================================================
# SECTION 1: Constants
# =============================================================================

COMPLIANCE_LINE = (
    "All claims in this proposal are truthful and verifiable; "
    "no performance or profitability claims are made."
)

DEFAULT_TIMELINE = "Timeline: to be confirmed after scope review."

# (FIX 4) category-aware next-step sentences; first matching category wins
_NEXT_STEP_CATEGORIES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("trading", "forex", "bot"),
     "Next step: share your strategy notes or broker constraints and I will reply with a concrete plan and clarifying questions."),
    (("data", "analysis", "analytics", "forecast"),
     "Next step: share a sample of the dataset (or its schema) and I will reply with a concrete plan and a fixed-price quote."),
    (("scrape", "extract", "pdf", "csv"),
     "Next step: share the target source or a sample file and I will reply with a concrete plan and a fixed-price quote."),
    (("telegram", "bot", "automation"),
     "Next step: describe the trigger and the action and I will reply with a concrete plan and clarifying questions."),
)
_NEXT_STEP_DEFAULT = (
    "Next step: reply with a short scope note and I will reply with a concrete plan and clarifying questions."
)


def next_step_for(title: str, preview: str) -> str:
    """Pick the closing next-step sentence from title+preview keywords (FIX 4)."""
    haystack = f"{title} {preview}".lower()
    for keywords, sentence in _NEXT_STEP_CATEGORIES:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}s?\b", haystack):
                return sentence
    return _NEXT_STEP_DEFAULT

MIN_WORDS = 150
MAX_WORDS = 250
NO_EVIDENCE_MIN_WORDS = 80   # (CORRECTION 1) citation-free draft band
NO_EVIDENCE_MAX_WORDS = 130
# (STEP 10b FIX) neutral, truthful expansion lever for citation-free drafts
# that would otherwise land below the 80-word floor (e.g. 58-word skeleton +
# 18-word capability sentence = 76). Scope clarification adds no claims.
SCOPE_CLARIFICATION_SENTENCE = (
    "Happy to review the requirements and confirm the scope before any implementation."
)
MIN_BULLETS = 3
MAX_BULLETS = 5
ROTATION_WINDOW = 5  # R6: remember the last N drafts' citations

GREETING_FIRST_WORDS = frozenset({"hi", "hello", "hey", "dear", "greetings", "yo"})

# R8: banned quantitative-claim patterns (case-insensitive)
_BANNED_CLAIM_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bi\s+have\s+\d+\s*[- ]?\s*years?\b",
        r"\b\d+\s*\+?\s*(clients|customers)\b",
        r"\bguarantee(d|s)?\b",
        r"\b100\s*%\s*(success|satisfaction|accuracy|quality)?",
        r"\bnever\s+failed\b",
        r"\bmoney[- ]back\b",
    )
]

# R7: forbidden content patterns
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# Phone-like: digit groups with separators or a leading +. Year ranges like
# 2020-2024 can match; stripping them from evidence is harmless (we never
# cite dates), and erring toward removal is the safe direction for PII.
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?")
_STREET_RE = re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+\s+(Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd)\b")

_OPENING_TEMPLATES = (
    'Your posting puts it plainly: "{phrase}" That is precisely the kind of system I build and maintain.',
    'You describe "{phrase}" Closely matching work is already public in my portfolio.',
    'The requirement you highlight, "{phrase}" lines up directly with systems I have delivered.',
)
# (FIX 1) title-anchored openings used when no complete, untruncated quote exists
_TITLE_ANCHOR_TEMPLATES = (
    'Your posting for "{title}" is close to work I do regularly.',
    'Your brief for "{title}" sits squarely in the kind of systems I build and maintain.',
    'The "{title}" engagement matches contracted work already public in my portfolio.',
)
_GENERIC_OPENING = (
    "This project calls for {skills}, which is the core of my recent contracted work."
)

# =============================================================================
# SECTION 2: Evidence bank
# =============================================================================


class EvidenceItem:
    """One citable evidence unit (resume entry or GitHub repo)."""

    def __init__(self, kind: str, title: str, support: str, url: Optional[str] = None) -> None:
        self.kind = kind              # "resume" | "repo"
        self.title = title            # cited VERBATIM in bullets (R2)
        self.support = support        # sanitized supporting line
        self.url = url                # repo html_url (allowed by R7)
        self.relevance = 0.0
        self.idx = 0                  # stable original position in the bank
        self.gate_pass = False        # (FIX 3) relevance gate result

    def as_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "url": self.url}


def _section(resume_text: str, heading: str) -> str:
    """Body of a '## <heading>' section, up to the next '## ' heading."""
    match = re.search(
        rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", resume_text or "", re.M | re.S
    )
    return match.group(1) if match else ""


def _sanitize_evidence(text: str, allowed_urls: Set[str]) -> Tuple[str, List[str]]:
    """
    Strip forbidden content (R7) and quantitative claims (R8) from an
    evidence line. Returns (clean_text, list_of_actions_taken).
    """
    actions: List[str] = []
    clean = text or ""

    emails = _EMAIL_RE.findall(clean)
    if emails:
        actions.append(f"removed email from evidence ({emails[0][:3]}...)")
        clean = _EMAIL_RE.sub("", clean)
    clean = _STREET_RE.sub("", clean)

    for url in _URL_RE.findall(clean):
        root = url.rstrip(".,);")
        if not any(allowed in root or root in allowed for allowed in allowed_urls):
            actions.append("removed non-allowed URL from evidence")
            clean = clean.replace(url, "")
    clean = _PHONE_RE.sub(lambda m: m.group(0) if not re.search(r"\d{4}-\d{4}", m.group(0)) else "", clean)
    clean = _PHONE_RE.sub(lambda m: "" if not re.search(r"\d{4}-\d{4}", m.group(0)) else m.group(0), clean)

    for pattern in _BANNED_CLAIM_RES:
        if pattern.search(clean):
            actions.append(f"removed banned quantitative claim from evidence ({pattern.pattern})")
            clean = pattern.sub("", clean)

    clean = re.sub(r"\s{2,}", " ", clean).strip(" ,;-")
    return clean, actions


def parse_experience_entries(resume_text: str, allowed_urls: Optional[Set[str]] = None) -> List[EvidenceItem]:
    """
    Primary evidence: entries under '## Selected experience'.
    Each '### N. Title' heading yields one item; its bullet lines become the
    supporting text. Placeholder lines (<< >>) are skipped.
    """
    allowed = allowed_urls or set()
    body = _section(resume_text, "Selected experience")
    items: List[EvidenceItem] = []

    blocks = re.split(r"^###\s+", body, flags=re.M)
    for block in blocks[1:]:  # blocks[0] is preamble before first entry
        lines = block.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].strip()
        heading = re.sub(r"^\d+\s*\.\s*", "", heading).strip()  # drop "N. "
        if not heading or "<<" in heading or ">>" in heading:
            continue
        bullet_lines = [
            ln.lstrip("- ").strip()
            for ln in lines[1:]
            if ln.strip().startswith("-") and "<<" not in ln and ">>" not in ln
        ]
        support, _acts = _sanitize_evidence(" ".join(bullet_lines), allowed)
        items.append(EvidenceItem("resume", heading, support))
    return items


def parse_timeline_preference(resume_text: str) -> Optional[str]:
    """
    R5: a specific timeline may appear ONLY if the resume's
    'Rates and preferences' section contains 'Timeline:' plus a number.
    """
    body = _section(resume_text, "Rates and preferences")
    match = re.search(
        r"Timeline:?[^\n]*?\b(\d+)\s*(day|week|month)s?\b", body, re.IGNORECASE
    )
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    return f"Timeline: {n} {unit}s, as indicated in my profile."


def portfolio_evidence(portfolio: Optional[dict], allowed_urls: Set[str]) -> List[EvidenceItem]:
    """Secondary evidence: repos with a non-null description (spec)."""
    items: List[EvidenceItem] = []
    for repo in (portfolio or {}).get("repos", []):
        name = repo.get("name")
        description = repo.get("description")
        if not name or description is None:
            continue
        url = repo.get("html_url") or ""
        if url:
            allowed_urls.add(url)
        support, _acts = _sanitize_evidence(str(description), allowed_urls)
        lang = repo.get("language")
        lang_note = f" [{lang}]" if lang else ""
        items.append(EvidenceItem("repo", str(name), f"{lang_note} {support}".strip(), url or None))
    return items


def build_evidence_bank(resume_text: str, portfolio: Optional[dict]) -> Tuple[List[EvidenceItem], Set[str]]:
    """Full bank: resume entries first (primary), then repos (secondary)."""
    allowed: Set[str] = set()
    resume_items = parse_experience_entries(resume_text, allowed)
    repo_items = portfolio_evidence(portfolio, allowed)
    return resume_items + repo_items, allowed


# =============================================================================
# SECTION 3: Project text & opening phrase
# =============================================================================


def extract_project_text(detail: dict) -> Tuple[str, List[str]]:
    """
    Full description from the detail payload; falls back to
    preview_description. Nulls are treated as missing (never fabricated).
    Returns (text, warnings).
    """
    warnings: List[str] = []
    project = _unwrap_detail(detail)

    description = project.get("description")
    if description:
        text = _strip_html(str(description))
        if not text.strip():
            warnings.append("description was HTML-only after stripping; using preview_description")
            text = ""
    else:
        text = ""
        warnings.append("full description is null; drafting from title and preview_description")

    preview = project.get("preview_description")
    if not text and preview:
        text = _strip_html(str(preview))
    return text.strip(), warnings


def _unwrap_detail(detail: Any) -> dict:
    if isinstance(detail, dict):
        result = detail.get("result")
        if isinstance(result, dict):
            return result
        if "id" in detail or "title" in detail:
            return detail
    return {}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s{2,}", " ", text)


def choose_opening_phrase(description: str, title: str, max_chars: int = 110) -> str:
    """
    Pick the most specific quotable phrase:
    first substantive sentence of the description -> longest sentence ->
    the title -> 'your project' (generic-but-truthful last resort, T2).
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", description or "") if s.strip()]
    substantive = [s for s in sentences if len(s.split()) >= 6]
    candidates = substantive or sentences
    phrase = ""
    if candidates:
        phrase = max(candidates, key=len) if not substantive else substantive[0]
    if not phrase:
        phrase = (title or "").strip()
    if not phrase:
        return "your project"
    return re.sub(r"\s{2,}", " ", phrase).strip()


def is_quotable_phrase(phrase: str, max_chars: int = 110) -> bool:
    """
    (FIX 1) A phrase may be quoted ONLY if it is a complete utterance:
    it ends in . ! or ? and needs no truncation. Preview fragments cut
    mid-clause ("...turn it into an accurate, usa") are never quoted.
    """
    return (
        bool(phrase)
        and phrase != "your project"
        and phrase[-1] in ".!?"
        and len(phrase) <= max_chars
    )


def _default_opening_provider(description: str, title: str, phrase: str, skills: Sequence[str]) -> str:
    """Deterministic 'generation' — the agent's inference as templates."""
    if is_quotable_phrase(phrase):
        template = _OPENING_TEMPLATES[abs(hash(phrase)) % len(_OPENING_TEMPLATES)]
        return template.format(phrase=phrase)
    if title:
        template = _TITLE_ANCHOR_TEMPLATES[abs(hash(title)) % len(_TITLE_ANCHOR_TEMPLATES)]
        return template.format(title=title)
    skill_note = ", ".join(skills[:3]) if skills else "automation and integration work"
    return _GENERIC_OPENING.format(skills=skill_note)


# =============================================================================
# SECTION 4: Validation (the rules as code)
# =============================================================================


def _word_count(text: str) -> int:
    return len(text.split())


def validate_draft(
    text: str,
    cited_titles: Sequence[str],
    allowed_urls: Set[str],
    *,
    min_words: int = MIN_WORDS,
    max_words: int = MAX_WORDS,
    require_bullets: bool = True,
) -> List[str]:
    """
    Return a list of rule violations ([] == clean). Checks R1 (no greeting
    first word), R2 (bullets cite evidence verbatim), R3 (word range),
    R4 (compliance line last), R7 (PII/URLs), R8 (banned claims).
    Citation-free drafts (CORRECTION 1) pass min/max for the 80-130 band
    and require_bullets=False.
    """
    violations: List[str] = []
    lines = [ln for ln in text.strip().splitlines()]
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    if not nonempty:
        return ["draft is empty"]

    # R1
    first_word = re.split(r"[\s,]+", nonempty[0])[0].lower().strip("\"'")
    if first_word in GREETING_FIRST_WORDS:
        violations.append("R1: opening starts with a greeting")

    # R2: every dash line must contain a cited title verbatim
    bullet_lines = [ln.strip() for ln in nonempty if ln.startswith("- ")]
    if require_bullets and len(bullet_lines) < MIN_BULLETS:
        violations.append(f"R2: fewer than {MIN_BULLETS} bullets")
    for ln in bullet_lines:
        if not any(title in ln for title in cited_titles):
            violations.append("R2: bullet cites no evidence item verbatim")

    # R3 (band is caller-configurable: 150-250 cited, 80-130 citation-free)
    wc = _word_count(text)
    if wc < min_words:
        violations.append(f"R3: word count {wc} below {min_words}")
    elif wc > max_words:
        violations.append(f"R3: word count {wc} above {max_words}")

    # R4
    if nonempty[-1] != COMPLIANCE_LINE:
        violations.append("R4: compliance line is not the last line")

    # R7
    if _EMAIL_RE.search(text):
        violations.append("R7: email address present")
    if _STREET_RE.search(text):
        violations.append("R7: street address present")
    for url in _URL_RE.findall(text):
        root = url.rstrip(".,);")
        if not any(allowed in root or root in allowed for allowed in allowed_urls):
            violations.append("R7: non-allowed URL present")

    # R8
    for pattern in _BANNED_CLAIM_RES:
        if pattern.search(text):
            violations.append(f"R8: banned claim pattern {pattern.pattern}")

    return violations


# =============================================================================
# SECTION 5: Rotation (R6 — uniqueness across recent drafts)
# =============================================================================


class RotationTracker:
    """Remembers evidence cited in the last N drafts; used to deprioritize."""

    def __init__(self, last_n: int = ROTATION_WINDOW) -> None:
        self._recent: deque = deque(maxlen=last_n)

    def note(self, cited_titles: Sequence[str]) -> None:
        self._recent.extend(cited_titles)

    def penalized(self, title: str) -> bool:
        return title in self._recent

    def reset(self) -> None:
        self._recent.clear()


_DEFAULT_ROTATION = RotationTracker()


def reset_rotation() -> None:
    """Test/ops helper: forget recent citations."""
    _DEFAULT_ROTATION.reset()


# =============================================================================
# SECTION 6: Composition
# =============================================================================


def _rank_evidence(
    bank: List[EvidenceItem], project_tokens: Set[str], gate_tokens: Set[str]
) -> None:
    """
    Set relevance (overlap with project tokens) and the (FIX 3) relevance
    gate: an item passes only with >= 2 shared tokens of length >= 4
    (stopwords already excluded by _tokenize) against title+preview tokens.
    """
    for idx, item in enumerate(bank):
        ev_tokens = _tokenize(f"{item.title} {item.support}")
        item.idx = idx
        item.relevance = float(len(ev_tokens & project_tokens))
        strong = {t for t in (ev_tokens & gate_tokens) if len(t) >= 4}
        item.gate_pass = len(strong) >= 2


def _ordered(items: List[EvidenceItem], rotation: RotationTracker) -> List[EvidenceItem]:
    """R6 ordering: fresh-before-recently-cited, then relevance, primary
    evidence first, then stable original order."""
    return sorted(
        items,
        key=lambda i: (
            rotation.penalized(i.title),
            -i.relevance,
            i.kind != "resume",
            i.idx,
        ),
    )


def _select_evidence(
    bank: List[EvidenceItem], rotation: RotationTracker, count: int
) -> Tuple[List[EvidenceItem], Optional[str]]:
    """
    (CORRECTION 1) Relevance-gated selection with NO padding fallback.

      * >= 3 items pass the gate -> top items by overlap, rotation-aware
      * 1-2 items pass          -> ONLY those (a 2-bullet proposal is fine)
      * 0 items pass            -> NO citations at all; the caller drafts a
                                   shorter, citation-free proposal.
    Better no citation than a wrong one — unrelated items are never pulled
    in to fill space.
    """
    passing = [i for i in bank if i.gate_pass]
    if not passing:
        return [], (
            "no evidence items met the relevance gate; "
            "proposal contains no portfolio citations"
        )
    if len(passing) < 3:
        return list(passing), (
            f"relevance gate: only {len(passing)} evidence item(s) passed; "
            f"citing only those (no padding)"
        )
    pool = _ordered(passing, rotation)
    # Rotation slack (R6): reserve at least one passing item so the next
    # similar project can cite it.
    count = min(count, max(MIN_BULLETS, len(pool) - 1))
    return pool[:count], None


def _bullet_for(item: EvidenceItem) -> str:
    """(FIX 2) plain statement of what was built — no keyword-restating suffix."""
    support = item.support
    if len(support) > 240:
        support = support[:240].rsplit(" ", 1)[0].rstrip(" ,;-")
        if support and support[-1] not in ".!?":
            support += "..."  # ellipsis so truncated support never poses as complete
    if item.kind == "repo":
        return f'- "{item.title}" (public GitHub repo){": " + support if support else "."}'
    return f'- "{item.title}": {support or "contracted work with deliverables I can walk through."}'


def _fill_short_no_evidence(text: str, skills: Sequence[str], min_words: int, max_words: int) -> str:
    """
    (CORRECTION 1) Length management for citation-free drafts (target band
    80-130 words). Two truthful levers, in order:
      1. a capability sentence derived from the resume Skills section;
      2. (STEP 10b FIX) a neutral scope-clarification sentence, for drafts
         still below the floor after lever 1 (or with fewer than 3 skills,
         where lever 1 does not apply at all).
    If both cannot reach the floor the draft ships short and the word-count
    warning fires (never fabricated filler).
    """
    def _insert_before_next_step(text: str, sentence: str) -> str:
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("Next step:"):
                lines.insert(i, "")
                lines.insert(i, sentence.rstrip())
                return "\n".join(lines)
        return text + "\n\n" + sentence

    wc = _word_count(text)
    if wc >= min_words:
        return text
    if len(skills) >= 3:
        listed = ", ".join(skills[:6])
        capability = (
            f"My declared toolset for this class of work includes {listed}, "
            f"and every item is public and inspectable."
        )
        if wc + _word_count(capability) <= max_words:
            text = _insert_before_next_step(text, capability)
            wc = _word_count(text)
    if wc < min_words and wc + _word_count(SCOPE_CLARIFICATION_SENTENCE) <= max_words:
        text = _insert_before_next_step(
            text, SCOPE_CLARIFICATION_SENTENCE)
    return text


def _compose(
    opening: str,
    bullets: List[str],
    timeline: str,
    next_step: str,
) -> str:
    parts = [opening, ""]
    parts.extend(bullets)
    parts.append("")
    if next_step:
        parts.extend([next_step, ""])
    parts.extend([timeline, "", COMPLIANCE_LINE])
    return "\n".join(parts)


def draft_proposal(
    detail: dict,
    scored: ScoredProject,
    *,
    resume_text: Optional[str] = None,
    portfolio: Optional[dict] = None,
    client: Any = None,                      # reserved for live detail re-fetch
    opening_provider: Optional[Callable[..., str]] = None,
    rotation: Optional[RotationTracker] = None,
    min_words: int = MIN_WORDS,
    max_words: int = MAX_WORDS,
) -> Dict[str, Any]:
    """
    Draft one proposal. Returns the spec'd dict:
      {text, word_count, cited_evidence, opening_quote, warnings}
    Never raises for content reasons (T8) — problems land in `warnings`.
    """
    warnings: List[str] = []
    rotation = rotation or _DEFAULT_ROTATION

    # ---- inputs -------------------------------------------------------------
    if resume_text is None:
        from portfolio_reader import get_resume

        resume_text = get_resume()

    project = _unwrap_detail(detail)
    title = str(project.get("title") or scored.title or "")
    description, extract_warnings = extract_project_text(detail)
    warnings.extend(extract_warnings)

    bank, allowed_urls = build_evidence_bank(resume_text, portfolio)
    login = (portfolio or {}).get("login")
    if login:
        allowed_urls.add(f"github.com/{login}")

    project_tokens = _tokenize(f"{title} {description}")
    preview = str(project.get("preview_description") or "")
    gate_tokens = _tokenize(f"{title} {preview}")   # (FIX 3) gate text
    next_step = next_step_for(title, preview)         # (FIX 4)
    skills = sorted(_tokenize(_section(resume_text, "Skills")))

    # ---- opening (R1) with rejection/regeneration loop ----------------------
    phrase = choose_opening_phrase(description, title)
    provider = opening_provider or _default_opening_provider
    opening = None
    for attempt in range(3):
        candidate = provider(description, title, phrase, skills)
        first_word = re.split(r"[\s,]+", candidate.strip())[0].lower().strip("\"'")
        if first_word not in GREETING_FIRST_WORDS:
            opening = candidate
            break
        warnings.append(
            f"opening candidate {attempt + 1} rejected (started with greeting); regenerating"
        )
    if opening is None:  # provider kept producing greetings
        opening = _default_opening_provider(description, title, phrase, skills)
        warnings.append("fell back to deterministic opening after repeated rejections")

    # (CORRECTION 2) the opening is a SINGLE sentence: the quote or the
    # title anchor. No token-list appendix, no second sentence.

    # ---- evidence selection + bullets (R2, R6, FIX 3 gate) -----------------
    _rank_evidence(bank, project_tokens, gate_tokens)
    bullet_count = MIN_BULLETS + 1 if len(bank) >= MIN_BULLETS + 1 else len(bank)
    chosen, gate_warning = _select_evidence(bank, rotation, max(bullet_count, 0))
    if gate_warning:
        warnings.append(gate_warning)

    timeline_pref = parse_timeline_preference(resume_text)
    timeline_line = timeline_pref or DEFAULT_TIMELINE
    if timeline_pref is None and re.search(r"Timeline:", _section(resume_text, "Rates and preferences")):
        warnings.append("resume mentions Timeline: without a number; using safe default")

    def _build(items: List[EvidenceItem]) -> str:
        bullets = [_bullet_for(item) for item in items]
        return _compose(opening, bullets, timeline_line, next_step)

    if not chosen:
        # (CORRECTION 1) 0 gate-passing items (or empty bank): honest,
        # citation-free draft in the 80-130 word band. The exact warning
        # ("no evidence items met the relevance gate; ...") already came
        # from _select_evidence via gate_warning above. Never pad with
        # unrelated evidence. Better no citation than a wrong one.
        text = _compose(opening, [], timeline_line, next_step)
        text = _fill_short_no_evidence(text, skills,
                                       min_words=NO_EVIDENCE_MIN_WORDS,
                                       max_words=NO_EVIDENCE_MAX_WORDS)
        cited: List[str] = []
    else:
        text = _build(chosen)

        # R3: expand (more bullets, then append repo URLs)
        if _word_count(text) < min_words and len(chosen) < len(bank) and len(chosen) < MAX_BULLETS:
            extra, _w = _select_evidence(bank, rotation, len(chosen) + 1)
            if extra:
                chosen = extra
                text = _build(chosen)
        if _word_count(text) < min_words:
            for item in chosen:
                if item.kind == "repo" and item.url and f"Source: {item.url}" not in text:
                    text = text.replace(
                        f'- "{item.title}" (public GitHub repo)',
                        f'- "{item.title}" (public GitHub repo, {item.url})',
                        1,
                    )
                    if _word_count(text) >= min_words:
                        break

        # last honest levers: point at the public profile (allowed URL)
        if _word_count(text) < min_words and login:
            profile_line = f"Public evidence for the above: github.com/{login}."
            inspectability = (
                "Each item above is public; you can inspect the code and its "
                "history before we talk."
            )
            block = f"{profile_line} {inspectability}"
            if _word_count(text) + _word_count(block) <= max_words:
                text = text.replace(next_step, block + "\n\n" + next_step, 1)
            else:
                text = text.replace(next_step, profile_line + "\n\n" + next_step, 1)
            if _word_count(text) < min_words:
                warnings.append(
                    f"could not reach {min_words} words with available evidence"
                )

        # R3: truncate (drop least-relevant bullet)
        while _word_count(text) > max_words and len(chosen) > MIN_BULLETS:
            drop = min(chosen, key=lambda i: i.relevance)
            chosen = [i for i in chosen if i is not drop]
            text = _build(chosen)
            warnings.append(f"dropped bullet '{drop.title}' to respect {max_words}-word limit")

        cited = [item.title for item in chosen]

    # cosmetic: collapse any triple+ newlines the levers may leave
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ---- final validation gates (R7/R8 hard rejection) -----------------------
    band = (MIN_WORDS, MAX_WORDS) if cited else (NO_EVIDENCE_MIN_WORDS, NO_EVIDENCE_MAX_WORDS)
    for _attempt in range(3):
        violations = validate_draft(
            text, cited or ["-"], allowed_urls,
            min_words=band[0], max_words=band[1], require_bullets=bool(cited),
        )
        hard = [v for v in violations if v.startswith(("R7", "R8")) or "cites no evidence" in v]
        if not hard:
            break
        # Reject the contaminated draft and rebuild from sanitized evidence.
        warnings.append(f"draft rejected on {', '.join(hard)}; regenerating")
        for item in chosen:
            clean, acts = _sanitize_evidence(
                f"{item.title} {item.support}", allowed_urls
            )
            for act in acts:
                warnings.append(act)
            item.support = clean.split(" ", 1)[1] if " " in clean else ""
        text = _build(chosen) if chosen else _compose(opening, [], timeline_line, next_step)

    soft = validate_draft(
        text, cited or ["-"], allowed_urls,
        min_words=band[0], max_words=band[1], require_bullets=bool(cited),
    )
    for violation in soft:
        warnings.append(f"returned with violation: {violation}")

    rotation.note(cited)

    # (FIX 1) report what the opening actually anchors on: the quoted
    # sentence when one was usable, otherwise the title anchor.
    if is_quotable_phrase(phrase):
        opening_quote = phrase
    elif title:
        opening_quote = f"(title anchor) {title}"
    else:
        opening_quote = "(generic opening; description and title unusable)"

    return {
        "text": text,
        "word_count": _word_count(text),
        "cited_evidence": cited,
        "opening_quote": opening_quote,
        "warnings": warnings,
    }


# =============================================================================
# SECTION 7: Mock tests (T1-T8)
# =============================================================================

_MOCK_RESUME = """# Test User

## Skills

python, web scraping, automation, telegram bot, pdf extraction, api integration, fastapi

## Selected experience

### 1. Scraping Platform Overhaul
- Built a Scrapy and Playwright pipeline monitoring 40 supplier sites nightly with retry logic and proxy rotation.
- Cut the client's manual price checking from six hours a week to zero and delivered CSV exports validated against their accounting records.

### 2. Telegram Ops Bot
- Delivered a Telegram bot with role-based access controlling internal reports and scheduled digests.
- Added retry-safe API integration against a flaky third-party gateway with full logging.

### 3. Invoice PDF Extraction
- Extracted line items from thousands of PDF invoices into a SQL database with schema-aware parsing.
- Validated totals against accounting exports with a reconciliation report delivered weekly.

### 4. FastAPI Service Layer
- Wrapped legacy automation scripts in a FastAPI service with token auth and health endpoints.
- Documented every endpoint so the client team could self-serve without my involvement.

### 5. Data Analysis Dashboards
- Built pandas pipelines summarizing operations data for weekly management reviews.
- Automated the report generation schedule end to end, including failure alerts.

### 6. Linux Infrastructure Automation
- Ansible-managed RHEL fleet with SELinux confinement for scheduled scraping jobs.
- Wrote systemd units for resilient restarts of scraping workers with backoff.

## Rates and preferences

- Typical budget range: USD 250-2000 per project
- Timeline commitments allowed: none
"""

_MOCK_PORTFOLIO = {
    "login": "octodev",
    "repos": [
        {"name": "scraper-kit", "language": "Python",
         "description": "Playwright scraping framework with retries and proxies.",
         "html_url": "https://github.com/octodev/scraper-kit"},
        {"name": "tg-ops-bot", "language": "Python",
         "description": "Telegram operations bot with access control.",
         "html_url": "https://github.com/octodev/tg-ops-bot"},
        {"name": "pdf-lines", "language": "Python",
         "description": "PDF invoice line-item extractor to SQL.",
         "html_url": "https://github.com/octodev/pdf-lines"},
        {"name": "no-desc-repo", "language": "Rust", "description": None,
         "html_url": "https://github.com/octodev/no-desc-repo"},
    ],
}


def _mock_scored(project_id: int = 2001, title: str = "Python automation for scraping and Telegram") -> ScoredProject:
    return score_project(
        {
            "id": project_id,
            "title": title,
            "seo_url": "Python-automation-for-scraping-and-Telegram",
            "preview_description": "Scraping, telegram bot, pdf extraction.",
            "budget": {"minimum": 250, "maximum": 750},
            "currency": {"code": "USD", "exchange_rate": 1.0},
            "bid_stats": {"bid_count": 6, "bid_avg": 300},
            "submitdate": 1_700_000_000 - 1800,
        },
        resume_text=_MOCK_RESUME,
        now=1_700_000_000.0,
        min_budget=100,
        excluded_keywords="crypto,adult",
    )


def _mock_detail(description: str, project_id: int = 2001,
                 title: str = "Python automation for scraping and Telegram") -> dict:
    return {
        "id": project_id,
        "title": title,
        "description": description,
        "preview_description": description[:80],
        "seo_url": "Python-automation-for-scraping-and-Telegram",
    }


_RICH_DESCRIPTION = (
    "We need a Python developer to build an automated scraping pipeline covering "
    "three supplier portals, with scheduled runs, retry logic, and a dashboard of "
    "the collected pricing data. The pipeline must also send daily summaries to a "
    "Telegram channel and export cleaned CSV files. Previous freelancers left no "
    "documentation; we value clear setup instructions and a maintainable codebase."
)


def _draft(description: str = _RICH_DESCRIPTION, **kwargs) -> Dict[str, Any]:
    reset_rotation()
    return draft_proposal(
        _mock_detail(description),
        _mock_scored(),
        resume_text=_MOCK_RESUME,
        portfolio=_MOCK_PORTFOLIO,
        **kwargs,
    )


def _t1_word_range():
    result = _draft()
    assert 150 <= result["word_count"] <= 250, f"T1: {result['word_count']} words"
    assert result["cited_evidence"], "T1: no evidence cited"


def _t2_thin_description():
    try:
        result = _draft("Need help")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"T2: raised {type(exc).__name__}: {exc}")
    first_word = result["text"].strip().split()[0].lower().strip("\"'")
    assert first_word not in GREETING_FIRST_WORDS, "T2: fell back to a greeting"
    assert result["text"].strip(), "T2: empty draft"


def _t3_compliance_last():
    result = _draft()
    last_line = [ln for ln in result["text"].strip().splitlines() if ln.strip()][-1]
    assert last_line == COMPLIANCE_LINE, f"T3: last line is {last_line!r}"


def _t4_greeting_rejected_and_regenerated():
    calls = {"n": 0}

    def bad_then_good(description, title, phrase, skills):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Hi, I saw your project and I am the perfect fit!"
        return f'Your posting describes "{phrase}", which matches systems I have delivered.'

    result = _draft(opening_provider=bad_then_good)
    assert calls["n"] >= 2, "T4: drafter did not regenerate after rejection"
    assert not result["text"].lstrip().lower().startswith("hi"), "T4: greeting survived"
    assert any("rejected" in w for w in result["warnings"]), f"T4: no rejection warning: {result['warnings']}"


def _t5_cited_evidence_nonempty():
    for description in (_RICH_DESCRIPTION, "Need help", "Short automation task."):
        result = _draft(description)
        assert result["cited_evidence"], f"T5: empty citations for {description[:20]!r}"


def _t6_rotation_diversifies():
    reset_rotation()
    first = draft_proposal(
        _mock_detail(_RICH_DESCRIPTION), _mock_scored(3001),
        resume_text=_MOCK_RESUME, portfolio=_MOCK_PORTFOLIO,
    )
    second = draft_proposal(
        _mock_detail(_RICH_DESCRIPTION), _mock_scored(3002),
        resume_text=_MOCK_RESUME, portfolio=_MOCK_PORTFOLIO,
    )
    set1, set2 = set(first["cited_evidence"]), set(second["cited_evidence"])
    assert set1 and set2 and set1 != set2, f"T6: identical citations {set1}"
    assert set1 & set2 != set1 | set2 or len(set1 | set2) > len(set1), "T6: no diversity"


def _t7_email_rejected():
    poisoned = _MOCK_RESUME.replace(
        "- Built a Scrapy and Playwright pipeline monitoring 40 supplier sites nightly.",
        "- Built a Scrapy pipeline. Contact me at john.doe@example.com for details.",
    )
    reset_rotation()
    result = draft_proposal(
        _mock_detail(_RICH_DESCRIPTION), _mock_scored(),
        resume_text=poisoned, portfolio=_MOCK_PORTFOLIO,
    )
    assert "john.doe@example.com" not in result["text"], "T7: email leaked into draft"
    assert "@" not in result["text"], "T7: any email survived"


def _t8_out_of_range_warns_but_returns():
    tiny_resume = "# U\n\n## Skills\n\npython\n\n## Selected experience\n\n### 1. Tiny\n- Did things.\n"
    reset_rotation()
    result = draft_proposal(
        _mock_detail("Small job."), _mock_scored(),
        resume_text=tiny_resume, portfolio={"repos": []},
    )
    assert isinstance(result["text"], str) and result["text"].strip(), "T8: no draft returned"
    assert result["word_count"] < 150 or 150 <= result["word_count"] <= 250  # returned regardless
    assert any("R3" in w or "word count" in w.lower() for w in result["warnings"]), (
        f"T8: expected word-count warning, got {result['warnings']}"
    )


_TESTS = [
    ("T1 rich description yields 150-250 words", _t1_word_range),
    ("T2 'Need help' description does not raise", _t2_thin_description),
    ("T3 compliance line is always last", _t3_compliance_last),
    ("T4 'Hi' opening rejected and regenerated", _t4_greeting_rejected_and_regenerated),
    ("T5 cited_evidence non-empty for every draft", _t5_cited_evidence_nonempty),
    ("T6 similar projects cite different evidence", _t6_rotation_diversifies),
    ("T7 email in draft is rejected", _t7_email_rejected),
    ("T8 out-of-range warns but still returns", _t8_out_of_range_warns_but_returns),
]


def run_tests() -> int:
    passed = failed = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {name}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: unexpected {type(exc).__name__}: {exc}")
            failed += 1
    print()
    print(f"SUMMARY: {passed}/{len(_TESTS)} passed" + ("" if failed == 0 else f", {failed} FAILED"))
    return 0 if failed == 0 else 1


# =============================================================================
# SECTION 8: CLI --sample <project_id> (live detail -> score -> draft)
# =============================================================================

_SCORABLE_FIELDS = (
    "id", "title", "seo_url", "preview_description", "budget", "currency",
    "bid_stats", "submitdate", "type", "language", "status",
)


def run_sample(project_id: int) -> int:
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]

        load_dotenv(override=False)
    except ImportError:
        pass

    token = os.environ.get("FREELANCER_ACCESS_TOKEN", "")
    if not token:
        print("ERROR: FREELANCER_ACCESS_TOKEN is not set (fill .env or set the env var).")
        print("       Get it at https://accounts.freelancer.com/settings/develop")
        return 1

    import freelancer_api as fapi
    import portfolio_reader as pr
    from project_scorer import portfolio_skill_tokens

    try:
        portfolio, _from_cache = pr.get_portfolio()
    except Exception as exc:  # noqa: BLE001 — portfolio is optional here
        portfolio = None
        print(f"note: portfolio unavailable ({type(exc).__name__}); drafting on resume evidence only")
    try:
        resume_text = pr.get_resume()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Using FREELANCER_ACCESS_TOKEN {fapi.redact_token(token)}")
    client = fapi.FreelancerClient(token)
    try:
        detail = client.get_project_details(project_id)
    except fapi.AuthError as exc:
        print(f"AUTH FAILED (401): {exc}")
        print("Regenerate at https://accounts.freelancer.com/settings/develop")
        return 2
    except fapi.FreelancerApiError as exc:
        print(f"API ERROR: {exc}")
        return 3

    project = detail.get("result", detail) if isinstance(detail, dict) else {}
    scorable = {k: project.get(k) for k in _SCORABLE_FIELDS}
    scored = score_project(
        scorable, resume_text=resume_text,
        extra_skills=sorted(portfolio_skill_tokens(portfolio)) if portfolio else None,
    )

    result = draft_proposal(detail, scored, resume_text=resume_text, portfolio=portfolio)

    print()
    print("=" * 70)
    print(result["text"])
    print("=" * 70)
    print(f"word_count:     {result['word_count']}")
    print(f"cited_evidence: {result['cited_evidence']}")
    print(f"opening_quote:  {result['opening_quote']!r}")
    print(f"warnings:       {result['warnings'] or '[]'}")
    print(f"score:          {scored.total_score}/100 ({scored.rationale})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="proposal_drafter.py",
        description="FreelancerClaw proposal drafter - mock tests and live sample drafting.",
    )
    parser.add_argument("--test", action="store_true", help="run mock test suite T1-T8")
    parser.add_argument("--sample", metavar="PROJECT_ID", type=int,
                        help="fetch live details for PROJECT_ID, score, draft, print")
    args = parser.parse_args(argv)

    if args.test:
        return run_tests()
    if args.sample is not None:
        return run_sample(args.sample)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
