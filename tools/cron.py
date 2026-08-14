"""
Cron-style reminders.

The model schedules jobs via the schedule_reminder tool; a background task in
monitor.py (run_cron_scheduler) fires due jobs and sends them to the user
through Telegram. Jobs persist to data/cron_jobs.json so they survive restarts.

Cron format: standard 5-field expression  minute hour day-of-month month day-of-week
  *            any value
  */n          every n units (e.g. */30 in minutes = every 30 minutes)
  a-b          range
  a,b,c        list
  day-of-week: 0-6 where 0 = Sunday (matches cron convention)
"""
import json
import os
import threading
import uuid
from datetime import datetime

from config import PROJECT_ROOT

JOBS_FILE = os.path.join(PROJECT_ROOT, "data", "cron_jobs.json")
_LOCK = threading.Lock()


def load_jobs() -> list[dict]:
    try:
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []
    return []


def _save_jobs(jobs: list[dict]):
    os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)


def add_job(message: str, cron: str, repeat: bool = True, source: str = "model") -> str:
    """Add a cron job. Returns a status string for the model to report."""
    if not message or not message.strip():
        return "Reminder message cannot be empty."
    if not _validate_cron(cron):
        return (
            f"Invalid cron expression '{cron}'. Use 5 fields: "
            "minute hour day-of-month month day-of-week (0-6, 0=Sunday). "
            "Example daily at 09:00 → '0 9 * * *'"
        )
    job = {
        "id": uuid.uuid4().hex[:8],
        "message": message.strip(),
        "cron": cron,
        "repeat": bool(repeat),
        "source": source,
        "last_fired": None,
        "created_at": datetime.now().isoformat(),
        "active": True,
    }
    with _LOCK:
        jobs = load_jobs()
        jobs.append(job)
        _save_jobs(jobs)
    return f"Reminder scheduled. ID: `{job['id']}` | cron `{cron}` | repeat={repeat}"


def list_jobs() -> list[dict]:
    with _LOCK:
        return load_jobs()


def remove_job(job_id: str) -> str:
    with _LOCK:
        jobs = load_jobs()
        remaining = [j for j in jobs if j.get("id") != job_id]
        if len(remaining) == len(jobs):
            return f"No reminder found with ID {job_id}."
        _save_jobs(remaining)
    return f"Reminder {job_id} removed."


def fire_due_jobs() -> list[dict]:
    """
    Return jobs whose cron matches the current minute and update their state.
    A job fires at most once per minute; one-shot jobs (repeat=False) are
    deactivated after firing. Thread-safe.
    """
    now = datetime.now()
    fired = []
    with _LOCK:
        jobs = load_jobs()
        for j in jobs:
            if not j.get("active", True):
                continue
            if not cron_matches(j.get("cron", ""), now):
                continue
            last = j.get("last_fired")
            if last:
                try:
                    if _same_minute(datetime.fromisoformat(last), now):
                        continue
                except ValueError:
                    pass
            fired.append(j)
            j["last_fired"] = now.isoformat()
            if not j.get("repeat", True):
                j["active"] = False
        if fired:
            _save_jobs(jobs)
    return fired


def cron_matches(cron: str, now: datetime) -> bool:
    try:
        minute, hour, dom, month, dow = cron.split()
    except ValueError:
        return False
    return (
        _field_matches(minute, now.minute)
        and _field_matches(hour, now.hour)
        and _field_matches(dom, now.day)
        and _field_matches(month, now.month)
        and _field_matches(dow, now.isoweekday() % 7)  # 0 = Sunday
    )


def _validate_cron(cron: str) -> bool:
    parts = cron.split()
    if len(parts) != 5:
        return False
    bounds = [59, 23, 31, 12, 7]
    try:
        for i, part in enumerate(parts):
            if not _valid_field(part, bounds[i]):
                return False
        return True
    except Exception:
        return False


def _valid_field(pattern: str, max_val: int) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*/"):
        step = pattern[2:]
        return step.isdigit() and 0 < int(step) <= max_val
    for part in pattern.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            if not (lo.isdigit() and hi.isdigit()):
                return False
            if not (0 <= int(lo) <= int(hi) <= max_val):
                return False
        else:
            if not part.isdigit() or not (0 <= int(part) <= max_val):
                return False
    return True


def _field_matches(pattern: str, value: int) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*/"):
        return value % int(pattern[2:]) == 0
    if "," in pattern:
        return any(_field_matches(part, value) for part in pattern.split(","))
    if "-" in pattern:
        lo, hi = map(int, pattern.split("-"))
        return lo <= value <= hi
    return value == int(pattern)


def _same_minute(a: datetime, b: datetime) -> bool:
    return (
        a.year == b.year
        and a.month == b.month
        and a.day == b.day
        and a.hour == b.hour
        and a.minute == b.minute
    )
