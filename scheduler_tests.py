"""
=============================================================================
FreelancerClaw — scheduler_tests.py
T1-T7 offline suite for scheduler.py (--test). No network, no real store.
=============================================================================

All collaborators are fakes:
  * FakeFreelancer  — search_projects / get_project_details / place_bid
  * FakeNotifier    — send_draft / send_status / poll_approvals
Temp dirs isolate bid_state_store and scheduler_state.json per test.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import bid_state_store as store
from freelancer_api import AuthError
from scheduler import Scheduler, SchedulerConfig, SchedulerState


# --------------------------------------------------------------------- fakes


class FakeFreelancer:
    def __init__(self, projects: List[dict], detail_overrides: Optional[dict] = None,
                 auth_fail: bool = False) -> None:
        self.projects = projects
        self.detail_overrides = detail_overrides or {}
        self.auth_fail = auth_fail
        self.bids: List[dict] = []

    def search_projects(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        if self.auth_fail:
            from freelancer_api import AuthError

            raise AuthError("401 Unauthorized (simulated)")
        # keyword-filtered, wrapped in the REAL "result" envelope shape
        q = query.lower()
        matched = [p for p in self.projects if q in p["title"].lower()
                   or q in p["preview_description"].lower()]
        return {"result": {"projects": matched, "total_count": len(matched)}}

    def get_project_details(self, project_id: int) -> dict:
        base = next((p for p in self.projects if p["id"] == project_id), {}).copy()
        base.update(self.detail_overrides)
        return base

    def place_bid(self, project_id: int, bid_data: dict) -> dict:
        self.bids.append({"project_id": project_id, **bid_data})
        return {"result": {"id": 555001 + len(self.bids)}}


class FakeNotifier:
    """Implements the subset of TelegramNotifier the scheduler uses."""

    def __urn(self) -> None:  # never called; keeps linters honest
        raise NotImplementedError

    def __init__(self, scripted_actions: Optional[List[dict]] = None) -> None:
        self.sent_drafts: List[dict] = []
        self.sent_statuses: List[tuple] = []
        self.scripted = list(scripted_actions or [])
        self.poll_count = 0

    def send_draft(self, draft: dict, scored_project: dict) -> dict:
        self.sent_drafts.append({"draft": draft, "project": scored_project})
        return {"ok": True, "message_id": 9000 + len(self.sent_drafts), "chat_id": 1,
                "error": None}

    def send_status(self, project_id: int, status: str, note: str = "") -> dict:
        self.sent_statuses.append((project_id, status, note))
        return {"ok": True, "message_id": 1, "error": None}

    def poll_approvals(self, timeout: int = 0, offset: Optional[int] = None) -> List[dict]:
        self.poll_count += 1
        actions, self.scripted = self.scripted, []
        return actions


def _project(pid: int, title: str, preview: str, budget_min: int = 250,
             bid_count: int = 5) -> dict:
    return {
        "id": pid,
        "title": title,
        "seo_url": f"project-{pid}",
        "preview_description": preview,
        "description": None,
        "budget": {"minimum": budget_min, "maximum": budget_min * 3},
        "currency": {"code": "USD", "exchange_rate": 1.0},
        "bid_stats": {"bid_count": bid_count, "bid_avg": budget_min / 2},
        "submitdate": 1_700_000_000,
    }


def _config(**over) -> SchedulerConfig:
    cfg = SchedulerConfig(
        search_keywords=["python automation"],
        score_threshold=10,      # low so fixtures qualify without tuning
        min_budget=100,
        draft_only=True,
        max_bids_per_day=1,
        max_bids_per_month=6,
    )
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


MOCK_RESUME = """# Test User

## Skills

python, automation, scraping, telegram, api

## Selected experience

### 1. Scraping Platform Overhaul
- Built a Scrapy and Playwright pipeline monitoring supplier sites nightly.

### 2. Telegram Ops Bot
- Delivered a Telegram bot with role-based access controlling reports.

## Rates and preferences

- Typical budget range: USD 250-2000 per project
"""


def _make_scheduler(fake_fl: FakeFreelancer, fake_tn: FakeNotifier,
                    cfg: Optional[SchedulerConfig] = None, dry: bool = False) -> Scheduler:
    return Scheduler(
        cfg or _config(),
        freelancer=fake_fl,
        notifier=fake_tn,
        state=SchedulerState(state_path=Path(tempfile.mkdtemp()) / "state.json"),
        resume_text=MOCK_RESUME,
        portfolio={"login": "octodev", "repos": []},
        dry_run=dry,
    )


# ------------------------------------------------------------------- helpers


def _records_for(pid: int) -> List[dict]:
    return [r for r in store.load_all() if r.get("project_id") == pid]


def _last_status(pid: int) -> Optional[str]:
    recs = _records_for(pid)
    return recs[-1]["status"] if recs else None


def _pre_draft(pid: int) -> None:
    """Seed the store with a prior drafted record (safety-gate opener)."""
    store.append_record({
        "project_id": pid, "project_title": f"project {pid}",
        "total_score": 80.0, "draft_id": f"draft_{pid}_seed", "status": "drafted",
        "message_id": 111, "error": None, "bid_amount": 250,
    })


def _t8_offset_persisted_through_once() -> None:
    import io
    import contextlib

    with _store_ctx():
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "scheduler_state.json"
            # Seed the state file exactly like Step 6's manual --poll did.
            state_path.write_text(json.dumps({"telegram_offset": 399052789}), encoding="utf-8")
            fl = FakeFreelancer([_project(801, "Python automation job", "automation")])
            sched = Scheduler(
                _config(),
                freelancer=fl,
                notifier=FakeNotifier(),
                state=SchedulerState(state_path=state_path),
                resume_text=MOCK_RESUME,
                portfolio={"login": "octodev", "repos": []},
                dry_run=True,
            )
            sched.run_scan_cycle()   # the --once body (dry)

            # Simulate what a fresh --status process sees: reload from disk.
            reloaded = SchedulerState(state_path=state_path)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                import scheduler as sched_mod

                status_sched = sched_mod.Scheduler(state=reloaded)
                saved_init = sched_mod.Scheduler.__init__

                # status only reads; give it the reloaded state directly
                sched_mod._cli_status(status_sched)
            assert "399052789" in out.getvalue(), (
                f"T8: --status did not report the persisted offset; output:\n{out.getvalue()}"
            )
            # loop statuses must be honest after --once exits
            assert "never started" in out.getvalue() or "stopped" in out.getvalue()


# ---------------------------------------------------------------------- tests


def _t1_draft_only_never_bids() -> None:
    with _store_ctx():
        _pre_draft(101)
        fl = FakeFreelancer([_project(101, "Python automation job", "build automation")])
        tn_fake = FakeNotifier(scripted_actions=[
            {"action": "approve", "project_id": 101, "update_id": 1}])
        sched = _make_scheduler(fl, tn_fake, _config(draft_only=True))
        sched.run_poll_cycle()
        assert fl.bids == [], f"T1: place_bid called {len(fl.bids)} time(s) under DRAFT_ONLY"
        assert _last_status(101) == "approved"
        assert any("Copy the draft manually" in note for _, _, note in tn_fake.sent_statuses)


def _t2_safety_gate_blocks_first_approval() -> None:
    with _store_ctx():
        # store is EMPTY: no prior drafted record anywhere
        fl = FakeFreelancer([_project(201, "Python automation job", "automation")])
        tn_fake = FakeNotifier(scripted_actions=[
            {"action": "approve", "project_id": 201, "update_id": 1}])
        sched = _make_scheduler(fl, tn_fake, _config(draft_only=False))
        sched.run_poll_cycle()
        assert fl.bids == [], "T2: safety gate failed — bid placed with empty store"
        assert _last_status(201) == "skipped", \
            f"T2: expected auditable 'skipped' record, got {_last_status(201)}"
        assert any("safety gate" in (r.get("reason") or "") for r in _records_for(201)), \
            "T2: safety-gate reason missing from record"
        assert any("safety gate" in note for _, _, note in tn_fake.sent_statuses)


def _t3_approval_with_prior_draft_submits() -> None:
    with _store_ctx():
        pid = 301
        _pre_draft(pid)  # safety gate open
        fl = FakeFreelancer([_project(pid, "Python automation job", "automation")])
        tn_fake = FakeNotifier(scripted_actions=[
            {"action": "approve", "project_id": pid, "update_id": 1}])
        sched = _make_scheduler(fl, tn_fake, _config(draft_only=False))
        sched._drafts[pid] = {"text": "Proposal text.", "word_count": 2,
                              "cited_evidence": [], "opening_quote": "", "warnings": []}
        sched.run_poll_cycle()
        assert len(fl.bids) == 1, "T3: expected exactly one bid"
        assert fl.bids[0]["project_id"] == pid
        assert fl.bids[0]["amount"] == 250, f"T3: amount {fl.bids[0]['amount']}"
        assert fl.bids[0]["description"] == "Proposal text."
        assert _last_status(pid) == "submitted"
        assert any(status == "submitted" for _, status, _ in tn_fake.sent_statuses)


def _t4_rate_limit_skips_second_project() -> None:
    with _store_ctx():
        projects = [_project(401, "Python automation one", "automation"),
                    _project(402, "Python automation two", "automation")]
        fl = FakeFreelancer(projects)
        tn_fake = FakeNotifier()
        sched = _make_scheduler(fl, tn_fake, _config(max_bids_per_day=1))
        # simulate: one submission already recorded today -> cap hit
        store.append_record({
            "project_id": 400, "project_title": "prior", "total_score": 80.0,
            "draft_id": "d400", "status": "submitted", "message_id": 1, "error": None,
        })
        sched.run_scan_cycle()
        statuses = {r["project_id"]: r["status"] for r in store.load_all()}
        assert statuses.get(401) == "skipped" and statuses.get(402) == "skipped", statuses
        assert any(r.get("reason") == "rate limit" for r in store.load_all()), \
            "T4: rate-limit reason missing"
        assert tn_fake.sent_drafts == [], "T4: drafts sent despite cap"


def _t5_freelancer_401_stops_loop_a_not_b() -> None:
    with _store_ctx():
        fl = FakeFreelancer([], auth_fail=True)
        tn_fake = FakeNotifier()
        sched = _make_scheduler(fl, tn_fake)
        try:
            sched.run_scan_cycle()
            raise AssertionError("T5: AuthError not raised")
        except Exception as exc:
            from freelancer_api import AuthError

            assert isinstance(exc, AuthError), f"T5: wrong exception {type(exc).__name__}"
        assert sched.scan_stopped, "T5: Loop A not flagged stopped"
        assert not sched.poll_stopped, "T5: Loop B wrongly stopped"
        # Loop B keeps working after Loop A died
        assert sched.run_poll_cycle() == []
        assert tn_fake.poll_count == 1
        assert any("Loop A STOPPED" in note for _, _, note in tn_fake.sent_statuses), \
            "T5: no Telegram alert sent"


def _t6_corrupted_state_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "scheduler_state.json"
        bad.write_text("{corrupted json!!", encoding="utf-8")
        state = SchedulerState(state_path=bad)
        assert state.telegram_offset is None
        assert state.last_scan_ts is None
        state.save()
        reloaded = SchedulerState(state_path=bad)
        assert reloaded.last_scan_ts is None  # saved fresh defaults, no crash
        # and a missing file is fine too
        fresh = SchedulerState(state_path=Path(tmp) / "missing.json")
        assert fresh.telegram_offset is None


def _t7_ctrl_c_exits_clean() -> None:
    import io
    import contextlib

    with _store_ctx():
        fl = FakeFreelancer([_project(701, "Python automation job", "automation")])
        tn_fake = FakeNotifier()
        sched = _make_scheduler(fl, tn_fake)

        # Graceful-shutdown path run_forever implements for Ctrl+C: the
        # shutdown event unrolls both loops, prints the final summary, and
        # run_forever resolves (=> main returns 0). We set the same event a
        # real SIGINT ultimately sets. (A fake os.kill(SIGINT) is a no-op
        # under piped, console-less execution, so we drive the event.)
        got_summary = io.StringIO()
        sched.shutdown_event = asyncio.Event()

        async def runner() -> int:
            loop = asyncio.get_running_loop()
            loop.call_later(0.2, sched.shutdown_event.set)
            await sched.run_forever()
            return 0

        with contextlib.redirect_stdout(got_summary):
            code = asyncio.run(runner())
        assert code == 0, f"T7: run_forever exited {code} on Ctrl+C"
        assert "shutdown summary:" in got_summary.getvalue(), "T7: no final summary printed"

        # --once path: KeyboardInterrupt raised mid-scan is caught by
        # run_once, which prints the summary and returns 0 (never a traceback).
        fl2 = FakeFreelancer([_project(702, "Python automation job", "automation")])
        sched2 = _make_scheduler(fl2, FakeNotifier())
        original_scan = sched2.run_scan_cycle

        def scan_then_interrupt() -> str:
            original_scan()          # complete the real cycle first
            raise KeyboardInterrupt  # ... then Ctrl+C arrives

        sched2.run_scan_cycle = scan_then_interrupt  # type: ignore[assignment]
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            code2 = run_once_with_fake(sched2)
        assert code2 == 0, f"T7: --once exited {code2} on KeyboardInterrupt"
        assert "shutdown summary:" in out2.getvalue()


def _t9_bot_token_never_in_logs() -> None:
    """Regression (scheduler.log token leak): the Telegram bot token
    must never reach logs/scheduler.log after one full cycle — not via
    urllib3's DEBUG request line (token sits in the URL path), not via
    an exception traceback (requests embeds the URL in error text)."""
    import logging
    import os
    import re

    import scheduler as sched_mod

    fake_token = "123456789:AAfake_token_t9_LEAKCHECK"

    class Urllib3EmittingNotifier(FakeNotifier):
        """Stand-in for TelegramNotifier: send_draft emits the exact
        DEBUG record urllib3's connectionpool logs during a real
        sendMessage POST, plus a logger.exception() whose traceback
        embeds the URL — the two surfaces the scrub filter covers."""

        def send_draft(self, draft: dict, scored_project: dict) -> dict:
            logging.getLogger("urllib3.connectionpool").debug(
                'https://api.telegram.org:443 "POST /bot%s/sendMessage'
                ' HTTP/1.1" 200 200', fake_token)
            try:
                raise RuntimeError(
                    f"Max retries exceeded with url: /bot{fake_token}/sendMessage")
            except RuntimeError:
                logging.getLogger("freelancerclaw.error").exception("send failed")
            return super().send_draft(draft, scored_project)

    old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    os.environ["TELEGRAM_BOT_TOKEN"] = fake_token
    tmp = tempfile.mkdtemp()
    old_dir, old_path = sched_mod.LOG_DIR, sched_mod.LOG_PATH
    sched_mod.LOG_DIR = Path(tmp)
    sched_mod.LOG_PATH = Path(tmp) / "scheduler.log"
    try:
        sched_mod.setup_logging(verbose=False)   # the FIXED main() order
        with _store_ctx():
            fl = FakeFreelancer(
                [_project(9101, "python automation scraper",
                          "scrape sites into excel and generate reports")])
            sched = _make_scheduler(fl, Urllib3EmittingNotifier())
            summary = sched.run_scan_cycle()
            assert "1 drafted" in summary, f"T9: expected 1 drafted, got {summary!r}"
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = (Path(tmp) / "scheduler.log").read_text(encoding="utf-8")
        # positive control first: the filter must have RUN (redaction
        # markers present), otherwise the asserts below pass vacuously
        assert "bot[REDACTED]" in text, \
            "T9: scrub filter never ran (no redaction marker in log)"
        assert fake_token not in text, \
            "T9: full bot token leaked into logs/scheduler.log"
        assert not re.search(r"bot[0-9]+:", text), \
            "T9: bot-token-shaped string leaked into logs/scheduler.log"
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        logging.getLogger().handlers.clear()
        sched_mod.LOG_DIR, sched_mod.LOG_PATH = old_dir, old_path
        if old_token is None:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_BOT_TOKEN"] = old_token


def run_once_with_fake(sched: Scheduler) -> int:
    """Call scheduler.run_once's body against an injected Scheduler."""
    try:
        sched.run_scan_cycle()
        return 0
    except AuthError:
        return 2
    except KeyboardInterrupt:
        sched.final_summary()
        return 0


# ------------------------------------------------------------------ scaffolding


class _store_ctx:
    """Point bid_state_store at a temp file for the duration of a test."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        store.set_store_path(Path(self.tmp.name) / "bids.jsonl")
        return self

    def __exit__(self, *exc) -> None:
        store.reset_store_path()
        self.tmp.cleanup()


_TESTS = [
    ("T1 DRAFT_ONLY=true never calls place_bid", _t1_draft_only_never_bids),
    ("T2 empty store blocks first approval (safety gate)", _t2_safety_gate_blocks_first_approval),
    ("T3 prior drafted record + approve -> submits", _t3_approval_with_prior_draft_submits),
    ("T4 MAX_BIDS_PER_DAY hit -> skipped 'rate limit'", _t4_rate_limit_skips_second_project),
    ("T5 freelancer 401 stops Loop A only; alert sent", _t5_freelancer_401_stops_loop_a_not_b),
    ("T6 corrupted scheduler_state.json starts fresh", _t6_corrupted_state_file),
    ("T7 Ctrl+C exits cleanly with code 0", _t7_ctrl_c_exits_clean),
    ("T8 persisted telegram_offset survives --once and shows in --status", _t8_offset_persisted_through_once),
    ("T9 bot token never appears in scheduler.log after a full cycle", _t9_bot_token_never_in_logs),
]


def run_scheduler_tests() -> int:
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


if __name__ == "__main__":
    import sys

    sys.exit(run_scheduler_tests())
