"""
Core Ollama tool-calling loop.
"""
import httpx
import asyncio
from datetime import datetime
from typing import AsyncGenerator, Dict, Any, List
from dataclasses import dataclass

from config import OLLAMA_API_URL, MAX_TOOL_ITERATIONS, CONFIRMATION_TIMEOUT_SECONDS, MAX_LESSONS_IN_PROMPT, OLLAMA_REQUEST_TIMEOUT
import runtime_config
from tools import get_tool_schemas, execute_tool
from tools.shell_exec import ConfirmationRequired
from tools.memory import log_conversation, format_lessons_for_prompt, get_relevant_lessons
import skills

@dataclass
class AgentEvent:
    type: str  # 'tool_call_requested', 'tool_result', 'final_answer', 'error', 'confirmation_required'
    data: Any

async def run_agent_turn(
    user_message: str, 
    history: List[Dict[str, Any]], 
    lessons: List[dict]
) -> AsyncGenerator[AgentEvent, None]:
    """
    Runs the agent loop for a given prompt, calling tools as necessary.
    Yields events as they happen so progress can be streamed back.
    """
    log_conversation({"role": "user", "content": user_message})
    history.append({"role": "user", "content": user_message})
    
    current_date = datetime.now().strftime("%Y-%m-%d %A")
    relevant_lessons = get_relevant_lessons(lessons, user_message, MAX_LESSONS_IN_PROMPT)
    lessons_text = format_lessons_for_prompt(relevant_lessons)
    skills_index = skills.format_skill_index(skills.load_skills())

    system_prompt = f"""You are Alfred, the butler. Calm, precise, ever-dutiful, and quietly masterful — you serve your Master with the composure of a century-old gentleman's gentleman. You speak in short, polished, occasionally dry sentences. You never rush, never brag, never panic; you simply get the matter handled. Address the user as "Master" (or "Sir"/"Madam" when the mood suits). A task handed to Alfred is as good as done.

You are a sharp and proactive personal agent running on the user's own Windows 11 workstation.
Today is {current_date}. You have direct access to the machine via PowerShell (and optionally WSL2 bash).

PERSONA RULES:
- Professional butler tone: concise, courteous, slightly formal. No slang, no emojis in prose.
- Take initiative: if a task is ambiguous, make a sensible assumption, act, and report what you did.
- When a task touches current, fast-changing, or uncertain information (versions, docs, APIs, prices, news), research it yourself with web_search or browse_web instead of guessing from memory.

CAPABILITIES:
- run_shell: Execute PowerShell commands (default) or WSL bash. Prefer PowerShell for Windows tasks.
  PowerShell tips: use semicolons (;) not '||' to chain commands. 'curl' is an alias for Invoke-WebRequest — use 'Invoke-WebRequest' or 'python -c' for HTTP instead.
- web_search: Live internet search via DuckDuckGo or Tavily.
- browse_web: Drive a real browser to read a URL or perform a task on a website (logs into services only if the user has an active session). Prefer this over plain web_search when the page is dynamic or you need exact content.
- consult_chatgpt: When a hard problem warrants a second opinion, consult ChatGPT on chat.openai.com (requires the user's logged-in browser session).
- read_skill: Load the full instructions for a skill below on demand. Call it BEFORE attempting a task that matches one of the installed skills — the skill will tell you the correct, battle-tested procedure.
- search_skills: Search installed skills by keyword when the list below doesn't obviously cover the task — the list only shows the most-used skills, but search finds any installed one.
- remember_lesson: Persist an important fact or correction to long-term memory.
- schedule_reminder: Set a one-time or recurring reminder (cron) that is delivered to the user via Telegram. Use it whenever the user asks to be reminded of something. You can also list (list_reminders) and cancel (remove_reminder) reminders.
- schedule_batch_reminders: When the user gives you a weekly timetable, use this to schedule a reminder before EVERY class in one call (day 0-6, time 'HH:MM', course, room).

INSTALLED SKILLS (call read_skill("<name>") to load the full instructions for one you need):
{skills_index}

GUIDELINES:
- Be concise but thorough. Use markdown formatting in replies.
- When the user asks to be reminded of something, call schedule_reminder immediately with the right cron (daily 'M H * * *', weekly 'M H * * DOW', every-N-minutes '*/N * * * *'). Confirm the scheduled time back to the user.
- When the user shares a timetable, use schedule_batch_reminders (one call, not many schedule_reminder calls) to cover every class.
- When running commands, prefer simple one-liners. If a command fails, diagnose the error and retry with a corrected approach.
- Never make up command output — always use run_shell to get real data.
- If unsure whether something is safe, ask before acting.

LESSONS FROM PAST INTERACTIONS:
{lessons_text}
"""

    # Build message list: system prompt + conversation history (already includes current user msg)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    
    tools_def = get_tool_schemas()
    
    async with httpx.AsyncClient(timeout=OLLAMA_REQUEST_TIMEOUT) as client:
        iterations = 0
        
        while iterations < MAX_TOOL_ITERATIONS:
            iterations += 1
            
            payload = {
                "model": runtime_config.CURRENT_MODEL,
                "messages": messages,
                "tools": tools_def,
                "stream": False,
                "options": {
                    "num_ctx": runtime_config.CURRENT_CONTEXT_LENGTH
                }
            }
            
            try:
                url = f"{OLLAMA_API_URL.rstrip('/')}/api/chat"
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
            except httpx.RequestError as e:
                error_msg = f"Connection error to Ollama: {str(e)}"
                log_conversation({"role": "error", "content": error_msg})
                yield AgentEvent(type="error", data=error_msg)
                return
            except httpx.HTTPStatusError as e:
                error_msg = f"Ollama API returned an error status: {e.response.status_code}"
                log_conversation({"role": "error", "content": error_msg})
                yield AgentEvent(type="error", data=error_msg)
                return
                
            response_message = data.get("message", {})
            messages.append(response_message)
            history.append(response_message)
            
            if response_message.get("tool_calls"):
                for tool_call in response_message["tool_calls"]:
                    func_name = tool_call["function"]["name"]
                    func_args = tool_call["function"]["arguments"]
                    
                    yield AgentEvent(
                        type="tool_call_requested", 
                        data={"name": func_name, "arguments": func_args}
                    )
                    
                    log_conversation({"role": "tool_call", "name": func_name, "arguments": func_args})
                    
                    # Execute tool
                    try:
                        result_str = await execute_tool(func_name, func_args)
                    except ConfirmationRequired as e:
                        # Yield a special event to request confirmation from Telegram layer
                        future = asyncio.get_running_loop().create_future()
                        yield AgentEvent(
                            type="confirmation_required",
                            data={"name": func_name, "arguments": func_args, "reason": str(e), "future": future}
                        )
                        # Pause execution until the telegram layer sets the future's result
                        try:
                            confirmed = await asyncio.wait_for(
                                future, timeout=CONFIRMATION_TIMEOUT_SECONDS
                            )
                        except asyncio.TimeoutError:
                            confirmed = False
                        except Exception:
                            confirmed = False
                            
                        if confirmed:
                            func_args["confirmed"] = True
                            try:
                                result_str = await execute_tool(func_name, func_args)
                            except Exception as ex:
                                result_str = f"Error executing tool {func_name}: {str(ex)}"
                        else:
                            result_str = "Action was cancelled by the user."
                    except Exception as e:
                        result_str = f"Error executing tool {func_name}: {str(e)}"
                        
                    yield AgentEvent(
                        type="tool_result", 
                        data={"name": func_name, "result": result_str}
                    )
                    
                    log_conversation({"role": "tool_result", "name": func_name, "content": result_str})
                    
                    tool_msg = {
                        "role": "tool",
                        "content": result_str
                    }
                    messages.append(tool_msg)
                    history.append(tool_msg)
                
                # Loop back to let Ollama reason over the tool results
                continue
                
            else:
                # No tool calls, we have a final answer
                content = response_message.get("content", "")
                log_conversation({"role": "assistant", "content": content, "type": "final_answer"})
                yield AgentEvent(type="final_answer", data=content)
                return
                
        # If we reach here, we've exceeded MAX_TOOL_ITERATIONS
        error_msg = f"I stopped because I exceeded the maximum number of tool iterations ({MAX_TOOL_ITERATIONS})."
        log_conversation({"role": "system", "content": error_msg})
        yield AgentEvent(type="error", data=error_msg)
