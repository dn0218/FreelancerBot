"""
=============================================================================
FreelancerClaw — Module 6: bid_state_store.py
Append-only JSONL bid state store. Pure stdlib. No network. No third-party
dependencies. Windows-safe.
=============================================================================

Design invariants
------------------
  * data/bids.jsonl is APPEND-ONLY. Status changes are NEW records with the
    same project_id; the original "drafted" record is never mutated. The
    file is the audit log — history cannot be rewritten.
  * One JSON object per line, sort_keys=True, no trailing whitespace.
  * ts is ISO 8601 WITH local timezone offset (e.g. 2026-10-07T14:23:11+08:00).
  * Validated writes: bad records raise ValueError at the door.
  * Tolerant reads: a malformed line logs a warning (line number + first
    80 chars) and is skipped; one bad line never crashes load_all().

Record schema
-------------
  {"draft_id": "...", "error": null, "message_id": 12345,
   "project_id": 40754444, "project_title": "...", "status": "drafted",
   "total_score": 47.0, "ts": "2026-10-07T14:23:11+08:00"}

Public API
----------
  append_record(record)                    validate + append one record
  load_all()                               oldest-first list of records
  get_processed_project_ids()              set of project_ids ever recorded
  update_status(project_id, new_status, error=None)  append a new record
  count_today(status=None) / count_this_month(status=None)  quota helpers
  get_last(n=10)                           newest-first tail
  set_store_path / reset_store_path        test/ops path override

CLI
---
  python bid_state_store.py --test    mock suite T1-T8 (temp dir, hermetic)
  python bid_state_store.py --tail N  last N records, JSON, one per line
  python bid_state_store.py --count   per-status counts for today + month
  python bid_state_store.py --path    resolved absolute path of bids.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

logger = logging.getLogger("freelancerclaw.store")

# =============================================================================
# SECTION 1: Constants and paths
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_PATH = PROJECT_ROOT / "data" / "bids.jsonl"

ALLOWED_STATUSES = {"drafted", "approved", "rejected", "submitted", "failed", "skipped"}

# Module-level override: tests point this at a temp dir. All production
# code goes through _store_path() so the override is honored everywhere.
_store_path_override: Optional[Path] = None


def set_store_path(path: Union[str, Path]) -> None:
    """Point the store at `path` (tests use a TemporaryDirectory)."""
    global _store_path_override
    _store_path_override = Path(path)


def reset_store_path() -> None:
    """Restore the default production path."""
    global _store_path_override
    _store_path_override = None


def _store_path() -> Path:
    return _store_path_override if _store_path_override is not None else DEFAULT_STORE_PATH


def get_store_path() -> Path:
    """Public: the resolved absolute path (CLI --path)."""
    return _store_path().resolve()


# =============================================================================
# SECTION 2: Validation
# =============================================================================


def _validate_record(record: dict) -> None:
    """Raise ValueError on any rule violation. Never partially writes."""
    if not isinstance(record, dict):
        raise ValueError("record must be a dict")

    project_id = record.get("project_id")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        raise ValueError(
            f"project_id must be int, got {type(project_id).__name__} ({project_id!r})"
        )

    status = record.get("status")
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(ALLOWED_STATUSES)}, got {status!r}"
        )

    total_score = record.get("total_score")
    if total_score is not None:
        if isinstance(total_score, bool) or not isinstance(total_score, (int, float)):
            raise ValueError(
                "total_score must be a number 0-100 or None, "
                f"got {type(total_score).__name__}"
            )
        if not (0 <= total_score <= 100):
            raise ValueError(f"total_score must be 0-100, got {total_score}")

    # Optional-but-typed fields: missing is fine (e.g. no Telegram message).
    if record.get("message_id") is not None and not isinstance(record["message_id"], int):
        raise ValueError(f"message_id must be int or null, got {record['message_id']!r}")
    if record.get("error") is not None and not isinstance(record["error"], str):
        raise ValueError(f"error must be str or null, got {record['error']!r}")


def _now_iso() -> str:
    """ISO 8601 with local timezone offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# =============================================================================
# SECTION 3: Core operations
# =============================================================================


def append_record(record: dict) -> None:
    """
    Validate and append one record to bids.jsonl. Adds "ts" (ISO 8601,
    local tz) if missing. Creates data/ if needed. Flushes immediately.
    Raises ValueError on invalid input — nothing is written.
    """
    _validate_record(record)
    record = dict(record)  # never mutate the caller's dict
    record.setdefault("ts", _now_iso())

    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # survive abrupt shutdown / power loss


def load_all() -> List[dict]:
    """
    Read every record, oldest first. Blank lines are skipped. A malformed
    line logs a warning (line number + first 80 chars) and is skipped —
    one bad line never crashes the read.
    """
    path = _store_path()
    records: List[dict] = []
    if not path.exists():
        return records
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    raise ValueError("not a JSON object")
            except ValueError as exc:
                logger.warning(
                    "bids.jsonl:%d malformed line skipped (%s): %.80s",
                    line_no, exc, stripped,
                )
                continue
            records.append(record)
    return records


def get_processed_project_ids() -> Set[int]:
    """project_ids with at least one record — scheduler's dedupe set."""
    ids: Set[int] = set()
    for record in load_all():
        pid = record.get("project_id")
        if isinstance(pid, int) and not isinstance(pid, bool):
            ids.add(pid)
    return ids


def update_status(project_id: int, new_status: str, error: Optional[str] = None) -> dict:
    """
    Append a NEW record for an existing project (append-only invariant:
    the original "drafted" line stays). Returns the new record.
    Raises ValueError if the project has no prior record.
    """
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        raise ValueError(f"project_id must be int, got {project_id!r}")
    if new_status not in ALLOWED_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(ALLOWED_STATUSES)}, got {new_status!r}"
        )

    prior = [r for r in load_all() if r.get("project_id") == project_id]
    if not prior:
        raise ValueError(
            f"project_id {project_id} has no prior record; cannot transition to "
            f"{new_status!r} (you can't approve something that was never drafted)"
        )

    last = prior[-1]
    new_record: Dict[str, Any] = {
        "project_id": project_id,
        "status": new_status,
        "error": error,
        "ts": _now_iso(),
    }
    # Carry identity fields forward so each line stands alone.
    for key in ("project_title", "draft_id", "total_score"):
        if key in last:
            new_record[key] = last[key]
    if "message_id" in last and new_status in ("approved", "rejected"):
        new_record["message_id"] = last["message_id"]

    append_record(new_record)
    return new_record


# =============================================================================
# SECTION 4: Time-window counters (quota enforcement)
# =============================================================================


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a record ts; None for missing/invalid (never counted)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def count_today(status: Optional[str] = None) -> int:
    """Records whose ts falls on the current local date (optionally by status)."""
    today = datetime.now().astimezone().date()
    count = 0
    for record in load_all():
        if status is not None and record.get("status") != status:
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is not None and ts.astimezone().date() == today:
            count += 1
    return count


def count_this_month(status: Optional[str] = None) -> int:
    """Records whose ts falls in the current calendar month (local time)."""
    now = datetime.now().astimezone()
    count = 0
    for record in load_all():
        if status is not None and record.get("status") != status:
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        local = ts.astimezone()
        if local.year == now.year and local.month == now.month:
            count += 1
    return count


def get_last(n: int = 10) -> List[dict]:
    """Last n records, newest first."""
    if n <= 0:
        return []
    return list(reversed(load_all()[-n:]))


# =============================================================================
# SECTION 5: CLI
# =============================================================================


def _cli_counts() -> int:
    print(f"store: {get_store_path()}")
    print(f"\ntoday:      {count_today()} record(s)")
    print(f"this month: {count_this_month()} record(s)")
    print("\nper status:")
    for status in sorted(ALLOWED_STATUSES):
        print(f"  {status:<10} today={count_today(status):<4} month={count_this_month(status)}")
    print(f"\nprocessed project ids: {len(get_processed_project_ids())}")
    return 0


def _cli_tail(n: int) -> int:
    records = get_last(n)
    if not records:
        print(f"(no records in {get_store_path()})")
        return 0
    for record in records:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bid_state_store.py",
        description="FreelancerClaw append-only bid state store.",
    )
    parser.add_argument("--test", action="store_true", help="run mock suite T1-T8 (temp dir)")
    parser.add_argument("--tail", type=int, metavar="N",
                        help="print the last N records (newest first), one JSON per line")
    parser.add_argument("--count", action="store_true",
                        help="per-status counts for today and this month")
    parser.add_argument("--path", action="store_true",
                        help="print the resolved absolute store path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.test:
        return run_tests()
    if args.tail is not None:
        return _cli_tail(args.tail)
    if args.count:
        return _cli_counts()
    if args.path:
        print(get_store_path())
        return 0

    parser.print_help()
    return 0


# =============================================================================
# SECTION 6: Mock tests T1-T8 (temporary directory only — never data/)
# =============================================================================


def run_tests() -> int:
    """T1-T8, each against a fresh TemporaryDirectory store. 0 == all pass."""
    results: List[tuple] = []

    def register(name, fn):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                set_store_path(Path(tmp) / "data" / "bids.jsonl")
                try:
                    fn()
                finally:
                    reset_store_path()
            print(f"PASS  {name}")
            results.append((name, True, None))
        except AssertionError as exc:
            print(f"FAIL  {name}: {exc}")
            results.append((name, False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: unexpected {type(exc).__name__}: {exc}")
            results.append((name, False, f"unexpected {type(exc).__name__}: {exc}"))

    register("T1 append_record writes one line; load_all returns it", _t1)
    register("T2 five appends produce five clean lines", _t2)
    register("T3 update_status appends; two ordered records", _t3)
    register("T4 update_status on unknown project raises ValueError", _t4)
    register("T5 malformed middle line skipped by load_all", _t5)
    register("T6 count_today / count_this_month boundaries", _t6)
    register("T7 get_processed_project_ids dedupes", _t7)
    register("T8 invalid status rejected; file untouched", _t8)

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} passed"
          + ("" if not failed else f", {len(failed)} FAILED"))
    return 0 if not failed else 1


def _base_record(**over) -> dict:
    record = {
        "project_id": 40754444,
        "project_title": "Automated Trading System Development in Python",
        "total_score": 47.0,
        "draft_id": "draft_40754444_1730904191",
        "status": "drafted",
        "message_id": 12345,
        "error": None,
    }
    record.update(over)
    return record


def _lines() -> List[str]:
    path = _store_path()
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh]


def _t1():
    append_record(_base_record())
    lines = _lines()
    assert len(lines) == 1, f"T1: expected 1 line, got {len(lines)}"
    raw = open(_store_path(), "rb").read()
    assert raw.endswith(b"\n"), "T1: line must end with a newline"
    assert raw.decode("utf-8").splitlines()[0] == lines[0], "T1: encoding round-trip"
    loaded = load_all()
    assert len(loaded) == 1 and loaded[0]["project_id"] == 40754444
    assert loaded[0]["status"] == "drafted"
    assert "ts" in loaded[0] and loaded[0]["ts"].count("+") + loaded[0]["ts"].count("-") > 0, \
        "T1: ts added with local timezone offset"


def _t2():
    for i in range(5):
        append_record(_base_record(project_id=1000 + i, message_id=2000 + i))
    lines = _lines()
    assert len(lines) == 5, f"T2: expected 5 lines, got {len(lines)}"
    parsed = [json.loads(ln) for ln in lines]  # every line must parse
    assert [p["project_id"] for p in parsed] == [1000, 1001, 1002, 1003, 1004]
    assert all(ln == ln.strip() for ln in lines), "T2: no trailing whitespace"
    loaded = load_all()
    assert len(loaded) == 5 and loaded[0]["project_id"] == 1000  # oldest first


def _t3():
    append_record(_base_record())  # drafted
    new = update_status(40754444, "approved")
    assert new["status"] == "approved" and new["project_id"] == 40754444
    loaded = load_all()
    assert len(loaded) == 2, f"T3: expected 2 records, got {len(loaded)}"
    assert loaded[0]["status"] == "drafted" and loaded[1]["status"] == "approved"
    assert loaded[0]["draft_id"] == loaded[1]["draft_id"], "T3: draft_id carried forward"
    assert loaded[1]["message_id"] == 12345, "T3: message_id carried forward on decision"
    assert loaded[0]["ts"] <= loaded[1]["ts"], "T3: append-only order"


def _t4():
    append_record(_base_record(project_id=1))
    try:
        update_status(999999, "approved")
        raise AssertionError("T4: ValueError not raised")
    except ValueError as exc:
        assert "no prior record" in str(exc), f"T4: unclear message: {exc}"


def _t5():
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_base_record(project_id=1), sort_keys=True) + "\n")
        fh.write('{"broken": tru' + "x" * 90 + "\n")  # garbage, >80 chars
        fh.write("\n")  # blank line
        fh.write(json.dumps(_base_record(project_id=2), sort_keys=True) + "\n")
    loaded = load_all()
    assert [r["project_id"] for r in loaded] == [1, 2], \
        f"T5: bad line disrupted load: {[r.get('project_id') for r in loaded]}"


def _t6():
    # Fixed ts fixtures (no reliance on now() inside the records).
    now = datetime.now().astimezone()
    today_same = now.isoformat(timespec="seconds")
    yesterday = (now - timedelta(days=1)).isoformat(timespec="seconds")
    far_last_month = (now - timedelta(days=32)).isoformat(timespec="seconds")

    # Guard: a "32 days ago" fixture can collide with the current month when
    # today is the 1st-4th; on collision re-derive it as (first of this
    # month - 1 day) so it is genuinely outside the month.
    if far_last_month[:7] == today_same[:7]:
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        far_last_month = (first - timedelta(days=1)).isoformat(timespec="seconds")
    # Same guard for "yesterday" vs today (matters only at month boundaries
    # for the MONTH counter; the DAY counter needs it to be a different date).
    if yesterday[:10] == today_same[:10]:  # impossible, but stay safe
        yesterday = (now - timedelta(days=1)).isoformat(timespec="seconds")

    append_record(_base_record(project_id=1, ts=today_same, status="drafted"))
    append_record(_base_record(project_id=2, ts=yesterday, status="drafted"))
    append_record(_base_record(project_id=3, ts=far_last_month, status="drafted"))
    append_record(_base_record(project_id=4, ts=today_same, status="submitted"))
    append_record(_base_record(project_id=5, ts=None, status="drafted"))  # uncounted
    append_record(_base_record(project_id=6, ts=yesterday, status="submitted"))

    # expectations derived from the fixtures, so month boundaries can't break them
    yesterday_in_month = yesterday[:7] == today_same[:7]
    exp_month = 2 + (2 if yesterday_in_month else 0)

    assert count_today() == 2, f"T6: count_today={count_today()}"
    assert count_today("submitted") == 1, "T6: today submitted"
    assert count_today("drafted") == 1, "T6: today drafted"
    assert count_this_month() == exp_month, \
        f"T6: count_this_month={count_this_month()}, expected {exp_month}"
    assert count_this_month("submitted") == (2 if yesterday_in_month else 1)
    assert count_this_month("drafted") == (2 if yesterday_in_month else 1)


def _t7():
    append_record(_base_record(project_id=42))
    update_status(42, "approved")
    update_status(42, "submitted")
    ids = get_processed_project_ids()
    assert ids == {42}, f"T7: expected {{42}}, got {ids}"
    assert isinstance(ids, set)


def _t8():
    append_record(_base_record(project_id=7))
    before = len(_lines())
    for bad in ("nope", 123, None, "drafted "):
        try:
            append_record(_base_record(project_id=8, status=bad))
            raise AssertionError(f"T8: status {bad!r} accepted")
        except ValueError:
            pass
    assert len(_lines()) == before, "T8: file modified by invalid append"


if __name__ == "__main__":
    import sys

    sys.exit(main())
