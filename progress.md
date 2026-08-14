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
├── ollama_utils.py          # Async helpers: list models, detect context window
├── tools/
│   ├── __init__.py          # Tool registry + execute_tool dispatcher
│   ├── web_search.py        # Tavily (if key) or DuckDuckGo search
│   ├── shell_exec.py        # PowerShell/WSL execution, dangerous pattern detection
│   └── memory.py            # Lesson persistence, conversation logging
├── data/
│   ├── lessons.json         # Persisted lessons (max MAX_LESSONS entries)
│   ├── conversations.jsonl  # Append-only full conversation log
│   ├── audit_log.jsonl      # Append-only shell command audit trail
│   └── self_review_state.json  # Tracks last reviewed line pointer for self-review
├── logs/                    # WinSW service stdout/stderr logs
├── alfred-service.xml       # WinSW service config (runs as alfredsvc account)
├── .env                     # Secrets — NOT in git
├── .env.example             # Template for .env
├── requirements.txt         # Python dependencies
└── README.md                # Deployment instructions
```

---

## Key Files — Detailed Role

### `config.py`
Loads all settings from `.env` at import time. Values are **static** — they do not change at runtime.
- `OLLAMA_API_URL`, `OLLAMA_MODEL`, `MAX_TOOL_ITERATIONS`, `SELF_REVIEW_EVERY_N_TURNS`
- `SHELL_WORKING_DIR` uses `or PROJECT_ROOT` fallback so empty string in `.env` works correctly.
- `TELEGRAM_ALLOWED_USER_IDS` is parsed as a comma-separated list of integers.

### `runtime_config.py`
Holds values that **can change at runtime** (e.g., via `/model`).
- `CURRENT_MODEL: str` — initialised from `config.OLLAMA_MODEL`, updated by `/model` command.
- `CURRENT_CONTEXT_LENGTH: int` — detected via `ollama_utils.get_model_context_length()` on startup and on model switch.
- **All code that sends to Ollama reads from here, not `config.py`.**

### `agent_loop.py`
- `run_agent_turn(user_message, history, lessons)` — async generator yielding `AgentEvent` objects.
- Event types: `tool_call_requested`, `confirmation_required`, `tool_result`, `final_answer`, `error`.
- History management: **the user message is appended to `history` ONCE** (line ~30), then `messages` is built as `[system] + history`. Do NOT append the user message to `messages` separately — that caused a critical duplication bug (now fixed).
- Tool iterations loop continues until no more `tool_calls` in the response or `MAX_TOOL_ITERATIONS` is hit.
- Passes `num_ctx: CURRENT_CONTEXT_LENGTH` in `options` so Ollama respects the detected context window.

### `telegram_bot.py`
Central file. Key globals:
- `CHAT_HISTORIES: Dict[int, List]` — per-chat conversation history (in-memory).
- `PENDING_CONFIRMATIONS: Dict[str, asyncio.Future]` — futures for agent-requested shell confirmations.
- `DIRECT_SHELL_CACHE: Dict[str, str]` — stores full command strings for `/shell` dangerous confirmations (avoids Telegram's 64-byte callback_data limit).
- `MODEL_CACHE: List[str]` — populated by `/model` command for inline keyboard index lookup.

**Commands:**
| Command | Handler | Notes |
|---|---|---|
| `/start` | `start_command` | Greeting |
| `/help` | `help_command` | Full command list |
| `/model` | `model_command` | Fetches models from Ollama, shows inline keyboard |
| `/shell <cmd>` | `shell_command` | Runs directly (no LLM), dangerous commands need confirmation |
| `/search <q>` | `search_command` | Runs web search directly (no LLM) |
| `/status` | `status_command` | Uptime, model, context window, history size, lesson count |
| `/clear` | `clear_command` | Wipes in-memory chat history |
| `/lessons` | `lessons_command` | Lists stored lessons |
| `/correct <text>` | `correct_command` | Saves a user lesson |
| `/forget <idx>` | `forget_command` | Removes a lesson by 1-based index |

**Callback routing in `handle_callback`:**
1. `model_sel|<idx>` — switch model, detect context window, update `runtime_config`.
2. `direct_shell|yes|<id>` / `direct_shell|no|<id>` — resolve `/shell` dangerous confirmation from `DIRECT_SHELL_CACHE`.
3. `conf_<update_id>_<n>|yes` / `...no` — resolve agent-loop `asyncio.Future` for dangerous shell commands requested by the LLM.

**`prune_history`:** Context-aware. Keeps at most `0.6 * CURRENT_CONTEXT_LENGTH / 150` messages (~16-32 depending on model). Adapts automatically when you switch models via `/model`.

**`post_init`:** Called by `python-telegram-bot` after bot init. Starts self-review background task + detects startup model context window + registers command menu with Telegram.

**`post_shutdown`:** Cancels the self-review task gracefully (prevents "Task destroyed while pending" errors on Ctrl+C).

**`error_handler`:** Global handler registered with `app.add_error_handler()` — logs all unhandled exceptions from any handler to the logger instead of crashing silently.

### `self_review.py`
- `TURN_COUNTER: int` (module global) — incremented by `handle_text` after every completed agent turn.
- `run_self_review_cycle(bot)` — runs forever as a background asyncio task. Every 5 seconds checks if `TURN_COUNTER >= SELF_REVIEW_EVERY_N_TURNS`. If so, resets counter and calls `_perform_review`.
- `_perform_review` — reads new lines from `conversations.jsonl` since the last checkpoint (tracked in `data/self_review_state.json`), sends them to Ollama with a strict prompt to find only genuine mistakes/corrections, parses the JSON list response, and calls `add_lesson(text, source="self_review")` for each.
- Sends a Telegram notification to all `ALLOWED_USER_IDS` when new lessons are learned.

### `ollama_utils.py`
- `get_available_models()` — GET `/api/tags`, returns list of model name strings.
- `get_model_context_length(model_name)` — POST `/api/show`, searches `model_info` dict for `*context_length*` keys, then falls back to `parameters` string for `num_ctx`, then defaults to 4096.

### `tools/__init__.py`
- `TOOL_REGISTRY` dict: maps tool name → `{schema, func}`.
- `get_tool_schemas()` — returns list of Ollama-compatible JSON schema dicts.
- `execute_tool(name, arguments)` — dispatches to the registered function. Uses `inspect.iscoroutinefunction` (not the deprecated `asyncio.iscoroutinefunction`) for Python 3.14 compatibility.

### `tools/shell_exec.py`
- `is_dangerous(command) -> tuple[bool, str]` — returns `(True, reason)` or `(False, "")`. Checks against `DANGEROUS_PATTERNS` regex list.
- `run_shell(command, shell, confirmed)` — executes via `subprocess.run`. Raises `ConfirmationRequired` if dangerous and not confirmed. Truncates stdout/stderr to 4000 chars. Audit-logs every execution.
- `ConfirmationRequired` exception — caught by `agent_loop.py` to pause the generator and yield a `confirmation_required` event.

### `tools/web_search.py`
- `search(query, max_results)` — tries Tavily if `TAVILY_API_KEY` is set, else falls back to `duckduckgo_search` (DDGS). Returns a formatted string of results.
- **Note:** Both providers are called synchronously (blocking). This is acceptable since `execute_tool` runs them in the same event loop thread, but for very slow searches this could block other coroutines. Future improvement: run in `asyncio.to_thread()`.

### `tools/memory.py`
- `add_lesson(text, source)` — deduplicates, appends with timestamp, prunes to `MAX_LESSONS`.
- `remove_lesson(index)` — 1-based index removal.
- `format_lessons_for_prompt(lessons)` — formats lessons as a bullet list for the system prompt.
- `log_conversation(entry)` — append-only JSONL write to `data/conversations.jsonl`.
- `log_failed_command(command, stderr)` — saves auto-categorized lesson on shell failure (source="auto").

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

---

## Known Limitations & Future Work

- **`web_search` is synchronous** — runs blocking I/O in the async event loop. Should be wrapped in `asyncio.to_thread()` for responsiveness under load.
- **WSL2 from service account** — Invoking WSL from a non-interactive service account (`alfredsvc`) is unreliable. Needs manual testing. See README for details.
- **No semantic search on lessons** — lessons are all passed verbatim into the system prompt. With 50+ lessons this could eat context. Future: vector search / relevance scoring.
- **In-memory history only** — restarting the bot clears chat history. For persistence, write/reload `CHAT_HISTORIES` from disk on startup/shutdown.
- **Single-user design** — history is per-chat but the bot is designed for one trusted user. Multi-user scenarios are not tested.
- **Model context detection** — relies on `model_info` keys or `parameters` string. If Ollama changes its API format, detection may fall back to 4096.

---

## Running

```powershell
# Development
.\venv\Scripts\python main.py

# As a Windows Service (see README.md for full steps)
.\alfred-service.exe install
.\alfred-service.exe start
```

---

*Last updated: 2026-08-14*
