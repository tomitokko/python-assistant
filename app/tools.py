"""Tools the model can use: OpenAI's built-in web search plus our own Python functions."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from app.database import Database
from app.scheduler import next_daily_run, parse_local_datetime, to_utc_iso



logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """What our functions may need: the database and the owner's time zone."""
    db: Database
    timezone: str


# --- Our own Python functions -------------------------------------------------

def get_current_time(ctx: ToolContext, timezone: str) -> dict:
    """Return the current date and time in an IANA time zone, e.g. Europe/London."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {timezone!r}. Use a name like 'Europe/London'.")
    return {"timezone": timezone, "now": now.strftime("%A %d %B %Y, %H:%M")}



def save_memory(ctx: ToolContext, fact: str) -> dict:
    fact = fact.strip()
    if not fact:
        raise ValueError("The fact to remember is empty.")
    memory_id = ctx.db.add_memory(fact)
    return {"saved": True, "memory_id": memory_id, "fact": fact}



def create_reminder(ctx: ToolContext, text: str, when: str) -> dict:
    run_at = parse_local_datetime(when, ctx.timezone)
    if run_at <= datetime.now(ZoneInfo(ctx.timezone)):
        raise ValueError(f"{when} is in the past.")
    job_id = ctx.db.add_job("reminder", text, to_utc_iso(run_at))
    return {"created": True, "job_id": job_id, "when": f"{when} {ctx.timezone}"}


def create_daily_briefing(ctx: ToolContext, topic: str, time: str) -> dict:
    first_run = next_daily_run(time, ctx.timezone, datetime.now(ZoneInfo("UTC")))
    job_id = ctx.db.add_job("briefing", topic, to_utc_iso(first_run), daily_time=time.strip())
    return {"created": True, "job_id": job_id, "first_run": first_run.strftime("%Y-%m-%d %H:%M")}


def list_jobs(ctx: ToolContext) -> dict:
    jobs = []
    for job in ctx.db.active_jobs():
        local = datetime.fromisoformat(job["next_run_utc"]).astimezone(ZoneInfo(ctx.timezone))
        jobs.append({"job_id": job["id"], "kind": job["kind"], "text": job["text"],
                     "next_run": local.strftime("%Y-%m-%d %H:%M")})
    return {"timezone": ctx.timezone, "jobs": jobs}


def cancel_job(ctx: ToolContext, job_id: int) -> dict:
    if not ctx.db.deactivate_job(job_id):
        raise ValueError(f"No active job with id {job_id}.")
    return {"cancelled": True, "job_id": job_id}


# Maps the name the model uses to the Python function we run.
FUNCTIONS = {
    "get_current_time": get_current_time,
    "save_memory": save_memory,
    "create_reminder": create_reminder,
    "create_daily_briefing": create_daily_briefing,
    "list_jobs": list_jobs,
    "cancel_job": cancel_job,
}


# --- Tool definitions sent to OpenAI -----------------------------------------

TOOLS = [
    # Built in: OpenAI runs the search on its side and returns cited sources.
    {"type": "web_search"},
    # Ours: the model asks for it, and our code runs it (see run_function below).
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Get the current date and time in a given time zone.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA time zone name, e.g. Europe/London or America/New_York.",
                },
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "save_memory",
        "description": "Save a fact about the user for future conversations. "
                       "Only use when the user explicitly asks you to remember something.",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "The fact, e.g. 'Prefers Python'."}},
            "required": ["fact"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_reminder",
        "description": "Schedule a one-off reminder message to the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind the user about."},
                "when": {"type": "string", "description": "Local time as 'YYYY-MM-DD HH:MM' (24-hour)."},
            },
            "required": ["text", "when"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_daily_briefing",
        "description": "Schedule a daily research briefing on a topic, sent every day at a local time.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "What to research, e.g. 'Python news'."},
                "time": {"type": "string", "description": "Local time as 'HH:MM' (24-hour)."},
            },
            "required": ["topic", "time"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_jobs",
        "description": "List the user's active reminders and daily briefings.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "cancel_job",
        "description": "Cancel a reminder or daily briefing by its job_id (use list_jobs first).",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def run_function(name: str, arguments_json: str, ctx: ToolContext) -> str:
    """Run one function call from the model and return the result as a JSON string.

    Errors are returned to the model as {"error": ...} so it can tell the user
    honestly, instead of pretending the call worked.
    """
    function = FUNCTIONS.get(name)
    if function is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("Arguments must be a JSON object.")
        result = function(ctx, **arguments)
    except Exception as error:  # Any failure becomes an error message for the model.
        logger.warning("Tool %s failed: %s", name, error)
        return json.dumps({"error": str(error)})
    logger.info("Tool %s succeeded", name)
    return json.dumps(result)
