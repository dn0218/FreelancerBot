"""
=============================================================================
FreelancerClaw — Module 1: freelancer_api.py
Authenticated client for the official Freelancer.com API.
=============================================================================

Facts this client is built on (VERIFIED — do not change without re-verifying):

  * Auth: direct personal Access Token. NO OAuth flow, no client_id,
    no client_secret, no refresh_token.
  * Token source: os.environ["FREELANCER_ACCESS_TOKEN"] (or passed
    explicitly — OpenWebUI Valves will do that in Step 9).
  * Base URL: https://www.freelancer.com/api
  * Every request carries: Authorization: Bearer <token>
  * 401 -> AuthError; the caller must stop the cycle and alert via Telegram.
  * Search results do NOT contain description/jobs/owner_* (they are null);
    full details require GET /projects/0.1/projects/{id}.

Safety properties enforced here:

  * The token is NEVER logged and NEVER appears in an exception message
    (defense-in-depth: a logging filter scrubs it, and every raised message
    passes through _scrub()).
  * 429/5xx/network errors are retried with exponential backoff,
    MAX 3 attempts total, honouring the Retry-After header when present.
  * This module is a thin API client. Business gates (DRAFT_ONLY,
    first-scan safety gate, approval flow) live in scheduler.py /
    the OpenWebUI Tool — place_bid() here performs no policy checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import requests

# =============================================================================
# SECTION 1: Constants & configuration
# =============================================================================

BASE_URL = "https://www.freelancer.com/api"

ENV_VAR_NAME = "FREELANCER_ACCESS_TOKEN"

# Retry policy: 3 attempts TOTAL (1 original + 2 retries), exponential
# backoff -> waits of 2s, then 4s between attempts — unless the server
# sends Retry-After, which always takes precedence over our own guess.
# (Formula: BACKOFF_BASE_SECONDS ** attempt, attempt = 1, 2, ...)
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0

# Retry-After ceiling (seconds). The header is honoured as the wait time,
# but if a server asks us to block for longer than this we raise
# RateLimitError instead of freezing the scan cycle for potentially an
# hour — the scheduler simply retries on its next cycle. Documented
# trade-off; adjust here if you want unconditional blocking waits.
RETRY_AFTER_CEILING_SECONDS = 300.0

# (connect, read) timeouts — keep the scanner responsive on flaky Wi-Fi.
REQUEST_TIMEOUT = (10, 30)

USER_AGENT = "FreelancerClaw/0.1 (personal bidding assistant; API-only, no scraping)"

logger = logging.getLogger("freelancerclaw.api")


# =============================================================================
# SECTION 2: Exceptions
# =============================================================================


class FreelancerApiError(Exception):
    """Base class for all FreelancerClaw API errors."""


class AuthError(FreelancerApiError):
    """
    HTTP 401 — the token is missing, invalid, or expired.

    Callers MUST stop the current cycle immediately and ask the user to
    regenerate the token at https://accounts.freelancer.com/settings/develop
    Never retry an AuthError blindly.
    """


class RateLimitError(FreelancerApiError):
    """HTTP 429 persisted after MAX_ATTEMPTS, or Retry-After above ceiling."""


class ServerError(FreelancerApiError):
    """HTTP 5xx persisted after MAX_ATTEMPTS."""


class ApiClientError(FreelancerApiError):
    """Other non-retryable HTTP 4xx (bad request, forbidden, not found...)."""


class ApiConnectionError(FreelancerApiError):
    """Network-level failure (DNS, refused, timeout) after MAX_ATTEMPTS."""


# =============================================================================
# SECTION 3: Token hygiene (redaction helpers)
# =============================================================================


def redact_token(token: Optional[str]) -> str:
    """
    Safe-to-print form of a token: only the last 4 characters survive.
    e.g. "flSecure_abcdef123456" -> "...3456". Never raises, never leaks.
    """
    if not token:
        return "(no token set)"
    return "..." + token[-4:] if len(token) >= 4 else "...(too short)"


def _scrub(text: Any, token: Optional[str]) -> str:
    """
    Replace every occurrence of the raw token in `text` with [REDACTED].
    Applied to EVERY exception message before it is raised or logged.
    In practice the token travels in a header, not the URL, so it should
    never appear — this is defense-in-depth.
    """
    s = str(text)
    if token:
        s = s.replace(token, "[REDACTED]")
    return s


class _TokenScrubFilter(logging.Filter):
    """
    Logging filter that scrubs the token from any record, whoever logs it.
    Attached to the module's handling of log output in the CLI below.
    """

    def __init__(self, token: Optional[str]) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        scrubbed = _scrub(msg, self._token)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = None
        return True


# =============================================================================
# SECTION 4: The API client
# =============================================================================


class FreelancerClient:
    """
    Thin authenticated wrapper around the Freelancer.com API.

    Methods (all return parsed JSON dicts, fields untouched — nulls stay
    null, they are NEVER coerced to "" or 0):

        get_self()                         GET /users/0.1/users/self
        search_projects(query, limit, offset)
                                           GET /projects/0.1/projects/active
        get_project_details(project_id)    GET /projects/0.1/projects/{id}
        place_bid(project_id, bid_data)    POST /projects/0.1/bids
    """

    def __init__(self, token: Optional[str] = None, session: Optional[requests.Session] = None) -> None:
        # Explicit token wins (OpenWebUI Valves path); else environment.
        self._token = token or os.environ.get(ENV_VAR_NAME, "")
        if not self._token:
            # Don't raise yet — the CLI wants to print a friendly message.
            logger.warning("%s is not set; requests will fail with AuthError.", ENV_VAR_NAME)

        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )

    # ---------------------------------------------------------------- helpers

    def _request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
        """
        Perform one HTTP request with the full retry/backoff policy.

        Retryable: 429, 5xx, network timeouts/connection errors.
        Not retryable: 401 (AuthError — never blind-retry auth failures),
        other 4xx.
        """
        url = f"{BASE_URL}{path}"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=REQUEST_TIMEOUT,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise ApiConnectionError(
                        _scrub(f"network failure after {attempt} attempts on {method} {path}: {exc}", self._token)
                    ) from None
                wait = BACKOFF_BASE_SECONDS ** attempt
                logger.warning("network error on %s %s (attempt %d/%d), retrying in %.0fs",
                               method, path, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue

            status = response.status_code

            # --- 401: hard stop, never retried --------------------------
            if status == 401:
                raise AuthError(
                    _scrub(
                        f"401 Unauthorized on {method} {path} - token missing, invalid or expired. "
                        f"Regenerate at https://accounts.freelancer.com/settings/develop "
                        f"(token {redact_token(self._token)})",
                        self._token,
                    )
                )

            # --- 429 / 5xx: backoff and retry ---------------------------
            if status == 429 or status >= 500:
                if attempt == MAX_ATTEMPTS:
                    detail = self._error_snippet(response)
                    exc_class = RateLimitError if status == 429 else ServerError
                    raise exc_class(
                        _scrub(f"{status} persisted after {MAX_ATTEMPTS} attempts on {method} {path}: {detail}", self._token)
                    )
                wait = self._retry_after_seconds(response)
                if wait is None:
                    wait = BACKOFF_BASE_SECONDS ** attempt
                elif wait > RETRY_AFTER_CEILING_SECONDS:
                    raise RateLimitError(
                        _scrub(
                            f"429 on {method} {path} asks to wait {wait:.0f}s, above our "
                            f"{RETRY_AFTER_CEILING_SECONDS:.0f}s ceiling; aborting this cycle "
                            f"(scheduler will retry later)",
                            self._token,
                        )
                    )
                logger.warning("%d on %s %s (attempt %d/%d), honouring backoff %.0fs",
                               status, method, path, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue

            # --- other 4xx: fail fast ----------------------------------
            if status >= 400:
                raise ApiClientError(
                    _scrub(f"{status} on {method} {path}: {self._error_snippet(response)}", self._token)
                )

            # --- 2xx: parse ---------------------------------------------
            return self._parse_json(response)

        # Unreachable: the loop either returns or raises.
        raise FreelancerApiError(f"exhausted retries on {method} {path}")  # pragma: no cover

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> Optional[float]:
        """Read the Retry-After header (seconds form). None if absent/invalid."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            value = float(raw)
            return value if value >= 0 else None
        except ValueError:
            # HTTP-date form is legal per RFC; we fall back to exponential
            # backoff rather than parsing dates.
            return None

    @staticmethod
    def _error_snippet(response: requests.Response) -> str:
        """Short, safe snippet of an error body for exception messages."""
        try:
            body = response.text[:300].strip()
        except Exception:  # noqa: BLE001 — never let logging helpers blow up
            body = "(unprintable body)"
        return body.replace("\n", " ") or "(empty body)"

    @staticmethod
    def _parse_json(response: requests.Response) -> dict:
        """
        Parse a 2xx body. Also catches Freelancer's occasional 200-with-
        error-body. IMPORTANT: the parsed structure is returned untouched —
        nulls remain null (hard constraint: no coercion to "" or 0).
        """
        try:
            data = response.json()
        except ValueError:
            raise ApiClientError(
                f"non-JSON response from {response.url} (HTTP {response.status_code}): "
                f"{response.text[:200]!r}"
            ) from None
        # Freelancer sometimes reports errors inside a 200 body.
        if isinstance(data, dict) and data.get("status") == "error":
            raise ApiClientError(f"API body status=error: {json.dumps(data)[:300]}")
        return data

    # ------------------------------------------------------------- endpoints

    def get_self(self) -> dict:
        """GET /users/0.1/users/self — the authenticated user's profile."""
        return self._request("GET", "/users/0.1/users/self")

    def search_projects(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        """
        GET /projects/0.1/projects/active — search active projects.

        NOTE (verified): results contain id, title, seo_url, currency,
        preview_description, submitdate, bid_stats{bid_count,bid_avg}, ...
        but description/jobs/owner_* come back NULL here. Scoring uses
        preview_description; drafting must call get_project_details().
        """
        return self._request(
            "GET",
            "/projects/0.1/projects/active",
            params={"query": query, "limit": limit, "offset": offset},
        )

    def get_project_details(self, project_id: int | str) -> dict:
        """GET /projects/0.1/projects/{id} — full detail incl. description/jobs."""
        return self._request("GET", f"/projects/0.1/projects/{project_id}")

    def place_bid(self, project_id: int | str, bid_data: dict) -> dict:
        """
        POST /projects/0.1/bids — place a bid on a project.

        bid_data example: {"amount": 250, "period": 7, "description": "..."}
        project_id is forced into the payload.

        !!! POLICY WARNING !!! This method is the raw trigger. It performs
        NO approval checks — the DRAFT_ONLY gate, the first-scan safety
        gate, and the Telegram approval flow live in scheduler.py and the
        OpenWebUI Tool. Those must all pass before anyone calls this.
        """
        payload = dict(bid_data or {})
        payload["project_id"] = int(project_id)
        return self._request("POST", "/projects/0.1/bids", json_body=payload)


# =============================================================================
# SECTION 5: Standalone CLI — `python freelancer_api.py --self`
# Quick live verification of the token against /users/0.1/users/self.
# =============================================================================


def _cli_load_env() -> None:
    """
    Load `.env` via python-dotenv ONLY when the env var is not already set.
    load_dotenv(override=False) is exactly that: pre-existing environment
    variables always win (env var > .env, matching our documented
    precedence: Valves > environment > defaults).
    """
    if os.environ.get(ENV_VAR_NAME):
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        print("note: python-dotenv is not installed; relying on the environment only.")
        print("      install with:  pip install -r requirements.txt")
        return
    loaded = load_dotenv(override=False)
    if loaded:
        print("note: loaded credentials from .env (env var was not set).")


def _extract_user(profile: Any) -> dict | None:
    """
    Pull the user object out of a /users/self response. Handles both the
    wrapped form {"result": {...}}} and a flat user object. Returns None
    when the shape is unexpected — we print 'unknown' rather than crash
    (never fabricate, never guess).
    """
    if isinstance(profile, dict):
        result = profile.get("result")
        if isinstance(result, dict):
            return result
        if "id" in profile or "username" in profile:
            return profile
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freelancer_api.py",
        description="FreelancerClaw API client - standalone token sanity check.",
    )
    parser.add_argument(
        "--self",
        action="store_true",
        help="call GET /users/0.1/users/self and print username + id",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not args.self:
        parser.print_help()
        return 0

    _cli_load_env()

    token = os.environ.get(ENV_VAR_NAME, "")
    if not token:
        # Friendly, actionable error — no KeyError traceback.
        print()
        print("ERROR: FREELANCER_ACCESS_TOKEN is not set.")
        print()
        print("  Fix one of:")
        print("    1. copy env.example .env    and fill in FREELANCER_ACCESS_TOKEN")
        print("       (this command auto-loads .env when the env var is unset)")
        print("    2. set FREELANCER_ACCESS_TOKEN=your-token     (current cmd session)")
        print("    3. setx FREELANCER_ACCESS_TOKEN \"your-token\"  (persistent; reopen terminal)")
        print()
        print("  Get the token at: https://accounts.freelancer.com/settings/develop")
        return 1

    # Confirm which token we are using WITHOUT printing it.
    print(f"Using FREELANCER_ACCESS_TOKEN {redact_token(token)}")

    # Scrub defence applies to every log line from here on.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TokenScrubFilter(token))

    client = FreelancerClient(token)

    print("Calling GET /users/0.1/users/self ...")
    try:
        profile = client.get_self()
    except AuthError as exc:
        print()
        print("AUTH FAILED (401).")
        print(f"  detail: {exc}")
        print()
        print("  Regenerate the token at:")
        print("  https://accounts.freelancer.com/settings/develop")
        print("  then update your .env / environment / Valves and re-run.")
        return 2
    except FreelancerApiError as exc:
        print()
        print(f"API ERROR: {exc}")
        return 3

    user = _extract_user(profile)
    if user is None:
        print()
        print("WARNING: unexpected response shape - could not locate the user object.")
        print(f"raw response (first 400 chars): {json.dumps(profile)[:400]}")
        print("username: unknown, id: unknown")
        return 0

    # username/id may legitimately be absent -> print 'unknown', never guess.
    username = user.get("username")
    user_id = user.get("id")
    print()
    print("SUCCESS - authenticated as:")
    print(f"  username: {username if username is not None else 'unknown'}")
    print(f"  user id:  {user_id if user_id is not None else 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
