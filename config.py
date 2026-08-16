import os
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Load .env from the repo root explicitly. Running as a Windows service the
# working dir is %SystemRoot%\System32, so a bare load_dotenv() would miss it.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# data/ is gitignored, so a fresh clone won't have it — create it now or every
# log/memory write fails with "No such file or directory" on first run.
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Parse comma-separated list into a set of ints, if possible
_allowed_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
TELEGRAM_ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in _allowed_ids.split(",") if uid.strip().isdigit()
)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "30"))
SELF_REVIEW_EVERY_N_TURNS = int(os.getenv("SELF_REVIEW_EVERY_N_TURNS", "5"))

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SHELL_WORKING_DIR = os.getenv("SHELL_WORKING_DIR") or PROJECT_ROOT
SHELL_TIMEOUT_SECONDS = int(os.getenv("SHELL_TIMEOUT_SECONDS", "30"))
MAX_LESSONS = int(os.getenv("MAX_LESSONS", "50"))

# Background monitoring
MODEL_CHECK_INTERVAL = int(os.getenv("MODEL_CHECK_INTERVAL", "60"))
GIT_UPDATE_CHECK_INTERVAL = int(os.getenv("GIT_UPDATE_CHECK_INTERVAL", "300"))
AUTO_PULL = os.getenv("AUTO_PULL", "true").lower() in ("1", "true", "yes", "on")

# How often (seconds) the cron scheduler checks for due reminders
CRON_CHECK_INTERVAL = int(os.getenv("CRON_CHECK_INTERVAL", "20"))

# Seconds to wait for a user to confirm a dangerous command before auto-cancelling
CONFIRMATION_TIMEOUT_SECONDS = int(os.getenv("CONFIRMATION_TIMEOUT_SECONDS", "120"))

# Command used to restart the bot (e.g. service restart). Empty = respawn self (dev mode).
RESTART_COMMAND = os.getenv("RESTART_COMMAND", "")

# Max size of append-only logs before rotation (bytes)
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "10485760"))

# Max lessons fed into the system prompt (relevance-scored)
MAX_LESSONS_IN_PROMPT = int(os.getenv("MAX_LESSONS_IN_PROMPT", "20"))

# Trial mode: require confirmation for EVERY shell command, not just dangerous ones
CONFIRM_ALL_COMMANDS = os.getenv("CONFIRM_ALL_COMMANDS", "false").lower() in ("1", "true", "yes", "on")

# Per-request timeout for Ollama API calls. The first /api/chat after startup
# triggers a cold model load which can take a while on big models, so keep this generous.
OLLAMA_REQUEST_TIMEOUT = int(os.getenv("OLLAMA_REQUEST_TIMEOUT", "300"))

# Hard cap on the context window sent to Ollama (num_ctx). 0 = use the
# auto-detected value. Set this (e.g. 32768) on machines that can't allocate
# the full KV cache for a huge-context model like ornith:9b (262144).
OLLAMA_CONTEXT_LENGTH = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "0"))


def effective_context_length(detected: int) -> int:
    """Context window to actually send to Ollama: env override wins, else detected."""
    if OLLAMA_CONTEXT_LENGTH > 0:
        return OLLAMA_CONTEXT_LENGTH
    return detected


# ── Skills (SKILL.md packs cloned into data/skills) ─────────────────────────
# Comma-separated git URLs of skill repositories Alfred should install and keep
# updated. Skills are plain Markdown; the model loads their full text on demand.
SKILL_REPOS = [
    u.strip()
    for u in os.getenv(
        "SKILL_REPOS",
        "https://github.com/addyosmani/agent-skills.git,"
        "https://github.com/mattpocock/skills.git",
    ).split(",")
    if u.strip()
]
# Seconds between background skill-repo refresh + GitHub discovery passes
SKILL_UPDATE_INTERVAL = int(os.getenv("SKILL_UPDATE_INTERVAL", "21600"))

# ── Self-improvement loop (self_improve.py) ─────────────────────────────────
# Seconds between autonomous research passes. 0 = disabled.
SELF_IMPROVE_INTERVAL = int(os.getenv("SELF_IMPROVE_INTERVAL", "1800"))
# Hard cap on autonomous code applies per rolling day (safety valve).
SELF_IMPROVE_MAX_PER_DAY = int(os.getenv("SELF_IMPROVE_MAX_PER_DAY", "3"))
# Second Ollama model used as a "critic" for second opinions during research.
# Empty = reuse the active model.
CRITIC_MODEL = os.getenv("CRITIC_MODEL", "")
# Max files a single autonomous apply may touch (keep patches tiny).
SELF_IMPROVE_MAX_FILES = int(os.getenv("SELF_IMPROVE_MAX_FILES", "2"))
# The self-improvement journal (committed to git).
PROGRESS_AGENT_FILE = os.getenv("PROGRESS_AGENT_FILE", "progress_agent.md")

# ── Browser automation (browser-use / Playwright) ───────────────────────────
# Requires `pip install browser-use playwright` + `playwright install chromium`.
# Falls back to plain HTTP fetch when unavailable.
BROWSER_USE_ENABLED = os.getenv("BROWSER_USE_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").lower() in (
    "1", "true", "yes", "on"
)
BROWSER_USE_TIMEOUT = int(os.getenv("BROWSER_USE_TIMEOUT", "180"))

# ── External LLM (future OpenAI-compatible API for the research loop) ───────
# Leave empty to use only local Ollama. When set, the research loop may use an
# external OpenAI-compatible endpoint in addition to the local critic model.
EXTERNAL_LLM_BASE_URL = os.getenv("EXTERNAL_LLM_BASE_URL", "")
EXTERNAL_LLM_API_KEY = os.getenv("EXTERNAL_LLM_API_KEY", "")
EXTERNAL_LLM_MODEL = os.getenv("EXTERNAL_LLM_MODEL", "")
