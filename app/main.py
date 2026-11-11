"""Telegram entry point: registers the handlers and starts long polling."""

import logging

from openai import OpenAIError
from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.agent import Agent
from app.config import ConfigError, Settings, load_settings
from app.database import Database
from app.scheduler import CHECK_EVERY_SECONDS, run_due_jobs
from app.tools import list_jobs


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL, and Telegram URLs contain the bot token. Keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_LENGTH = 4096
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I'm your personal assistant.\n"
        "Ask me anything. I can research the web, remember things you ask me to, "
        "and send you reminders and daily briefings.\n\n"
        "/memories shows what I remember. /jobs shows scheduled reminders and briefings."
    )


async def show_memories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    memories = context.bot_data["db"].list_memories()
    text = "\n".join(f"- {m}" for m in memories) if memories else "I haven't saved any memories yet."
    await update.message.reply_text(text)


async def show_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = list_jobs(context.bot_data["agent"].tool_context)
    lines = [f"#{j['job_id']} {j['kind']} at {j['next_run']}: {j['text']}" for j in result["jobs"]]
    text = "\n".join(lines) + f"\n(times in {result['timezone']})" if lines else "Nothing scheduled."
    await update.message.reply_text(text)

def split_message(text: str, limit: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split long text into Telegram-sized chunks, preferring to break at newlines."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.bot_data["agent"]
    # Show "typing..." in Telegram while we wait for OpenAI.
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await agent.reply(update.message.text)
    except OpenAIError:
        logger.exception("OpenAI request failed")
        await update.message.reply_text("Sorry, I couldn't reach the AI just now. Please try again.")
        return

    for chunk in split_message(answer):
        # Turn off link previews so a list of sources doesn't flood the chat with cards.
        await update.message.reply_text(chunk, link_preview_options=NO_PREVIEW)


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.warning("Rejected a message from unauthorised user id=%s", user.id if user else "unknown")
    await update.message.reply_text("Sorry, this is a private bot.")


async def on_startup(app: Application) -> None:
    # Runs once the token has been accepted by Telegram.
    logger.info("Bot @%s is running with model %s. Press Ctrl+C to stop.",
                app.bot.username, app.bot_data["agent"].model)


def build_application(settings: Settings) -> Application:
    owner_only = filters.User(user_id=settings.owner_telegram_id)

    app = Application.builder().token(settings.telegram_bot_token).post_init(on_startup).build()
    # bot_data is a shared dictionary every handler can read through context.bot_data.
    db = Database(settings.database_path)
    app.bot_data["db"] = db
    app.bot_data["owner_id"] = settings.owner_telegram_id
    app.bot_data["timezone"] = settings.timezone
    app.bot_data["agent"] = Agent(
        api_key=settings.openai_api_key, model=settings.openai_model,
        db=db, timezone=settings.timezone,
    )
    app.job_queue.run_repeating(run_due_jobs, interval=CHECK_EVERY_SECONDS, first=5)
    # Handlers are checked in order; the first one whose filter matches wins.
    app.add_handler(CommandHandler("start", start, filters=owner_only))
    app.add_handler(CommandHandler("memories", show_memories, filters=owner_only))
    app.add_handler(CommandHandler("jobs", show_jobs, filters=owner_only))
    app.add_handler(MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(MessageHandler(~owner_only, reject))
    return app


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(f"Configuration error: {error}")

    app = build_application(settings)
    # Only ask Telegram for new messages; Ctrl+C stops polling cleanly.
    app.run_polling(allowed_updates=[Update.MESSAGE])


if __name__ == "__main__":
    main()
