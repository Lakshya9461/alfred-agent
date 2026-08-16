"""
Always-on self-improvement: Alfred researches (web search + browser-use +
second-opinion models), records everything to progress_agent.md, and — when the
research is concrete — applies small upgrades to its own code, commits, pushes
and restarts. It also wakes for task-triggered research (note_turn).

Safety nets built in (this code edits the running bot):
  - SELF_IMPROVE_MAX_PER_DAY rolling cap on autonomous applies.
  - BLOCKED_PATHS: it can never touch .env, data/, venv/, logs/, service.py,
    deploy.py, self_improve.py, progress.md or git internals.
  - Changes are py_compile-verified; on failure the working tree is reverted
    via git checkout.
  - Every change is a git commit, so anything can be rolled back.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import httpx

from config import (
    PROJECT_ROOT,
    SELF_IMPROVE_INTERVAL,
    SELF_IMPROVE_MAX_PER_DAY,
    SELF_IMPROVE_MAX_FILES,
    CRITIC_MODEL,
    PROGRESS_AGENT_FILE,
    OLLAMA_API_URL,
    OLLAMA_REQUEST_TIMEOUT,
    TELEGRAM_ALLOWED_USER_IDS,
)
from runtime_config import CURRENT_MODEL, CURRENT_CONTEXT_LENGTH
import monitor

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(PROJECT_ROOT, "data", "self_improve.json")
JOURNAL = os.path.join(PROJECT_ROOT, PROGRESS_AGENT_FILE)

# Path segments / filenames this module may NEVER write to: configuration,
# secrets, data, and deployment-critical or self-protective files.
BLOCKED_SEGMENTS = {"data", "venv", "logs", ".git"}
BLOCKED_NAMES = {
    "service.py",
    "deploy.py",
    "deploy_config.json",
    "self_improve.py",
    "progress.md",
    "runtime_config.py",
}

_write_lock = threading.Lock()
_wake_event = asyncio.Event()

APPLY_INSTRUCTION = """You are improving the Alfred Telegram bot repository. The user chose
FULL AUTONOMY: you implement the change yourself.

Research findings and the target file(s) are below. Produce a minimal, correct
patch. Output ONLY one or more blocks in EXACTLY this format (nothing else):

### FILE <relative/path/file.py>
<complete new content of that file>
### END

Rules:
- Keep the change minimal and focused on the improvement. Never rewrite files
  wholesale.
- Preserve existing imports, style and the surrounding code; only add what the
  improvement needs.
- Do not touch any file other than the ones listed in the request.
- Do not add comments beyond what the change requires.
- If you cannot make the change safely, reply with exactly: NO_CHANGE"""


# ── state / journal ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("candidates", [])
            st.setdefault("applied_days", {})
            st.setdefault("last_pass", "")
            return st
    except Exception as e:
        logger.warning(f"self_improve: cannot read state: {e}")
    return {"candidates": [], "applied_days": {}, "last_pass": ""}


def _save_state(st: dict) -> None:
    with _write_lock:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"self_improve: cannot write state: {e}")


def _append_journal(kind: str, title: str, body: str) -> None:
    """Thread-safe append of a dated block to progress_agent.md."""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n\n---\n\n## [{kind}] {now} — {title}\n\n{body.strip()}"
    )
    with _write_lock:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        try:
            with open(JOURNAL, "a", encoding="utf-8") as f:
                f.write(block + "\n")
        except Exception as e:
            logger.error(f"self_improve: cannot append journal: {e}")


# ── git helpers ──────────────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    trust = f"safe.directory={PROJECT_ROOT.replace(os.sep, '/')}"
    return subprocess.run(
        ["git", "-c", trust, *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _git_ok() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").returncode == 0


# ── ollama chat helper (sync; call via asyncio.to_thread) ────────────────────

def _ollama_chat(system: str, prompt: str, model: str = None) -> str:
    """Single non-tool Ollama /api/chat completion."""
    base = OLLAMA_API_URL.rstrip("/")
    try:
        with httpx.Client(timeout=OLLAMA_REQUEST_TIMEOUT) as client:
            resp = client.post(
                f"{base}/api/chat",
                json={
                    "model": model or CURRENT_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": {"num_ctx": CURRENT_CONTEXT_LENGTH},
                },
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.error(f"self_improve: ollama chat failed: {e}")
        return ""


# ── candidate selection ──────────────────────────────────────────────────────

def _recent_failed_commands(n: int = 3) -> list:
    """Pull recently failed shell commands from the audit log."""
    path = os.path.join(PROJECT_ROOT, "data", "audit_log.jsonl")
    out = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("outcome", "").startswith("executed_exit") and \
                            e.get("outcome") != "executed_exit_0":
                        out.append(e)
    except Exception:
        pass
    return out[-n:]


def _pick_candidate(st: dict) -> dict | None:
    open_items = [c for c in st["candidates"] if c.get("status") in (None, "open")]
    if open_items:
        open_items.sort(key=lambda c: c.get("created", ""))
        return open_items[0]

    fails = _recent_failed_commands(1)
    if fails:
        cmd = fails[-1].get("command", "")
        return {
            "topic": f"Recently failed shell command — investigate and improve handling",
            "detail": f"Command that failed: {cmd}\n(Auto-suggested from audit log.)",
            "source": "audit",
        }
    return None


# ── research ─────────────────────────────────────────────────────────────────

def _research_sync(candidate: dict) -> str:
    """Synchronous research: web search + critic model + (optionally) ChatGPT."""
    from tools.web_search import search

    topic = candidate.get("topic", "improve the bot")
    queries = [f"{topic} best practice 2026"]
    findings = []
    for q in queries:
        findings.append(f"### Search: {q}\n{search(q, max_results=4)}")

    detail = candidate.get("detail", "")
    critic = CRITIC_MODEL or CURRENT_MODEL
    critic_prompt = (
        f"You are a senior software reviewer consulted for a second opinion.\n"
        f"Improvement topic: {topic}\n"
        f"{detail}\n\n"
        f"Search findings:\n{chr(10).join(findings)}\n\n"
        f"Give a concrete, minimal recommendation for improving this Python "
        f"Telegram bot (python-telegram-bot + Ollama tool loop, Windows). "
        f"Specify exactly which file(s) to change and why. Keep it to 1-3 "
        f"sentences if the finding is weak."
    )
    critic_opinion = _ollama_chat(
        "You are a critical second opinion for a self-improving agent.",
        critic_prompt,
        model=critic,
    )

    summary = "\n\n".join([*findings, f"### Critic ({critic})\n{critic_opinion}"])
    return summary


async def research(bot, candidate: dict) -> dict | None:
    """Run a research pass. Returns a proposal dict or None."""
    topic = candidate.get("topic", "improve the bot")
    summary = await asyncio.to_thread(_research_sync, candidate)
    _append_journal("RESEARCH", topic, summary)

    # Ask the main model to turn findings into a concrete proposal.
    repo_overview = _repo_overview()
    propose_prompt = (
        f"Improvement topic: {topic}\n"
        f"{candidate.get('detail', '')}\n\n"
        f"Repository overview:\n{repo_overview}\n\n"
        f"Research findings:\n{summary}\n\n"
        f"Decide if there is a concrete, low-risk code improvement that follows "
        f"directly from the findings. If yes, output a JSON object:\n"
        f'{{"decision": "apply", "files": ["relative/path.py"], '
        f'"change_summary": "one sentence"}}\n'
        f"If not (research is too weak or risky), output: "
        f'{{"decision": "skip", "files": [], "change_summary": "why"}}'
    )
    raw = _ollama_chat(
        "You are Alfred, deciding whether a researched improvement is safe to apply.",
        propose_prompt,
    )
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        decision = json.loads(m.group(0)) if m else {}
    except Exception:
        decision = {}

    if decision.get("decision") != "apply" or not decision.get("files"):
        _append_journal("SKIP", topic, decision.get("change_summary", "no concrete finding"))
        return None

    return {
        "topic": topic,
        "files": [f.replace("\\", "/") for f in decision["files"]],
        "change_summary": decision.get("change_summary", ""),
        "research": summary,
    }


def _repo_overview() -> str:
    files = sorted(
        f for f in os.listdir(PROJECT_ROOT)
        if f.endswith(".py") and os.path.isfile(os.path.join(PROJECT_ROOT, f))
    )
    log = _git("log", "-5", "--oneline").stdout.strip()
    return "Top-level .py files: " + ", ".join(files) + f"\nRecent commits:\n{log}"


# ── apply (autonomous) ───────────────────────────────────────────────────────

def _blocked(path: str) -> bool:
    p = path.replace("\\", "/")
    segs = p.split("/")
    if any(s in BLOCKED_SEGMENTS for s in segs):
        return True
    if segs[-1] in BLOCKED_NAMES:
        return True
    if segs[-1].startswith(".env") and not segs[-1].endswith(".example"):
        return True
    return False


def _read_file(path: str, max_chars: int = 6000) -> str:
    try:
        with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8", errors="replace") as f:
            return f.read()[:max_chars]
    except Exception:
        return ""


def _parse_patch(text: str) -> dict:
    """Parse '### FILE <path> ... ### END' blocks from the model output."""
    blocks = re.findall(r"### FILE\s+([^\n]+)\n(.*?)\n### END", text, re.DOTALL)
    return {p.strip(): c for p, c in blocks}


def _apply_sync(proposal: dict) -> dict:
    """Generate + write + verify the patch. Returns an outcome dict."""
    files = proposal["files"][:SELF_IMPROVE_MAX_FILES]
    files = [f for f in files if not _blocked(f) and not f.startswith(("/", ".."))]
    if not files:
        return {"ok": False, "reason": "all proposed files are blocked or invalid"}

    ctx = []
    for f in files:
        ctx.append(f"### FILE {f} (current content)\n{_read_file(f)}\n### END\n")
    prompt = (
        f"Improvement: {proposal.get('change_summary', proposal['topic'])}\n"
        f"Research:\n{proposal.get('research', '')[:4000]}\n\n"
        f"Files allowed to change (all of them):\n- "
        + "\n- ".join(files)
        + "\n\n"
        + "\n".join(ctx)
    )
    raw = _ollama_chat(APPLY_INSTRUCTION, prompt)
    if "NO_CHANGE" in raw.strip():
        return {"ok": False, "reason": "model declined (NO_CHANGE)"}

    patch = _parse_patch(raw)
    patch = {k: v for k, v in patch.items() if k in files}
    if not patch:
        return {"ok": False, "reason": "patch did not parse"}

    changed = []
    for path, content in patch.items():
        if _blocked(path) or path.startswith(("/", "..")):
            continue
        full = os.path.join(PROJECT_ROOT, path)
        os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        changed.append(path)

    if not changed:
        return {"ok": False, "reason": "nothing written"}

    # Verify: py_compile every changed .py file.
    py_files = [c for c in changed if c.endswith(".py")]
    if py_files:
        ok = True
        for c in py_files:
            res = subprocess.run(
                [sys.executable, "-m", "py_compile", os.path.join(PROJECT_ROOT, c)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if res.returncode != 0:
                ok = False
                logger.error(f"self_improve: py_compile failed for {c}: {res.stderr}")
        if not ok:
            _git("checkout", "--", *changed)
            return {"ok": False, "reason": f"py_compile failed; reverted {changed}"}

    return {"ok": True, "changed": changed, "summary": proposal.get("change_summary", "")}


def _commit_and_push(changed: list) -> str:
    """Commit + push the change and the journal. Returns a status string."""
    _git("add", "--", *changed, JOURNAL)
    _git(
        "-c", "user.name=Alfred",
        "-c", "user.email=alfred@local",
        "commit", "-m", f"[auto] self-improvement: {', '.join(changed)}",
    )
    push = _git("push", "origin", "master", timeout=90)
    if push.returncode != 0:
        return f"committed locally; push failed: {push.stderr.strip()[:200]}"
    return "committed and pushed"


async def apply(bot, proposal: dict) -> None:
    """Autonomous apply pipeline with daily cap."""
    today = dt.date.today().isoformat()
    st = _load_state()
    used = st.get("applied_days", {}).get(today, 0)
    if used >= SELF_IMPROVE_MAX_PER_DAY:
        _append_journal(
            "SKIP", proposal["topic"],
            f"Daily apply cap ({SELF_IMPROVE_MAX_PER_DAY}) reached.",
        )
        return

    outcome = await asyncio.to_thread(_apply_sync, proposal)
    if not outcome.get("ok"):
        _append_journal("FAILED", proposal["topic"], outcome.get("reason", "apply failed"))
        st["candidates"].append(
            {"topic": proposal["topic"], "status": "failed",
             "detail": outcome.get("reason", ""), "created": dt.datetime.now().isoformat()}
        )
        _save_state(st)
        return

    changed = outcome["changed"]
    status = _commit_and_push(changed)
    st["applied_days"][today] = used + 1
    _save_state(st)
    _append_journal(
        "APPLIED", proposal["topic"],
        f"Files changed: {', '.join(changed)}\n{outcome['summary']}\n\n{status}",
    )

    for user_id in TELEGRAM_ALLOWED_USER_IDS:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"🧠 *Self-improvement applied*\n\n"
                    f"`{outcome['summary']}`\n\n"
                    f"Files: `{', '.join(changed)}`\n{status}\n"
                    f"See `progress_agent.md` for research details."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"self_improve: notify failed: {e}")

    if any(c.endswith(".py") for c in changed):
        logger.info(f"self_improve: code changed, restarting ({changed})")
        await asyncio.sleep(1)
        await asyncio.to_thread(monitor.restart_bot)


# ── loop ─────────────────────────────────────────────────────────────────────

async def run_self_improve(bot):
    """Background loop: research candidates, apply, then wait (wakeable)."""
    if SELF_IMPROVE_INTERVAL <= 0:
        return
    await asyncio.sleep(45)
    while True:
        try:
            st = _load_state()
            candidate = _pick_candidate(st)
            if candidate and _git_ok():
                proposal = await research(bot, candidate)
                if proposal:
                    await apply(bot, proposal)
            st = _load_state()
            st["last_pass"] = dt.datetime.now().isoformat()
            _save_state(st)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"self_improve: pass failed: {e}")

        try:
            _wake_event.clear()
            await asyncio.wait_for(_wake_event.wait(), timeout=SELF_IMPROVE_INTERVAL)
        except asyncio.TimeoutError:
            pass


def kick() -> None:
    """Wake the loop early (called after task turns)."""
    _wake_event.set()


def note_turn(user_message: str, summary: str, failed: bool = False) -> None:
    """Post-turn hook: record the task in the journal; queue research on failure."""
    _append_journal(
        "TASK", user_message[:120],
        summary[:1500] + ("\n\n⚠️ This turn had errors — queued for research." if failed else ""),
    )
    if failed:
        st = _load_state()
        st["candidates"].append(
            {
                "topic": f"Investigate and fix the failure in: {user_message[:100]}",
                "detail": summary[:800],
                "source": "task",
                "status": "open",
                "created": dt.datetime.now().isoformat(),
            }
        )
        _save_state(st)
        kick()