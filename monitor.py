"""
Background monitoring tasks:
- monitor_models: polls Ollama for newly installed models and notifies the user.
- check_for_updates: periodically fetches the git remote and auto-pulls (ff-only).
"""
import asyncio
import logging
import os
import subprocess
import sys

from config import (
    PROJECT_ROOT,
    TELEGRAM_ALLOWED_USER_IDS,
    MODEL_CHECK_INTERVAL,
    GIT_UPDATE_CHECK_INTERVAL,
    AUTO_PULL,
    CRON_CHECK_INTERVAL,
    SKILL_UPDATE_INTERVAL,
)
import ollama_utils
from tools import cron as cron_tools
import skills

logger = logging.getLogger(__name__)


async def monitor_models(bot):
    """
    Watch for newly installed Ollama models.
    The first successful fetch sets the baseline; subsequent new models trigger
    a Telegram notification to all allowed users.
    """
    known = None
    while True:
        await asyncio.sleep(MODEL_CHECK_INTERVAL)
        try:
            models = set(await ollama_utils.get_available_models())
        except Exception as e:
            logger.error(f"monitor_models: failed to fetch models: {e}")
            continue

        if not models:
            continue
        if known is None:
            known = models
            continue

        new_models = models - known
        if new_models:
            msg = "🔔 *New model(s) detected in Ollama:*\n\n" + "\n".join(
                f"- `{m}`" for m in sorted(new_models)
            ) + "\n\nUse /model to switch."
            for user_id in TELEGRAM_ALLOWED_USER_IDS:
                try:
                    await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"monitor_models: failed to notify {user_id}: {e}")
        known = models


async def run_cron_scheduler(bot):
    """
    Periodically fire due cron reminders. A job fires at most once per minute.
    Fired reminders are sent as Telegram messages to all allowed users.
    """
    await asyncio.sleep(10)  # small initial delay so startup isn't interrupted
    while True:
        try:
            due = await asyncio.to_thread(cron_tools.fire_due_jobs)
            for job in due:
                msg = f"⏰ *Reminder:* {job.get('message', '')}"
                for user_id in TELEGRAM_ALLOWED_USER_IDS:
                    try:
                        await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"run_cron_scheduler: failed to notify {user_id}: {e}")
        except Exception as e:
            logger.error(f"run_cron_scheduler: {e}")
        await asyncio.sleep(CRON_CHECK_INTERVAL)


async def update_skills(bot):
    """
    Background task: keep installed skill repos up to date and periodically
    search GitHub for new agent-skill repositories, notifying the user with
    Install / Dismiss buttons for each new candidate.
    """
    await asyncio.sleep(25)
    while True:
        try:
            await asyncio.to_thread(skills.ensure_repos)
        except Exception as e:
            logger.error(f"update_skills: refresh failed: {e}")

        try:
            candidates = await asyncio.to_thread(skills.new_candidates)
        except Exception as e:
            logger.error(f"update_skills: discovery failed: {e}")
            candidates = []

        for cand in candidates[:2]:
            msg = (
                f"🧠 *New skill repo candidate:*\n\n"
                f"**{cand['full_name']}** ⭐{cand['stars']}\n"
                f"{cand['description']}\n\n"
                f"[Repo]({cand['html_url']})"
            )
            reply = {
                "inline_keyboard": [
                    [
                        {"text": "Install", "callback_data": f"skill|yes|{cand['clone_url']}"},
                        {"text": "Dismiss", "callback_data": f"skill|no|{cand['full_name']}"},
                    ]
                ]
            }
            for user_id in TELEGRAM_ALLOWED_USER_IDS:
                try:
                    await bot.send_message(
                        chat_id=user_id, text=msg, parse_mode="Markdown",
                        reply_markup=reply,
                    )
                except Exception as e:
                    logger.error(f"update_skills: failed to notify {user_id}: {e}")

        await asyncio.sleep(SKILL_UPDATE_INTERVAL)


async def check_for_updates(bot):
    """
    Periodically fetch the git remote and, if behind, auto-pull (ff-only).
    Notifies allowed users about available/resulting updates.
    """
    await asyncio.sleep(15)  # small initial delay so startup isn't interrupted
    while True:
        try:
            messages = await asyncio.to_thread(_check_and_pull)
        except Exception as e:
            logger.error(f"check_for_updates: {e}")
            await asyncio.sleep(GIT_UPDATE_CHECK_INTERVAL)
            continue

        for msg in messages:
            for user_id in TELEGRAM_ALLOWED_USER_IDS:
                try:
                    await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"check_for_updates: failed to notify {user_id}: {e}")

        await asyncio.sleep(GIT_UPDATE_CHECK_INTERVAL)


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in the project root.

    safe.directory is passed per-invocation: the service runs as LocalSystem,
    which doesn't own the repo, so git would refuse ('dubious ownership')
    without trusting the path explicitly."""
    trust = f"safe.directory={PROJECT_ROOT.replace(os.sep, '/')}"
    return subprocess.run(
        ["git", "-c", trust, *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _check_and_pull() -> list[str]:
    """
    Fetch origin and pull new commits (ff-only). Runs in a worker thread.
    Returns a list of notification strings (empty when up to date).
    """
    messages = []

    fetch = _git("fetch", "origin")
    if fetch.returncode != 0:
        logger.warning(f"check_for_updates: git fetch failed: {fetch.stderr.strip()}")
        return messages

    head = _git("rev-parse", "HEAD").stdout.strip()
    upstream = _git("rev-parse", "@{u}").stdout.strip()
    if not upstream or head == upstream:
        return messages

    behind = _git("rev-list", "--count", f"{head}..{upstream}").stdout.strip() or "?"

    if not AUTO_PULL:
        messages.append(
            f"🔔 *Update available:* {behind} new commit(s) on the remote.\n"
            "Pull the changes (or restart me) to apply them."
        )
        return messages

    pull = _git("pull", "--ff-only")
    if pull.returncode == 0:
        new_head = _git("log", "-1", "--oneline").stdout.strip()
        messages.append(
            f"✅ *Auto-updated to:* `{new_head}`\n"
            "A restart is recommended to apply the changes."
        )
    else:
        messages.append(
            f"⚠️ *Auto-update failed:* {behind} new commit(s) available.\n"
            f"`{pull.stderr.strip()[:300]}`"
        )
    return messages


def pull_updates() -> tuple[str, bool]:
    """
    Fetch origin and ff-pull the remote (manual trigger for /update).
    Returns (message, changed) where changed is True only if a pull succeeded.
    """
    fetch = _git("fetch", "origin")
    if fetch.returncode != 0:
        return (f"❌ *git fetch failed:*\n`{fetch.stderr.strip()[:500]}`", False)

    head = _git("rev-parse", "HEAD").stdout.strip()
    upstream = _git("rev-parse", "@{u}").stdout.strip()
    if not upstream:
        return ("⚠️ No upstream branch is configured — can't auto-update.", False)
    if head == upstream:
        return ("✅ Already up to date.", False)

    pull = _git("pull", "--ff-only")
    if pull.returncode != 0:
        return (f"❌ *git pull failed:*\n`{pull.stderr.strip()[:500]}`", False)

    new_head = _git("log", "-1", "--oneline").stdout.strip()
    return (f"✅ *Updated to* `{new_head}`", True)


def restart_bot() -> None:
    """
    Restart the bot process.
    If RESTART_COMMAND is set (custom service command) it is executed; under the
    pywin32 service (ALFRED_SERVICE_NAME set by service.py) a detached
    `python service.py restart` asks the SCM to restart us; otherwise the bot
    respawns itself (dev mode). The current process then exits immediately.
    """
    from config import RESTART_COMMAND

    kwargs = {
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    service_name = os.environ.get("ALFRED_SERVICE_NAME", "")
    if RESTART_COMMAND:
        subprocess.Popen(RESTART_COMMAND, shell=True, **kwargs)
    elif service_name:
        service_py = os.path.join(PROJECT_ROOT, "service.py")
        subprocess.Popen(
            [sys.executable, service_py, "restart"],
            cwd=PROJECT_ROOT,
            **kwargs,
        )
    else:
        subprocess.Popen([sys.executable, "main.py"], cwd=PROJECT_ROOT, **kwargs)
    os._exit(0)
