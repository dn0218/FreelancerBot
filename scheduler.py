"""
=============================================================================
FreelancerClaw — Module 7: scheduler.py
End-to-end orchestration: scan -> score -> draft -> notify -> (approve) -> bid
=============================================================================

Two concurrent loops (stdlib asyncio, per the Step 1 dependency decision;
the requests-based modules run in the default ThreadPoolExecutor via
run_in_executor so nothing blocks the event loop):

  Loop A [scan]   every SCAN_INTERVAL_MINUTES (default 30, floor 15)
      keywords -> search -> union by id -> dedupe vs state store ->
      score -> skip (excluded/below-threshold/rate-limit) or
      details -> draft -> Telegram send -> persist "drafted"

  Loop B [poll]   every POLL_INTERVAL_SECONDS (default 15)
      getUpdates -> parse approve/reject ->
        approve & DRAFT_ONLY=false  -> place_bid -> "submitted"/"failed"
        approve & DRAFT_ONLY=true   -> "approved" + manual-copy notice
        reject                      -> "rejected"
      persist new offset to cache/scheduler_state.json (tmp + os.replace)

HARD CONSTRAINTS enforced here
------------------------------
  * place_bid fires ONLY when ALL hold:
      (a) explicit Telegram approval for that action,
      (b) DRAFT_ONLY=false,
      (c) first-scan safety gate: the state store already holds at least
          one prior "drafted" record — a fresh process cannot submit on a
          stale/first approval with an empty store (T2/T3).
  * First freelancer 401: Telegram alert, Loop A stops, Loop B keeps
    running. Manual restart required. Telegram 401: mirror for Loop B.
  * Ctrl+C: both loops stop, final summary, exit 0.
  * State store is written BEFORE the corresponding Telegram notification;
    a Telegram failure never loses a state change (draft records are
    pre-written before send_draft, then amended with message_id).

CLI
---
  python scheduler.py --once          one scan cycle, no poll loop
  python scheduler.py --once --dry    scan but print drafts to stdout
  python scheduler.py                 both loops forever
  python scheduler.py --status        keywords, scan times, counts, offset
  python scheduler.py --reset-offset  clear stored Telegram offset
  python scheduler.py --test          T1-T7 with mocked APIs (no network)
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import bid_state_store as store
import telegram_notifier as tn
from freelancer_api import AuthError, FreelancerApiError, FreelancerClient

# =============================================================================
# SECTION 1: Config
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_PATH = LOG_DIR / "scheduler.log"
STATE_PATH = PROJECT_ROOT / "cache" / "scheduler_state.json"

SCAN_INTERVAL_FLOOR_MIN = 15


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw.strip()))
    except ValueError:
        return default


@dataclass
class SchedulerConfig:
    scan_interval_minutes: int = 30
    poll_interval_seconds: int = 15
    draft_only: bool = True
    search_keywords: List[str] = field(default_factory=list)
    score_threshold: float = 70.0
    min_budget: float = 100.0
    max_bids_per_day: int = 1
    max_bids_per_month: int = 6
    bid_period_days: int = 7
    excluded_keywords: Optional[List[str]] = None   # None -> scorer env default

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        keywords = [
            kw.strip()
            for kw in re.split(r"[,;]", os.environ.get("SEARCH_KEYWORDS", ""))
            if kw.strip()
        ]
        excluded_raw = os.environ.get("EXCLUDED_KEYWORDS", "").strip()
        return cls(
            scan_interval_minutes=max(SCAN_INTERVAL_FLOOR_MIN,
                                      _env_int("SCAN_INTERVAL_MINUTES", 30)),
            poll_interval_seconds=max(1, _env_int("POLL_INTERVAL_SECONDS", 15)),
            draft_only=_env_bool("DRAFT_ONLY", True),
            search_keywords=keywords,
            score_threshold=float(_env_int("SCORE_THRESHOLD", 70)),
            min_budget=float(_env_int("MIN_BUDGET", 100)),
            max_bids_per_day=_env_int("MAX_BIDS_PER_DAY", 1),
            max_bids_per_month=_env_int("MAX_BIDS_PER_MONTH", 6),
            bid_period_days=_env_int("BID_PERIOD_DAYS", 7),
            excluded_keywords=([k.strip() for k in re.split(r"[,;]", excluded_raw)
                                if k.strip()] or None),
        )


# =============================================================================
# SECTION 2: Logging ([scan] / [poll] / [submit] / [error] tags)
# =============================================================================


class _TagAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"{self.extra['tag']} {msg}", kwargs


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # Re-arm token scrubbing if a notifier was already built: the handlers
    # above are brand-new (and bare) after root.handlers.clear(), so any
    # previously installed _TokenScrubFilter is gone. Root runs at DEBUG,
    # and urllib3's connection pool logs the FULL Telegram request path
    # (which embeds the bot token) at that level.
    token = os.environ.get(tn.ENV_TOKEN, "").strip()
    if token:
        tn.install_token_scrub(token)


# =============================================================================
# SECTION 3: Persistent scheduler state (cache/scheduler_state.json)
# =============================================================================


class SchedulerState:
    """Persistent state: scan ts, telegram offset, loop statuses, owner pid.
    Atomic writes (tmp + os.replace); tolerant loads (never crashes)."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self.path = Path(state_path) if state_path is not None else STATE_PATH
        self.last_scan_ts: Optional[str] = None
        self.telegram_offset: Optional[int] = None
        self.process_start_ts: Optional[str] = None
        self.loop_a_status: Optional[str] = None   # "running" | "stopped" | None
        self.loop_b_status: Optional[str] = None
        self.owner_pid: Optional[int] = None       # pid owning the "running" claim
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logging.getLogger("freelancerclaw.state").warning(
                "[error] scheduler_state.json unreadable (%s); starting fresh", exc)
            return
        self.last_scan_ts = data.get("last_scan_ts")
        offset = data.get("telegram_offset")
        self.telegram_offset = offset if isinstance(offset, int) else None
        self.process_start_ts = data.get("process_start_ts")
        for attr in ("loop_a_status", "loop_b_status"):
            value = data.get(attr)
            setattr(self, attr, value if value in ("running", "stopped") else None)
        pid = data.get("owner_pid")
        self.owner_pid = pid if isinstance(pid, int) else None

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            payload = {
                "last_scan_ts": self.last_scan_ts,
                "telegram_offset": self.telegram_offset,
                "process_start_ts": self.process_start_ts,
                "loop_a_status": self.loop_a_status,
                "loop_b_status": self.loop_b_status,
                "owner_pid": self.owner_pid,
            }
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=True, indent=1)
            os.replace(tmp, self.path)  # atomic on Windows and POSIX
        except OSError as exc:
            logging.getLogger("freelancerclaw.state").warning(
                "[error] could not persist scheduler state: %s", exc)

    def reset_offset(self) -> None:
        self.telegram_offset = None
        self.save()


# =============================================================================
# SECTION 4: The scheduler
# =============================================================================


class Scheduler:
    """
    Orchestrates every module. All side-effectful collaborators are
    injectable so the T1-T7 suite runs fully offline.
    """

    def __init__(
        self,
        config: Optional[SchedulerConfig] = None,
        *,
        freelancer: Any = None,
        notifier: Any = None,
        state: Optional[SchedulerState] = None,
        state_path: Optional[Path] = None,
        resume_text: Optional[str] = None,
        portfolio: Optional[dict] = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config or SchedulerConfig.from_env()
        self.dry_run = dry_run
        self.state = state or SchedulerState(state_path or STATE_PATH)
        self.freelancer = freelancer
        self.notifier = notifier
        self._resume_text = resume_text
        self._portfolio = portfolio
        self._extra_skills: Optional[List[str]] = None

        # loop control
        self.scan_stopped = False       # 401 from freelancer stops Loop A
        self.poll_stopped = False       # 401 from telegram stops Loop B
        self.shutdown_event: Optional[asyncio.Event] = None

        # in-memory registry: project_id -> draft dict (for place_bid text)
        self._drafts: Dict[int, dict] = {}

        # lifetime counters for the final summary line
        self.stats = {"scans": 0, "drafted": 0, "skipped": 0,
                      "submitted": 0, "approved": 0, "rejected": 0, "failed": 0}

        self.last_results: List[dict] = []   # outcomes of the latest poll cycle

        self.log_scan = _TagAdapter(logging.getLogger("freelancerclaw.scan"), {"tag": "[scan]"})
        self.log_poll = _TagAdapter(logging.getLogger("freelancerclaw.poll"), {"tag": "[poll]"})
        self.log_submit = _TagAdapter(logging.getLogger("freelancerclaw.submit"), {"tag": "[submit]"})
        self.log_error = _TagAdapter(logging.getLogger("freelancerclaw.error"), {"tag": "[error]"})

    # ------------------------------------------------------------ collaborators

    def _get_freelancer(self) -> Any:
        if self.freelancer is None:
            self.freelancer = FreelancerClient()
        return self.freelancer

    def _get_notifier(self) -> Any:
        if self.notifier is None:
            token = os.environ.get(tn.ENV_TOKEN, "").strip()
            chat = os.environ.get(tn.ENV_CHAT, "").strip()
            # Scrub the bot token from EVERY log record, whoever emitted it
            # (including urllib3's DEBUG request lines, which contain the
            # token in the URL path). Must happen before the first request.
            tn.install_token_scrub(token)
            self.notifier = tn.TelegramNotifier(token, chat)
        return self.notifier

    def _get_resume(self) -> str:
        if self._resume_text is None:
            import portfolio_reader as pr

            self._resume_text = pr.get_resume()
        return self._resume_text

    def _get_portfolio(self) -> Optional[dict]:
        if self._portfolio is None and self._portfolio != {}:
            try:
                import portfolio_reader as pr

                self._portfolio, _from_cache = pr.get_portfolio()
            except Exception as exc:  # noqa: BLE001 — portfolio is optional
                self.log_error.warning("portfolio unavailable (%s); resume evidence only", exc)
                self._portfolio = {}
        return self._portfolio or None

    def _get_extra_skills(self) -> Optional[List[str]]:
        if self._extra_skills is None:
            try:
                from project_scorer import portfolio_skill_tokens

                portfolio = self._get_portfolio()
                self._extra_skills = sorted(portfolio_skill_tokens(portfolio)) or None
            except Exception:  # noqa: BLE001
                self._extra_skills = None
        return self._extra_skills

    def _telegram_note(self, text: str) -> None:
        """Best-effort Telegram alert that never breaks the caller."""
        try:
            self._get_notifier().send_status(0, "alert", note=text)
        except Exception as exc:  # noqa: BLE001
            self.log_error.warning("telegram alert failed: %s", exc)

    # ------------------------------------------------------------------ Loop A

    def run_scan_cycle(self) -> str:
        """
        One Loop A iteration. Returns a one-line summary. Raises AuthError
        upward ONLY after having stopped Loop A and sent the Telegram alert.
        """
        if self.scan_stopped:
            return "scan: loop stopped (freelancer 401); manual restart required"

        keywords = self.config.search_keywords
        if not keywords:
            self.log_scan.warning("SEARCH_KEYWORDS empty; nothing to scan")
            return "scan: 0 keywords, 0 new projects, 0 drafted, 0 skipped"

        client = self._get_freelancer()
        projects: Dict[Any, dict] = {}
        try:
            for keyword in keywords:
                response = client.search_projects(keyword, limit=20, offset=0)
                # The Freelancer API wraps payloads in a "result" envelope.
                envelope = (response or {}).get("result", response) or {}
                returned = len(envelope.get("projects", []) or [])
                self.log_scan.info('keyword="%s": returned %d projects', keyword, returned)
                for project in envelope.get("projects", []):
                    pid = project.get("id")
                    if pid is not None:
                        projects[pid] = project
        except AuthError as exc:
            # FIRST 401: alert, stop Loop A only. Loop B keeps running.
            self.scan_stopped = True
            self.log_error.error("freelancer 401 during scan: %s", exc)
            self._telegram_note(
                f"Loop A STOPPED - Freelancer API returned 401. "
                f"Regenerate the token, then restart the scheduler. ({exc})"
            )
            raise
        except FreelancerApiError as exc:
            self.log_error.error("scan aborted on API error: %s", exc)
            return f"scan: aborted on API error ({exc})"

        self.log_scan.info("union: %d unique projects before dedupe", len(projects))
        processed = store.get_processed_project_ids()
        fresh = [p for pid, p in projects.items() if pid not in processed]
        self.log_scan.info("after dedupe vs store: %d new projects", len(fresh))
        drafted = skipped = 0

        for project in fresh:
            pid = project["id"]
            try:
                from project_scorer import score_project

                scored = score_project(
                    project,
                    resume_text=self._get_resume(),
                    extra_skills=self._get_extra_skills(),
                    min_budget=self.config.min_budget,
                    excluded_keywords=self.config.excluded_keywords,
                )
            except ValueError as exc:
                store.append_record(self._record(pid, project, None, "skipped",
                                                 reason=f"scoring error: {exc}"))
                skipped += 1
                continue

            if scored.discarded:
                store.append_record(self._record(pid, project, scored, "skipped",
                                                 reason=f"excluded keyword: {scored.reason}"))
                skipped += 1
                continue
            if scored.total_score < self.config.score_threshold:
                store.append_record(self._record(pid, project, scored, "skipped",
                                                 reason="below threshold"))
                skipped += 1
                continue

            # rate limits (count REAL submissions, not drafts)
            if store.count_today("submitted") >= self.config.max_bids_per_day \
                    or store.count_this_month("submitted") >= self.config.max_bids_per_month:
                store.append_record(self._record(pid, project, scored, "skipped",
                                                 reason="rate limit"))
                skipped += 1
                self.log_scan.info("project %s skipped: daily/monthly submit cap reached", pid)
                continue

            try:
                outcome = self._draft_and_send(pid, project, scored)
                drafted += 1 if outcome in ("drafted", "dry") else 0
                if outcome == "failed":
                    self.stats["failed"] += 1
            except AuthError:
                raise  # detail fetch 401: Loop A already stopped + alerted
            except Exception as exc:  # noqa: BLE001 — one project must not kill the scan
                self.log_error.exception("project %s: draft/send failed", pid)
                store.append_record(self._record(pid, project, scored, "failed",
                                                 error=str(exc)))
                self.stats["failed"] += 1

        self.stats["scans"] += 1
        self.stats["drafted"] += drafted
        self.stats["skipped"] += skipped
        self.state.last_scan_ts = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state.save()

        summary = (f"scan: {len(keywords)} keywords, {len(fresh)} new projects, "
                   f"{drafted} drafted, {skipped} skipped")
        self.log_scan.info(summary)
        return summary

    def _record(self, pid: Any, project: dict, scored: Any, status: str,
                reason: Optional[str] = None, error: Optional[str] = None,
                message_id: Optional[int] = None) -> dict:
        base = {
            "project_id": pid,
            "project_title": (project or {}).get("title")
                              or (getattr(scored, "title", "") if scored else "")
                              or "unknown",
            "total_score": getattr(scored, "total_score", None) if scored else None,
            "draft_id": f"draft_{pid}_{int(time.time())}",
            "status": status,
            "message_id": message_id,
            "error": error,
        }
        budget = (project or {}).get("budget") or {}
        minimum = budget.get("minimum") if isinstance(budget, dict) else None
        if isinstance(minimum, (int, float)):
            base["bid_amount"] = minimum  # carried to place_bid on approval
        if reason:
            base["reason"] = reason
        return base

    def _draft_and_send(self, pid: int, project: dict, scored: Any) -> str:
        """Details -> draft -> persist -> Telegram. Returns final outcome."""
        from proposal_drafter import draft_proposal

        detail = self._get_freelancer().get_project_details(pid)
        draft = draft_proposal(detail, scored,
                               resume_text=self._get_resume(),
                               portfolio=self._get_portfolio())
        self._drafts[pid] = draft  # in-memory registry for place_bid

        # HARD CONSTRAINT: state BEFORE the Telegram action. The record is
        # pre-written without message_id, then amended after a successful
        # send (append-only: the amendment is a second "drafted" line).
        record = self._record(pid, project, scored, "drafted")
        store.append_record(record)

        if self.dry_run:
            print("=" * 70)
            print(f"[DRY] draft for project {pid} (score {scored.total_score}):")
            print("-" * 70)
            print(draft["text"])
            print("-" * 70)
            print(f"word_count={draft['word_count']} cited={draft['cited_evidence']} "
                  f"warnings={draft['warnings']}")
            return "dry"

        result = self._get_notifier().send_draft(draft, self._notify_project(pid, project, scored))
        if result.get("ok"):
            if result.get("message_id") is not None:
                store.append_record(self._record(pid, project, scored, "drafted",
                                                 message_id=result["message_id"]))
            self.log_scan.info("draft sent for %s (message_id=%s)",
                               pid, result.get("message_id"))
            return "drafted"
        # Telegram failed — the state store already holds the draft record.
        store.append_record(self._record(pid, project, scored, "failed",
                                         error=result.get("error") or "telegram send failed"))
        self.log_error.error("telegram send failed for %s: %s", pid, result.get("error"))
        return "failed"

    def _notify_project(self, pid: int, project: dict, scored: Any) -> dict:
        """Merged dict for telegram_notifier.send_draft (scored + raw fields)."""
        return {
            "project_id": pid,
            "title": getattr(scored, "title", None) or project.get("title") or "unknown",
            "url": getattr(scored, "url", None)
                   or f"https://www.freelancer.com/projects/{project.get('seo_url') or pid}",
            "total_score": getattr(scored, "total_score", 0),
            "rationale": getattr(scored, "rationale", "unknown"),
            "budget": project.get("budget"),
            "currency": project.get("currency"),
            "bid_stats": project.get("bid_stats"),
        }

    # ------------------------------------------------------------------ Loop B

    def run_poll_cycle(self) -> List[dict]:
        """
        One Loop B iteration: poll -> act -> persist offset. Returns the
        parsed actions. Never places a bid unless ALL hard constraints hold.
        """
        if self.poll_stopped:
            return []

        try:
            actions = self._get_notifier().poll_approvals(
                timeout=10, offset=self.state.telegram_offset)
        except tn.TelegramAuthError as exc:
            self.poll_stopped = True
            self.log_error.error("telegram 401: Loop B stopped (Loop A continues). %s", exc)
            return []
        except tn.TelegramError as exc:
            self.log_error.error("poll cycle failed: %s", exc)
            return []

        for action in actions:
            pid = action["project_id"]
            kind = action["action"]
            try:
                if kind == "approve":
                    result = self._handle_approve(pid)
                    if isinstance(result, dict):
                        self.last_results.append(result)
                elif kind == "reject":
                    store.update_status(pid, "rejected")   # state BEFORE telegram
                    self._send_status_safe(pid, "rejected")
                    self.stats["rejected"] += 1
                    self.log_poll.info("project %s rejected", pid)
                else:
                    self.log_poll.warning("unknown action %r for %s", kind, pid)
            except Exception as exc:  # noqa: BLE001 — keep polling
                self.log_error.exception("action %s %s failed", kind, pid)

        # Advance the offset whenever getUpdates saw ANY update (matched or
        # not) so stale backlog can never re-trigger, and so the offset only
        # moves forward (Step 6's manually polled offset is the floor).
        notifier = self._get_notifier()
        last_update_id = getattr(notifier, "last_update_id", None)
        if last_update_id is not None:
            if self.state.telegram_offset is None or last_update_id + 1 > self.state.telegram_offset:
                self.state.telegram_offset = last_update_id + 1
                self.state.save()  # atomic tmp + os.replace
        return actions

    def _handle_approve(self, pid: int) -> Optional[dict]:
        """Apply one approval; returns a result dict for callers (Tool/API).

        Result shape: {action, project_id, outcome, detail, draft_text}.
        outcome is one of approved | submitted | failed | skipped.
        """
        all_records = store.load_all()

        if self.config.draft_only:
            known = [r for r in all_records if r.get("project_id") == pid]
            if not known:
                self.log_poll.warning("approval for unknown project %s ignored (DRAFT_ONLY)", pid)
                return {"action": "approve", "project_id": pid, "outcome": "skipped",
                        "detail": "no record for this project in the state store",
                        "draft_text": None}
            record = known[-1]
            draft_text = self._recover_draft_text(pid, record)
            store.update_status(pid, "approved")           # state BEFORE telegram
            self._send_status_safe(
                pid, "approved",
                note="DRAFT_ONLY=true, no bid submitted. Copy the draft manually.")
            self.stats["approved"] += 1
            self.log_submit.info("project %s approved (DRAFT_ONLY=true; no bid)", pid)
            return {"action": "approve", "project_id": pid, "outcome": "approved",
                    "detail": "DRAFT_ONLY=true — draft text attached for manual copy",
                    "draft_text": draft_text}

        # HARD CONSTRAINT (c): first-scan safety gate — the store must hold
        # at least one prior "drafted" record before ANY submission. Checked
        # BEFORE the known-project check so an approval on an empty store
        # gets an explicit, notified refusal rather than silence.
        if not any(r.get("status") == "drafted" for r in all_records):
            store.append_record({
                "project_id": pid, "status": "skipped",
                "reason": "safety gate: no prior drafted record in store",
                "error": None,
            })
            self._send_status_safe(
                pid, "skipped",
                note="first-scan safety gate: no prior drafted record; bid NOT submitted")
            self.log_submit.warning("project %s: safety gate closed; no submission", pid)
            return {"action": "approve", "project_id": pid, "outcome": "skipped",
                    "detail": "first-scan safety gate: no prior drafted record in store; "
                              "bid NOT submitted",
                    "draft_text": None}

        known = [r for r in all_records if r.get("project_id") == pid]
        if not known:
            self.log_poll.warning("approval for unknown project %s ignored", pid)
            return {"action": "approve", "project_id": pid, "outcome": "skipped",
                    "detail": "no record for this project in the state store",
                    "draft_text": None}

        last_draft = next((r for r in reversed(known) if r.get("status") == "drafted"), None)
        amount = (last_draft or {}).get("bid_amount")
        draft_text = self._recover_draft_text(pid, last_draft or {})
        if not draft_text or amount is None:
            store.update_status(pid, "failed",
                                error="draft text or bid amount unavailable "
                                      "(scheduler restarted?); re-draft required")
            self._send_status_safe(pid, "failed", note="could not reconstruct bid payload")
            self.log_submit.error("project %s: missing draft/amount; cannot submit", pid)
            return {"action": "approve", "project_id": pid, "outcome": "failed",
                    "detail": "draft text or bid amount unavailable; re-draft required",
                    "draft_text": None}

        self.log_submit.info("placing bid for project %s (amount=%s period=%s)",
                             pid, amount, self.config.bid_period_days)
        try:
            result = self._get_freelancer().place_bid(pid, {
                "amount": amount,
                "period": self.config.bid_period_days,
                "description": draft_text,
            })
        except FreelancerApiError as exc:
            store.append_record({
                "project_id": pid, "status": "failed", "error": str(exc),
                "bid_amount": amount,
            })
            self._send_status_safe(pid, "failed", note=str(exc)[:200])
            self.log_submit.error("bid failed for %s: %s", pid, exc)
            return {"action": "approve", "project_id": pid, "outcome": "failed",
                    "detail": f"place_bid failed: {exc}", "draft_text": draft_text}

        store.append_record({
            "project_id": pid, "status": "submitted", "error": None,
            "bid_amount": amount, "message_id": (last_draft or {}).get("message_id"),
        })
        self._send_status_safe(pid, "submitted",
                               note=f"bid id {result.get('result', {}).get('id', 'unknown')}"
                                    if isinstance(result, dict) else None)
        self.stats["submitted"] += 1
        self.log_submit.info("project %s submitted", pid)
        return {"action": "approve", "project_id": pid, "outcome": "submitted",
                "detail": "bid submitted via Freelancer API",
                "bid_id": result.get("result", {}).get("id") if isinstance(result, dict) else None,
                "draft_text": draft_text}

    def _recover_draft_text(self, pid: int, record: dict) -> Optional[str]:
        """
        Best-effort recovery of a proposal's text for project `pid`:
        in-memory registry first, then the Telegram message cache (the
        drafted record stores the message_id). Used by approve_bid so the
        draft survives scheduler restarts.
        """
        draft = self._drafts.get(pid)
        if draft and draft.get("text"):
            return draft["text"]
        message_id = (record or {}).get("message_id")
        if message_id is None:
            return None
        try:
            notifier = self._get_notifier()
            text = notifier._message_texts.get(str(message_id))
            if text:
                # strip the [DRAFT ...] header and reply footer to recover
                # just the proposal body
                body = text.split("\n---\n", 2)
                return body[1].strip() if len(body) == 3 else text
            return None
        except Exception:  # noqa: BLE001 — recovery is best-effort by design
            return None

    def _send_status_safe(self, pid: int, status: str, note: str = "") -> None:
        try:
            self._get_notifier().send_status(pid, status, note=note)
        except Exception as exc:  # noqa: BLE001 — state already persisted
            self.log_error.warning("status message failed for %s: %s", pid, exc)

    # ------------------------------------------------------------- entrypoints

    async def scan_loop(self) -> None:
        interval = self.config.scan_interval_minutes * 60
        while not self.scan_stopped and not self._shutdown():
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.run_scan_cycle)
            except AuthError:
                pass  # already alerted + flagged; loop condition exits
            except Exception as exc:  # noqa: BLE001
                self.log_error.exception("scan cycle crashed: %s", exc)
            try:
                await asyncio.wait_for(self._wait_shutdown(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def poll_loop(self) -> None:
        while not self.poll_stopped and not self._shutdown():
            await asyncio.get_event_loop().run_in_executor(None, self.run_poll_cycle)
            try:
                await asyncio.wait_for(self._wait_shutdown(),
                                       timeout=self.config.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def _shutdown(self) -> bool:
        return self.shutdown_event is not None and self.shutdown_event.is_set()

    async def _wait_shutdown(self) -> None:
        if self.shutdown_event is not None:
            await self.shutdown_event.wait()

    async def run_forever(self) -> None:
        if self.shutdown_event is None:  # never orphan an injected event
            self.shutdown_event = asyncio.Event()
        self.state.process_start_ts = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state.loop_a_status = "running"
        self.state.loop_b_status = "running"
        self.state.owner_pid = os.getpid()
        self.state.save()
        tasks = [asyncio.create_task(self.scan_loop()),
                 asyncio.create_task(self.poll_loop())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:  # drain exceptions so they are never "unretrieved"
            try:
                exc = task.exception()
                if exc is not None:
                    self.log_error.error("loop task ended on %s", type(exc).__name__)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        self.state.loop_a_status = "stopped"
        self.state.loop_b_status = "stopped"
        self.state.owner_pid = None
        self.state.save()
        self.final_summary()

    def final_summary(self) -> None:
        line = (f"shutdown summary: scans={self.stats['scans']} "
                f"drafted={self.stats['drafted']} skipped={self.stats['skipped']} "
                f"submitted={self.stats['submitted']} approved={self.stats['approved']} "
                f"rejected={self.stats['rejected']} failed={self.stats['failed']}")
        self.log_scan.info(line)
        print(line)


# =============================================================================
# SECTION 5: CLI
# =============================================================================


def _loop_status(state: "SchedulerState", attr: str, stopped_flag: bool) -> str:
    """
    Honest loop status for --status. Priority:
      1. a *live* owner pid claiming "running" (pid alive AND this is the
         running scheduler) -> "running"
      2. persisted "stopped" (or a "running" claim whose owner died) -> "stopped"
      3. never started -> "never started"
    A --once process has exited by the time --status runs, so its persisted
    "stopped" is what you see — never a phantom "running".
    """
    persisted = getattr(state, attr)
    if persisted == "running":
        if state.owner_pid and _pid_alive(state.owner_pid):
            return "running"
        return "stopped (stale running flag; owner process gone)"
    if persisted == "stopped":
        return "stopped"
    if stopped_flag:
        return "stopped"
    return "never started"


def _pid_alive(pid: int) -> bool:
    try:
        process, num = None, 0
        kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
        if kernel32 is not None:
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _cli_status(scheduler: Scheduler) -> int:
    cfg = scheduler.config
    state = scheduler.state
    last = state.last_scan_ts
    next_scan = None
    if last:
        try:
            from datetime import timedelta

            last_dt = datetime.fromisoformat(last)
            next_scan = (last_dt + timedelta(minutes=cfg.scan_interval_minutes)) \
                .astimezone().isoformat(timespec="seconds")
        except ValueError:
            next_scan = None
    drafted_today = len({r.get("project_id") for r in store.load_all()
                         if r.get("status") == "drafted"
                         and str(r.get("ts", ""))[:10] ==
                         datetime.now().astimezone().isoformat()[:10]})
    print(f"keywords:        {', '.join(cfg.search_keywords) or '(none set)'}")
    print(f"last scan:       {last or '(never)'}")
    print(f"next scan:       {next_scan or '(pending start)'}")
    print(f"drafts today:    {drafted_today} (unique projects)")
    print(f"submitted today: {store.count_today('submitted')}")
    print(f"submitted month: {store.count_this_month('submitted')}")
    print(f"draft_only:      {cfg.draft_only}")
    print(f"telegram offset: {state.telegram_offset if state.telegram_offset is not None else '(none)'}")
    print(f"loop A:          {_loop_status(state, 'loop_a_status', scheduler.scan_stopped)}"
          + ("  [freelancer 401]" if scheduler.scan_stopped else ""))
    print(f"loop B:          {_loop_status(state, 'loop_b_status', scheduler.poll_stopped)}"
          + ("  [telegram 401]" if scheduler.poll_stopped else ""))
    return 0


# ---------------------------------------------------------------------------
# Module-level API for the OpenWebUI Tool (and the CLI calls these too).
# ---------------------------------------------------------------------------


def scan_once(config: Optional[SchedulerConfig] = None, dry: bool = False) -> dict:
    """
    Run ONE scan cycle (keywords -> search -> score -> draft -> Telegram
    unless dry). Never submits bids. Returns a summary dict:
    {ok, summary, drafted, skipped, new_projects, error}.
    Loop statuses are persisted honestly (stopped when this returns).
    """
    scheduler = Scheduler(config, dry_run=dry)
    scheduler.state.loop_a_status = "running"
    scheduler.state.loop_b_status = "stopped"  # one-shot never runs Loop B
    scheduler.state.owner_pid = os.getpid()
    scheduler.state.save()
    try:
        summary = scheduler.run_scan_cycle()
        return {
            "ok": True,
            "summary": summary,
            "drafted": scheduler.stats["drafted"],
            "skipped": scheduler.stats["skipped"],
            "failed": scheduler.stats["failed"],
            "error": None,
        }
    except AuthError as exc:
        return {"ok": False, "summary": f"scan aborted: freelancer 401 ({exc})",
                "drafted": 0, "skipped": 0, "failed": 0, "error": str(exc)}
    finally:
        scheduler.state.loop_a_status = "stopped"
        scheduler.state.owner_pid = None
        scheduler.state.save()


def handle_approve(config: Optional[SchedulerConfig] = None, project_id: int = 0) -> dict:
    """
    Apply one approval for `project_id` under the triple lock (explicit
    approval, DRAFT_ONLY=false, first-scan safety gate). Returns the
    result dict from Scheduler._handle_approve, including draft_text when
    DRAFT_ONLY=true so the caller can paste it manually.
    """
    scheduler = Scheduler(config)
    result = scheduler._handle_approve(int(project_id))
    return result or {"action": "approve", "project_id": project_id,
                      "outcome": "skipped", "detail": "no result", "draft_text": None}


def poll_once(config: Optional[SchedulerConfig] = None) -> dict:
    """
    Run ONE Telegram approval poll cycle (poll -> act -> persist offset).
    Returns {actions: [...], outcomes: [...], offset, error}.
    """
    scheduler = Scheduler(config)
    try:
        actions = scheduler.run_poll_cycle()
        return {"actions": actions, "outcomes": list(scheduler.last_results),
                "offset": scheduler.state.telegram_offset, "error": None}
    except tn.TelegramError as exc:
        return {"actions": [], "outcomes": [], "offset": scheduler.state.telegram_offset,
                "error": str(exc)}


def read_status(config: Optional[SchedulerConfig] = None) -> dict:
    """
    Snapshot of scheduler + store state for dashboards/Tool status calls:
    keywords, last/next scan, loop statuses, telegram offset, counts,
    processed project ids, and the last N store records.
    """
    cfg = config or SchedulerConfig.from_env()
    state = SchedulerState()
    records = store.get_last(10)
    next_scan = None
    if state.last_scan_ts:
        try:
            from datetime import timedelta

            next_scan = (datetime.fromisoformat(state.last_scan_ts)
                         + timedelta(minutes=cfg.scan_interval_minutes)) \
                .astimezone().isoformat(timespec="seconds")
        except ValueError:
            next_scan = None
    today = datetime.now().astimezone().isoformat()[:10]
    return {
        "keywords": cfg.search_keywords,
        "score_threshold": cfg.score_threshold,
        "draft_only": cfg.draft_only,
        "last_scan_ts": state.last_scan_ts,
        "next_scan_ts": next_scan,
        "telegram_offset": state.telegram_offset,
        "loop_a_status": state.loop_a_status,
        "loop_b_status": state.loop_b_status,
        "drafts_today": len({r.get("project_id") for r in store.load_all()
                             if r.get("status") == "drafted"
                             and str(r.get("ts", ""))[:10] == today}),
        "submitted_today": store.count_today("submitted"),
        "submitted_month": store.count_this_month("submitted"),
        "processed_project_ids": len(store.get_processed_project_ids()),
        "recent_records": records,
    }


def run_once(dry: bool) -> int:
    result = scan_once(dry=dry)
    if not result["ok"]:
        return 2
    return 0


def _cli_load_env() -> None:
    """Load .env (override=False) at CLI entry — never at import time."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        return
    load_dotenv(override=False)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scheduler.py", description="FreelancerClaw orchestration scheduler.")
    parser.add_argument("--once", action="store_true", help="run ONE scan cycle and exit")
    parser.add_argument("--dry", action="store_true",
                        help="with --once: print drafts instead of sending Telegram")
    parser.add_argument("--status", action="store_true", help="print current scheduler state")
    parser.add_argument("--reset-offset", action="store_true",
                        help="clear the stored Telegram offset")
    parser.add_argument("--test", action="store_true", help="run T1-T7 offline suite")
    parser.add_argument("--verbose", action="store_true", help="debug console logging")
    args = parser.parse_args(argv)

    # Load .env BEFORE building log handlers: setup_logging() arms the
    # token-scrub filter from the environment, so the Telegram token
    # must already be loaded when the root DEBUG handlers are created.
    # Logging first would leave the file handler unscrubbed until the
    # first Telegram send armed the filter retroactively.
    _cli_load_env()
    setup_logging(verbose=args.verbose)

    if args.test:
        from scheduler_tests import run_scheduler_tests

        return run_scheduler_tests()

    if args.reset_offset:
        SchedulerState().reset_offset()
        print("telegram offset cleared; next poll re-reads recent updates")
        return 0

    if args.status:
        return _cli_status(Scheduler())

    if args.once:
        return run_once(dry=args.dry)

    scheduler = Scheduler()
    try:
        asyncio.run(scheduler.run_forever())
        return 0
    except KeyboardInterrupt:
        scheduler.final_summary()
        return 0


if __name__ == "__main__":
    sys.exit(main())
