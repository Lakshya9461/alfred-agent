"""
Tool registry.
Maps tool names to their corresponding functions and schemas.
"""
import json
import asyncio
import inspect
from typing import Callable, Dict, Any
from .web_search import search
from .shell_exec import run_shell, ConfirmationRequired
from .memory import add_lesson
from .cron import add_job, add_batch, list_jobs, remove_job

TOOL_REGISTRY = {
    "web_search": {
        "schema": {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Searches the web for a given query and returns a summary of results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query."
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default 5)."
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "func": search
    },
    "run_shell": {
        "schema": {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": "Executes a shell command on the host.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute."
                        },
                        "shell": {
                            "type": "string",
                            "description": "The shell to use, either 'powershell' or 'wsl'.",
                            "enum": ["powershell", "wsl"]
                        },
                        "confirmed": {
                            "type": "boolean",
                            "description": "Set to true if user has confirmed execution. Do not set this yourself unless explicitly directed."
                        }
                    },
                    "required": ["command"]
                }
            }
        },
        "func": run_shell
    },
    "remember_lesson": {
        "schema": {
            "type": "function",
            "function": {
                "name": "remember_lesson",
                "description": "Saves a learned lesson to memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The lesson to remember."
                        }
                    },
                    "required": ["text"]
                }
            }
        },
        "func": add_lesson
    },
    "schedule_reminder": {
        "schema": {
            "type": "function",
            "function": {
                "name": "schedule_reminder",
                "description": (
                    "Schedules a one-time or recurring reminder that will be sent to the user "
                    "via Telegram at the scheduled time. Use this when the user asks to be "
                    "reminded about something (e.g. 'remind me at 5pm', 'remind me every "
                    "morning at 9', 'nudge me every 30 minutes'). The 'cron' argument uses "
                    "standard 5-field cron syntax: minute hour day-of-month month day-of-week. "
                    "day-of-week is 0-6 where 0=Sunday. Examples: '0 17 * * *' = every day at "
                    "17:00; '0 9 * * 1' = every Monday at 09:00; '*/30 * * * *' = every 30 "
                    "minutes; '0 9 1 * *' = on the 1st of each month at 09:00. For a one-time "
                    "reminder, set repeat=false and pick the minute/hour the user asked for."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The reminder text to send to the user."
                        },
                        "cron": {
                            "type": "string",
                            "description": "5-field cron schedule (minute hour dom month dow)."
                        },
                        "repeat": {
                            "type": "boolean",
                            "description": "True for a recurring reminder, false for a one-time reminder. Default true."
                        }
                    },
                    "required": ["message", "cron"]
                }
            }
        },
        "func": add_job
    },
    "list_reminders": {
        "schema": {
            "type": "function",
            "function": {
                "name": "list_reminders",
                "description": "Lists all currently scheduled reminders (ID, cron schedule, repeat flag, status, message).",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        "func": list_jobs
    },
    "remove_reminder": {
        "schema": {
            "type": "function",
            "function": {
                "name": "remove_reminder",
                "description": "Cancels a scheduled reminder by its ID. Use list_reminders first to find the ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The ID of the reminder to cancel (e.g. '3f9c2ab1')."
                        }
                    },
                    "required": ["job_id"]
                }
            }
        },
        "func": remove_job
    },
    "schedule_batch_reminders": {
        "schema": {
            "type": "function",
            "function": {
                "name": "schedule_batch_reminders",
                "description": (
                    "Schedules MANY weekly-recurring class/timetable reminders in a single call. "
                    "Use this when the user provides a weekly timetable and wants a reminder before "
                    "each class. Each entry needs: day (0=Sunday, 1=Monday, ..., 6=Saturday), "
                    "time (24h 'HH:MM' class start), course, and room. A reminder fires "
                    "lead_minutes before every class and includes the course name and room number."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entries": {
                            "type": "array",
                            "description": "List of classes to schedule.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "day": {
                                        "type": "integer",
                                        "description": "0=Sunday, 1=Monday, ..., 6=Saturday"
                                    },
                                    "time": {
                                        "type": "string",
                                        "description": "Class start time in 24h format, e.g. '09:00' or '14:30'"
                                    },
                                    "course": {
                                        "type": "string",
                                        "description": "Course or subject name"
                                    },
                                    "room": {
                                        "type": "string",
                                        "description": "Room number or location, e.g. 'TB114' or 'BYOD'"
                                    }
                                },
                                "required": ["day", "time", "course", "room"]
                            }
                        },
                        "lead_minutes": {
                            "type": "integer",
                            "description": "Minutes before the class to fire the reminder. Default 15."
                        }
                    },
                    "required": ["entries"]
                }
            }
        },
        "func": add_batch
    }
}

def get_tool_schemas() -> list[Dict[str, Any]]:
    """
    Returns the list of tool definitions for Ollama API.
    """
    return [tool["schema"] for tool in TOOL_REGISTRY.values()]

async def execute_tool(name: str, arguments: dict) -> str:
    """
    Executes a tool by name and returns the result as a string.
    Raises ConfirmationRequired if the tool needs user confirmation.
    """
    if name not in TOOL_REGISTRY:
        return f"Tool {name} not found."
    
    func = TOOL_REGISTRY[name]["func"]
    
    if inspect.iscoroutinefunction(func):
        result = await func(**arguments)
    else:
        # Run sync tools (shell, web search, memory) in a worker thread so
        # blocking I/O never freezes the event loop.
        result = await asyncio.to_thread(func, **arguments)
        
    # Standardize output for shell exec and others
    if isinstance(result, dict) or isinstance(result, list):
        return json.dumps(result, indent=2)
    return str(result)
