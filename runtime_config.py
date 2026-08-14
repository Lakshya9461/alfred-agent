"""
Mutable runtime configuration — values here can be changed at runtime
(e.g. via /model) without restarting the bot.
"""
import json
import os

from config import OLLAMA_MODEL, PROJECT_ROOT

# Active model — changed by /model command
CURRENT_MODEL: str = OLLAMA_MODEL

# Context window detected from Ollama model metadata
CURRENT_CONTEXT_LENGTH: int = 4096

# Kill switch: whether shell execution is allowed. Persisted to data/shell_lock.json
# so a /lockdown survives restarts.
SHELL_ENABLED: bool = True
_LOCK_FILE = os.path.join(PROJECT_ROOT, "data", "shell_lock.json")


def _load_shell_state():
    global SHELL_ENABLED
    try:
        if os.path.exists(_LOCK_FILE):
            with open(_LOCK_FILE, "r", encoding="utf-8") as f:
                SHELL_ENABLED = bool(json.load(f).get("enabled", True))
    except Exception:
        pass


def set_shell_enabled(enabled: bool) -> None:
    """Enable/disable shell execution and persist the state."""
    global SHELL_ENABLED
    SHELL_ENABLED = enabled
    try:
        with open(_LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": enabled}, f)
    except Exception:
        pass


_load_shell_state()
