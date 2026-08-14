"""
Memory management tool for learning lessons and keeping track of conversation context.
"""
import json
import os
import re
import threading
from datetime import datetime, UTC
from config import PROJECT_ROOT, MAX_LESSONS, LOG_MAX_BYTES

# Serializes JSONL appends — Windows "append" mode is not atomic across threads,
# so concurrent writers (block=False turns, background tasks) would drop lines.
_LOG_LOCK = threading.Lock()

def _rotate_if_large(filepath: str, max_bytes: int):
    """Rotate an append-only log once it exceeds max_bytes."""
    try:
        if os.path.exists(filepath) and os.path.getsize(filepath) > max_bytes:
            backup = filepath + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(filepath, backup)
    except Exception:
        pass

def _append_jsonl(filepath: str, entry: dict, max_bytes: int):
    """Thread-safe append of a JSON line, rotating the file first if oversized."""
    with _LOG_LOCK:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        _rotate_if_large(filepath, max_bytes)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

def log_conversation(entry: dict):
    """
    Logs a conversation turn to conversations.jsonl.
    """
    filepath = os.path.join(PROJECT_ROOT, "data", "conversations.jsonl")
    entry['timestamp'] = datetime.now(UTC).isoformat()
    try:
        _append_jsonl(filepath, entry, LOG_MAX_BYTES)
    except Exception as e:
        print(f"Failed to log conversation: {e}")

def load_lessons() -> list[dict]:
    lessons_path = os.path.join(PROJECT_ROOT, "data", "lessons.json")
    try:
        if os.path.exists(lessons_path):
            with open(lessons_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        return []
    except Exception:
        return []

def _save_lessons(lessons: list[dict]):
    lessons_path = os.path.join(PROJECT_ROOT, "data", "lessons.json")
    os.makedirs(os.path.dirname(lessons_path), exist_ok=True)
    with open(lessons_path, 'w', encoding='utf-8') as f:
        json.dump(lessons, f, indent=2)

def add_lesson(text: str, source: str = "user") -> str:
    lessons = load_lessons()
    
    # Dedupe check by text
    if any(l.get("text") == text for l in lessons):
        return "Lesson already exists."
        
    lessons.append({
        "text": text,
        "source": source,
        "timestamp": datetime.now(UTC).isoformat()
    })
    
    # Prune
    if len(lessons) > MAX_LESSONS:
        lessons = lessons[-MAX_LESSONS:]
        
    try:
        _save_lessons(lessons)
        return "Lesson saved successfully."
    except Exception as e:
        return f"Failed to save lesson: {e}"

def format_lessons_for_prompt(lessons: list[dict]) -> str:
    if not lessons:
        return "- None yet."
    
    formatted = []
    for l in lessons:
        ts = l.get("timestamp", "")[:10]
        src = l.get("source", "user").upper()
        formatted.append(f"- [{ts}] [{src}] {l.get('text')}")
    return "\n".join(formatted)

def score_lesson(lesson: dict, user_message: str) -> float:
    """Keyword-overlap score (dominant) plus a small recency bonus."""
    text = lesson.get("text", "").lower()
    msg = user_message.lower()
    text_words = set(re.findall(r"\w+", text))
    msg_words = set(re.findall(r"\w+", msg))
    overlap = len(text_words & msg_words)
    recency = 0.5 if lesson.get("timestamp", "")[:10] >= "2026" else 0.0
    return overlap * 3.0 + recency

def get_relevant_lessons(lessons: list[dict], user_message: str, top_n: int = 20) -> list[dict]:
    """
    Return the most relevant lessons for a user message: keyword overlap first,
    most recent fill the remaining slots so some context is always included.
    """
    if not lessons:
        return []
    ranked = sorted(lessons, key=lambda l: score_lesson(l, user_message), reverse=True)
    return ranked[:top_n]

def remove_lesson(index: int) -> str:
    """Removes a lesson by 1-based index."""
    lessons = load_lessons()
    if not (1 <= index <= len(lessons)):
        return f"Invalid index. Please choose a number between 1 and {len(lessons)}."
    
    removed = lessons.pop(index - 1)
    try:
        _save_lessons(lessons)
        return f"Removed lesson: {removed.get('text')}"
    except Exception as e:
        return f"Failed to remove lesson: {e}"

def log_failed_command(command: str, stderr: str) -> None:
    """
    Stores a structured note distinct from user-taught lessons.
    """
    lessons = load_lessons()
    
    text = f"Command failed: {command} | Error: {stderr}"
    # Dedupe
    if any(l.get("text") == text for l in lessons):
        return
        
    lessons.append({
        "text": text,
        "source": "auto",
        "timestamp": datetime.now(UTC).isoformat()
    })
    
    if len(lessons) > MAX_LESSONS:
        lessons = lessons[-MAX_LESSONS:]
        
    try:
        _save_lessons(lessons)
    except Exception:
        pass
