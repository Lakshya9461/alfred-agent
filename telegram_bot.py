import os
import io
import time
import asyncio
import logging
from functools import wraps
from typing import Dict, List, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS
from agent_loop import run_agent_turn
from tools.memory import load_lessons, remove_lesson
import self_review
import runtime_config
import ollama_utils

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory conversation history: chat_id -> list of message dicts
CHAT_HISTORIES: Dict[int, List[Dict[str, Any]]] = {}

# Pending futures for tool confirmations: callback_data_id -> Future
PENDING_CONFIRMATIONS: Dict[str, asyncio.Future] = {}

# Bot start time for /status uptime
START_TIME = time.time()

# Temporary cache of model names for /model inline keyboard
MODEL_CACHE: list = []

# Cache for /shell dangerous command confirmations: short_id -> cmd string
DIRECT_SHELL_CACHE: Dict[str, str] = {}
_direct_shell_counter = 0

def get_chat_history(chat_id: int) -> List[Dict[str, Any]]:
    if chat_id not in CHAT_HISTORIES:
        CHAT_HISTORIES[chat_id] = []
    return CHAT_HISTORIES[chat_id]

def prune_history(history: List[Dict[str, Any]]):
    """Keep history within ~60% of the model's context window (rough token estimate)."""
    # Estimate: average message ≈ 150 tokens; reserve 40% for system prompt + response
    safe_tokens = int(runtime_config.CURRENT_CONTEXT_LENGTH * 0.6)
    max_messages = max(10, safe_tokens // 150)
    if len(history) > max_messages:
        del history[:-max_messages]

def whitelist_only(func):
    """Decorator to silently ignore unauthorized users."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id not in TELEGRAM_ALLOWED_USER_IDS:
            logger.warning(f"Unauthorized access attempt from user ID {user.id if user else 'Unknown'}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@whitelist_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I am *Alfred*, your personal agent running on this workstation.\n\n"
        "I can run shell commands, search the web, and remember lessons you teach me.\n\n"
        "Type /help to see all available commands.",
        parse_mode="Markdown"
    )

@whitelist_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*Alfred — Available Commands*\n\n"
        "🗣 *Conversation*\n"
        "`/start` — Greet Alfred\n"
        "`/help` — Show this help message\n"
        "`/clear` — Clear your conversation history\n\n"
        "🛠 *Tools*\n"
        "`/shell <command>` — Run a PowerShell command directly\n"
        "`/search <query>` — Search the web for something\n\n"
        "🧠 *Memory*\n"
        "`/lessons` — List all lessons Alfred has learned\n"
        "`/correct <text>` — Teach Alfred a new lesson\n"
        "`/forget <index>` — Delete a lesson by its index\n\n"
        "📊 *System*\n"
        "`/status` — Show bot status and uptime"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

@whitelist_only
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CHAT_HISTORIES[chat_id] = []
    await update.message.reply_text("🧹 Conversation history cleared. Starting fresh!")

@whitelist_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import OLLAMA_API_URL, SELF_REVIEW_EVERY_N_TURNS
    
    uptime_secs = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_secs, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s"
    
    lessons_count = len(load_lessons())
    chat_id = update.effective_chat.id
    history_len = len(CHAT_HISTORIES.get(chat_id, []))
    turns_until_review = max(0, SELF_REVIEW_EVERY_N_TURNS - self_review.TURN_COUNTER)
    ctx_k = runtime_config.CURRENT_CONTEXT_LENGTH // 1000
    
    msg = (
        f"📊 *Alfred Status*\n\n"
        f"⏱ Uptime: `{uptime_str}`\n"
        f"🤖 Model: `{runtime_config.CURRENT_MODEL}`\n"
        f"📐 Context window: `{runtime_config.CURRENT_CONTEXT_LENGTH:,}` tokens (~{ctx_k}k)\n"
        f"🌐 Ollama: `{OLLAMA_API_URL}`\n"
        f"💬 History: `{history_len}` messages in current chat\n"
        f"🧠 Lessons: `{lessons_count}` stored\n"
        f"🔍 Next self-review in: `{turns_until_review}` turns"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

@whitelist_only
async def shell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run a PowerShell command directly without going through the LLM."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/shell <command>`\nExample: `/shell Get-Date`",
            parse_mode="Markdown"
        )
        return
    
    cmd = " ".join(context.args)
    status_msg = await update.message.reply_text(f"💻 Running: `{cmd[:80]}`...", parse_mode="Markdown")
    
    from tools.shell_exec import run_shell, is_dangerous
    
    is_danger, reason = is_dangerous(cmd)
    if is_danger:
        global _direct_shell_counter
        _direct_shell_counter += 1
        shell_id = f"ds{_direct_shell_counter}"
        DIRECT_SHELL_CACHE[shell_id] = cmd
        keyboard = [[
            InlineKeyboardButton("✅ Run", callback_data=f"direct_shell|yes|{shell_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"direct_shell|no|{shell_id}"),
        ]]
        await status_msg.edit_text(
            f"⚠️ *Dangerous command detected*\n{reason}\n\nRun anyway?\n\n`{cmd}`",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    result = run_shell(cmd)
    stdout = result.get("stdout", "").strip()
    stderr = result.get("stderr", "").strip()
    rc = result.get("returncode", -1)
    
    output = stdout or stderr or "(no output)"
    icon = "✅" if rc == 0 else "❌"
    reply = f"{icon} `{cmd}`\n\n```\n{output[:3000]}\n```"
    
    await status_msg.edit_text(reply, parse_mode="Markdown")

@whitelist_only
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available Ollama models as an inline keyboard for switching."""
    global MODEL_CACHE
    status_msg = await update.message.reply_text("🔄 Fetching available models from Ollama...")
    
    models = await ollama_utils.get_available_models()
    
    if not models:
        await status_msg.edit_text("❌ Could not reach Ollama or no models are installed.")
        return
    
    MODEL_CACHE = models
    
    # Build keyboard: 2 models per row
    keyboard = []
    for i in range(0, len(models), 2):
        row = []
        for idx in range(i, min(i + 2, len(models))):
            label = f"✅ {models[idx]}" if models[idx] == runtime_config.CURRENT_MODEL else models[idx]
            row.append(InlineKeyboardButton(label, callback_data=f"model_sel|{idx}"))
        keyboard.append(row)
    
    ctx_k = runtime_config.CURRENT_CONTEXT_LENGTH // 1000
    await status_msg.edit_text(
        f"🤖 *Active model:* `{runtime_config.CURRENT_MODEL}` ({ctx_k}k ctx)\n"
        f"Select a model to switch:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

@whitelist_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run a web search directly without going through the LLM."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/search <query>`\nExample: `/search Python async tutorials`",
            parse_mode="Markdown"
        )
        return
    
    query = " ".join(context.args)
    status_msg = await update.message.reply_text(f"🔍 Searching for: *{query}*...", parse_mode="Markdown")
    
    from tools.web_search import search
    result = search(query)
    
    if len(result) > 3500:
        bio = io.BytesIO(result.encode("utf-8"))
        bio.seek(0)
        await status_msg.delete()
        await update.message.reply_document(
            document=bio,
            filename="search_results.txt",
            caption=f"Search results for: {query}"
        )
    else:
        await status_msg.edit_text(result)


@whitelist_only
async def correct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Please provide a lesson text. Usage: /correct <text>")
        return
        
    from tools.memory import add_lesson
    res = add_lesson(text)
    await update.message.reply_text(res)

@whitelist_only
async def lessons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lessons = load_lessons()
    if not lessons:
        await update.message.reply_text("No lessons learned yet.")
        return
        
    lines = []
    for i, l in enumerate(lessons, 1):
        lines.append(f"{i}. [{l.get('source', 'user').upper()}] {l.get('text')}")
    
    msg = "\n".join(lines)
    if len(msg) > 3500:
        await update.message.reply_document(
            document=io.BytesIO(msg.encode("utf-8")),
            filename="lessons.txt",
            caption="Current Lessons"
        )
    else:
        await update.message.reply_text(f"Current lessons:\n\n{msg}")

@whitelist_only
async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Please provide a valid index. Usage: /forget <index>")
        return
    
    idx = int(context.args[0])
    res = remove_lesson(idx)
    await update.message.reply_text(res)

@whitelist_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    chat_id = update.effective_chat.id
    history = get_chat_history(chat_id)
    lessons = load_lessons()
    
    status_msg = await update.message.reply_text("🤔 Thinking...")
    
    try:
        async for event in run_agent_turn(user_msg, history, lessons):
            if event.type == "tool_call_requested":
                func_name = event.data["name"]
                args = event.data["arguments"]
                if func_name == "web_search":
                    query = args.get("query", "")
                    await status_msg.edit_text(f"🔍 searching: {query}")
                elif func_name == "run_shell":
                    cmd = args.get("command", "")
                    await status_msg.edit_text(f"💻 running: {cmd[:100]}")
                    
            elif event.type == "confirmation_required":
                func_name = event.data["name"]
                args = event.data["arguments"]
                reason = event.data["reason"]
                future = event.data["future"]
                
                cmd = args.get("command", "")
                
                # Create a unique callback ID
                callback_id = f"conf_{update.update_id}_{len(PENDING_CONFIRMATIONS)}"
                PENDING_CONFIRMATIONS[callback_id] = future
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Run", callback_data=f"{callback_id}|yes"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"{callback_id}|no")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"⚠️ {reason}\n\nRun this command?\n\n`{cmd}`",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                
                await status_msg.edit_text("⏸️ Waiting for your confirmation...")
                
            elif event.type == "tool_result":
                await status_msg.edit_text("🤔 Analyzing results...")
                
            elif event.type == "final_answer":
                ans = str(event.data)
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                
                if not ans or not ans.strip():
                    ans = "*(No response generated by the model.)*"
                
                if len(ans) > 3500:
                    bio = io.BytesIO(ans.encode("utf-8"))
                    bio.seek(0)
                    await update.message.reply_document(
                        document=bio,
                        filename="response.txt",
                        caption="Response was too long, attached as file."
                    )
                else:
                    await update.message.reply_text(ans)
                    
            elif event.type == "error":
                await status_msg.edit_text(f"❌ Error: {event.data}")
                
    except Exception as e:
        logger.exception("Error during agent turn")
        try:
            await status_msg.edit_text(f"❌ Internal error: {e}")
        except Exception:
            await update.message.reply_text(f"❌ Internal error: {e}")
        
    self_review.TURN_COUNTER += 1
    prune_history(history)

@whitelist_only
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if "|" not in data:
        return

    # ── Model selection ──────────────────────────────────────────────────────
    if data.startswith("model_sel|"):
        _, idx_str = data.split("|", 1)
        try:
            idx = int(idx_str)
            chosen = MODEL_CACHE[idx]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Invalid model index — please run /model again.")
            return
        
        await query.edit_message_text(f"⏳ Switching to `{chosen}` and detecting context window...", parse_mode="Markdown")
        
        ctx_len, _ = await ollama_utils.get_model_context_length(chosen)
        runtime_config.CURRENT_MODEL = chosen
        runtime_config.CURRENT_CONTEXT_LENGTH = ctx_len
        ctx_k = ctx_len // 1000
        
        await query.edit_message_text(
            f"✅ *Switched to* `{chosen}`\n📐 Context window: `{ctx_len:,}` tokens (~{ctx_k}k)",
            parse_mode="Markdown"
        )
        return

    # ── Direct /shell confirmation ─────────────────────────────────────────────
    if data.startswith("direct_shell|"):
        parts = data.split("|", 2)
        action = parts[1] if len(parts) > 1 else "no"
        shell_id = parts[2] if len(parts) > 2 else ""
        cmd = DIRECT_SHELL_CACHE.pop(shell_id, None)
        
        if action == "no" or not cmd:
            await query.edit_message_text("*❌ Command cancelled.*", parse_mode="Markdown")
            return
        
        await query.edit_message_text(f"💻 Running: `{cmd[:80]}`...", parse_mode="Markdown")
        from tools.shell_exec import run_shell
        result = run_shell(cmd, confirmed=True)
        stdout = result.get("stdout", "").strip()
        stderr = result.get("stderr", "").strip()
        rc = result.get("returncode", -1)
        output = stdout or stderr or "(no output)"
        icon = "✅" if rc == 0 else "❌"
        reply = f"{icon} `{cmd}`\n\n```\n{output[:3000]}\n```"
        await query.edit_message_text(reply, parse_mode="Markdown")
        return

    # ── Agent shell confirmation ───────────────────────────────────────────────
    cb_id, action = data.split("|", 1)
    
    if cb_id in PENDING_CONFIRMATIONS:
        future = PENDING_CONFIRMATIONS.pop(cb_id)
        if not future.done():
            if action == "yes":
                future.set_result(True)
                await query.edit_message_text(f"{query.message.text}\n\n*✅ Confirmed by user.*", parse_mode="Markdown")
            else:
                future.set_result(False)
                await query.edit_message_text(f"{query.message.text}\n\n*❌ Cancelled by user.*", parse_mode="Markdown")

async def post_init(app: Application):
    """Callback triggered after bot is initialized but before polling starts."""
    task = asyncio.create_task(self_review.run_self_review_cycle(app.bot))
    app.bot_data["self_review_task"] = task
    
    # Register the command menu so it appears in the Telegram UI
    await app.bot.set_my_commands([
        BotCommand("start",   "Greet Alfred"),
        BotCommand("help",    "Show all available commands"),
        BotCommand("model",   "Switch the active Ollama model"),
        BotCommand("shell",   "Run a PowerShell command directly"),
        BotCommand("search",  "Search the web for a query"),
        BotCommand("status",  "Show bot status and uptime"),
        BotCommand("clear",   "Clear your conversation history"),
        BotCommand("lessons", "List all stored lessons"),
        BotCommand("correct", "Teach Alfred a new lesson"),
        BotCommand("forget",  "Delete a lesson by index"),
    ])
    logger.info("Bot command menu registered with Telegram.")
    
    # Auto-detect context window for the startup model
    ctx_len, _ = await ollama_utils.get_model_context_length(runtime_config.CURRENT_MODEL)
    runtime_config.CURRENT_CONTEXT_LENGTH = ctx_len
    logger.info(f"Startup model: {runtime_config.CURRENT_MODEL} | context window: {ctx_len:,} tokens")

async def post_shutdown(app: Application):
    """Callback triggered during application shutdown."""
    task = app.bot_data.get("self_review_task")
    if task:
        task.cancel()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — logs exceptions so they don't silently vanish."""
    logger.error("Unhandled exception in handler:", exc_info=context.error)

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        return
        
    if not TELEGRAM_ALLOWED_USER_IDS:
        logger.warning("TELEGRAM_ALLOWED_USER_IDS is empty! No one will be able to use the bot.")
        
    logger.info("Starting Alfred Telegram Bot...")
    
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("clear",   clear_command))
    app.add_handler(CommandHandler("status",  status_command))
    app.add_handler(CommandHandler("model",   model_command,  block=False))
    app.add_handler(CommandHandler("shell",   shell_command,  block=False))
    app.add_handler(CommandHandler("search",  search_command, block=False))
    app.add_handler(CommandHandler("correct", correct_command))
    app.add_handler(CommandHandler("lessons", lessons_command))
    app.add_handler(CommandHandler("forget",  forget_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text, block=False))
    app.add_handler(CallbackQueryHandler(handle_callback, block=False))
    app.add_error_handler(error_handler)
    
    logger.info("Bot is polling for updates...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
