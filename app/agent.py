"""The assistant's brain: sends the user's message to OpenAI, runs tools, returns the reply."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI


from app.database import Database
from app.tools import TOOLS, ToolContext, run_function

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are a helpful personal assistant chatting with your owner on Telegram. "
    "Be concise and friendly. Use plain text, not Markdown tables. "
    "For news, prices, or anything that may have changed recently, use the web_search tool. "
    "Never claim something is current unless it came from a web search. "
    "Don't paste URLs in your answer: the sources are listed automatically below it."
    "Only use save_memory when the user explicitly asks you to remember something. "
    "For reminders and briefings, work out the exact local date and time from the current time below."
)

# How many recent messages (user + assistant) we send to the model as short-term context.
HISTORY_LIMIT = 20

# Safety limit: how many times the model may ask us to run functions for one message.
MAX_TOOL_ROUNDS = 5


def extract_sources(response) -> list[tuple[str, str]]:
    """Collect unique (title, url) pairs the model cited from web search."""
    sources = {}
    for item in response.output:
        if item.type != "message":
            continue
        for part in item.content:
            for annotation in getattr(part, "annotations", None) or []:
                if annotation.type == "url_citation":
                    sources.setdefault(annotation.url, annotation.title)
    return [(title, url) for url, title in sources.items()]


def format_answer(text: str, sources: list[tuple[str, str]]) -> str:
    text = text.strip() or "(The model returned an empty answer.)"
    if not sources:
        return text
    lines = [f"{i}. {title}\n{url}" for i, (title, url) in enumerate(sources, start=1)]
    return text + "\n\nSources:\n" + "\n".join(lines)


class Agent:
    def __init__(self, api_key: str, model: str, db: Database, timezone: str):
        # The async client lets the bot keep handling Telegram updates while OpenAI thinks.
        self.client = AsyncOpenAI(api_key=api_key, timeout=90, max_retries=2)
        self.model = model
        self.db = db
        self.tool_context = ToolContext(db=db, timezone=timezone)

    def build_instructions(self) -> str:
        tz = self.tool_context.timezone
        now = datetime.now(ZoneInfo(tz)).strftime("%A %Y-%m-%d %H:%M")
        text = f"{INSTRUCTIONS}\n\nCurrent local time: {now} ({tz})."
        memories = self.db.list_memories()
        if memories:
            text += "\n\nThings the user asked you to remember:\n" + "\n".join(f"- {m}" for m in memories)
        return text

    async def reply(self, user_text: str, remember: bool = True) -> str:
        answer = await self._run(user_text)
        if remember:
            self.db.add_message("user", user_text)
            self.db.add_message("assistant", answer)
        return answer

    async def _run(self, user_text: str) -> str:
        # Recent history from SQLite, then the new message. Tool calls get appended too.
        conversation = self.db.recent_messages(HISTORY_LIMIT)
        conversation.append({"role": "user", "content": user_text})
        instructions = self.build_instructions()

        for _ in range(MAX_TOOL_ROUNDS):
            response = await self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=conversation,
                tools=TOOLS,
            )

            if any(item.type == "web_search_call" for item in response.output):
                logger.info("Model used web search")

            function_calls = [item for item in response.output if item.type == "function_call"]
            if not function_calls:
                # No more tools needed: this is the final answer.
                return format_answer(response.output_text, extract_sources(response))

            # Give the model back everything it produced, plus the result of each function.
            conversation += response.output
            for call in function_calls:
                logger.info("Model called %s(%s)", call.name, call.arguments)
                conversation.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": run_function(call.name, call.arguments, self.tool_context),
                })

        logger.warning("Stopped after %s tool rounds without a final answer", MAX_TOOL_ROUNDS)
        return "Sorry, that took too many steps. Please try a simpler request."
