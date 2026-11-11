"""Load the bot's settings from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from dotenv import load_dotenv

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_DATABASE_PATH = "data/assistant.db"
DEFAULT_TIMEZONE = "Europe/London"

class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_telegram_id: int
    openai_api_key: str
    openai_model: str
    database_path: str
    timezone: str

def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_settings() -> Settings:
    # Read .env into the environment. Real environment variables win over .env values.
    load_dotenv()

    token = _require("TELEGRAM_BOT_TOKEN")
    owner_raw = _require("OWNER_TELEGRAM_ID")
    try:
        owner_id = int(owner_raw)
    except ValueError:
        raise ConfigError("OWNER_TELEGRAM_ID must be a number, e.g. 123456789.") from None

    tz_name = os.getenv("TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TIMEZONE {tz_name!r} is not valid. Use a name like Europe/London.") from None


    return Settings(
        telegram_bot_token=token,
        owner_telegram_id=owner_id,
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL,
        database_path=os.getenv("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH,
        timezone=tz_name,
    )
