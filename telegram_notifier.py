"""
=============================================================================
FreelancerClaw — Module 5: telegram_notifier.py
Draft delivery + approval collection via the Telegram Bot API.
=============================================================================

Dependency decision (locked at Step 1 review): raw requests to
https://api.telegram.org — NO python-telegram-bot. We use four Bot API
methods: sendMessage, editMessageText, getUpdates, answerCallbackQuery.

Config
------
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    * inside OpenWebUI: Valves > environment (Tool passes them in)
    * standalone CLI:   environment > .env (python-dotenv, override=False),
      loaded in the CLI entry point — NEVER at import time.

Public surface
--------------
  send_draft(draft, scored_project) -> {ok, message_id, chat_id, error[, message_ids]}
      Exact System-Prompt format, inline Approve/Reject keyboard
      (callback_data "approve:<id>" / "reject:<id>"), auto-split at 4000
      chars with the keyboard on the LAST message only.
  send_status(project_id, status, note="") -> dict
      Short "[STATUS] Project <id> -> <status>" line, no keyboard.
  edit_message_status(message_id, new_status_line) -> dict
      editMessageText prepending the status line (history-friendly);
      benign failures (deleted / not modified) are swallowed.
  poll_approvals(timeout=0, offset=None) -> [{action, project_id, update_id}]
      getUpdates long-poll (timeout clamped to <=30), parses BOTH text
      replies ^(approve|reject)\\s+(\\d+)$ AND callback_query data
      ^(approve|reject):(\\d+)$, answers callbacks (stops the spinner).
      Never places bids or touches the store — that is scheduler's job.

Security & errors
-----------------
  * TELEGRAM_BOT_TOKEN redacted in every log line and exception message
    (same _scrub / redact_token / log-filter pattern as freelancer_api.py).
  * missing creds -> human-readable stderr message, exit 1, no traceback
  * 401 -> "bot token invalid or revoked; regenerate via @BotFather", exit 2
  * 429 -> read Retry-After (body parameter or header), wait ONCE, retry
    once; a second 429 raises (never loops)
  * 400 -> "TELEGRAM_CHAT_ID is wrong or you haven't messaged the bot yet",
    exit 3
  * nulls render as "unknown" — never fabricated

CLI
---
  python telegram_notifier.py --self         getMe -> bot username + id
  python telegram_notifier.py --test         live test draft (project_id=0)
  python telegram_notifier.py --test-buttons live keyboard test (tap once)
  python telegram_notifier.py --poll         one poll cycle (timeout=10)
  python telegram_notifier.py --offline      mock tests T1-T6, no network

Exit codes: 0 ok | 1 missing creds | 2 bad token | 3 bad chat id |
            4 rate limited | 5 other API error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# =============================================================================
# SECTION 1: Constants & configuration
# =============================================================================

TELEGRAM_API_BASE = "https://api.telegram.org"

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

# Telegram hard limit is 4096; the spec mandates splitting at 4000.
MESSAGE_CHAR_LIMIT = 4000
POLL_TIMEOUT_CAP = 30

USER_AGENT = "FreelancerClaw/0.1 (draft notifier)"

PROJECT_ROOT = Path(__file__).resolve().parent
MESSAGE_CACHE_PATH = PROJECT_ROOT / "cache" / "telegram_messages.json"

logger = logging.getLogger("freelancerclaw.telegram")

_REPLY_FOOTER = 'Reply "approve {pid}" to submit, or "reject {pid}" to discard.'

_TEXT_CMD_RE = re.compile(r"^(approve|reject)\s+(\d+)$", re.IGNORECASE)
_CALLBACK_CMD_RE = re.compile(r"^(approve|reject):(\d+)$", re.IGNORECASE)

# Error substrings that mean "nothing to edit" — benign, never raised.
_BENIGN_EDIT_ERRORS = ("message to edit not found", "message is not modified", "message to edit not found")


# =============================================================================
# SECTION 2: Exceptions (with CLI exit codes attached)
# =============================================================================


class TelegramError(Exception):
    """Base class; .exit_code is what the CLI should return."""

    exit_code = 5


class TelegramMissingCredentials(TelegramError):
    exit_code = 1


class TelegramAuthError(TelegramError):
    """401 from Telegram."""

    exit_code = 2


class TelegramChatError(TelegramError):
    """400 'chat not found' family."""

    exit_code = 3


class TelegramRateLimited(TelegramError):
    """429 persisted after the single Retry-After wait."""

    exit_code = 4

    def __init__(self, message: str, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# =============================================================================
# SECTION 3: Token hygiene — identical pattern to freelancer_api.py
# =============================================================================


def redact_token(token: Optional[str]) -> str:
    if not token:
        return "(no token set)"
    return "..." + token[-4:] if len(token) >= 4 else "...(too short)"


# Shape of a Telegram bot token as it appears inside request URLs
# ("https://api.telegram.org/bot<token>/sendMessage"). Deliberately
# broad: better one false positive than one leaked credential — this
# also catches old/rotated tokens the exact match below cannot know.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{15,}")


def _scrub(text: Any, token: Optional[str]) -> str:
    s = str(text)
    if token:
        s = s.replace(token, "[REDACTED]")
    # Second, shape-based pass (see _BOT_TOKEN_RE): redacts ANY
    # bot-token-looking substring, not just the configured one.
    return _BOT_TOKEN_RE.sub("bot[REDACTED]", s)


class _TokenScrubFilter(logging.Filter):
    """Scrub the bot token from any log record, whoever emitted it.

    Two surfaces are scrubbed:
      * the formatted message (msg + args), and
      * exception tracebacks (exc_info/exc_text/stack_info) — the
        HANDLER formats tracebacks AFTER this filter runs, so a
        message-only scrub misses tokens embedded in exception text
        (requests embeds the request path /bot<token>/... in its
        ConnectionError messages; logger.exception() would bypass).
        We pre-format and scrub the traceback here, store it in
        record.exc_text and clear exc_info so the handler reuses the
        scrubbed text verbatim instead of re-rendering it.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        scrubbed = _scrub(msg, self._token)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = None
        if record.exc_info:
            record.exc_text = _scrub(
                "".join(traceback.format_exception(*record.exc_info)),
                self._token,
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _scrub(record.exc_text, self._token)
        if record.stack_info:
            record.stack_info = _scrub(record.stack_info, self._token)
        return True


def install_token_scrub(token: Optional[str]) -> None:
    """
    Attach _TokenScrubFilter to EVERY current root handler (idempotent).

    Why this exists: Telegram embeds the bot token in the request PATH
    (https://api.telegram.org/bot<TOKEN>/sendMessage), and urllib3 logs the
    full request line at DEBUG on the 'urllib3.connectionpool' logger:

        https://api.telegram.org:443 "POST /bot<TOKEN>/sendMessage HTTP/1.1" 200 200

    Any host app that runs root logging at DEBUG (scheduler.py does) captures
    those lines into its log file UNLESS a token-scrubbing filter sits on the
    root handlers themselves. Scrubbing only this module's own logger (the
    old main()-only install) does not cover urllib3's records. The
    filter also scrubs exception tracebacks (exc_info), which handlers
    format AFTER filters run — requests embeds /bot<token>/... in its
    ConnectionError text, so logger.exception() would otherwise leak
    through message-only scrubbing.
    """
    if not token:
        return
    fresh = _TokenScrubFilter(token)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _TokenScrubFilter) for f in handler.filters):
            handler.addFilter(fresh)


# =============================================================================
# SECTION 4: Field access helpers (nulls stay "unknown", never fabricated)
# =============================================================================


def _field(source: Any, name: str, default: Any = None) -> Any:
    """Read `name` from a dict or an object (ScoredProject) uniformly."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _fmt_budget(scored_project: Any) -> str:
    budget = _field(scored_project, "budget")
    currency = _field(scored_project, "currency")
    if isinstance(budget, dict) and budget.get("minimum") is not None:
        minimum = budget.get("minimum")
        maximum = budget.get("maximum", minimum)
        code = currency.get("code") if isinstance(currency, dict) else None
        code = code if code else "unknown"
        maximum = maximum if maximum is not None else "unknown"
        return f"{minimum}-{maximum} {code}"
    return "unknown"


def _fmt_bid_count(scored_project: Any) -> str:
    stats = _field(scored_project, "bid_stats")
    if isinstance(stats, dict) and stats.get("bid_count") is not None:
        return str(stats["bid_count"])
    bid_count = _field(scored_project, "bid_count")
    return str(bid_count) if bid_count is not None else "unknown"


# =============================================================================
# SECTION 5: Message building & splitting (pure functions — T1/T2 targets)
# =============================================================================


def build_draft_message(draft: dict, scored_project: Any) -> str:
    """
    Render the EXACT System-Prompt draft format:

        [DRAFT <project_id>]
        Project: <title>
        URL: <url>
        Budget: <min>-<max> <currency>
        Bids so far: <n>
        Match score: <total>/100
        Rationale: <rationale>
        ---
        <proposal text>
        ---
        Reply "approve <id>" to submit, or "reject <id>" to discard.
    """
    pid = _field(scored_project, "project_id", "unknown")
    title = _field(scored_project, "title") or "unknown"
    url = _field(scored_project, "url") or "unknown (seo_url is null)"
    total = _field(scored_project, "total_score")
    total = total if total is not None else "unknown"
    rationale = _field(scored_project, "rationale") or "unknown"
    proposal = (draft or {}).get("text") or "(no proposal text)"

    header = (
        f"[DRAFT {pid}]\n"
        f"Project: {title}\n"
        f"URL: {url}\n"
        f"Budget: {_fmt_budget(scored_project)}\n"
        f"Bids so far: {_fmt_bid_count(scored_project)}\n"
        f"Match score: {total}/100\n"
        f"Rationale: {rationale}\n"
        f"---"
    )
    footer = "---\n" + _REPLY_FOOTER.format(pid=pid)
    return f"{header}\n{proposal.strip()}\n{footer}"


def _split_at_word(text: str, limit: int) -> Tuple[str, str]:
    """Split `text` into (head <= limit chars, rest) on a word boundary."""
    if len(text) <= limit:
        return text, ""
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(), text[len(cut):].lstrip()


def split_draft_message(full_text: str, limit: int = MESSAGE_CHAR_LIMIT) -> List[str]:
    """
    Split an over-long draft into messages, each <= `limit` chars:
    message 1 = header + '---' + first part; the LAST message ends with
    the '---' + reply footer. The inline keyboard is attached only to the
    final message by the caller.
    """
    if len(full_text) <= limit:
        return [full_text]

    # peel off header (up to the first '---') and footer (after the last '---')
    parts = full_text.split("\n---\n")
    header = parts[0] + "\n---"
    footer = "---\n" + parts[-1]
    proposal = "\n---\n".join(parts[1:-1]) if len(parts) > 2 else parts[1]

    chunks: List[str] = []
    remaining = proposal.strip()
    first = True
    while remaining:
        room = limit - len(header) - 32 if first else limit - 32
        chunk, remaining = _split_at_word(remaining, max(200, room))
        prefix = f"{header}\n" if first else ""
        marker = "\n(continued in next message)" if remaining else ""
        chunks.append(f"{prefix}{chunk}{marker}")
        first = False
    # attach footer to the last chunk
    if chunks:
        chunks[-1] = chunks[-1] + "\n" + footer
    return chunks


def approve_keyboard(project_id: Any) -> dict:
    """Inline keyboard: Approve / Reject with callback_data '<action>:<pid>'."""
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve:{project_id}"},
            {"text": "Reject", "callback_data": f"reject:{project_id}"},
        ]]
    }


# =============================================================================
# SECTION 6: The notifier client
# =============================================================================


class TelegramNotifier:
    """Raw-requests Bot API client for drafts, statuses and approvals."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        session: Optional[requests.Session] = None,
        message_cache_path: Optional[Path] = None,
    ) -> None:
        self._token = str(token)
        self._chat_id = str(chat_id)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        # highest update_id seen by the last getUpdates call (any update,
        # matched or not) — lets the scheduler advance its offset and flush
        # stale updates even when no approve/reject was parsed
        self.last_update_id: Optional[int] = None
        # message_id -> last known text (for edit_message_status)
        self._cache_path = Path(message_cache_path) if message_cache_path else MESSAGE_CACHE_PATH
        self._message_texts = self._load_message_cache()

    # ------------------------------------------------------------------ core

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self._token}/{method}"

    def _api(self, method: str, payload: dict, *, read_timeout: float = 30.0) -> dict:
        """
        POST one Bot API call. Maps 401/400/429 per the spec; on 429 reads
        Retry-After (body parameter, else header), waits ONCE and retries
        exactly once. All error text is scrubbed of the token.
        """
        for attempt in (1, 2):
            try:
                response = self._session.post(
                    self._url(method), json=payload, timeout=(10, read_timeout)
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise TelegramError(
                    _scrub(f"network failure calling {method}: {exc}", self._token)
                ) from None

            status = response.status_code
            body_snippet = response.text[:300]

            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise TelegramError(
                        _scrub(f"non-JSON response from {method}: {body_snippet}", self._token)
                    ) from None
                if not data.get("ok"):
                    raise TelegramError(
                        _scrub(f"{method} returned ok=false: {json.dumps(data)[:300]}", self._token)
                    )
                return data

            if status == 401:
                raise TelegramAuthError(
                    _scrub(
                        "401 from Telegram - bot token invalid or revoked; "
                        "regenerate via @BotFather "
                        f"(token {redact_token(self._token)})",
                        self._token,
                    )
                )

            if status == 400 and "chat not found" in response.text.lower():
                raise TelegramChatError(
                    _scrub(
                        "400 chat not found - TELEGRAM_CHAT_ID is wrong or you haven't "
                        "messaged the bot yet (open a chat with your bot and send any "
                        "message, then re-check the id via getUpdates)",
                        self._token,
                    )
                )

            if status == 429:
                wait = self._retry_after_seconds(response)
                if attempt == 1:
                    logger.warning("429 from %s - waiting %ss once, then one retry",
                                   method, wait if wait is not None else "?")
                    time.sleep(wait if wait is not None else 5)
                    continue
                raise TelegramRateLimited(
                    _scrub(f"429 from {method} persisted after one Retry-After wait", self._token),
                    retry_after=wait,
                )

            raise TelegramError(
                _scrub(f"{status} from {method}: {body_snippet}", self._token)
            )

        raise TelegramError(f"unreachable retry loop for {method}")  # pragma: no cover

    def _retry_after_seconds(self, response: requests.Response) -> Optional[int]:
        raw = response.headers.get("Retry-After")
        try:
            if raw is not None:
                return max(0, int(float(raw)))
        except ValueError:
            pass
        try:
            body = response.json()
            value = body.get("parameters", {}).get("retry_after")
            if value is not None:
                return max(0, int(value))
        except (ValueError, AttributeError):
            pass
        return None

    # ---------------------------------------------------------------- cache

    def _load_message_cache(self) -> Dict[str, str]:
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_message_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._message_texts, fh, ensure_ascii=True, indent=1)
            tmp.replace(self._cache_path)
        except OSError as exc:
            logger.debug("could not persist message cache: %s", exc)

    # --------------------------------------------------------------- sends

    def _send_message(self, text: str, reply_markup: Optional[dict] = None) -> dict:
        payload: Dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self._api("sendMessage", payload)
        message = result.get("result", {})
        message_id = message.get("message_id")
        if message_id is not None:
            self._message_texts[str(message_id)] = text
            self._save_message_cache()
        return message

    def send_draft(self, draft: dict, scored_project: Any) -> dict:
        """
        Send the draft (splitting if needed). Keyboard rides the LAST
        message only. Returns {ok, message_id, chat_id, error[, message_ids]}
        where message_id is the LAST (keyboard-bearing) message.
        """
        try:
            full_text = build_draft_message(draft, scored_project)
            chunks = split_draft_message(full_text)
            pid = _field(scored_project, "project_id", "unknown")

            message_ids: List[Any] = []
            for index, chunk in enumerate(chunks):
                is_last = index == len(chunks) - 1
                markup = approve_keyboard(pid) if is_last else None
                message = self._send_message(chunk, markup)
                if message.get("message_id") is not None:
                    message_ids.append(message["message_id"])

            return {
                "ok": True,
                "message_id": message_ids[-1] if message_ids else None,
                "message_ids": message_ids,
                "chat_id": self._chat_id,
                "error": None,
            }
        except TelegramError as exc:
            return {
                "ok": False,
                "message_id": None,
                "chat_id": self._chat_id,
                "error": str(exc),
            }

    def send_status(self, project_id: int, status: str, note: str = "") -> dict:
        """Short status line, no keyboard. Returns {ok, message_id, error}."""
        line = f"[STATUS] Project {project_id} -> {status}"
        if note:
            line += f" ({note})"
        try:
            message = self._send_message(line)
            return {"ok": True, "message_id": message.get("message_id"), "error": None}
        except TelegramError as exc:
            return {"ok": False, "message_id": None, "error": str(exc)}

    def edit_message_status(self, message_id: int, new_status_line: str) -> dict:
        """
        Prepend `new_status_line` to the original draft message via
        editMessageText so chat history shows the outcome. The inline
        keyboard is intentionally NOT re-supplied: after approve/reject the
        buttons must stop working (double-submit protection). Benign
        failures (message deleted / nothing changed) return ok=False with
        the reason instead of raising.
        """
        key = str(message_id)
        original = self._message_texts.get(key)
        if original is None:
            return {"ok": False, "error": "original text not tracked; cannot edit safely"}
        new_text = f"{new_status_line}\n\n{original}"
        try:
            self._api(
                "editMessageText",
                {"chat_id": self._chat_id, "message_id": message_id, "text": new_text},
            )
        except TelegramError as exc:
            low = str(exc).lower()
            if any(marker in low for marker in _BENIGN_EDIT_ERRORS):
                return {"ok": False, "error": f"not edited: {exc}"}
            return {"ok": False, "error": str(exc)}
        self._message_texts[key] = new_text
        self._save_message_cache()
        return {"ok": True, "error": None}

    # -------------------------------------------------------------- polling

    def poll_approvals(self, timeout: int = 0, offset: Optional[int] = None) -> List[dict]:
        """
        One getUpdates cycle. `timeout` is clamped to <=30s; the HTTP read
        timeout exceeds it by 10s. Returns parsed approvals; each callback
        press is acknowledged with answerCallbackQuery. Never submits bids.
        """
        timeout = max(0, min(int(timeout), POLL_TIMEOUT_CAP))
        payload: Dict[str, Any] = {
            "allowed_updates": ["message", "callback_query"],
            "timeout": timeout,
        }
        if offset is not None:
            payload["offset"] = int(offset)

        result = self._api("getUpdates", payload, read_timeout=timeout + 10.0)
        updates = result.get("result", [])
        self.last_update_id = max(
            (u.get("update_id") or 0 for u in updates if isinstance(u, dict)),
            default=None,
        )
        matches = parse_approvals(updates)

        answered: List[dict] = []
        for match in matches:
            callback_id = match.get("_callback_query_id")
            if callback_id:
                try:
                    # Required by the Telegram API, otherwise the client
                    # shows a spinning button forever.
                    self._api("answerCallbackQuery", {"callback_query_id": callback_id})
                except TelegramError as exc:
                    logger.warning(
                        "answerCallbackQuery failed: %s. This is expected when "
                        "polling manually after a delay; the Step 8 scheduler "
                        "polls continuously and answers callbacks within "
                        "seconds. The action was still parsed correctly.",
                        exc,
                    )
            answered.append(
                {"action": match["action"], "project_id": match["project_id"],
                 "update_id": match["update_id"]}
            )
        return answered

    def get_me(self) -> dict:
        """getMe -> the bot's own identity (username, id)."""
        return self._api("getMe", {}).get("result", {})

    def send_note(self, text: str, project_id: Any = 0) -> dict:
        """Send arbitrary text with the Approve/Reject keyboard attached
        (used by --test-buttons). Returns {ok, message_id, error}."""
        try:
            message = self._send_message(text, approve_keyboard(project_id))
            return {"ok": True, "message_id": message.get("message_id"), "error": None}
        except TelegramError as exc:
            return {"ok": False, "message_id": None, "error": str(exc)}


# =============================================================================
# SECTION 7: Approval parsing (pure — T3 target)
# =============================================================================


def parse_approvals(updates: List[dict]) -> List[dict]:
    """
    Extract approve/reject commands from Telegram updates.

    Matches:
      * message.text            ^(approve|reject)\\s+(\\d+)$
      * callback_query.data     ^(approve|reject):(\\d+)$
    Returns [{action, project_id, update_id[, _callback_query_id]}];
    internal keys are consumed by poll_approvals.
    """
    matches: List[dict] = []
    for update in updates or []:
        update_id = (update or {}).get("update_id")

        message = update.get("message") or update.get("edited_message") or {}
        text = (message.get("text") or "").strip()
        m = _TEXT_CMD_RE.match(text)
        if m:
            matches.append(
                {"action": m.group(1).lower(), "project_id": int(m.group(2)),
                 "update_id": update_id}
            )
            continue

        callback = update.get("callback_query") or {}
        data = (callback.get("data") or "").strip()
        m = _CALLBACK_CMD_RE.match(data)
        if m:
            matches.append(
                {"action": m.group(1).lower(), "project_id": int(m.group(2)),
                 "update_id": update_id, "_callback_query_id": callback.get("id")}
            )
    return matches


# =============================================================================
# SECTION 8: Offline mock tests T1-T6 (--offline; no network, no real creds)
# =============================================================================


class _FakeResp:
    def __init__(self, status, json_body=None, text=""):
        self.status_code = status
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Records sendMessage calls; serves scripted responses."""

    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls: List[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        step = self.responses.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _fake_ok(message_id=100):
    return _FakeResp(200, json_body={"ok": True, "result": {
        "message_id": message_id, "chat": {"id": 555}}})


def _sample_scored(over: Optional[dict] = None) -> dict:
    base = {
        "project_id": 40754444,
        "title": "Automated Trading System Development in Python",
        "url": "https://www.freelancer.com/projects/Automated-Trading-System",
        "total_score": 84.3,
        "rationale": "skill 0.62 | budget 1.0 | comp 0.85 | recency 1.0 -> 84.3",
        "budget": {"minimum": 250, "maximum": 750},
        "currency": {"code": "USD"},
        "bid_stats": {"bid_count": 9},
    }
    if over:
        base.update(over)
    return base


def _t1_header_fields():
    text = build_draft_message({"text": "Proposal body text."}, _sample_scored())
    for needed in (
        "[DRAFT 40754444]",
        "Project: Automated Trading System Development in Python",
        "URL: https://www.freelancer.com/projects/Automated-Trading-System",
        "Budget: 250-750 USD",
        "Bids so far: 9",
        "Match score: 84.3/100",
        "Rationale: skill 0.62 | budget 1.0 | comp 0.85 | recency 1.0 -> 84.3",
        "---",
        'Reply "approve 40754444" to submit, or "reject 40754444" to discard.',
    ):
        assert needed in text, f"T1: missing {needed!r}"
    assert text.index("[DRAFT") < text.index("Project:") < text.index("URL:")


def _t2_split_and_keyboard():
    long_proposal = "word " * 1200  # ~6000 chars -> must split
    session = _FakeSession([_fake_ok(1), _fake_ok(2)])
    notifier = TelegramNotifier("123:ABCDEF_TOKENXYZ", "555", session=session,
                                message_cache_path=PROJECT_ROOT / "cache" / "_t2_cache.json")
    try:
        result = notifier.send_draft({"text": long_proposal}, _sample_scored())
    finally:
        notifier._cache_path.unlink(missing_ok=True)
    sends = [c for c in session.calls if c["url"].endswith("/sendMessage")]
    assert result["ok"] and len(sends) == 2, f"T2: expected 2 sends, got {len(sends)}"
    assert all(len(c["json"]["text"]) <= MESSAGE_CHAR_LIMIT for c in sends), "T2: chunk over limit"
    assert "reply_markup" not in sends[0]["json"], "T2: keyboard on first message"
    kb = sends[1]["json"].get("reply_markup", {})
    buttons = kb.get("inline_keyboard", [[]])[0]
    assert {b["callback_data"] for b in buttons} == {"approve:40754444", "reject:40754444"}
    assert result["message_id"] == 2, "T2: message_id must be the LAST (keyboard) message"


def _t3_parse_approvals():
    updates = [
        {"update_id": 1, "message": {"text": "approve 12345"}},
        {"update_id": 2, "message": {"text": "reject 12345"}},
        {"update_id": 3, "message": {"text": "approve"}},
        {"update_id": 4, "message": {"text": "approved 12345"}},
        {"update_id": 5, "message": {"text": "approve 12a45"}},
        {"update_id": 6, "callback_query": {"id": "cb1", "data": "approve:777"}},
        {"update_id": 7, "callback_query": {"id": "cb2", "data": "reject:888"}},
        {"update_id": 8, "callback_query": {"id": "cb3", "data": "approve:"}},
        {"update_id": 9, "message": {"text": "please approve 12345 now"}},
    ]
    matches = parse_approvals(updates)
    assert {"action": "approve", "project_id": 12345, "update_id": 1} in matches
    assert {"action": "reject", "project_id": 12345, "update_id": 2} in matches
    cb = [m for m in matches if m["update_id"] in (6, 7)]
    assert {(m["action"], m["project_id"]) for m in cb} == {("approve", 777), ("reject", 888)}
    assert len(matches) == 4, f"T3: expected 4 matches, got {len(matches)}: {matches}"


def _t4_token_redacted_in_errors():
    token = "777777:AAE_redactedsecret_TOKEN"
    session = _FakeSession([_FakeResp(500, text=f"boom {token} internal")])
    notifier = TelegramNotifier(token, "555", session=session,
                                message_cache_path=PROJECT_ROOT / "cache" / "_t4_cache.json")
    try:
        result = notifier.send_status(1, "submitted")  # senders catch + return
        assert result["ok"] is False, "T4: expected failure result"
        assert token not in (result["error"] or ""), "T4: raw token leaked in error"
        assert "[REDACTED]" in result["error"] or "500" in result["error"]
    finally:
        notifier._cache_path.unlink(missing_ok=True)
    # and the log filter scrubs too
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(_TokenScrubFilter(token))
    logger.addHandler(handler)
    try:
        logger.error("failed while using %s", token)
        assert token not in stream.getvalue(), "T4: token leaked into log output"
    finally:
        logger.removeHandler(handler)


def _t5_missing_credentials(capsys):
    """T5 runs main(['--self']) with creds stripped; main returns 1 with a
    human message and no traceback (the cred check fires before any call)."""
    saved = {k: os.environ.pop(k) for k in (ENV_TOKEN, ENV_CHAT) if k in os.environ}
    os.environ["FREELANCERCLAW_SKIP_DOTENV"] = "1"  # keep the real .env out
    try:
        code = main(["--self"])
        captured = capsys()
        assert code == 1, f"T5: expected exit 1, got {code}"
        assert "TELEGRAM_BOT_TOKEN" in captured, "T5: no human-readable message"
        assert "Traceback" not in captured, "T5: traceback leaked"
    finally:
        os.environ.pop("FREELANCERCLAW_SKIP_DOTENV", None)
        os.environ.update(saved)


def _t6_chat_not_found():
    session = _FakeSession([_FakeResp(400, text='{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}')])
    notifier = TelegramNotifier("123:ABC", "999", session=session,
                                message_cache_path=PROJECT_ROOT / "cache" / "_t6_cache.json")
    try:
        result = notifier.send_status(1, "submitted")  # senders catch + return
        assert result["ok"] is False, "T6: expected failure result"
        assert "TELEGRAM_CHAT_ID is wrong" in (result["error"] or ""), \
            f"T6: wrong message: {result['error']}"
        assert "haven't" in (result["error"] or "")
    finally:
        notifier._cache_path.unlink(missing_ok=True)
    assert _cli_exit_code(TelegramChatError("x")) == 3


_TESTS = [
    ("T1 message body contains all header fields", _t1_header_fields),
    ("T2 >4000 chars splits; keyboard on last message only", _t2_split_and_keyboard),
    ("T3 parse_approvals matches only valid commands", _t3_parse_approvals),
    ("T4 token redacted from errors and logs", _t4_token_redacted_in_errors),
    ("T5 missing token exits 1 with human message", _t5_missing_credentials),
    ("T6 400 triggers chat-not-found branch (exit 3)", _t6_chat_not_found),
]


def run_offline_tests() -> int:
    passed = failed = 0
    import contextlib
    import io

    for name, fn in _TESTS:
        try:
            if fn is _t5_missing_credentials:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    fn(lambda: buf.getvalue())
            else:
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
# SECTION 9: CLI
# =============================================================================


def _cli_load_env() -> None:
    """Load .env (override=False) only for creds not already in the env.
    FREELANCERCLAW_SKIP_DOTENV=1 disables discovery (offline tests, Valves)."""
    if os.environ.get("FREELANCERCLAW_SKIP_DOTENV"):
        return
    if os.environ.get(ENV_TOKEN) and os.environ.get(ENV_CHAT):
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        print("note: python-dotenv not installed; relying on the environment only.")
        return
    if load_dotenv(override=False):
        print("note: loaded credentials from .env (env vars, where set, took precedence).")


def _cli_exit_code(exc: TelegramError) -> int:
    return getattr(exc, "exit_code", 5)


def _build_cli_notifier(token: str, chat_id: str) -> TelegramNotifier:
    """Indirection point so offline tests can stub CLI construction."""
    return TelegramNotifier(token, chat_id)


def _resolve_creds() -> Tuple[Optional[str], Optional[str]]:
    token = os.environ.get(ENV_TOKEN, "").strip()
    chat_id = os.environ.get(ENV_CHAT, "").strip()
    if not token:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is not set.\n"
            "  Fix: copy env.example .env and fill it in, or\n"
            "       set TELEGRAM_BOT_TOKEN=123456:ABC...   (current session)\n"
            "  Create the bot with @BotFather (/newbot) and copy its token.",
            file=sys.stderr,
        )
        return None, None
    if not chat_id:
        print(
            "ERROR: TELEGRAM_CHAT_ID is not set.\n"
            "  Send any message to your bot, then open\n"
            "    https://api.telegram.org/bot<TOKEN>/getUpdates\n"
            "  and copy the numeric chat.id into TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        return None, None
    return token, chat_id


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram_notifier.py",
        description="FreelancerClaw Telegram draft notifier.",
    )
    parser.add_argument("--self", action="store_true", help="getMe: print bot username + id")
    parser.add_argument("--test", action="store_true", help="send a live test draft message")
    parser.add_argument("--test-buttons", action="store_true", help="send a live keyboard test")
    parser.add_argument("--poll", action="store_true", help="one approvals poll cycle (timeout=10)")
    parser.add_argument("--offline", action="store_true", help="run mock tests T1-T6 (no network)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.offline:
        return run_offline_tests()

    _cli_load_env()
    token, chat_id = _resolve_creds()
    if not token or not chat_id:
        return 1

    for handler in logging.getLogger().handlers:
        handler.addFilter(_TokenScrubFilter(token))
    print(f"Using TELEGRAM_BOT_TOKEN {redact_token(token)} / chat {chat_id}")

    notifier = _build_cli_notifier(token, chat_id)

    try:
        if args.self:
            me = notifier.get_me()
            print("SUCCESS - bot identity:")
            print(f"  username: @{me.get('username', 'unknown')}")
            print(f"  id:       {me.get('id', 'unknown')}")
            return 0

        if args.test:
            draft = {
                "text": (
                    "This is a test draft to confirm delivery. "
                    "Reply 'reject 0' to confirm the reply parser works."
                ),
                "word_count": 19,
                "cited_evidence": [],
                "opening_quote": "(title anchor) [TEST]",
                "warnings": [],
            }
            scored = _sample_scored({
                "project_id": 0,
                "title": "[TEST] FreelancerClaw live test",
                "url": "https://www.freelancer.com/projects/unknown",
                "total_score": 0,
                "rationale": "live delivery test",
                "bid_stats": {"bid_count": 0},
            })
            result = notifier.send_draft(draft, scored)
            if not result["ok"]:
                print(f"SEND FAILED: {result['error']}")
                return 5
            print(f"SENT - message_id={result['message_id']} (chat {result['chat_id']})")
            print("Watch Telegram; reply 'reject 0' there, then run --poll.")
            return 0

        if args.test_buttons:
            result = notifier.send_status(0, "keyboard test", note="tap one button below")
            if not result["ok"]:
                print(f"SEND FAILED: {result['error']}")
                return 5
            draft = {"text": "Tap Approve or Reject below, then run --poll to parse the callback."}
            pid = 0
            sends = split_draft_message(build_draft_message(draft, _sample_scored({
                "project_id": pid, "title": "[TEST] inline keyboard",
                "total_score": 0, "rationale": "keyboard test"})))
            kb_result = notifier._send_message(sends[-1], approve_keyboard(pid))
            print(f"SENT keyboard message_id={kb_result.get('message_id')}")
            print("Tap the button once in Telegram, then run: python telegram_notifier.py --poll")
            return 0

        if args.poll:
            updates = notifier.poll_approvals(timeout=10)
            if not updates:
                print("No approve/reject commands found in this cycle.")
                return 0
            for u in updates:
                print(f"parsed: {u['action']} project {u['project_id']} (update_id {u['update_id']})")
            print(f"next offset: {max(u['update_id'] for u in updates) + 1}")
            return 0

    except TelegramAuthError as exc:
        print(f"AUTH FAILED: {exc}", file=sys.stderr)
        print("bot token invalid or revoked; regenerate via @BotFather", file=sys.stderr)
        return _cli_exit_code(exc)
    except TelegramChatError as exc:
        print(f"CHAT ERROR: {exc}", file=sys.stderr)
        print("TELEGRAM_CHAT_ID is wrong or you haven't messaged the bot yet", file=sys.stderr)
        return _cli_exit_code(exc)
    except TelegramRateLimited as exc:
        print(f"RATE LIMITED: {exc}", file=sys.stderr)
        return _cli_exit_code(exc)
    except TelegramError as exc:
        print(f"API ERROR: {exc}", file=sys.stderr)
        return _cli_exit_code(exc)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
