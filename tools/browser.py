"""
Browser automation via browser-use + Playwright (real Chromium), with an httpx
fallback for plain pages.

Three entry points, all sync (execute_tool runs them via asyncio.to_thread):
  - browse_web(url=None, task=None): open a URL and/or run a natural-language
    browsing task. The browser is driven by Ollama through browser-use's native
    ChatOllama adapter.
  - consult_chatgpt(question): drive a real browser to chat.openai.com, send a
    prompt to ChatGPT and return its reply. Requires a logged-in ChatGPT session
    in the Playwright user profile (see README). Best-effort: fails gracefully.
  - fetch_url(url): plain HTTP GET fallback when browser-use is unavailable.

Browser-use is a heavy optional dependency — imports are lazy so the bot still
works (with fetch_url fallback) before `pip install browser-use playwright` and
`playwright install chromium` have been run.
"""
import asyncio
import logging
import os

from config import (
    BROWSER_USE_ENABLED,
    BROWSER_HEADLESS,
    BROWSER_USE_TIMEOUT,
    OLLAMA_API_URL,
    PROJECT_ROOT,
)
from runtime_config import CURRENT_MODEL, CURRENT_CONTEXT_LENGTH

logger = logging.getLogger(__name__)

_CHATGPT_URL = "https://chat.openai.com"
# Persistent browser profile so the ChatGPT login survives restarts.
_PROFILE_DIR = os.path.join(PROJECT_ROOT, "data", "browser_profile")

_llm = None


def _get_llm():
    """Lazy browser-use ChatOllama adapter (drives the browser via local Ollama)."""
    global _llm
    if _llm is None:
        from browser_use.llm.ollama.chat import ChatOllama

        _llm = ChatOllama(
            model=CURRENT_MODEL,
            host=OLLAMA_API_URL.rstrip("/"),
            timeout=BROWSER_USE_TIMEOUT,
            ollama_options={"num_ctx": CURRENT_CONTEXT_LENGTH},
        )
    return _llm


def browser_available() -> bool:
    if not BROWSER_USE_ENABLED:
        return False
    try:
        import browser_use  # noqa: F401
        from browser_use.llm.ollama.chat import ChatOllama  # noqa: F401
        return True
    except ImportError:
        return False


def _run_agent(task: str, timeout: int = None) -> str:
    """Run a browser-use agent to completion and return its final output."""
    from browser_use import Agent

    timeout = timeout or BROWSER_USE_TIMEOUT
    try:
        result = asyncio.run(
            asyncio.wait_for(
                Agent(task=task, llm=_get_llm()).run(max_steps=15),
                timeout=timeout,
            )
        )
    except asyncio.TimeoutError:
        return f"Browsing timed out after {timeout}s (Ollama or the target site may be slow)."
    except Exception as e:
        return f"browser-use error: {type(e).__name__}: {e}"
    text = str(result).strip()
    if not text:
        return "(browser-use returned no readable output)"
    return text[:8000]


def browse_web(url: str = None, task: str = None) -> str:
    """Open a URL and/or run a natural-language browsing task in a real browser.

    The model describes what to do in plain language (e.g. "go to X and extract
    Y"). browser-use + Ollama translate that into clicks/scrolls/reads.
    """
    if not browser_available():
        if url:
            return fetch_url(url)
        return (
            "browser-use is not installed on this host. Install with "
            "`pip install browser-use playwright` + `playwright install chromium`."
        )
    if url and not task:
        task = f"Open {url}, read the page, and return a concise summary of its main content."
    elif url and task:
        task = f"Go to {url}. Then: {task}"
    if not task:
        return "browse_web needs a url and/or a task."
    return _run_agent(task)


def consult_chatgpt(question: str) -> str:
    """Ask ChatGPT a question by driving a real browser to chat.openai.com.

    Requires an already-logged-in ChatGPT session in the persistent Playwright
    profile (data/browser_profile). If a login wall is hit, this reports that
    instead of trying to authenticate.
    """
    if not browser_available():
        return (
            "browser-use is not installed on this host — cannot consult ChatGPT. "
            "Install with `pip install browser-use playwright` + `playwright install chromium`."
        )
    task = (
        f"Open {_CHATGPT_URL}. If you are not logged in (a login/signup screen is "
        f"shown), stop immediately and report: 'ChatGPT login required'. Otherwise "
        f"send the following as a new prompt in the chat, wait for ChatGPT to finish "
        f"replying, and return the full text of ChatGPT's answer verbatim.\n\n"
        f"QUESTION: {question}"
    )
    return _run_agent(task, timeout=240)


def fetch_url(url: str) -> str:
    """Plain HTTP fetch of a URL (fallback when browser-use is unavailable)."""
    import httpx

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            resp.raise_for_status()
        text = resp.text
    except Exception as e:
        return f"fetch_url error: {type(e).__name__}: {e}"
    # Strip obvious markup noise for the LLM.
    import re

    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000] or "(empty page)"