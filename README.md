# FreelancerClaw

An autonomous **bidding assistant** for Freelancer.com, built as an
OpenWebUI Tool (class `Tools`) with an optional scheduler Pipeline.
It scans for open projects, scores them against your resume + GitHub
portfolio, drafts tailored proposals, and pushes them to Telegram —
**you** approve or reject every bid. Nothing is ever submitted without
your explicit approval.

> Status: **Step 1 of 10 — scaffolding complete.** The module sections below
> document the full target design and will be filled in step by step.

---

## What it does (one cycle)

1. **Scan** — search Freelancer's official API for active projects matching
   your keywords. (No HTML scraping, ever.)
2. **Score** — rate each project 0–100 on skill match, budget fit,
   competition, and recency, cross-checked against `resume.md` and your
   GitHub portfolio. Excluded keywords kill a project outright.
3. **Draft** — projects ≥ `SCORE_THRESHOLD` get a tailored, portfolio-cited
   proposal (150–250 words, ends with a fixed compliance line).
4. **Approve** — the draft lands in Telegram with inline Approve/Reject
   buttons. `DRAFT_ONLY` defaults to **true**; a hard safety gate also
   blocks the *first* scan from ever submitting.
5. **Log** — every scan/score/draft/outcome is appended to
   `data/bids.jsonl` (append-only, restart-safe).

## Repository layout

```
FreelancerClaw/
├── README.md               <- this file
├── env.example             <- template for .env (secrets)
├── requirements.txt        <- runtime deps (requests, python-dotenv)
├── .gitignore              <- keeps .env and data/ out of git
├── resume.md               <- YOUR profile (authoritative, fill in below)
├── freelancer_api.py       <- Module 1: authenticated API client        [Step 2]
├── portfolio_reader.py     <- Module 4: GitHub portfolio + resume       [Step 3]
├── project_scorer.py       <- Module 2: 0-100 scoring engine            [Step 4]
├── proposal_drafter.py     <- Module 3: proposal generation             [Step 5]
├── telegram_notifier.py    <- Module 5: drafts + approvals via Telegram [Step 6]
├── bid_state_store.py      <- Module 6: append-only JSONL ledger        [Step 7]
├── scheduler.py            <- Module 7: timed scan-score-draft cycles   [Step 8]
├── openwebui_tool.py       <- OpenWebUI Tool (class Tools + Valves)     [Step 9]
├── test_offline.py         <- end-to-end tests with mocked APIs        [Step 10]
├── data/
│   └── bids.jsonl          <- append-only store (created at runtime)
└── cache/
    └── github_portfolio_*.json  <- 24h-TTL portfolio cache
```

---

## Setup

### 1. Set the Freelancer token

1. Go to <https://accounts.freelancer.com/settings/develop>
2. Under **API / Personal access token**, click **Generate** (or copy your
   existing token — regenerate it if it has expired).
3. Put the token in your environment:

   - **Windows (cmd, current session):**
     `set FREELANCER_ACCESS_TOKEN= paste-token-here`
   - **Windows (persistent, user-level):**
     `setx FREELANCER_ACCESS_TOKEN "paste-token-here"` *(restart terminals after `setx`)*
   - **`.env` file (used by `scheduler.py` / `test_offline.py`):**
     `copy env.example .env`  then fill in the values.
   - **OpenWebUI:** paste it into the Tool's **Valves** screen instead —
     nothing touches the filesystem.

   If the token expires, any request returns **401** → the agent stops the
   cycle immediately and sends you a Telegram alert pointing you back to
   that same settings page to regenerate.

### 2. Create the Telegram bot

1. Message **@BotFather** in Telegram → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you.
3. Open a chat with your new bot and send any message (e.g. `hi`) —
   bots cannot message users first.
4. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and find
   `"chat":{"id": 123456789, ...}` — that number is your chat id.
5. Fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` or Valves.

### 3. Generate the GitHub PAT

1. Fine-grained tokens: <https://github.com/settings/personal-access-tokens/new>
2. **Repository access:** all repos, or select the ones you want cited.
3. **Permissions → Contents: Read-only** (minimum).
4. Fill `GITHUB_PAT` in `.env` or Valves.

### 4. Load Valves in OpenWebUI

1. In OpenWebUI go to **Workspace → Tools** and add the FreelancerClaw
   tool (`openwebui_tool.py`, delivered in Step 9).
2. Open the tool's **gear icon → Valves** and fill in:
   `FREELANCER_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `GITHUB_PAT`.
3. Tune behaviour there too: `SEARCH_KEYWORDS`, `SCORE_THRESHOLD` (70),
   `MIN_BUDGET` (100 USD), `MAX_BIDS_PER_DAY` (1), `MAX_BIDS_PER_MONTH` (6),
   `SCAN_INTERVAL_MINUTES` (30, floor 15), `EXCLUDED_KEYWORDS`,
   `DRAFT_ONLY` (true).
4. Valves always beat `.env`/environment values; nothing is hardcoded.

### 5. Run the first scan

*(concrete commands are added when the modules land in Steps 2–10; the
shape will be:)*

```cmd
cd C:\Users\User\Desktop\FreelancerClaw
pip install -r requirements.txt
:: Step 2 — sanity-check the Freelancer token (prints username + id)
python freelancer_api.py --self

:: Step 8 — run one full scan cycle (reads .env on its own)
python scheduler.py --once
```

The **first scan never submits anything** — a hard safety gate requires at
least one manually approved draft before any submit path is even armed.

---

## Safety & compliance

- **Human-in-the-loop:** no bid is ever placed without an explicit Telegram
  approval; `DRAFT_ONLY` defaults to `true`.
- **First-run gate:** the first scan cannot submit, regardless of settings.
- **No fabrication:** `null` API fields are reported as `"unknown"`, never
  guessed into budgets, bid counts, or client history.
- **Rate limits:** 3 attempts with exponential backoff on 429/5xx,
  `Retry-After` honoured.
- **No scraping:** official REST API only.
- **Secret hygiene:** tokens are never logged; errors are redacted.
- **Audit trail:** `data/bids.jsonl` is append-only and restart-safe.
- **Every proposal ends with:**
  *"All claims in this proposal are truthful and verifiable; no performance
  or profitability claims are made."*

## Operations

### Start / stop the scheduler

**Interactive (foreground, current window):**

```cmd
cd C:\Users\User\Desktop\FreelancerClaw
python -X utf8 scheduler.py
```

The default mode is the two-loop cycle (Loop A = scan every `SCAN_INTERVAL_MINUTES`, Loop B = Telegram approval poll every 15 s). Stop with `Ctrl+C` — it prints a final summary and exits with code 0.

**One-shot variants:**

```cmd
python -X utf8 scheduler.py --once           :: one scan cycle, then exit
python -X utf8 scheduler.py --once --dry     :: ...but only print drafts locally
python -X utf8 scheduler.py --status         :: state snapshot, no scan
```

**Background (cmd start, detached):**

```cmd
cd C:\Users\User\Desktop\FreelancerClaw
start "ClawScheduler" /MIN cmd /c "python -X utf8 scheduler.py >> logs\scheduler.log 2>&1"
```

The window is minimized; `Ctrl+C` needs a focused window, so to stop: `taskkill /FI "WINDOWTITLE eq ClawScheduler*"`. To simply see it is alive: `tasklist /FI "WINDOWTITLE eq ClawScheduler*"`.

**Task Scheduler (boot-time autostart):**

```cmd
schtasks /Create /TN "FreelancerClaw Scheduler" /TR "cmd /c cd /d C:\Users\User\Desktop\FreelancerClaw && python -X utf8 scheduler.py >> logs\scheduler.log 2>&1" /SC ONSTART /RU <yourWindowsUser>
```

(One-liner as requested. For production use the GUI variant with "Run whether user is logged on or not" — it keeps the process alive across logoffs; `schtasks /Run /TN "FreelancerClaw Scheduler"` to start without rebooting, `schtasks /End /TN "FreelancerClaw Scheduler"` to stop, `schtasks /Delete /TN "FreelancerClaw Scheduler"` to remove.)

### Monitoring

**Tail logs/scheduler.log on Windows (getUpdates-free equivalents):**

```cmd
:: last 50 lines, once
powershell -NoProfile -Command "Get-Content logs\scheduler.log -Tail 50"

:: follow live (Ctrl+C to stop)
powershell -NoProfile -Command "Get-Content logs\scheduler.log -Tail 20 -Wait"
```

Log rotation: `RotatingFileHandler` at 2 MB × 3 backups (`scheduler.log`, `scheduler.log.1`, `.2`, `.3`).

**Status snapshot:**

```cmd
python -X utf8 scheduler.py --status
```

**Inspect bids.jsonl safely (read-only):** it is an append-only ledger — never edit it; read it with anything that does not lock the writer:

```cmd
:: last 10 records, pretty-printed
python -X utf8 -c "import json,pathlib;recs=[json.loads(l) for l in pathlib.Path('data/bids.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()];[print(json.dumps(r,ensure_ascii=False)) for r in recs[-10:]]"

:: or: last 20 raw lines
powershell -NoProfile -Command "Get-Content data\bids.jsonl -Tail 20"
```

### Recovery procedures

**Expired Freelancer token (401 → Loop A halts, Telegram alert fires):**

1. New token: <https://accounts.freelancer.com/settings/develop> → **API / Personal access token** → Generate.
2. Update it everywhere it lives: `.env` (if used) **and** OpenWebUI Tool Valves.
3. Verify: `python -X utf8 freelancer_api.py --self` → prints your username + id.
4. Restart the scheduler (or call `scan_and_draft()` once through the Tool).

**Revoked Telegram bot (alerts stop arriving):**

1. Check the bot still exists: open `https://api.telegram.org/bot<TOKEN>/getMe` in a browser — `401` means revoked.
2. Recreate via @BotFather `/newbot` (or un-revoke the token), update `TELEGRAM_BOT_TOKEN` in `.env`/Valves.
3. Send the new bot any message (bots cannot write first), confirm the chat id, update `TELEGRAM_CHAT_ID` if it changed.
4. Verify: `python -X utf8 telegram_notifier.py --self` → prints bot username + id; `--test` sends a live draft-style message.
5. If stale approvals might be pending: `python -X utf8 scheduler.py --reset-offset`, then let the scheduler run one poll cycle (or call the Tool's `poll_approvals()`) to flush old updates — the standalone `telegram_notifier.py --poll` prints but does not persist the offset.

**Revoked GitHub PAT (portfolio citations degrade):**

1. Check: `python -X utf8 portfolio_reader.py --refresh` — auth errors here mean the PAT is dead. Drafts continue with resume-only evidence (honest, fewer citations, warning logged).
2. New fine-grained PAT: <https://github.com/settings/personal-access-tokens/new>, repos: all/selected, **Contents: Read-only**.
3. Update `GITHUB_PAT` in `.env`/Valves, then `python -X utf8 portfolio_reader.py --refresh` to repopulate the cache (24 h TTL).

**Corrupted cache/scheduler_state.json:**

The scheduler already handles this: an unreadable state file is logged as `[error] scheduler_state.json unreadable (...); starting fresh` and a fresh state is written on next save. "Fresh" means: dedupe memory cleared (you may see one cycle re-scan recently seen projects) and the Telegram offset reset (the next poll may re-read recent updates — one scheduler poll cycle flushes them). If a corrupt file somehow blocks startup, simply delete `cache\scheduler_state.json` — same recovery, manual.

### Known limitations

- **Single-machine state:** everything lives in local files; no multi-host story.
- **Drafts are local while DRAFT_ONLY=true:** approvals mark records `approved` in the store but never call `place_bid` — pasting into Freelancer is manual.
- **First-scan safety gate:** an empty store blocks all submissions; a prior `drafted` record must exist first.
- **Telegram is approval-only:** `poll_approvals` parses Approve/Reject replies only; it does not chat.
- **Corrupt-state recovery is destructive-by-design** (start fresh) — keep `data/bids.jsonl` backed up if history matters.
- **0-citation drafts can still ship below 80 words** when the resume Skills section has fewer than 3 entries — the spec'd honest-shortcoming path (warns `R3: word count N below 80`). *(Step 10b fix note: the ≥3-skill 0-citation branch now always lands in 80–130 via the new scope-clarification sentence.)*

### Troubleshooting table

| Symptom | Likely cause | Fix |
|---|---|--- |
| `FREELANCER_ACCESS_TOKEN is not set` in console | env var missing in this shell | `set FREELANCER_ACCESS_TOKEN=...` (session) / `setx` (persistent) / fill Valves; `python freelancer_api.py --self` to verify |
| Scan prints `scan: aborted on API error (401)` | Freelancer token expired | Recovery: Expired Freelancer token (above) |
| Telegram draft/alert messages stopped | bot token revoked, or chat id changed | `getMe` in a browser; re-create bot; update token/chat id; `--reset-offset` + one `--poll` |
| `submitted today: 0` never grows though approvals arrive | DRAFT_ONLY still true | Intended: draft-only mode marks `approved` and returns text for manual copy; flip `DRAFT_ONLY` in Valves/.env to arm submission |
| Offset shows `(none)` after a manual `--poll` | Step 6's one-shot `--poll` printed but didn't persist | Fixed since Step 8: `run_poll_cycle` persists offset; run one scheduler poll to re-arm |
| Duplicate projects re-scanned once | corrupt/fresh scheduler state cleared dedupe memory | Benign one-cycle effect; `data/bids.jsonl` still prevents re-drafting |
| Drafts with no citations / fewer citations | GitHub PAT dead or relevance gate filtered all | `portfolio_reader.py --refresh`; check PAT; resume-only evidence is intentional honest fallback |
| `[error] scheduler_state.json unreadable; starting fresh` | corrupted state cache | Automatic recovery; see "Corrupted cache/scheduler_state.json" |
| `python scheduler.py --test` fails | regression in a module | Run `python test_offline.py` (full E2E) and the per-module `--test` suites to isolate |
| Scores look off | resume.md drifted from reality | Edit `resume.md`; it is the authoritative scoring input |

---

## Build progress

- [x] Step 1 — structure, `env.example`, README skeleton, `resume.md`
- [ ] Step 2 — `freelancer_api.py` + live `/users/self` test
- [ ] Step 3 — `portfolio_reader.py` + live GitHub test
- [ ] Step 4 — `project_scorer.py` + mock unit tests
- [ ] Step 5 — `proposal_drafter.py` + word-count/compliance tests
- [ ] Step 6 — `telegram_notifier.py` + live test message
- [ ] Step 7 — `bid_state_store.py` + persistence tests
- [ ] Step 8 — `scheduler.py`
- [ ] Step 9 — OpenWebUI Tool wrapper (`class Tools` + Valves)
- [ ] Step 10 — `test_offline.py` end-to-end
