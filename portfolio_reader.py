"""
=============================================================================
FreelancerClaw — Module 4: portfolio_reader.py
GitHub portfolio summarizer + resume.md loader.
=============================================================================

Purpose
-------
Fetches the authenticated user's GitHub repositories, grabs each README,
and builds a compact portfolio summary that the scorer (Module 2) and the
proposal drafter (Module 3) cite as evidence. Also loads resume.md (the
authoritative profile) into memory via get_resume().

Data sources (official APIs only):
  * GET https://api.github.com/user/repos?per_page=100&sort=updated
      -> repo list; the user's login is taken from owner.login of the
         first repo (NEVER hardcoded). If the account has zero repos we
         fall back to GET /user to learn the login.
  * GET /repos/{owner}/{repo}/readme  (Accept: application/vnd.github.raw)
      -> raw README markdown. 404 = no README -> recorded as "no_readme".

Cache
-----
cache/github_portfolio_<login>.json, 24-hour TTL. Fresh runs inside the
TTL load from disk and never touch the API. --refresh bypasses the cache.
The cache is written atomically (tmp file + os.replace) so a crash can
never leave a truncated/corrupt portfolio behind.

Hard constraints honoured here
------------------------------
* GITHUB_PAT comes from env or .env (load_dotenv, override=False) — never
  hardcoded, never logged; the same _scrub/redact pattern as
  freelancer_api.py is applied to every message that can reach a log.
* Nulls stay null: a repo with no language keeps language=null, a README
  with no usable prose keeps description=null (we fall back to the
  GitHub-side repo description — also user-authored — before giving up).
  Nothing is ever invented.
* GitHub rate limits (403/429 with X-RateLimit-Remaining: 0, or a
  Retry-After header) raise GithubRateLimited with a human-readable wait
  time; the CLI prints it and exits cleanly. No traceback, no crash.
* A rate-limited or failed fetch writes NO cache (partial data must not
  pose as a complete portfolio).

Standalone CLI
--------------
  python portfolio_reader             # use cache if fresh, else fetch
  python portfolio_reader --refresh   # bypass cache, force API fetch

Exit codes: 0 ok | 1 PAT missing | 2 auth failed | 3 other API error |
            4 rate limited | 5 resume.md missing/unreadable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# =============================================================================
# SECTION 1: Constants & configuration
# =============================================================================

GITHUB_API_BASE = "https://api.github.com"

ENV_VAR_NAME = "GITHUB_PAT"

# Where files live. This module must work both when run from the project
# root and when imported from elsewhere: anchor to THIS file's directory.
PROJECT_ROOT = Path(__file__).resolve().parent
RESUME_PATH = PROJECT_ROOT / "resume.md"
CACHE_DIR = PROJECT_ROOT / "cache"

PORTFOLIO_TTL_SECONDS = 24 * 60 * 60          # 24-hour cache TTL
DESCRIPTION_MAX_CHARS = 200                   # one-line description cap

# Same retry posture as Module 1: 3 attempts total, waits of 2s then 4s on
# 5xx / network errors. (Rate limits are NOT retried here — see docstring.)
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0

REQUEST_TIMEOUT = (10, 30)

USER_AGENT = "FreelancerClaw/0.1 (portfolio summarizer)"

logger = logging.getLogger("freelancerclaw.portfolio")


# =============================================================================
# SECTION 2: Exceptions
# =============================================================================


class PortfolioError(Exception):
    """Base class for portfolio_reader failures."""


class GithubAuthError(PortfolioError):
    """HTTP 401 — PAT missing/invalid (fine-grained PATs also 401 on wrong perms)."""


class GithubRateLimited(PortfolioError):
    """
    GitHub refused the request for quota reasons.

    Attributes:
        message:      safe (scrubbed) description
        retry_after:  friendly, human-readable wait time (e.g.
                      "try again after 14:32 (in 8 min)") or None
    """

    def __init__(self, message: str, retry_after: Optional[str] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GithubApiError(PortfolioError):
    """Other non-retryable GitHub HTTP errors."""


class GithubConnectionError(PortfolioError):
    """Network-level failure after MAX_ATTEMPTS."""


# =============================================================================
# SECTION 3: Token hygiene — same pattern as freelancer_api.py
# =============================================================================


def redact_token(token: Optional[str]) -> str:
    """Safe-to-print form: keep only the last 4 characters."""
    if not token:
        return "(no token set)"
    return "..." + token[-4:] if len(token) >= 4 else "...(too short)"


def _scrub(text: Any, token: Optional[str]) -> str:
    """Replace every occurrence of the raw token with [REDACTED]."""
    s = str(text)
    if token:
        s = s.replace(token, "[REDACTED]")
    return s


# =============================================================================
# SECTION 4: GitHub client
# =============================================================================


class GithubClient:
    """Minimal authenticated GitHub client for the two endpoints we need."""

    def __init__(self, token: str, session: Optional[requests.Session] = None) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            }
        )

    # ---------------------------------------------------------------- helpers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        accept: Optional[str] = None,
    ) -> requests.Response:
        """
        One request with the retry policy. Returns the raw Response so
        callers can decide how to parse (README fetch returns .text, not
        JSON). Raises before returning on error statuses.
        """
        url = f"{GITHUB_API_BASE}{path}"
        headers = {"Accept": accept} if accept else None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method, url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise GithubConnectionError(
                        _scrub(f"network failure after {attempt} attempts on {method} {path}: {exc}", self._token)
                    ) from None
                wait = BACKOFF_BASE_SECONDS ** attempt
                logger.warning("network error on %s %s (attempt %d/%d), retrying in %.0fs",
                               method, path, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue

            status = response.status_code

            # --- 401: bad PAT, never retried ----------------------------
            if status == 401:
                raise GithubAuthError(
                    _scrub(
                        f"401 Unauthorized on {method} {path} - GITHUB_PAT missing, invalid, "
                        f"or lacking permissions (token {redact_token(self._token)}). "
                        f"Create a fine-grained PAT with Contents: Read-only at "
                        f"https://github.com/settings/personal-access-tokens/new",
                        self._token,
                    )
                )

            # --- rate limit: 403/429 + exhausted quota, or Retry-After ---
            if status in (403, 429) and self._is_rate_limited(response):
                raise self._rate_limit_error(response, method, path)

            # --- 5xx: backoff and retry ---------------------------------
            if status >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise GithubApiError(
                        _scrub(f"{status} persisted after {MAX_ATTEMPTS} attempts on {method} {path}", self._token)
                    )
                wait = BACKOFF_BASE_SECONDS ** attempt
                logger.warning("%d on %s %s (attempt %d/%d), retrying in %.0fs",
                               status, method, path, attempt, MAX_ATTEMPTS, wait)
                time.sleep(wait)
                continue

            # --- anything else 4xx (incl. plain 404): caller decides ----
            return response

        raise GithubApiError(f"exhausted retries on {method} {path}")  # pragma: no cover

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        """GitHub signals quota exhaustion via headers on a 403/429."""
        if response.headers.get("Retry-After"):
            return True  # secondary rate limit
        remaining = response.headers.get("X-RateLimit-Remaining")
        return remaining == "0"

    def _rate_limit_error(self, response: requests.Response, method: str, path: str) -> GithubRateLimited:
        """Build a scrubbed, human-friendly rate-limit error."""
        wait_note = None

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                seconds = max(0, int(retry_after))
                wait_note = f"try again in {seconds // 60} min {seconds % 60}s"
            except ValueError:
                pass

        if wait_note is None:
            reset = response.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    when = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone()
                    wait_note = f"try again after {when:%H:%M} local time"
                except (ValueError, OSError, OverflowError):
                    pass

        message = _scrub(
            f"GitHub rate limit hit on {method} {path}"
            + (f" ({wait_note})" if wait_note else " (no retry time provided by GitHub)"),
            self._token,
        )
        return GithubRateLimited(message, retry_after=wait_note)

    # ------------------------------------------------------------- endpoints

    def list_repos(self) -> list[dict]:
        """
        GET /user/repos?per_page=100&sort=updated — exactly the documented
        call. NOTE: single page, so accounts with 100+ repos see the first
        100 (sorted by last update, which is what a portfolio wants).
        """
        response = self._request(
            "GET", "/user/repos", params={"per_page": 100, "sort": "updated"}
        )
        if response.status_code >= 400:
            raise GithubApiError(
                _scrub(f"{response.status_code} on /user/repos: {response.text[:200]}", self._token)
            )
        try:
            data = response.json()
        except ValueError:
            raise GithubApiError("non-JSON response from /user/repos") from None
        return data if isinstance(data, list) else []

    def get_login(self) -> str:
        """GET /user — only used as login fallback for zero-repo accounts."""
        response = self._request("GET", "/user")
        if response.status_code >= 400:
            raise GithubApiError(
                _scrub(f"{response.status_code} on /user: {response.text[:200]}", self._token)
            )
        try:
            login = response.json().get("login")
        except ValueError:
            login = None
        return login if login else "unknown"

    def get_readme_raw(self, owner: str, repo: str) -> Optional[str]:
        """
        GET /repos/{owner}/{repo}/readme with Accept: application/vnd.github.raw.
        Returns raw markdown, or None when the repo has no README (404).
        """
        response = self._request(
            "GET", f"/repos/{owner}/{repo}/readme", accept="application/vnd.github.raw"
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GithubApiError(
                _scrub(f"{response.status_code} fetching README for {repo}: {response.text[:200]}", self._token)
            )
        return response.text


# =============================================================================
# SECTION 5: README -> one-line description extraction
# =============================================================================

# Lines that are markdown furniture, not prose.
_FURNITURE_RE = re.compile(
    r"""^(?:
        \#{1,6}\s.*           |   # headings: # .. ######
        !\[.*?\]\(.*?\)         |   # images / badges
        \[!\[.*?\]\(.*?\)\]\(.*?\) | # linked badges: [![alt](img)](link)
        <!--.*?-->            |   # HTML comments (single-line)
        <[^>]+>               |   # stray HTML tags / anchors
        [-*_]{3,}             |   # horizontal rules
        \s*$                  |   # blank lines
    )$""",
    re.VERBOSE,
)


def first_paragraph(markdown_text: str) -> Optional[str]:
    """
    First non-empty PROSE paragraph of a README.

    Skips title headings, badges/images, HTML comments and rules (furniture
    that would make a useless one-liner like '# my-repo' or a badge URL),
    then collapses whitespace into a single line. Returns None when the
    README contains no usable prose — the caller then falls back to the
    GitHub-side repo description, and only then to null. Never fabricates.
    """
    if not markdown_text:
        return None

    for block in re.split(r"\n\s*\n", markdown_text):
        lines = [ln.strip() for ln in block.splitlines()]
        kept = [ln for ln in lines if ln and not _FURNITURE_RE.match(ln)]
        if kept:
            paragraph = " ".join(kept)
            paragraph = re.sub(r"\s+", " ", paragraph).strip()
            if paragraph:
                return paragraph
    return None


def truncate(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    """Truncate to `limit` chars on a word boundary, appending '...'."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 3].rsplit(" ", 1)[0] if " " in text[: limit - 3] else text[: limit - 3]
    return cut.rstrip(" ,;:-") + "..."


# =============================================================================
# SECTION 6: Portfolio assembly + cache
# =============================================================================


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path(login: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", login)  # path-safe login
    return CACHE_DIR / f"github_portfolio_{safe}.json"


def _find_fresh_cache() -> Optional[tuple]:
    """
    Locate THE fresh portfolio cache without any API traffic.

    The cache filename embeds the login, which is only learnable from the
    API — a chicken-and-egg we break by globbing instead: this is a
    single-user tool, so at most one login's cache should exist. Rules:
      * exactly one fresh cache            -> use it
      * several caches, same login         -> newest fetched_at wins
      * several caches, different logins   -> ignore cache (refetch; the
        successful fetch then cleans the stale account's file)
    """
    candidates = []
    for path in sorted(CACHE_DIR.glob("github_portfolio_*.json")):
        data = _load_cache(path)
        if data is not None:
            candidates.append((path, data))
    if not candidates:
        return None
    logins = {data.get("login") for _, data in candidates}
    if len(logins) > 1:
        logger.warning("multiple fresh portfolio caches for logins %s - refetching", sorted(map(str, logins)))
        return None
    return max(candidates, key=lambda pair: pair[1].get("fetched_at", ""))


def _load_cache(path: Path) -> Optional[dict]:
    """Return the cached portfolio if present, valid and inside the TTL."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.debug("cache miss: %s", path.name)   # normal first run, not a warning
        return None
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable cache %s (%s)", path.name, exc)
        return None

    fetched_at = data.get("fetched_at")
    if not fetched_at:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if age.total_seconds() > PORTFOLIO_TTL_SECONDS or age.total_seconds() < 0:
        return None
    data["cache_age_seconds"] = int(age.total_seconds())
    return data


def _write_cache(path: Path, portfolio: dict) -> None:
    """
    Atomic write: serialize to <path>.tmp then os.replace over the target.
    A crash mid-write can never corrupt an existing cache entry.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(portfolio, fh, ensure_ascii=True, indent=2)
    os.replace(tmp, path)


def fetch_portfolio(client: GithubClient, *, refresh: bool = False) -> tuple[dict, bool]:
    """
    Build (or reload) the portfolio summary.

    Returns (portfolio_dict, from_cache). Raises GithubRateLimited /
    GithubAuthError / GithubApiError / GithubConnectionError on failure —
    and NEVER writes a cache in that case.

    A fresh cache short-circuits EVERYTHING: zero API calls of any kind
    (hard requirement: "do not re-fetch on every scan").
    """
    # ---- cache short-circuit FIRST (skipped when refresh=True) -----------
    if not refresh:
        hit = _find_fresh_cache()
        if hit is not None:
            return hit[1], True

    # ---- pass 1: repo list (also tells us the login via owner.login) ----
    repos = client.list_repos()

    login = None
    for repo in repos:
        owner = repo.get("owner") or {}
        if owner.get("login"):
            login = owner["login"]
            break
    if login is None:
        # Zero repos (or owners stripped): ask /user directly. The login is
        # only used for the cache filename and README URLs — never trusted
        # for anything security-relevant.
        login = client.get_login()

    cache_file = _cache_path(login)

    # ---- pass 2: README per repo -----------------------------------------
    summaries: list[dict] = []
    no_readme: list[str] = []

    for repo in repos:
        name = repo.get("name") or "(unnamed)"
        owner_login = (repo.get("owner") or {}).get("login") or login

        raw = client.get_readme_raw(owner_login, name)
        if raw is None:
            # Spec: skip silently, record as "no_readme" in the summary.
            no_readme.append(name)
            continue

        # README prose first; GitHub-side description as fallback; else null.
        description = first_paragraph(raw)
        if description is None:
            description = repo.get("description")  # may legitimately be null
        else:
            description = truncate(description)

        summaries.append(
            {
                "name": name,
                "language": repo.get("language"),      # null stays null
                "description": description,             # null stays null
                "html_url": repo.get("html_url"),
            }
        )

    portfolio = {
        "login": login,
        "fetched_at": _now_utc_iso(),
        "ttl_seconds": PORTFOLIO_TTL_SECONDS,
        "repo_count": len(repos),
        "repos": summaries,
        "no_readme_repos": no_readme,
    }
    _write_cache(cache_file, portfolio)

    # Single-user hygiene: a successful authenticated fetch proves which
    # account this PAT belongs to, so caches under any other login are
    # stale artifacts of an old PAT and are removed.
    for other in CACHE_DIR.glob("github_portfolio_*.json"):
        if other != cache_file:
            try:
                other.unlink()
                logger.info("removed stale portfolio cache %s", other.name)
            except OSError as exc:
                logger.warning("could not remove stale cache %s: %s", other.name, exc)

    return portfolio, False


def get_portfolio(token: Optional[str] = None, *, refresh: bool = False) -> tuple[dict, bool]:
    """
    Convenience entry point used by the scheduler / Tool: resolves the token
    (parameter wins, then env), then fetch-or-cache.
    """
    pat = token or os.environ.get(ENV_VAR_NAME, "")
    if not pat:
        raise PortfolioError(
            "GITHUB_PAT is not set - copy env.example to .env and fill it in, "
            "or set the environment variable / OpenWebUI Valve"
        )
    return fetch_portfolio(GithubClient(pat), refresh=refresh)


# =============================================================================
# SECTION 7: resume.md — the authoritative profile
# =============================================================================

_RESUME_CACHE: dict[str, Any] = {"mtime": None, "text": None}


def get_resume(refresh: bool = False) -> str:
    """
    Return the full contents of FreelancerClaw/resume.md as one string.

    Cached in memory; re-reads when the file's mtime changes or when
    refresh=True, so users can edit resume.md without restarting scans.
    Raises FileNotFoundError with a friendly message if it is missing.
    """
    try:
        mtime = RESUME_PATH.stat().st_mtime
    except OSError:
        raise FileNotFoundError(
            f"resume.md not found at {RESUME_PATH} - it is the authoritative "
            f"profile every scorer and drafter reads; restore it from the template"
        ) from None

    if not refresh and _RESUME_CACHE["text"] is not None and _RESUME_CACHE["mtime"] == mtime:
        return _RESUME_CACHE["text"]

    try:
        text = RESUME_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FileNotFoundError(f"could not read {RESUME_PATH}: {exc}") from None

    _RESUME_CACHE["mtime"] = mtime
    _RESUME_CACHE["text"] = text
    return text


# =============================================================================
# SECTION 8: Standalone CLI
#   python portfolio_reader.py            -> cache if fresh, else fetch
#   python portfolio_reader.py --refresh  -> bypass cache, force fetch
# =============================================================================


def _cli_load_env() -> None:
    """
    Load .env with override=False: real environment variables always win,
    .env only fills the gaps (precedence: Valves > environment > .env).
    Unlike Module 1 there is no gate to check — override=False IS the
    precedence rule — so an unconditional call is safe and idempotent.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        print("note: python-dotenv not installed; relying on the environment only.")
        return
    if load_dotenv(override=False):
        print("note: loaded credentials from .env (env vars, where set, took precedence).")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portfolio_reader.py",
        description="FreelancerClaw portfolio summarizer - GitHub repos + resume.md.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the 24h cache and force a fresh API fetch",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    _cli_load_env()

    pat = os.environ.get(ENV_VAR_NAME, "")
    if not pat:
        # Friendly, actionable - no traceback (hard requirement).
        print()
        print("ERROR: GITHUB_PAT is not set.")
        print()
        print("  Fix one of:")
        print("    1. copy env.example .env   and fill in GITHUB_PAT")
        print("    2. set GITHUB_PAT=your-pat            (current cmd session)")
        print('    3. setx GITHUB_PAT "your-pat"         (persistent; reopen terminal)')
        print()
        print("  Create a fine-grained PAT (Contents: Read-only) at:")
        print("  https://github.com/settings/personal-access-tokens/new")
        return 1

    print(f"Using GITHUB_PAT {redact_token(pat)}")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TokenScrubFilter(pat))

    client = GithubClient(pat)

    # ---- portfolio (cache-aware) ------------------------------------------
    try:
        portfolio, from_cache = fetch_portfolio(client, refresh=args.refresh)
    except GithubAuthError as exc:
        print()
        print("AUTH FAILED (401).")
        print(f"  detail: {exc}")
        return 2
    except GithubRateLimited as exc:
        print()
        print("RATE LIMITED by GitHub.")
        print(f"  detail: {exc}")
        if exc.retry_after:
            print(f"  {exc.retry_after}")
        print("  Nothing was cached; re-run later (cache stays valid until TTL).")
        return 4
    except (GithubApiError, GithubConnectionError) as exc:
        print()
        print(f"API ERROR: {exc}")
        return 3

    repos = portfolio.get("repos", [])
    no_readme = portfolio.get("no_readme_repos", [])
    total = len(repos) + len(no_readme)
    login = portfolio.get("login", "unknown")

    # ---- preview (kept short) ----------------------------------------------
    if args.refresh or not from_cache:
        print(f"login: {login} (from owner.login)")
        print(f"fetched {total} repos, reading READMEs ...")
    for entry in repos[:5]:
        lang = entry["language"] if entry["language"] is not None else "unknown language"
        desc = entry["description"] if entry["description"] is not None else "(no description)"
        print(f"  - {entry['name']} [{lang}]: {desc}")
    if len(repos) > 5:
        print(f"  ... and {len(repos) - 5} more (see cache file)")

    # ---- resume -------------------------------------------------------------
    try:
        resume_text = get_resume()
        print(f"resume.md loaded ({len(resume_text)} chars)")
    except FileNotFoundError as exc:
        print()
        print(f"WARNING: {exc}")
        # Portfolio itself succeeded, but the profile is mandatory downstream.
        resume_missing = True
    else:
        resume_missing = False

    # ---- required one-line summary, LAST -------------------------------------
    cache_file = _cache_path(login)
    if from_cache and not args.refresh:
        age_h = portfolio.get("cache_age_seconds", 0) / 3600.0
        print()
        print(
            f"Loaded {total} repos ({len(repos)} with README, {len(no_readme)} without), "
            f"from cache {cache_file} (age {age_h:.1f}h of 24.0h TTL)"
        )
    else:
        print()
        print(
            f"Loaded {total} repos ({len(repos)} with README, {len(no_readme)} without), "
            f"cached to {cache_file}"
        )

    return 5 if resume_missing else 0


class _TokenScrubFilter(logging.Filter):
    """Scrub the PAT from any log record, whoever emitted it."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        scrubbed = _scrub(msg, self._token)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = None
        return True


if __name__ == "__main__":
    sys.exit(main())
