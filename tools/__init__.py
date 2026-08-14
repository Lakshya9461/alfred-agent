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
