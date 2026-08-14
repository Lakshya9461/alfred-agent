import os
import json
import asyncio
import httpx
import logging
from config import PROJECT_ROOT, OLLAMA_API_URL, OLLAMA_MODEL, TELEGRAM_ALLOWED_USER_IDS, SELF_REVIEW_EVERY_N_TURNS
from tools.memory import add_lesson

logger = logging.getLogger(__name__)

TURN_COUNTER = 0

async def run_self_review_cycle(bot):
    global TURN_COUNTER
    state_file = os.path.join(PROJECT_ROOT, "data", "self_review_state.json")
    conv_file = os.path.join(PROJECT_ROOT, "data", "conversations.jsonl")
    
    logger.info(f"Self-review background task started. Running every {SELF_REVIEW_EVERY_N_TURNS} turns.")
    
    try:
        while True:
            await asyncio.sleep(5)
            
            if TURN_COUNTER >= SELF_REVIEW_EVERY_N_TURNS:
                current_turns = TURN_COUNTER
                TURN_COUNTER = 0
                
                logger.info(f"Triggering self-review after {current_turns} turns.")
                try:
                    await _perform_review(bot, state_file, conv_file)
                except Exception as e:
                    logger.error(f"Error during self-review: {e}")
    except asyncio.CancelledError:
        logger.info("Self-review background task cancelled.")

async def _perform_review(bot, state_file, conv_file):
    if not os.path.exists(conv_file):
        logger.info("Self-review: conversations.jsonl does not exist yet.")
        return
        
    last_line = 0
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
                last_line = state.get("last_line", 0)
        except Exception:
            pass
            
    with open(conv_file, 'r', encoding='utf-8') as f:
        all_lines = f.readlines()
        
    if last_line >= len(all_lines):
        logger.info("Self-review: No new conversation lines to review.")
        return
        
    new_lines = all_lines[last_line:]
    new_last_line = len(all_lines)
    
    # Trim to most recent chunk to avoid blowing up context
    if len(new_lines) > 100:
        new_lines = new_lines[-100:]
    
    chunk_data = []
    for line in new_lines:
        try:
            chunk_data.append(json.loads(line))
        except Exception:
            pass
            
    if not chunk_data:
        return
        
    transcript = ""
    for entry in chunk_data:
        role = entry.get("role", "unknown")
        if role == "tool_call":
            transcript += f"[{role}] {entry.get('name')} {entry.get('arguments')}\n"
        else:
            content = str(entry.get("content", ""))
            if len(content) > 1000:
                content = content[:1000] + " ...[truncated]"
            transcript += f"[{role}] {content}\n"
            
    prompt = f"""You are an analytical AI tasked with reviewing recent conversation logs to extract SPECIFIC, concrete lessons.
Look for:
- A shell command that failed and what worked instead.
- A factual claim the user corrected.
- A repeated back-and-forth suggesting the first approach was wrong.

DO NOT invent lessons from ordinary successful exchanges. Only extract a lesson if an error was made and corrected, or explicit negative feedback was given.
If nothing genuinely actionable was learned, return an empty JSON list: []

Respond ONLY with a valid JSON array of strings, where each string is a concise lesson learned. No other text.

Conversation log:
{transcript}
"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": "You output only valid JSON arrays of strings."},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "format": "json"
        }
        
        url = f"{OLLAMA_API_URL.rstrip('/')}/api/chat"
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        
    reply_content = data.get("message", {}).get("content", "[]").strip()
    
    try:
        lessons_learned = json.loads(reply_content)
        if not isinstance(lessons_learned, list):
            lessons_learned = []
    except Exception as e:
        logger.warning(f"Self-review failed to parse JSON: {reply_content} - Error: {e}")
        lessons_learned = []
        
    added = []
    for lesson in lessons_learned:
        if isinstance(lesson, str) and lesson.strip():
            res = add_lesson(lesson.strip(), source="self_review")
            if "successfully" in res.lower():
                added.append(lesson.strip())
            
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump({"last_line": new_last_line}, f)
        
    if added:
        logger.info(f"Self-review added {len(added)} lessons.")
        msg = "🧠 *Self-Review Complete*\nI learned the following new lessons:\n\n"
        for idx, l in enumerate(added, 1):
            msg += f"{idx}. {l}\n"
            
        for user_id in TELEGRAM_ALLOWED_USER_IDS:
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send self-review notification to {user_id}: {e}")
    else:
        logger.info("Self-review completed. No actionable lessons found.")
