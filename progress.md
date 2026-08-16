# Alfred Agent — Project Progress & Architecture

> **Purpose of this file:** A living document for any coding agent (or human) picking up this project.
> It describes the architecture, every file's role, key design decisions, bugs fixed, and current status.

---

## Project Overview

Alfred is a **Telegram-controlled AI agent** running on a Windows 11 host.
It uses a local [Ollama](https://ollama.com/) instance as its LLM backend, and can:
- Execute PowerShell (and optionally WSL2 bash) commands on the host machine.
- Search the web via DuckDuckGo (free) or Tavily (API key optional).
- Persist learned lessons to disk and self-review its own conversation log.
- Switch active Ollama models at runtime, with auto-detected context windows.
- Schedule one-time or recurring reminders (cron) via Telegram, autonomously through the model or via `/cron`.

**Entry point:** `main.py` → `telegram_bot.main()` → `app.run_polling()`

---

## Directory Structure

```
alfred-agent/
├── main.py                  # Entrypoint — just calls telegram_bot.main()
├── config.py                # Reads all settings from .env (static, load-time only)
├── runtime_config.py        # Mutable runtime state: CURRENT_MODEL, CURRENT_CONTEXT_LENGTH
├── agent_loop.py            # Core Ollama tool-calling async generator loop
├── telegram_bot.py          # All Telegram handlers, commands, callbacks, wiring
├── self_review.py           # Background asyncio task: periodic conversation self-review
├── monitor.py               # Background tasks: new-model watcher, auto git update check/pull, cron reminder scheduler
├── ollama_utils.py          # Async helpers: list models, detect context window
├── service.py               # pywin32 Windows service wrapper (runs as LocalSystem)
├── deploy.py                # Push updates to other devices (robocopy + WinRM service reinstall)
├── tools/
│   ├── __init__.py          # Tool registry + execute_tool dispatcher
│   ├── web_search.py        # Tavily (if key) or DuckDuckGo search
│   ├── shell_exec.py        # PowerShell/WSL execution, dangerous pattern detection
│   ├── memory.py            # Lesson persistence, conversation logging
│   └── cron.py              # Cron-style reminders (persisted, fired by scheduler)
├── data/
│   ├── lessons.json         # Persisted lessons (max MAX_LESSONS entries)
│   ├── conversations.jsonl  # Append-only full conversation log (rotated via LOG_MAX_BYTES)
│   ├── audit_log.jsonl      # Append-only shell command audit trail (rotated via LOG_MAX_BYTES)
│   ├── self_review_state.json  # Tracks last reviewed line pointer for self-review
│   ├── chat_histories.json  # Persisted per-chat histories (survive restarts)
│   ├── shell_lock.json      # Kill-switch state for /lockdown (survives restarts)
│   └── cron_jobs.json       # Scheduled reminders (survive restarts)
├── logs/                    # Service-level Python logs (alfred-service.log, rotating)
├── deploy_config.json       # Deploy targets — NOT in git
├── .env                     # Secrets — NOT in git
├── .env.example             # Template for .env
├── requirements.txt         # Python dependencies (incl. pywin32)
└── README.md                # Deployment instructions (pywin32 service)
```

---

## Key Files — Detailed Role

### `config.py`
Loads all settings from `.env` at import time. Values are **static** — they do not change at runtime.
- `OLLAMA_API_URL`, `OLLAMA_MODEL`, `MAX_TOOL_ITERATIONS`, `SELF_REVIEW_EVERY_N_TURNS`
- `SHELL_WORKING_DIR` uses `or PROJECT_ROOT` fallback so empty string in `.env` works correctly.
- `TELEGRAM_ALLOWED_USER_IDS` is parsed as a comma-separated list of integers.
- `load_dotenv()` targets `PROJECT_ROOT/.env` explicitly (not cwd) — the Windows service runs with `cwd=%SystemRoot%\System32`.

### `runtime_config.py`
Holds values that **can change at runtime** (e.g., via `/model`).
- `CURRENT_MODEL: str` — initialised from `config.OLLAMA_MODEL`, updated by `/model` command.
- `CURRENT_CONTEXT_LENGTH: int` — detected via `ollama_utils.get_model_context_length()` on startup and on model switch.
- `SHELL_ENABLED: bool` — kill-switch flag. `/lockdown` sets False, `/unlock` sets True. Persisted to `data/shell_lock.json` so a lockdown survives restarts. `run_shell` refuses to execute while False.
- **All code that sends to Ollama reads from here, not `config.py`.**

### `agent_loop.py`
- `run_agent_turn(user_message, history, lessons)` — async generator yielding `AgentEvent` objects.
- Event types: `tool_call_requested`, `confirmation_required`, `tool_result`, `final_answer`, `error`.
- History management: **the user message is appended to `history` ONCE** (line ~30), then `messages` is built as `[system] + history`. Do NOT append the user message to `messages` separately — that caused a critical duplication bug (now fixed).
- Tool iterations loop continues until no more `tool_calls` in the response or `MAX_TOOL_ITERATIONS` is hit.
- Dangerous shell confirmations await the user's future with `asyncio.wait_for(..., CONFIRMATION_TIMEOUT_SECONDS)` — if the user never clicks, the command auto-cancels instead of hanging forever.
- Lessons fed to the system prompt are relevance-scored via `get_relevant_lessons(lessons, user_message, MAX_LESSONS_IN_PROMPT)` — keyword overlap dominates, most-recent fills the rest — instead of dumping all `MAX_LESSONS` verbatim.
- Passes `num_ctx: CURRENT_CONTEXT_LENGTH` in `options` so Ollama respects the detected context window.

### `telegram_bot.py`
Central file. Key globals:
- `CHAT_HISTORIES: Dict[int, List]` — per-chat conversation history, **persisted to `data/chat_histories.json`** and restored on startup (`load_histories`) so restarts don't wipe context.
- `PENDING_CONFIRMATIONS: Dict[str, asyncio.Future]` — futures for agent-requested shell confirmations.
- `DIRECT_SHELL_CACHE: Dict[str, Dict]` — stores `{command, timeout}` for `/shell` dangerous confirmations (avoids Telegram's 64-byte callback_data limit).
- `MODEL_CACHE: List[str]` — populated by `/model` command for inline keyboard index lookup.
- `_APP` / `_LOOP` — set in `post_init` (runs on the bot's event loop); `stop_bot()` schedules `app.stop_running()` on that loop via `run_coroutine_threadsafe`. Used by `service.py` for graceful shutdown from the SCM thread. **Do not move `_APP`/`_LOOP` assignment out of `post_init`** — that is the only hook that runs on the app's loop.

**Commands:**
| Command | Handler | Notes |
|---|---|---|
| `/start` | `start_command` | Greeting |
| `/help` | `help_command` | Full command list |
| `/model` | `model_command` | Fetches models from Ollama, shows inline keyboard |
| `/shell <cmd>` | `shell_command` | Runs directly (no LLM), dangerous commands need confirmation. Optional `--timeout <secs>` prefix for long-running commands |
| `/search <q>` | `search_command` | Runs web search directly (no LLM) |
| `/status` | `status_command` | Uptime, model, context window, history size, lesson count |
| `/clear` | `clear_command` | Wipes in-memory chat history |
| `/lessons` | `lessons_command` | Lists stored lessons |
| `/correct <text>` | `correct_command` | Saves a user lesson |
| `/forget <idx>` | `forget_command` | Removes a lesson by 1-based index |
| `/cron` | `cron_command` | Lists scheduled reminders; `/cron cancel <id>` cancels one |
| `/update` | `update_command` | `git pull --ff-only` + restarts the bot (manual version of the auto-updater) |
| `/lockdown` | `lockdown_command` | Kill switch — disables ALL shell execution (persisted to `shell_lock.json`) |
| `/unlock` | `unlock_command` | Re-enables shell execution after a lockdown |

**Callback routing in `handle_callback`:**
1. `model_sel|<idx>` — switch model, detect context window, update `runtime_config`.
2. `direct_shell|yes|<id>` / `direct_shell|no|<id>` — resolve `/shell` dangerous confirmation from `DIRECT_SHELL_CACHE`.
3. `conf_<update_id>_<n>|yes` / `...no` — resolve agent-loop `asyncio.Future` for dangerous shell commands requested by the LLM.

**`prune_history`:** Context-aware. Keeps at most `0.6 * CURRENT_CONTEXT_LENGTH / 150` messages (~16-32 depending on model). Adapts automatically when you switch models via `/model`.

**`post_init`:** Called by `python-telegram-bot` after bot init. Restores chat histories from disk; starts the self-review background task, the model watcher, and the git update-check task; detects startup model context window; registers command menu with Telegram.

**`post_shutdown`:** Cancels all three background tasks gracefully (prevents "Task destroyed while pending" errors on Ctrl+C).

**`error_handler`:** Global handler registered with `app.add_error_handler()` — logs all unhandled exceptions from any handler to the logger instead of crashing silently.

### `self_review.py`
- `TURN_COUNTER: int` (module global) — incremented by `handle_text` after every completed agent turn.
- `run_self_review_cycle(bot)` — runs forever as a background asyncio task. Every 5 seconds checks if `TURN_COUNTER >= SELF_REVIEW_EVERY_N_TURNS`. If so, resets counter and calls `_perform_review`.
- `_perform_review` — reads new lines from `conversations.jsonl` since the last checkpoint (tracked in `data/self_review_state.json`), sends them to Ollama with a strict prompt to find only genuine mistakes/corrections, parses the JSON list response, and calls `add_lesson(text, source="self_review")` for each.
- Sends a Telegram notification to all `ALLOWED_USER_IDS` when new lessons are learned.

### `ollama_utils.py`
- `get_available_models()` — GET `/api/tags`, returns list of model name strings.
- `get_model_context_length(model_name)` — POST `/api/show`, searches `model_info` dict for `*context_length*` keys, then falls back to `parameters` string for `num_ctx`, then defaults to 4096.

### `monitor.py`
Background tasks started in `post_init` (all cancelled in `post_shutdown`):
- `monitor_models(bot)` — every `MODEL_CHECK_INTERVAL` seconds, fetches the Ollama model list. The first successful fetch sets a baseline; any model that appears later triggers a Telegram notification to all `ALLOWED_USER_IDS`. No spam on startup/Ollama downtime because baseline only sets on a successful fetch.
- `check_for_updates(bot)` — every `GIT_UPDATE_CHECK_INTERVAL` seconds, runs `git fetch origin` in a worker thread (`asyncio.to_thread`) and compares local HEAD to `@{u}`. If behind: with `AUTO_PULL=true` it runs `git pull --ff-only` and notifies the new commit; with `AUTO_PULL=false` it only notifies that an update is available. Failed pulls (local changes/conflicts) are reported, not swallowed.
- `pull_updates() -> (message, changed)` — manual update for `/update`; fetches and ff-pulls, returns a Telegram-ready message and whether anything changed.
- `restart_bot()` — restarts the process. Priority: `RESTART_COMMAND` if set → if `ALFRED_SERVICE_NAME` env is set (pywin32 service mode) spawn a detached `venv python service.py restart` and `os._exit(0)` → otherwise self-respawn (`python main.py`) in dev mode. So the pywin32 service needs **no** `RESTART_COMMAND` in `.env`.
- `run_cron_scheduler(bot)` — every `CRON_CHECK_INTERVAL` seconds, calls `cron.fire_due_jobs()` in a worker thread and sends each due reminder to all `ALLOWED_USER_IDS` via Telegram.

### `service.py`
pywin32 `ServiceFramework` wrapper replacing WinSW (`alfred-service.xml`, now deleted). Runs the bot as **LocalSystem** (no password, survives logoff).
- `SvcDoRun` — chdir to repo root, sets `ALFRED_SERVICE_NAME`, runs `telegram_bot.main()` in a daemon thread, waits on a `win32event`, then joins the thread (30s grace) so `run_polling` can exit cleanly.
- `SvcStop` — runs on a separate SCM thread; calls `telegram_bot.stop_bot()` then signals the event.
- `Install` classmethod additionally sets SCM failure actions (`SC_ACTION_RESTART` after 10s then 30s, then `SC_ACTION_NONE`) so a crash auto-restarts the service.
- `setup_logging()` adds a rotating `logs/alfred-service.log` handler (SCM does not capture stdout/stderr).
- CLI via `win32serviceutil.HandleCommandLine` (elevated): `install --startup delayed`, `start`, `stop`, `restart`, `remove`, `update`.
- **Gotcha:** never import `telegram_bot` at module top of `service.py` — install/remove run without the bot context and must stay light.

### `deploy.py`
Push-update script: `python deploy.py <target> [--dry-run] [--no-elevate]`. Targets from gitignored `deploy_config.json` (template auto-created with `dev`/`prod` local paths). Per target:
1. `robocopy /MIR` the repo excluding `.env`, `data/`, `venv/`, `logs/`, `__pycache__`, `*.pyc`, `deploy_config.json` — **`.env` and `data/` are never overwritten** (each device keeps its own token/Ollama URL/memory).
2. Ensure venv + `pip install -r requirements.txt`.
3. Reinstall the service: `stop` → `remove` → `install --startup delayed` → `start` (stop/remove failures are ignored).
- Local targets run the steps directly; remote targets copy to `\\host\<drive>$` (admin share) and run the same steps over WinRM (`Invoke-Command`). Remote creds may live in the gitignored config `password` field or be prompted.
- Self-elevates via UAC (`ShellExecuteW runas`) when not admin; `--no-elevate` forbids that.

### `tools/cron.py`
- Standard 5-field cron reminders (`minute hour dom month dow`, dow 0-6 with 0=Sunday). Persisted to `data/cron_jobs.json`.
- `add_job(message, cron, repeat, source)` — validates the cron and adds a job; returns a status string for the model to report back.
- `list_jobs()` / `remove_job(job_id)` — management.
- `fire_due_jobs()` — thread-safe; returns jobs whose cron matches the current minute, stamps `last_fired` (fires at most once/minute), and deactivates one-shot jobs (`repeat=False`) after firing. Missed slots while the bot was down are simply skipped (cron semantics).
- Matcher supports `*`, `*/n`, `a-b`, `a,b`; validated against bounds (59/23/31/12/7).
- Exposed to the model as tools `schedule_reminder`, `list_reminders`, `remove_reminder`, and `schedule_batch_reminders`. User can also manage via `/cron` and `/cron cancel <id>`.
- `add_batch(entries, lead_minutes=15)` — one-call bulk scheduler for timetables. Each entry: `{day: 0-6 (0=Sunday), time: 'HH:MM', course, room}`. Creates weekly-recurring jobs firing `lead_minutes` before each class with the course + room in the message. Exists because the model's `MAX_TOOL_ITERATIONS=10` cap would break a 25-class timetable done via individual `schedule_reminder` calls.

### `tools/__init__.py`
- `TOOL_REGISTRY` dict: maps tool name → `{schema, func}`.
- `get_tool_schemas()` — returns list of Ollama-compatible JSON schema dicts.
- `execute_tool(name, arguments)` — dispatches to the registered function. Uses `inspect.iscoroutinefunction` (not the deprecated `asyncio.iscoroutinefunction`) for Python 3.14 compatibility. **Sync tools (`run_shell`, `web_search`, `add_lesson`) run via `asyncio.to_thread`** so blocking I/O never freezes the event loop.

### `tools/shell_exec.py`
- `is_dangerous(command) -> tuple[bool, str]` — returns `(True, reason)` or `(False, "")`. Checks against `DANGEROUS_PATTERNS` regex list.
- `run_shell(command, shell, confirmed, timeout=None)` — executes via `subprocess.run` with `encoding="utf-8", errors="replace"` (avoids cp1252 `UnicodeDecodeError` crashes on Windows). **Refuses execution when `runtime_config.SHELL_ENABLED` is False (lockdown).** With `CONFIRM_ALL_COMMANDS=true` (trial mode) every command requires confirmation, not just dangerous ones. Raises `ConfirmationRequired` if unconfirmed. Truncates stdout/stderr to 4000 chars. Audit-logs every execution. **Auto-saves an `AUTO` lesson on failure** (non-zero exit + stderr, timeout, or exception) so the model learns to avoid bad commands.
- `ConfirmationRequired` exception — caught by `agent_loop.py` to pause the generator and yield a `confirmation_required` event.
- `log_audit` — rotates `audit_log.jsonl` once it exceeds `LOG_MAX_BYTES`.

### `tools/web_search.py`
- `search(query, max_results)` — tries Tavily if `TAVILY_API_KEY` is set, else falls back to `duckduckgo_search` (DDGS). Returns a formatted string of results.
- Both providers are synchronous but are dispatched through `asyncio.to_thread` by `execute_tool`, so they no longer block the event loop.

### `tools/memory.py`
- `add_lesson(text, source)` — deduplicates, appends with timestamp, prunes to `MAX_LESSONS`.
- `remove_lesson(index)` — 1-based index removal.
- `format_lessons_for_prompt(lessons)` — formats lessons as a bullet list for the system prompt.
- `score_lesson(lesson, user_message)` / `get_relevant_lessons(lessons, user_message, top_n)` — keyword-overlap scoring + recency bonus; used by `agent_loop` to feed only the top `MAX_LESSONS_IN_PROMPT` relevant lessons instead of all of them.
- `log_conversation(entry)` — append-only JSONL write to `data/conversations.jsonl`, rotated via `LOG_MAX_BYTES`, serialized via `_append_jsonl`.
- `_append_jsonl(filepath, entry, max_bytes)` — shared, thread-safe append + rotation helper (also used by `shell_exec.log_audit`). Required because Windows append mode is not atomic across threads.
- `log_failed_command(command, stderr, returncode)` — saves auto-categorized lesson on shell failure (source="auto"). Called from `run_shell` when a command fails (non-zero exit with stderr, timeout, or exception), so the model learns from its own mistakes.

---

## Data Flow

```
User sends message
       │
       ▼
telegram_bot.handle_text()
  │  sends "🤔 Thinking..." status
  │
  ▼
agent_loop.run_agent_turn()  ←─── yields AgentEvent objects
  │
  ├─ POST /api/chat (Ollama) with model=runtime_config.CURRENT_MODEL
  │         num_ctx=runtime_config.CURRENT_CONTEXT_LENGTH
  │
  ├─ if tool_calls in response:
  │     ├─ tool_call_requested  → telegram_bot updates status message
  │     ├─ if run_shell + dangerous → confirmation_required event
  │     │     telegram_bot sends inline ✅/❌ keyboard
  │     │     agent_loop awaits asyncio.Future
  │     │     user clicks → future.set_result(True/False)
  │     │     agent_loop resumes
  │     ├─ execute_tool() → result string
  │     ├─ tool_result event → telegram_bot updates status
  │     └─ loop back to POST /api/chat with tool result appended
  │
  └─ if no tool_calls: final_answer event
        telegram_bot deletes status message, sends reply
        TURN_COUNTER += 1
        prune_history()
        save_histories()   # persist to data/chat_histories.json
```

---

## Bugs Fixed (Chronological)

| # | Bug | Fix |
|---|---|---|
| 1 | Ollama 307 redirect — trailing slash in `OLLAMA_API_URL` | `.rstrip('/')` before appending `/api/chat` |
| 2 | `Task destroyed while pending` on Ctrl+C (self_review) | Wrapped loop in `try/except asyncio.CancelledError`; `post_shutdown` cancels the task |
| 3 | Inline keyboard Run button deadlock | Added `block=False` to `MessageHandler` and `CallbackQueryHandler` |
| 4 | Empty `SHELL_WORKING_DIR` in `.env` caused `WinError 123` | `os.getenv(...) or PROJECT_ROOT` in config.py |
| 5 | Empty LLM response caused `BadRequest: Message text is empty` | Guard: replace empty `ans` with fallback string |
| 6 | `duckduckgo_search` vs `ddgs` package naming confusion | Changed `requirements.txt` to `duckduckgo-search` |
| 7 | **User message sent to Ollama twice per turn** (critical) | Removed duplicate `messages.append(user_message)` in `agent_loop.py` |
| 8 | `/shell` dangerous confirmation callback silent fail | Used `DIRECT_SHELL_CACHE` dict + short ID in callback_data to stay under Telegram's 64-byte limit |
| 9 | `asyncio.iscoroutinefunction` deprecation (Python 3.14) | Switched to `inspect.iscoroutinefunction` in `tools/__init__.py` |
| 10 | `is_dangerous` returned `bool`, callers needed a reason string | Changed return type to `tuple[bool, str]` |
| 11 | Stale `is_dangerous` self-test in `tools/shell_exec.py` | Updated `__main__` test block to unpack the `(bool, reason)` tuple (missed when return type changed in bug #10) |
| 12 | `UnicodeDecodeError` in subprocess reader thread on Windows (`charmap`/cp1252) crashed/could hang `subprocess.run` when a command output UTF-8 (e.g. `ollama pull` progress) | `encoding="utf-8", errors="replace"` in `run_shell` and `monitor._git` |
| 13 | Sync tools (`run_shell`, `web_search`) blocked the async event loop; a slow DDGS search or 30s shell command froze the whole bot | `execute_tool` runs sync tools via `asyncio.to_thread`; `/shell` handlers also use `asyncio.to_thread` |
| 14 | `await future` on shell confirmation waited forever — a turn hung indefinitely if the user never clicked ✅/❌ | `asyncio.wait_for(future, CONFIRMATION_TIMEOUT_SECONDS)` auto-cancels the command (default 120s) |
| 15 | `/shell ollama pull ...` died at the 30s default timeout | Added optional `--timeout <secs>` prefix to `/shell` (e.g. `/shell --timeout 600 ollama pull deepseek-r1:14b`) |
| 16 | Concurrent JSONL appends lost lines on Windows (~20% dropped under load) | Windows `open(..., "a")` is not thread-atomic; serialized all JSONL writes through a shared `threading.Lock` (`_append_jsonl` in `tools/memory.py`) |
| 17 | Fresh clone had no `data/` dir (gitignored) → every log/memory write failed with `Errno 2` | `config.py` now does `os.makedirs(DATA_DIR)` at import; write paths (`_append_jsonl`, `_save_lessons`, `save_histories`) defensively create it too |
| 18 | `log_failed_command` was never called — the "learn from failed commands" feature was dead code | Wired into `run_shell`: non-zero exit + stderr, timeouts, and exceptions now auto-save an `AUTO` lesson (deduped) that is relevance-fed back into the prompt |
| — | **Feature: cron reminders** | New `tools/cron.py` (5-field cron matcher, persisted `data/cron_jobs.json`), `schedule_reminder`/`list_reminders`/`remove_reminder` tools so the model schedules reminders autonomously, `monitor.run_cron_scheduler` background task fires due jobs as Telegram messages, `/cron` + `/cron cancel <id>` commands, `CRON_CHECK_INTERVAL` env var |
| — | **Feature: timetable bulk scheduler** | `schedule_batch_reminders` tool + `cron.add_batch(entries, lead_minutes)` — schedules a whole weekly timetable in one tool call (day 0-6, time HH:MM, course, room) firing `lead_minutes` before each class; avoids `MAX_TOOL_ITERATIONS` cap for large timetables |
| — | **Feature: pywin32 service migration** | Replaced WinSW (`alfred-service.xml`) with `service.py` (`ServiceFramework`, runs as LocalSystem, SCM failure-restart actions, graceful `stop_bot()` via `_APP`/`_LOOP` captured in `post_init`). Added `deploy.py` to push code + reinstall the service on other devices (`deploy_config.json`, robocopy excluding `.env`/`data/`/`venv`/`logs`, local or WinRM targets). `config.py` now loads `.env` from `PROJECT_ROOT` explicitly (service cwd is `System32`); `monitor.restart_bot()` detects service mode via `ALFRED_SERVICE_NAME` and runs `python service.py restart`, so no `RESTART_COMMAND` is needed under the service |
| 19 | `service.py install --startup delayed` printed usage + no pywin32 gave a cryptic `'NoneType' has no attribute 'ServiceFramework'` | pywin32's `getopt` requires options **before** the command (`service.py --startup delayed install`); `service.py` now reorders argv so the natural command-first form works. Also added a clear "pywin32 not installed → run pip install -r requirements.txt" guard at import |

---

## Environment Variables (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | From BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | ✅ | — | Comma-separated user IDs |
| `OLLAMA_API_URL` | — | `http://localhost:11434` | Ollama host (no trailing slash needed) |
| `OLLAMA_MODEL` | — | `llama3` | Default startup model |
| `MAX_TOOL_ITERATIONS` | — | `10` | Max tool calls per agent turn |
| `SELF_REVIEW_EVERY_N_TURNS` | — | `5` | Turns between self-review passes |
| `TAVILY_API_KEY` | — | `` | Optional; falls back to DDGS |
| `SHELL_WORKING_DIR` | — | project root | CWD for shell commands |
| `SHELL_TIMEOUT_SECONDS` | — | `30` | Shell command timeout |
| `MAX_LESSONS` | — | `50` | Max lessons to retain |
| `MODEL_CHECK_INTERVAL` | — | `60` | Seconds between checks for newly installed models |
| `GIT_UPDATE_CHECK_INTERVAL` | — | `300` | Seconds between git update checks |
| `CRON_CHECK_INTERVAL` | — | `20` | Seconds between checks for due cron reminders |
| `AUTO_PULL` | — | `true` | Auto `git pull --ff-only` when behind, or only notify |
| `CONFIRMATION_TIMEOUT_SECONDS` | — | `120` | How long to wait for a user to confirm a dangerous command before auto-cancelling |
| `RESTART_COMMAND` | — | `` | Custom restart command. **Unneeded under the pywin32 service** — `monitor.restart_bot()` detects `ALFRED_SERVICE_NAME` and runs `venv python service.py restart` automatically. Set only to override with a custom supervisor. Empty = self-respawn (dev mode) |
| `LOG_MAX_BYTES` | — | `10485760` | Rotate `conversations.jsonl` / `audit_log.jsonl` past this size (bytes) |
| `MAX_LESSONS_IN_PROMPT` | — | `20` | Max relevance-scored lessons fed into the system prompt |
| `CONFIRM_ALL_COMMANDS` | — | `false` | Trial mode: require confirmation for EVERY shell command |
| `OLLAMA_REQUEST_TIMEOUT` | — | `300` | Per-request timeout (s) for Ollama API calls; generous so cold model loads don't fail |
| `OLLAMA_CONTEXT_LENGTH` | — | `0` | Hard cap on `num_ctx` sent to Ollama. `0` = auto-detect. Set on machines that can't handle a huge-context model (e.g. `32768` for ornith:9b's 262144) — without a cap, `/api/chat` hangs as Ollama tries to allocate the full KV cache |

---

## Known Limitations & Future Work

- **WSL2 from service account** — Invoking WSL from a non-interactive service account (the service now runs as LocalSystem) is unreliable: WSL distros are registered per-user and SYSTEM may resolve to a different default user. Needs manual testing. See README for details.
- **Service install/control requires an elevated shell** — non-admin `service.py install` fails with "Access is denied. (5)" at the pywintypes DLL copy into the Python base dir (a protected location). Harmless; retry from an Administrator shell. `deploy.py` self-elevates via UAC.
- **Store (WindowsApps) Python breaks pywin32 install even as admin** — pywin32 copies `pywintypes*.dll` next to the base `python*.dll`; for the Microsoft Store Python that target is `C:\Program Files\WindowsApps\...`, protected even from Administrators. Fix: use a python.org Python (recreate the venv) or grant `icacls ... /grant Administrators:F /T` on the package folder. Affects any device whose venv was built from a Store Python — `deploy.py` will hit it at the install step too.
- **Lesson relevance is keyword-based, not semantic** — `get_relevant_lessons` uses token overlap + recency. Semantic/vector search would be more accurate but costs more.
- **Log rotation is size-based, not time-based** — `conversations.jsonl.1` / `audit_log.jsonl.1` are kept as single rotated backups, not a dated archive.
- **Single-user design** — history is per-chat but the bot is designed for one trusted user. Multi-user scenarios are not tested.
- **Model context detection** — relies on `model_info` keys or `parameters` string. If Ollama changes its API format, detection may fall back to 4096.
- **Kill switch is Telegram-only** — `/lockdown` persists to disk but still requires the bot to be reachable to take effect.
- **Reminders only fire while the bot is running** — `run_cron_scheduler` lives in-process, so a reminder missed while the bot is down is skipped (not back-filled). True always-on cron (Windows Task Scheduler / `schtasks`) would be a future enhancement.
- **DOW `7` not supported** — cron matcher normalizes Sunday to `0`; `7` in the day-of-week field validates but never matches (use `0`).

---

## Running

```powershell
# Development
.\venv\Scripts\python main.py

# As a Windows Service (pywin32 — elevated shell; see README.md for full steps)
.\venv\Scripts\python service.py install --startup delayed
.\venv\Scripts\python service.py start
.\venv\Scripts\python service.py restart   # or stop / remove
Get-Service alfred-agent

# Push an update to another device (see deploy.py / README)
.\venv\Scripts\python deploy.py prod --dry-run
.\venv\Scripts\python deploy.py prod
```

---

*Last updated: 2026-08-16*
