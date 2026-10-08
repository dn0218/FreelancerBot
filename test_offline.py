"""
=============================================================================
FreelancerClaw — test_offline.py (STEP 10a)
=============================================================================
HERMETIC end-to-end integration test: scan -> score -> draft -> notify ->
approve -> store, with every network-bearing surface mocked at the module
attribute level — the exact seams scheduler.py calls in production.

Mock surface (patched where scheduler.py calls them):
  * freelancer_api.FreelancerClient.search_projects ..... 3 fixtures:
        501 qualifies (score above threshold)
        502 discarded (matched excluded keyword)
        503 skipped   (below SCORE_THRESHOLD)
  * freelancer_api.FreelancerClient.get_project_details . full description
    for the qualifying project
  * freelancer_api.FreelancerClient.place_bid ........... captures call
    args, returns a success dict
  * telegram_notifier.TelegramNotifier.send_draft ....... captures the
    payload, returns {ok: True, message_id: 1}
  * telegram_notifier.TelegramNotifier.send_status ...... captures the
    approved/submitted status pings
  * telegram_notifier.TelegramNotifier.poll_approvals ... returns one
    approve action for project 501 on the SECOND call, empty on the first
  * portfolio_reader.get_resume / get_portfolio ......... fixed fixtures

Hermetic guarantees:
  * zero network — every outbound module call is a mock
  * no real .env — credential env vars are scrubbed before imports
  * no real data/bids.jsonl — store, scheduler state and the Telegram
    message cache all live inside per-test TemporaryDirectories
  * runs from any cwd — paths anchored to this file's location

Run:   python test_offline.py
Exit:  0 = all tests passed, 1 = any failure (CI-friendly).
=============================================================================
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
import tempfile
import time
import traceback
import unittest.mock as mock
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Hermeticity guard: scrub credential env vars BEFORE importing project
# modules, so no real .env / real tokens can influence this run.
# ---------------------------------------------------------------------------
for _var in ("FREELANCER_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
             "TELEGRAM_CHAT_ID", "GITHUB_PAT"):
    os.environ.pop(_var, None)
# Dummy tokens: real values are scrubbed above; these only silence the
# constructor "token not set" warnings. Every outbound method is mocked,
# so no request can escape this process.
os.environ["FREELANCER_ACCESS_TOKEN"] = "offline-test-token"
os.environ["TELEGRAM_BOT_TOKEN"] = "offline-test-token"
os.environ["TELEGRAM_CHAT_ID"] = "offline-test-chat"
os.environ.setdefault("EXCLUDED_KEYWORDS", "crypto,adult")

import bid_state_store as store                                   # noqa: E402
import telegram_notifier as tn                                    # noqa: E402
import portfolio_reader as pr                                     # noqa: E402
from freelancer_api import FreelancerClient                       # noqa: E402
from scheduler import Scheduler, SchedulerConfig, SchedulerState  # noqa: E402


# --------------------------------------------------------------------- fixtures

QUALIFY_ID = 501
EXCLUDED_ID = 502
BELOW_ID = 503

_NOW = time.time()

FIXTURE_PROJECTS: List[dict] = [
    {
        "id": QUALIFY_ID,
        "title": "Automate report generation with Python and Telegram alerts",
        "seo_url": "qualifying-project",
        "preview_description": (
            "Need a Python script that scrapes supplier pricing pages weekly, "
            "writes Excel reports, and sends Telegram alerts on anomalies."),
        "description": None,          # details are fetched only for 501
        "budget": {"minimum": 500, "maximum": 1500},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": 6, "bid_avg": 500},
        "submitdate": _NOW - 1800,
    },
    {
        "id": EXCLUDED_ID,
        "title": "Crypto trading bot with guaranteed returns",
        "seo_url": "excluded-project",
        "preview_description": (
            "Arbitrage bot for crypto exchanges with guaranteed returns "
            "and profit guarantee, Python preferred."),
        "description": None,
        "budget": {"minimum": 500, "maximum": 1500},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": 4, "bid_avg": 400},
        "submitdate": _NOW - 7200,
    },
    {
        "id": BELOW_ID,
        "title": "Weather alerts bot",
        "seo_url": "below-project",
        "preview_description": (
            "Simple bot that sends weather alerts, small budget project."),
        "description": None,
        "budget": {"minimum": 150, "maximum": 450},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": 3, "bid_avg": 200},
        "submitdate": _NOW - 900,
    },
]

FULL_DESCRIPTION_FOR_501 = (
    "We need an automation specialist to build a Python pipeline that "
    "logs into supplier portals, extracts pricing tables into Excel, and "
    "notifies our ops team through Telegram when prices shift by more than "
    "5%. Existing scripts exist but are unreliable; we want a robust, "
    "documented, scheduled solution with retry logic and error alerts."
)

MOCK_RESUME = (
    "# Test User\n\n"
    "## Skills\n\n"
    "python, automation, scraping, excel, telegram, api, scheduling\n\n"
    "## Selected experience\n\n"
    "### 1. Scraping Platform Overhaul\n"
    "- Built a Scrapy and Playwright pipeline monitoring supplier sites nightly.\n\n"
    "### 2. Telegram Ops Bot\n"
    "- Delivered a Telegram bot with role-based access controlling reports.\n\n"
    "## Rates and preferences\n\n"
    "- Typical budget range: USD 250-2000 per project\n"
)

MOCK_PORTFOLIO: Dict[str, Any] = {
    "login": "octodev",
    "repos": [
        {
            "name": "supplier-watch-telegram",
            "description": "Python pipeline that scrapes supplier pricing "
                           "pages into Excel and sends Telegram alerts.",
            "language": "Python",
            "html_url": "https://github.com/octodev/supplier-watch-telegram",
            "topics": ["python", "automation", "telegram", "scraping"],
        },
    ],
}


# --------------------------------------------------------------------- mocks

class MockFreelancer:
    """The FreelancerClient surface scheduler.py calls, recording everything."""

    def __init__(self) -> None:
        self.searches: List[dict] = []
        self.details: List[int] = []
        self.bids: List[dict] = []

    def search_projects(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        self.searches.append({"query": query, "limit": limit, "offset": offset})
        return {"result": {"projects": copy.deepcopy(FIXTURE_PROJECTS),
                           "total_count": len(FIXTURE_PROJECTS)}}

    def get_project_details(self, project_id: int) -> dict:
        self.details.append(project_id)
        base = copy.deepcopy(next(
            (p for p in FIXTURE_PROJECTS if p["id"] == project_id), {}))
        if project_id == QUALIFY_ID:
            base["description"] = FULL_DESCRIPTION_FOR_501
        return base

    def place_bid(self, project_id: int, bid_data: dict) -> dict:
        self.bids.append({"project_id": project_id, "bid_data": dict(bid_data)})
        return {"result": {"id": 555001}}


class MockTelegram:
    """The TelegramNotifier surface scheduler.py calls.

    poll_approvals follows the scripted contract: empty on the first call,
    one approve action for QUALIFY_ID on the second.
    """

    def __init__(self) -> None:
        self.sent_drafts: List[dict] = []
        self.sent_statuses: List[tuple] = []
        self.poll_count = 0
        self.last_update_id: Optional[int] = None

    def send_draft(self, draft: dict, scored_project: Any) -> dict:
        self.sent_drafts.append({"draft": draft, "project": scored_project})
        return {"ok": True, "message_id": 1, "chat_id": 99, "error": None}

    def send_status(self, project_id: int, status: str, note: str = "") -> dict:
        self.sent_statuses.append((project_id, status, note))
        return {"ok": True, "message_id": 2, "error": None}

    def poll_approvals(self, timeout: int = 0, offset: Optional[int] = None) -> List[dict]:
        self.poll_count += 1
        if self.poll_count == 2:
            self.last_update_id = 7001
            return [{"update_id": 7001, "action": "approve",
                     "project_id": QUALIFY_ID}]
        return []


class _Patched:
    """Patch every outbound seam for the duration of a `with` block."""

    def __enter__(self) -> "_Patched":
        self.stack = contextlib.ExitStack()
        self.freelancer = MockFreelancer()
        self.telegram = MockTelegram()
        fl, tg = self.freelancer, self.telegram
        self.stack.enter_context(mock.patch.object(
            FreelancerClient, "search_projects",
            side_effect=lambda q, limit=20, offset=0: fl.search_projects(q, limit, offset)))
        self.stack.enter_context(mock.patch.object(
            FreelancerClient, "get_project_details",
            side_effect=lambda pid: fl.get_project_details(pid)))
        self.stack.enter_context(mock.patch.object(
            FreelancerClient, "place_bid",
            side_effect=lambda pid, bd: fl.place_bid(pid, bd)))
        self.stack.enter_context(mock.patch.object(
            tn.TelegramNotifier, "send_draft",
            side_effect=lambda draft, sp: tg.send_draft(draft, sp)))
        self.stack.enter_context(mock.patch.object(
            tn.TelegramNotifier, "send_status",
            side_effect=lambda pid, st, note="": tg.send_status(pid, st, note)))
        self.stack.enter_context(mock.patch.object(
            tn.TelegramNotifier, "poll_approvals",
            side_effect=lambda timeout=0, offset=None: tg.poll_approvals(timeout, offset)))
        self.stack.enter_context(mock.patch.object(
            pr, "get_resume", side_effect=lambda: MOCK_RESUME))
        self.stack.enter_context(mock.patch.object(
            pr, "get_portfolio", side_effect=lambda: (MOCK_PORTFOLIO, False)))
        return self

    def __exit__(self, *exc) -> None:
        self.stack.close()


# --------------------------------------------------------------------- helpers

def _config(**overrides) -> SchedulerConfig:
    cfg = SchedulerConfig(
        search_keywords=["python automation"],
        score_threshold=70.0,
        min_budget=100.0,
        draft_only=True,
        max_bids_per_day=1,
        max_bids_per_month=6,
        excluded_keywords=["crypto", "adult"],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_scheduler(cfg: SchedulerConfig, tmp: Path) -> Scheduler:
    """Real Scheduler wired to temp paths; outbound seams patched by _Patched."""
    return Scheduler(
        cfg,
        freelancer=None,   # forces construction of the (patched) real client
        notifier=None,     # forces construction of the (patched) real notifier
        state=SchedulerState(state_path=tmp / "scheduler_state.json"),
        resume_text=None,  # forces the patched portfolio_reader.get_resume
        portfolio=None,    # forces the patched portfolio_reader.get_portfolio
        dry_run=False,
    )


@contextlib.contextmanager
def _hermetic_store():
    """Point bid_state_store at a temp file for the duration of a test."""
    with tempfile.TemporaryDirectory(prefix="claw_e2e_") as tmp:
        store.set_store_path(Path(tmp) / "bids.jsonl")
        yield Path(tmp)


def _records() -> List[dict]:
    return store.load_all()


def _for_pid(pid: int) -> List[dict]:
    return [r for r in _records() if r.get("project_id") == pid]


def _last(pid: int) -> dict:
    recs = _for_pid(pid)
    assert recs, f"no records for project {pid}"
    return recs[-1]


# ----------------------------------------------------------------------- tests

def t1_full_scan_cycle() -> None:
    """3 fixtures -> exactly 1 drafted, 1 skipped (excluded), 1 skipped (below)."""
    with _Patched(), _hermetic_store() as tmp:
        sched = _make_scheduler(_config(), tmp)
        summary = sched.run_scan_cycle()

        drafted = [r for r in _records() if r["status"] == "drafted"]
        assert len(_for_pid(QUALIFY_ID)) >= 1, "T1: qualifying project not drafted"
        assert all(r["status"] == "drafted" for r in _for_pid(QUALIFY_ID)), (
            f"T1: 501 records corrupted: {_for_pid(QUALIFY_ID)}")
        assert _last(EXCLUDED_ID)["status"] == "skipped", (
            f"T1: excluded project not skipped: {_last(EXCLUDED_ID)}")
        assert "crypto" in (_last(EXCLUDED_ID).get("reason") or ""), (
            f"T1: excluded reason missing keyword: {_last(EXCLUDED_ID)}")
        assert _last(BELOW_ID)["status"] == "skipped", (
            f"T1: below-threshold project not skipped: {_last(BELOW_ID)}")
        assert _last(BELOW_ID).get("reason") == "below threshold", (
            f"T1: wrong skip reason: {_last(BELOW_ID)}")
        assert len(drafted) >= 1 and all(r["project_id"] == QUALIFY_ID
                                         for r in drafted), (
            f"T1: unexpected drafted records: {drafted}")
        assert "1 drafted, 2 skipped" in summary, f"T1: bad summary {summary!r}"


def t2_message_id_matches_telegram() -> None:
    """The amended drafted record carries the message_id Telegram returned."""
    with _Patched() as patched, _hermetic_store() as tmp:
        sched = _make_scheduler(_config(), tmp)
        sched.run_scan_cycle()

        assert len(patched.telegram.sent_drafts) == 1, (
            f"T2: expected exactly 1 telegram draft, "
            f"got {len(patched.telegram.sent_drafts)}")
        payload = patched.telegram.sent_drafts[0]
        assert payload["project"].get("project_id") == QUALIFY_ID, (
            f"T2: telegram payload carries the wrong project: {payload['project']}")
        amended = [r for r in _for_pid(QUALIFY_ID) if r.get("message_id") is not None]
        assert amended, "T2: no drafted record carries message_id"
        assert amended[-1]["message_id"] == 1, (
            f"T2: message_id mismatch: {amended[-1]}")
        assert patched.freelancer.details == [QUALIFY_ID], (
            f"T2: details fetched for wrong projects: {patched.freelancer.details}")


def t3_draft_only_approve_never_bids() -> None:
    """DRAFT_ONLY=true: approve -> status 'approved', place_bid NEVER called."""
    with _Patched() as patched, _hermetic_store() as tmp:
        sched = _make_scheduler(_config(draft_only=True), tmp)
        sched.run_scan_cycle()          # drafts 501, registers in memory
        assert patched.freelancer.bids == [], "T3: place_bid called during scan"

        sched.run_poll_cycle()          # poll #1: empty (per mock contract)
        sched.run_poll_cycle()          # poll #2: approve 501

        assert _last(QUALIFY_ID)["status"] == "approved", (
            f"T3: expected approved, got {_last(QUALIFY_ID)}")
        assert patched.freelancer.bids == [], (
            f"T3: place_bid MUST NOT be called in DRAFT_ONLY: {patched.freelancer.bids}")
        statuses = [s for s in patched.telegram.sent_statuses
                    if s[0] == QUALIFY_ID and s[1] == "approved"]
        assert statuses, "T3: no 'approved' status ping sent to Telegram"


def t4_submit_flow_calls_place_bid_once() -> None:
    """DRAFT_ONLY=false + prior drafted record: approve -> submitted, one bid."""
    with _Patched() as patched, _hermetic_store() as tmp:
        sched = _make_scheduler(_config(draft_only=False), tmp)
        sched.run_scan_cycle()          # creates the prior drafted record
        assert patched.freelancer.bids == [], "T4: scan must not bid"

        sched.run_poll_cycle()          # poll #1: empty
        sched.run_poll_cycle()          # poll #2: approve 501 -> submit

        assert _last(QUALIFY_ID)["status"] == "submitted", (
            f"T4: expected submitted, got {_last(QUALIFY_ID)}")
        assert len(patched.freelancer.bids) == 1, (
            f"T4: place_bid must be called exactly once: {patched.freelancer.bids}")
        bid = patched.freelancer.bids[0]
        assert bid["project_id"] == QUALIFY_ID, f"T4: bid on wrong project {bid}"
        assert bid["bid_data"].get("amount") == 500, (
            f"T4: bid amount should be budget minimum 500: {bid}")
        assert bid["bid_data"].get("description"), "T4: bid carries no description"
        submitted_pings = [s for s in patched.telegram.sent_statuses
                           if s[0] == QUALIFY_ID and s[1] == "submitted"]
        assert submitted_pings, "T4: no 'submitted' ping to Telegram"


def t5_store_order_and_contents() -> None:
    """bids.jsonl holds the expected records in the expected order."""
    with _Patched(), _hermetic_store() as tmp:
        sched = _make_scheduler(_config(), tmp)
        sched.run_scan_cycle()

        got = [(r["project_id"], r["status"]) for r in _records()]
        expected = [
            (QUALIFY_ID, "drafted"),    # pre-send record (state BEFORE telegram)
            (QUALIFY_ID, "drafted"),    # amended with message_id after the send
            (EXCLUDED_ID, "skipped"),   # excluded keyword
            (BELOW_ID, "skipped"),      # below threshold
        ]
        assert got == expected, f"T5: record order mismatch\n  got      {got}\n  expected {expected}"
        assert _records()[1]["message_id"] == 1, "T5: amended record lacks message_id"


def t6_schema_validity() -> None:
    """Every record has valid ts (ISO 8601 with offset), int project_id, status."""
    valid_status = {"drafted", "skipped", "failed", "approved", "submitted", "rejected"}
    with _Patched() as patched, _hermetic_store() as tmp:
        sched = _make_scheduler(_config(), tmp)
        sched.run_scan_cycle()
        sched.run_poll_cycle()
        sched.run_poll_cycle()          # approve under DRAFT_ONLY -> 'approved'

        records = _records()
        assert records, "T6: no records to validate"
        for i, rec in enumerate(records):
            assert isinstance(rec.get("project_id"), int), (
                f"T6: record {i} project_id not int: {rec.get('project_id')!r}")
            assert rec.get("status") in valid_status, (
                f"T6: record {i} bad status {rec.get('status')!r}")
            ts = rec.get("ts")
            assert isinstance(ts, str) and ts, f"T6: record {i} missing ts"
            parsed = datetime.fromisoformat(ts)   # raises on malformed ts
            assert parsed.tzinfo is not None, (
                f"T6: record {i} ts lacks timezone offset: {ts}")
        # raw file level: every physical line is standalone-valid JSON and
        # the store round-trips exactly the same record count
        raw_lines = [ln for ln in (tmp / "bids.jsonl").read_text(encoding="utf-8")
                     .splitlines() if ln.strip()]
        assert len(raw_lines) == len(records), (
            f"T6: {len(raw_lines)} raw lines vs {len(records)} parsed records")
        for i, line in enumerate(raw_lines):
            json.loads(line)   # raises on a malformed line


# ---------------------------------------------------------------------- runner

TESTS = [
    ("T1 full scan: 1 drafted, 1 skipped (excluded), 1 skipped (below)",
     t1_full_scan_cycle),
    ("T2 drafted record message_id matches telegram return",
     t2_message_id_matches_telegram),
    ("T3 DRAFT_ONLY=true approve -> approved, place_bid never called",
     t3_draft_only_approve_never_bids),
    ("T4 DRAFT_ONLY=false + prior draft -> submitted, one place_bid",
     t4_submit_flow_calls_place_bid_once),
    ("T5 bids.jsonl contains all expected records in order",
     t5_store_order_and_contents),
    ("T6 every record has valid ts/project_id/status per schema",
     t6_schema_validity),
]


def main() -> int:
    print("FreelancerClaw — hermetic end-to-end integration test (Step 10a)")
    print("=" * 72)
    failures = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception as exc:  # noqa: BLE001 — a crash is a failure
            failures += 1
            print(f"FAIL  {name} (unexpected {type(exc).__name__}: {exc})")
            traceback.print_exc(file=sys.stderr)
    print("=" * 72)
    print(f"SUMMARY: {len(TESTS) - failures}/{len(TESTS)} passed"
          + ("" if failures == 0 else f", {failures} FAILED"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
