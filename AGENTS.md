# AGENTS.md

Telegram bot agent ("Alfred") that runs an Ollama tool-calling loop, executing shell commands, web search, memory, and cron reminders on a Windows 11 host. No framework beyond `python-telegram-bot` + `httpx`.

## Commands

- Run (dev): `.\venv\Scripts\python main.py` (venv is repo-local). Service mode = pywin32 (`service.py`, runs as **LocalSystem**): `.\venv\Scripts\python service.py install --startup auto` / `start` / `stop` / `restart` / `remove` — **must be elevated**. `deploy.py <target>` pushes code + reinstalls the service on other devices (targets in gitignored `deploy_config.json`).
- **No test/lint/typecheck tooling exists.** Don't add any. Verify with:
  - Syntax: `.\venv\Scripts\python -m py_compile <files>`
  - Logic: throwaway scripts via `.\venv\Scripts\python -c "..."` importing the module directly (see "Verification quirks").
  - `tools/shell_exec.py` ships a `__main__` regression self-test for `is_dangerous`: `.\venv\Scripts\python -m tools.shell_exec`.
- Git: single `master` branch, remote `origin` (GitHub). Workflow convention: after any change, `git add -A && git commit` **and `git push origin master`**. `data/`, `.env`, `venv/`, `logs/`, `deploy_config.json` are gitignored — never commit them.

## Architecture

- `main.py` → `telegram_bot.main()` → `app.run_polling()`. Entrypoint is trivial.
- `telegram_bot.py` — all handlers/commands/callbacks; `post_init` starts background tasks and registers the Telegram command menu; `post_shutdown` cancels them.
- `agent_loop.py` — `run_agent_turn()` async generator yields `AgentEvent`s; builds the system prompt, loops calling Ollama `/api/chat` with `tools` until `MAX_TOOL_ITERATIONS` or a final answer. Confirmation requests suspend on an `asyncio.Future`.
- `tools/__init__.py` — `TOOL_REGISTRY` maps tool name → `{schema, func}`; `execute_tool()` dispatches. **Adding a tool = add entry here + (optionally) a mention in the system prompt in `agent_loop.py`.** Registered tools: `web_search`, `run_shell`, `remember_lesson`, `schedule_reminder`, `list_reminders`, `remove_reminder`, `schedule_batch_reminders`.
- `monitor.py` — background tasks: `monitor_models` (new-Ollama-model alerts), `check_for_updates` (git fetch + auto `pull --ff-only`, notifies; does **not** restart), `run_cron_scheduler` (fires due cron reminders via Telegram). Every git call passes `-c safe.directory=<repo>` — the service runs as SYSTEM, which doesn't own the repo.
- `self_review.py` — every `SELF_REVIEW_EVERY_N_TURNS` turns, sends new `conversations.jsonl` lines to Ollama and saves extracted lessons.
- `config.py` / `runtime_config.py` — static `.env` settings vs. mutable state (`CURRENT_MODEL`, `CURRENT_CONTEXT_LENGTH`, `SHELL_ENABLED`). All Ollama requests read from `runtime_config`, not `config`.
- `ollama_utils.py` — model list + context-window detection.
- `service.py` — pywin32 `ServiceFramework` (LocalSystem). `SvcDoRun` runs `telegram_bot.main()` in a thread and chdirs to the repo; `SvcStop` calls `telegram_bot.stop_bot()`. `stop_bot()` needs the `_APP`/`_LOOP` globals captured in `post_init` — don't move that assignment. Install sets SCM failure-restart actions. On install/update it copies `pythonservice.exe` + its load-time DLLs into `venv\Scripts` and registers that ImagePath via `_exe_name_` — on Python 3.13 a host at the venv root can't import `servicemanager` (see gotchas).
- `deploy.py` — `python deploy.py <target> [--dry-run]`: robocopy the repo (excluding `.env`/`data/`/`venv`/`logs`) to each target in `deploy_config.json`, recreate venv + deps, reinstall service (stop→remove→install→start). Copies the service host exe + DLLs into both `venv\` and `venv\Scripts\` so `pythonservice.exe` can load. Remote targets use WinRM + admin share; `.env` is never touched.

## Gotchas (hard-earned)

- **Windows/Unicode:** every `subprocess.run`/`Popen` (incl. `monitor._git`) must pass `encoding="utf-8", errors="replace"` or you'll crash with `UnicodeDecodeError` (cp1252). Console may print mojibake for em-dashes etc. — that's display-only; JSON files store real UTF-8.
- **Never block the async loop.** `execute_tool` runs sync tools (shell, search, memory) via `asyncio.to_thread`; keep doing that for new sync tools. Use `inspect.iscoroutinefunction` (not the deprecated `asyncio.iscoroutinefunction`) — Python 3.14.
- **JSONL writes must go through `tools/memory._append_jsonl`** (thread-safe `threading.Lock`). Plain `open(..., "a")` on Windows is not thread-atomic and drops lines under concurrency.
- **Huge-context models hang Ollama.** `get_model_context_length` auto-detects 262144 for `ornith:9b`; sending `num_ctx: 262144` to a low-RAM Ollama makes `/api/chat` hang until timeout. Fix is the `OLLAMA_CONTEXT_LENGTH` env cap (`effective_context_length()` in `config.py`). First `/api/chat` after startup cold-loads the model — `OLLAMA_REQUEST_TIMEOUT` defaults to 300s for that reason.
- **Model learns from its own mistakes** two ways: `run_shell` auto-saves an `AUTO` lesson via `log_failed_command` on failure, and `self_review` extracts lessons periodically. Lessons are relevance-scored (`get_relevant_lessons`, `MAX_LESSONS_IN_PROMPT`) into the system prompt.
- **Cron reminders fire only while the bot process runs** (in-process scheduler, `data/cron_jobs.json`). Missed slots are skipped, not back-filled. `schedule_batch_reminders` exists because 25 timetable jobs would blow past `MAX_TOOL_ITERATIONS=30` as individual calls.
- **Kill switch:** `/lockdown` persists `data/shell_lock.json`; `run_shell` refuses while `runtime_config.SHELL_ENABLED` is False. Dangerous commands (and all commands under `CONFIRM_ALL_COMMANDS`) need user confirmation via inline button, auto-cancelled after `CONFIRMATION_TIMEOUT_SECONDS`.
- **Service runs as LocalSystem with `cwd=%SystemRoot%\System32`.** `config.py` must load `.env` from `PROJECT_ROOT` explicitly (never a bare `load_dotenv()`). `service.py` chdirs to the repo in `SvcDoRun` as a belt-and-braces measure. Service commands must run from an elevated shell (non-admin: `install` fails with access denied; `deploy.py` self-elevates via UAC).
- **`pythonservice.exe` needs `venv\Scripts` placement + adjacent DLLs.** The host links `python*.dll`, `vcruntime140*.dll` **and `pywintypes*.dll`** at load time (any missing → instant exit 0xC0000135 `STATUS_DLL_NOT_FOUND`). And on **Python 3.13** an interpreter exe at the venv *root* is treated as a base install, so `venv\Lib\site-packages` (where `servicemanager` lives) never lands on `sys.path` and the host dies with "unable to locate the service manager". `service.py install` / `deploy.py` copy the host + DLLs into `venv\Scripts` and point the ImagePath there — preserve that when touching either file.
- **The SYSTEM auto-updater can't touch your git repo without trust.** `monitor._git()` must keep passing `-c safe.directory=<repo>`, or the LocalSystem `git fetch`/`pull` fails with "dubious ownership" (the user owns the repo, SYSTEM doesn't).

## Verification quirks

- No unit tests. Ad-hoc verification scripts run with `sys.path.insert(0, <repo>)` then import modules directly.
- Scripts that call `add_job`/`add_lesson`/`_append_jsonl` write into the live `data/` files. Back up the file (or the whole `data/` dir) before testing and restore after — stray test lessons/jobs pollute the running bot's memory. `config.py` creates `data/` at import, so fresh clones won't fail on first write.
- Deployed in multiple places with **different `.env`**: `D:\Alfred\alfred-agent` (dev, remote Ollama `172.16.4.116`) and `C:\1\alfred-agent` (prod, `localhost:11434`) plus any `deploy_config.json` targets. The auto-updater `git pull`s new code but **never restarts** — push updates with `deploy.py` (which reinstalls + restarts the service) or `/update` to actually apply changes, so a "fixed" bug can still appear live until then.

## Docs

- `progress.md` is the living architecture + bug log (bugs #1–23, env-var table, command table). **Update it on every change.** `README.md` covers service deployment and is authoritative for WinSW/WSL quirks.
