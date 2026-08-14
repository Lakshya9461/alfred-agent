import os
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Parse comma-separated list into a set of ints, if possible
_allowed_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
TELEGRAM_ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in _allowed_ids.split(",") if uid.strip().isdigit()
)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "10"))
SELF_REVIEW_EVERY_N_TURNS = int(os.getenv("SELF_REVIEW_EVERY_N_TURNS", "5"))

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SHELL_WORKING_DIR = os.getenv("SHELL_WORKING_DIR") or PROJECT_ROOT
SHELL_TIMEOUT_SECONDS = int(os.getenv("SHELL_TIMEOUT_SECONDS", "30"))
MAX_LESSONS = int(os.getenv("MAX_LESSONS", "50"))

# Background monitoring
MODEL_CHECK_INTERVAL = int(os.getenv("MODEL_CHECK_INTERVAL", "60"))
GIT_UPDATE_CHECK_INTERVAL = int(os.getenv("GIT_UPDATE_CHECK_INTERVAL", "300"))
AUTO_PULL = os.getenv("AUTO_PULL", "true").lower() in ("1", "true", "yes", "on")
