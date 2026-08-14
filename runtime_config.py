"""
Mutable runtime configuration — values here can be changed at runtime
(e.g. via /model) without restarting the bot.
"""
from config import OLLAMA_MODEL

# Active model — changed by /model command
CURRENT_MODEL: str = OLLAMA_MODEL

# Context window detected from Ollama model metadata
CURRENT_CONTEXT_LENGTH: int = 4096
