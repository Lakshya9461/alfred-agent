"""
Memory management tool for learning lessons and keeping track of conversation context.
"""
import json
from datetime import datetime, UTC
import os
from config import PROJECT_ROOT, MAX_LESSONS

def log_conversation(entry: dict):
    """
    Logs a conversation turn to conversations.jsonl.
    """
    filepath = os.path.join(PROJECT_ROOT, "data", "conversations.jsonl")
    entry['timestamp'] = datetime.now(UTC).isoformat()
    try:
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
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
