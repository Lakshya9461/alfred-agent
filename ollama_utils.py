"""
Helpers for querying the Ollama API for model discovery and metadata.
"""
import httpx
import logging
from typing import List, Tuple

from config import OLLAMA_API_URL

logger = logging.getLogger(__name__)

_BASE = lambda: OLLAMA_API_URL.rstrip("/")


async def get_available_models() -> List[str]:
    """Return a list of model names available on the Ollama host."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE()}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        logger.error(f"Failed to fetch Ollama models: {e}")
        return []


async def get_model_context_length(model_name: str) -> Tuple[int, dict]:
    """
    Query Ollama /api/show for a model and extract its context window.
    Returns (context_length, raw_model_info).
    Falls back to 4096 if not detectable.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_BASE()}/api/show",
                json={"name": model_name}
            )
            resp.raise_for_status()
            info = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch model info for {model_name}: {e}")
        return 4096, {}

    # Ollama ≥0.1.32 puts arch-specific keys in model_info
    model_info = info.get("model_info", {})
    for key, val in model_info.items():
        if "context_length" in key:
            try:
                return int(val), info
            except (TypeError, ValueError):
                pass

    # Older builds: search the parameters string
    params = info.get("parameters", "")
    for line in params.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx":
            try:
                return int(parts[1]), info
            except ValueError:
                pass

    return 4096, info
