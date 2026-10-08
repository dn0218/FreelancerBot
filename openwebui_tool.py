"""
=============================================================================
FreelancerClaw — Module 8: openwebui_tool.py (OpenWebUI Tool wrapper)
=============================================================================
A single-file OpenWebUI Tool that ORCHESTRATES the FreelancerClaw modules
(freelancer_api, project_scorer, proposal_drafter, telegram_notifier,
bid_state_store, scheduler). No business logic is duplicated here.

Installing in OpenWebUI
-----------------------
1. Copy this file AND the six module files it imports into the same
   directory (OpenWebUI -> Admin Panel -> Tools -> New Tool, paste this
   file's source; keep the modules importable next to it).
2. Set credentials in the Tool's Valves (gear icon). Valves override
   environment variables; environment variables override .env.
3. The Tool exposes five callable functions; OpenWebUI shows their
   docstrings as the tool descriptions:
     - scan_and_draft()  one scan cycle (search -> score -> draft -> push)
     - approve_bid(id)   approve a draft (manual copy or gated submission)
     - get_status()      last 10 store records + scheduler status
     - poll_approvals()  one Telegram approval poll cycle
     - reset_offset()    clear the stored Telegram offset

Safety
------
  * scan_and_draft NEVER submits a bid.
  * approve_bid NEVER bypasses the first-scan safety gate (an empty store
    blocks all submissions), regardless of Valves.
  * No method blocks the event loop for more than ~25s; slow work runs in
    a worker thread and methods return summaries.

Local verification (before pasting into OpenWebUI):
  python openwebui_tool.py --test       smoke test (only /users/self call)
  python openwebui_tool.py --status     same as get_status()
  python openwebui_tool.py --scan-dry   dry scan, DRAFT_ONLY forced, no push
=============================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Path bootstrap — everything resolves relative to THIS file, never the cwd,
# so the Tool keeps working when OpenWebUI launches it from elsewhere.
# ---------------------------------------------------------------------------
TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

for _sub in ("logs", "data", "cache"):
    (TOOL_DIR / _sub).mkdir(parents=True, exist_ok=True)

# .env is the LOWEST precedence layer (Valves > environment > .env), so it
# may only fill variables that are not already set.
try:
    from dotenv import load_dotenv

    load_dotenv(TOOL_DIR / ".env", override=False)
except ImportError:
    pass

# Project modules (imported AFTER the sys.path bootstrap)
import bid_state_store as store            # noqa: E402
import scheduler as sched_mod              # noqa: E402
from freelancer_api import ApiClientError, AuthError, FreelancerClient   # noqa: E402
from scheduler import SchedulerConfig, SchedulerState                    # noqa: E402

TOOL_LOG_PATH = TOOL_DIR / "logs" / "openwebui_tool.log"
_OPERATION_TIMEOUT_S = 25.0   # hard cap; OpenWebUI must never hang on us


# ---------------------------------------------------------------------------
# Valves — every configurable value, OpenWebUI conventions (pydantic v2)
# ---------------------------------------------------------------------------
class Valves(BaseModel):
    """FreelancerClaw settings. Valves beat environment variables; env beats .env."""

    FREELANCER_ACCESS_TOKEN: str = Field(
        default="", description="Freelancer.com API OAuth access token")
    TELEGRAM_BOT_TOKEN: str = Field(
        default="", description="Telegram bot token from @BotFather")
    TELEGRAM_CHAT_ID: str = Field(
        default="", description="Telegram chat id that receives draft approvals")
    GITHUB_PAT: str = Field(
        default="", description="GitHub personal access token (public repo read)")
    SEARCH_KEYWORDS: str = Field(
        default="python automation,web scraping,telegram bot,pdf extraction,"
                "LLM integration,data analysis",
        description="Comma-separated Freelancer search keywords")
    SCORE_THRESHOLD: int = Field(
        default=70, ge=0, le=100,
        description="Minimum proposal score (0-100) required to draft")
    MIN_BUDGET: int = Field(
        default=100, ge=0, description="Minimum project budget (USD) to consider")
    MAX_BIDS_PER_DAY: int = Field(
        default=1, ge=1, description="Hard cap on submitted bids per day")
    MAX_BIDS_PER_MONTH: int = Field(
        default=6, ge=1, description="Hard cap on submitted bids per month")
    SCAN_INTERVAL_MINUTES: int = Field(
        default=30, ge=15, description="Minutes between scheduled scans (floor 15)")
    EXCLUDED_KEYWORDS: str = Field(
        default="crypto,adult,captcha bypass,guaranteed returns,profit guarantee,"
                "whatsapp business api,linkedin automation,cold email",
        description="Comma-separated keywords that disqualify a project outright")
    DRAFT_ONLY: bool = Field(
        default=True,
        description="True: never submit bids, only draft them for manual copy")


def _split_csv(raw: str) -> list:
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Tools — the class OpenWebUI instantiates
# ---------------------------------------------------------------------------
class Tools:
    """FreelancerClaw tool: scan Freelancer, draft scored proposals, push them
    to Telegram for approval, and (only with DRAFT_ONLY=False and every safety
    gate green) submit approved bids."""

    def __init__(self):
        """Instantiate Valves and set up logging to logs/openwebui_tool.log."""
        self.valves = Valves()
        self.log = logging.getLogger("freelancerclaw.openwebui_tool")
        if not self.log.handlers:                      # survive re-instantiation
            handler = logging.FileHandler(TOOL_LOG_PATH, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [openwebui] %(message)s"))
            self.log.addHandler(handler)
            self.log.setLevel(logging.INFO)
        self.log.info("OpenWebUI Tool initialized")

    # -- internal helpers ---------------------------------------------------

    def _apply_credentials_to_env(self) -> None:
        """
        Push Valve credentials into os.environ using the precedence rule
        'Valves override environment variables; environment overrides .env'.
        Only non-empty Valve values are written — a non-empty env value
        (e.g. loaded from .env) survives an empty Valve untouched.
        """
        for key in ("FREELANCER_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
                    "TELEGRAM_CHAT_ID", "GITHUB_PAT"):
            value = getattr(self.valves, key, "")
            if value:
                os.environ[key] = value

    def _cred(self, name: str) -> str:
        """Effective credential: Valve if set, else environment/.env."""
        return getattr(self.valves, name, "") or os.environ.get(name, "")

    def _config(self) -> SchedulerConfig:
        """Build a SchedulerConfig from the current Valves."""
        self._apply_credentials_to_env()
        return SchedulerConfig(
            scan_interval_minutes=max(15, self.valves.SCAN_INTERVAL_MINUTES),
            draft_only=True if self._force_draft_only else self.valves.DRAFT_ONLY,
            search_keywords=_split_csv(self.valves.SEARCH_KEYWORDS),
            score_threshold=float(self.valves.SCORE_THRESHOLD),
            min_budget=float(self.valves.MIN_BUDGET),
            max_bids_per_day=self.valves.MAX_BIDS_PER_DAY,
            max_bids_per_month=self.valves.MAX_BIDS_PER_MONTH,
            excluded_keywords=_split_csv(self.valves.EXCLUDED_KEYWORDS) or None,
        )

    _force_draft_only: bool = False   # set locally by --scan-dry

    async def _run_short(self, fn: Callable, timeout: float = _OPERATION_TIMEOUT_S) -> Any:
        """Run a blocking function in a worker thread, capped at `timeout`
        seconds so the event loop is never blocked for long."""
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, fn), timeout=timeout)
        except asyncio.TimeoutError:
            return TimeoutError(f"operation exceeded {timeout:.0f}s")

    # -- OpenWebUI-callable methods ------------------------------------------

    async def scan_and_draft(self) -> str:
        """
        Run one full scan cycle: search Freelancer projects for all configured
        keywords, score each result, and for any project above
        SCORE_THRESHOLD, draft a proposal and push it to Telegram. Returns a
        one-line summary. Never submits bids.
        """
        result = await self._run_short(
            functools.partial(sched_mod.scan_once, self._config(), False))
        if isinstance(result, TimeoutError):
            return "scan still running in the background; call get_status() for the outcome."
        line = (result or {}).get("summary") or "scan finished"
        self.log.info("scan_and_draft: %s", line)
        return line

    async def approve_bid(self, bid_id: str) -> str:
        """
        Approve a previously drafted bid. If DRAFT_ONLY is True, returns the
        draft text so you can paste it into Freelancer manually. If
        DRAFT_ONLY is False and all safety gates pass, submits the bid via
        the Freelancer API and returns the submission result. The first-scan
        safety gate can never be bypassed through this method.
        """
        try:
            project_id = int(str(bid_id).strip())
        except (TypeError, ValueError):
            return f"error: bid_id must be the numeric project id, got {bid_id!r}"

        result = await self._run_short(
            functools.partial(sched_mod.handle_approve, self._config(), project_id))
        if isinstance(result, TimeoutError):
            return "approval still processing; call get_status() to see the outcome."
        if not isinstance(result, dict):
            return f"error: unexpected approval result {result!r}"

        outcome = result.get("outcome")
        detail = result.get("detail", "")
        head = f"approve project {project_id}: {outcome} — {detail}"
        if self.valves.DRAFT_ONLY and result.get("draft_text"):
            body = result["draft_text"]
            return (f"{head}\n\n-------- draft (paste into Freelancer manually) "
                    f"--------\n{body}\n-------------------------------------------"
                    f"-----------------")
        return head

    async def get_status(self) -> str:
        """
        Return the last 10 records from the bid state store plus the current
        scheduler status: keywords, last scan time, drafts sent today,
        submitted count.
        """
        status = await self._run_short(
            functools.partial(sched_mod.read_status, self._config()), timeout=10.0)
        if isinstance(status, TimeoutError):
            return "status read timed out; the store may be busy — retry shortly."
        if not isinstance(status, dict):
            return f"error: unexpected status result {status!r}"

        lines = [
            f"keywords:      {', '.join(status.get('keywords') or [])}",
            f"score >=:      {status.get('score_threshold')}",
            f"draft_only:    {status.get('draft_only')}",
            f"last scan:     {status.get('last_scan_ts') or '(never)'}",
            f"next scan:     {status.get('next_scan_ts') or '(n/a)'}",
            f"drafts today:  {status.get('drafts_today')}",
            f"submitted:     today {status.get('submitted_today')}, "
            f"this month {status.get('submitted_month')}",
            f"telegram offset: {status.get('telegram_offset') if status.get('telegram_offset') is not None else '(none)'}",
            f"loop A / B:    {status.get('loop_a_status') or 'never'} / "
            f"{status.get('loop_b_status') or 'never'}",
            f"processed ids: {status.get('processed_project_ids')}",
            "last 10 records:",
        ]
        for rec in status.get("recent_records") or []:
            lines.append(
                "  {ts} project {project_id} [{status}] {reason}{amount}".format(
                    ts=str(rec.get("ts", ""))[:19],
                    project_id=rec.get("project_id"),
                    status=rec.get("status"),
                    reason=rec.get("reason") or rec.get("error") or "",
                    amount=f" @ {rec['bid_amount']} USD" if rec.get("bid_amount") else ""))
        return "\n".join(lines)

    async def poll_approvals(self) -> str:
        """
        Run one Telegram poll cycle: read pending approve/reject replies and
        apply them to the state store. Returns a summary of actions taken.
        Short and synchronous under the hood; returns immediately.
        """
        result = await self._run_short(
            functools.partial(sched_mod.poll_once, self._config()), timeout=20.0)
        if isinstance(result, TimeoutError):
            return "poll still running; the parsed replies will appear in get_status()."
        if not isinstance(result, dict):
            return f"error: unexpected poll result {result!r}"
        if result.get("error"):
            return f"poll failed: {result['error']}"

        actions = result.get("actions") or []
        if not actions:
            return f"no pending approvals (telegram offset now {result.get('offset')})."
        applied = "; ".join(
            f"{o.get('action')} {o.get('project_id')} -> {o.get('outcome')}"
            for o in (result.get("outcomes") or [])) or "processed"
        return f"{len(actions)} action(s): {applied} (offset {result.get('offset')})."

    async def reset_offset(self) -> str:
        """
        Clear the stored Telegram offset so the next poll re-reads recent
        updates.
        """
        state = SchedulerState()
        old = state.telegram_offset
        state.telegram_offset = None
        state.save()   # atomic tmp + os.replace, same file the scheduler uses
        self.log.info("reset_offset: %s -> None", old)
        return f"telegram offset cleared (was {old if old is not None else '(none)'}); " \
               f"the next poll_approvals() will re-read recent updates."


# ---------------------------------------------------------------------------
# CLI — local verification before pasting into OpenWebUI
# ---------------------------------------------------------------------------
def _die(message: str) -> "None":
    print(f"error: {message}")
    raise SystemExit(1)


def _cli_check_credentials(tool: Tools, verify_freelancer: bool) -> "FreelancerClient":
    missing = [name for name in ("FREELANCER_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
                                 "TELEGRAM_CHAT_ID", "GITHUB_PAT")
               if not tool._cred(name)]
    if missing:
        _die("missing credential(s): " + ", ".join(missing) +
             "\n  set them in the Tool's Valves, the environment, or .env")
    tool._apply_credentials_to_env()
    if not verify_freelancer:
        return None
    client = FreelancerClient(tool._cred("FREELANCER_ACCESS_TOKEN"))
    try:
        me = client.get_self()
    except AuthError:
        _die("FREELANCER_ACCESS_TOKEN rejected (401) — generate a fresh token")
    except ApiClientError as exc:
        _die(f"could not verify token against /users/self: {exc}")
    username = (me.get("result") or {}).get("username") or \
               (me.get("result") or {}).get("id") or "unknown"
    print(f"token OK — authenticated as {username}")
    return client


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FreelancerClaw OpenWebUI Tool — local verification CLI")
    parser.add_argument("--test", action="store_true",
                        help="smoke test: credentials + /users/self + get_status()")
    parser.add_argument("--status", action="store_true",
                        help="same as calling get_status() directly")
    parser.add_argument("--scan-dry", action="store_true",
                        help="scan_and_draft() with DRAFT_ONLY forced true and no Telegram push")
    args = parser.parse_args(argv)

    if not (args.test or args.status or args.scan_dry):
        parser.print_help()
        return 1

    tool = Tools()

    if args.status:
        print(asyncio.run(tool.get_status()))
        return 0

    if args.scan_dry:
        tool._force_draft_only = True
        result = sched_mod.scan_once(tool._config(), dry=True)
        print(result.get("summary") or "scan finished")
        return 0 if result.get("ok") else 1

    # --test: the ONLY network call allowed here is /users/self
    _cli_check_credentials(tool, verify_freelancer=True)
    print("\n--- get_status() ---")
    print(asyncio.run(tool.get_status()))
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
