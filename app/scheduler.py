"""Time-zone helpers and the background job runner for reminders and daily briefings."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# How often we check the database for due jobs.
CHECK_EVERY_SECONDS = 30


def to_utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_local_datetime(text: str, tz_name: str) -> datetime:
    """Turn 'YYYY-MM-DD HH:MM' in the owner's time zone into an aware datetime.

    Rejects times that don't exist because of a daylight-saving jump.
    """
    tz = ZoneInfo(tz_name)
    try:
        naive = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Use the format 'YYYY-MM-DD HH:MM', e.g. '2026-11-10 09:30'.") from None
    local = naive.replace(tzinfo=tz)
    # A time skipped by a clock change won't survive a round trip through UTC.
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        raise ValueError(f"{text} doesn't exist in {tz_name} because of a clock change.")
    return local


def next_daily_run(hhmm: str, tz_name: str, after: datetime) -> datetime:
    """The next time the clock shows HH:MM in tz_name, strictly after `after`."""
    try:
        hour, minute = (int(part) for part in hhmm.strip().split(":"))
        datetime(2000, 1, 1, hour, minute)  # validates the range
    except ValueError:
        raise ValueError("Use 24-hour 'HH:MM', e.g. '08:00'.") from None
    tz = ZoneInfo(tz_name)
    day = after.astimezone(tz).date()
    while True:
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if candidate > after:
            return candidate
        day += timedelta(days=1)


async def run_due_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called every CHECK_EVERY_SECONDS by python-telegram-bot's JobQueue."""
    app = context.application
    db = app.bot_data["db"]
    agent = app.bot_data["agent"]
    owner_id = app.bot_data["owner_id"]
    tz_name = app.bot_data["timezone"]

    now = datetime.now(timezone.utc)
    for job in db.due_jobs(to_utc_iso(now)):
        # Claim the job BEFORE doing the slow work, so it can't run twice.
        if job["kind"] == "reminder":
            db.deactivate_job(job["id"])
        else:
            db.reschedule_job(job["id"], to_utc_iso(next_daily_run(job["daily_time"], tz_name, now)))

        try:
            if job["kind"] == "reminder":
                text = f"⏰ Reminder: {job['text']}"
            else:
                logger.info("Running daily briefing #%s: %s", job["id"], job["text"])
                answer = await agent.reply(
                    f"Write my daily briefing on: {job['text']}. "
                    "Use web search for the latest information and keep it short.",
                    remember=False,
                )
                text = f"🗞 Daily briefing: {job['text']}\n\n{answer}"
            # Telegram's 4096-character limit: send long briefings in pieces.
            for start in range(0, len(text), 4096):
                await context.bot.send_message(
                    owner_id, text[start:start + 4096],
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            logger.info("Delivered %s #%s", job["kind"], job["id"])
        except Exception:
            logger.exception("Failed to deliver %s #%s", job["kind"], job["id"])