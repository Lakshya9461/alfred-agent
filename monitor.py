"""
Background monitoring tasks:
- monitor_models: polls Ollama for newly installed models and notifies the user.
- check_for_updates: periodically fetches the git remote and auto-pulls (ff-only).
"""
import asyncio
import logging
import subprocess

from config import (
    PROJECT_ROOT,
    TELEGRAM_ALLOWED_USER_IDS,
    MODEL_CHECK_INTERVAL,
    GIT_UPDATE_CHECK_INTERVAL,
    AUTO_PULL,
)
import ollama_utils

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
    """Run a git command in the project root."""
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
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
