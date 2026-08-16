"""
Skills: SKILL.md packs cloned from external repos (e.g. addyosmani/agent-skills,
mattpocock/skills) plus GitHub discovery for self-upgrade.

Design: skills are plain Markdown. The system prompt carries only a compact
*index* (name + one-line description); the model calls the `read_skill` tool to
load the full instructions for a skill it actually needs (progressive
disclosure — keeps tokens minimal, which is exactly how these packs are meant
to be consumed).

Repos live under data/skills/ (gitignored). state is persisted in
data/skills_config.json (enabled / ignored / seen).
"""
import json
import logging
import os
import re
import subprocess
import threading

from config import PROJECT_ROOT, SKILL_REPOS

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(PROJECT_ROOT, "data", "skills")
CONFIG_FILE = os.path.join(PROJECT_ROOT, "data", "skills_config.json")

_write_lock = threading.Lock()
_skill_cache = None


def repo_name(url: str) -> str:
    """Derive a safe directory name from a repo URL."""
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"


_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
}


def _git(cwd: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run git in a skills dir. safe.directory is passed per-invocation so the
    LocalSystem service (which doesn't own the checkout) can fetch/pull.
    GIT_TERMINAL_PROMPT=0 + DEVNULL stdin make HTTPS/SSH fail fast instead of
    hanging on an interactive credential prompt (there is no TTY in a service)."""
    trust = f"safe.directory={cwd.replace(os.sep, '/')}"
    return subprocess.run(
        ["git", "-c", trust, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **_GIT_ENV},
    )


def load_config() -> dict:
    """Persisted skills state. First run seeds `enabled` from config.SKILL_REPOS."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("enabled", [])
            cfg.setdefault("ignored", [])
            cfg.setdefault("seen", [])
            return cfg
    except Exception as e:
        logger.warning(f"skills: failed to read {CONFIG_FILE}: {e}")
    return {"enabled": list(SKILL_REPOS), "ignored": [], "seen": []}


def save_config(cfg: dict) -> None:
    with _write_lock:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"skills: failed to write {CONFIG_FILE}: {e}")


def ensure_repos() -> dict:
    """Clone missing enabled repos and ff-pull existing ones. Returns config."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    cfg = load_config()
    for url in cfg["enabled"]:
        name = repo_name(url)
        dest = os.path.join(SKILLS_DIR, name)
        try:
            if not os.path.exists(os.path.join(dest, ".git")):
                res = subprocess.run(
                    ["git", "clone", "--depth", "1", url, dest],
                    cwd=SKILLS_DIR,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, **_GIT_ENV},
                )
                if res.returncode != 0:
                    logger.warning(f"skills: clone {url} failed: {res.stderr.strip()[:200]}")
                    continue
                logger.info(f"skills: cloned {url}")
            else:
                res = _git(dest, "pull", "--ff-only", timeout=120)
                if res.returncode != 0:
                    logger.warning(f"skills: pull {name} failed: {res.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning(f"skills: git operation timed out for {url}")
        except Exception as e:
            logger.error(f"skills: error updating {url}: {e}")
    invalidate_cache()
    return cfg


def enable_repo(url: str) -> None:
    """Add a repo to the enabled list and clone it."""
    cfg = load_config()
    if url not in cfg["enabled"]:
        cfg["enabled"].append(url)
    save_config(cfg)
    ensure_repos()


def ignore_repo(full_name: str) -> None:
    """Never suggest or auto-add this repo again."""
    cfg = load_config()
    if full_name not in cfg["ignored"]:
        cfg["ignored"].append(full_name)
    if full_name not in cfg["seen"]:
        cfg["seen"].append(full_name)
    save_config(cfg)


def mark_seen(full_name: str) -> None:
    cfg = load_config()
    if full_name not in cfg["seen"]:
        cfg["seen"].append(full_name)
        save_config(cfg)


def invalidate_cache() -> None:
    global _skill_cache
    _skill_cache = None


def _parse_frontmatter(content: str) -> dict:
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta


def _scan() -> list:
    """Scan cloned repos for SKILL.md files and index name/description."""
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "SKILL.md" in files:
            path = os.path.join(root, "SKILL.md")
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                logger.warning(f"skills: cannot read {path}: {e}")
                continue
            meta = _parse_frontmatter(content)
            rel = os.path.relpath(root, SKILLS_DIR)
            name = meta.get("name") or os.path.basename(root)
            description = meta.get("description", "")
            if not description:
                # Fall back to the first non-empty body line
                body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
                description = next(
                    (ln.strip() for ln in body.splitlines() if ln.strip()), ""
                )
            skills.append(
                {
                    "name": name,
                    "description": description[:200],
                    "repo": rel.split(os.sep)[0],
                    "path": path,
                    "content": content,
                }
            )
    return skills


def load_skills() -> list:
    """Return the skill index (cached until a repo refresh invalidates it)."""
    global _skill_cache
    if _skill_cache is None:
        _skill_cache = _scan()
    return _skill_cache


def format_skill_index(skills: list) -> str:
    """Compact index for the system prompt."""
    if not skills:
        return "(no skills installed yet)"
    lines = []
    for s in sorted(skills, key=lambda x: (x["repo"], x["name"])):
        lines.append(f"- `{s['name']}` ({s['repo']}) — {s['description']}")
    return "\n".join(lines)


def read_skill(name: str) -> str:
    """Return the full SKILL.md content for a skill, or a helpful message."""
    skills = load_skills()
    if not skills:
        return "No skills are installed yet."
    for s in skills:
        if s["name"].lower() == name.strip().lower():
            return s["content"]
    for s in skills:
        if name.strip().lower() in s["name"].lower():
            return s["content"]
    available = ", ".join(sorted(s["name"] for s in skills)) or "none"
    return f"Skill '{name}' not found. Installed skills: {available}"


SEARCH_QUERIES = [
    "agent skills",
    "claude skills",
    "agentic skills",
    "ai agent skills",
    "agent tools",
    "llm agent skills",
]


def search_candidates() -> list:
    """GitHub API search for promising agent-skill repos.

    The query rotates through several phrasings across calls (persisted in the
    config file), so repeated discovery passes surface DIFFERENT repos instead
    of always returning the same top results for one fixed query.
    """
    cfg = load_config()
    idx = int(cfg.get("search_idx", 0))
    query = SEARCH_QUERIES[idx % len(SEARCH_QUERIES)]
    cfg["search_idx"] = idx + 1
    save_config(cfg)
    try:
        import httpx

        with httpx.Client(timeout=20) as client:
            resp = client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 20,
                },
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "alfred-agent",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as e:
        logger.warning(f"skills: GitHub search failed: {e}")
        return []

    candidates = []
    for it in items:
        if it.get("stargazers_count", 0) < 100:
            continue
        full = it.get("full_name", "")
        if not full:
            continue
        candidates.append(
            {
                "full_name": full,
                "html_url": it.get("html_url", f"https://github.com/{full}"),
                "stars": it.get("stargazers_count", 0),
                "description": (it.get("description") or "")[:140],
                "clone_url": it.get("clone_url") or f"https://github.com/{full}.git",
            }
        )
    return candidates


def new_candidates() -> list:
    """Candidates not yet seen/ignored/enabled. Used by the background task."""
    cfg = load_config()
    enabled_names = {repo_name(u) for u in cfg["enabled"]}
    out = []
    for c in search_candidates():
        if c["full_name"] in cfg["seen"] or c["full_name"] in cfg["ignored"]:
            continue
        if repo_name(c["clone_url"]) in enabled_names:
            continue
        out.append(c)
    return out


def reason_candidate(candidate: dict) -> str:
    """Ask the self-improve model why this repo's skills are worth installing.

    Returns a short rationale (or empty string if the model is unreachable).
    Lazy import of self_improve avoids a module-level import cycle.
    """
    try:
        from self_improve import _ollama_chat
    except Exception as e:
        logger.warning(f"skills: cannot import chat helper: {e}")
        return ""
    installed = ", ".join(sorted({s["repo"] for s in load_skills()})) or "none yet"
    prompt = (
        "A new skill-repo candidate was discovered for this Telegram agent "
        "(python-telegram-bot + Ollama tool loop on Windows).\n\n"
        f"Candidate repo: {candidate['full_name']} (⭐{candidate['stars']})\n"
        f"Description: {candidate.get('description', '')}\n"
        f"Already-installed skill repos: {installed}\n\n"
        "Give a concrete reason (1-2 short sentences) why this repo's skills would "
        "benefit the bot, or — if they're not a good fit — say so in one sentence. "
        "Be specific about which kind of skills it adds and when they'd be used."
    )
    raw = _ollama_chat(
        "You are Alfred the butler, deciding whether a skill repo is worth adopting.",
        prompt,
    )
    return raw.strip()[:300]