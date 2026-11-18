from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    user_id: int
    guild_id: int | None
    check_interval_minutes: int
    alert_cooldown_hours: int
    db_path: str
    has_amazon_prime: bool
    prefer_click_and_collect: bool
    keepa_api_key: str | None

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        channel_raw = os.getenv("DISCORD_CHANNEL_ID", "0").strip()
        user_raw = os.getenv("DISCORD_USER_ID", "0").strip()
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()

        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required in .env")
        if not channel_raw.isdigit() or int(channel_raw) == 0:
            raise ValueError("DISCORD_CHANNEL_ID must be a valid Discord channel ID")
        if not user_raw.isdigit() or int(user_raw) == 0:
            raise ValueError("DISCORD_USER_ID must be a valid Discord user ID")

        return cls(
            discord_token=token,
            channel_id=int(channel_raw),
            user_id=int(user_raw),
            guild_id=int(guild_raw) if guild_raw.isdigit() else None,
            check_interval_minutes=max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))),
            alert_cooldown_hours=max(1, int(os.getenv("ALERT_COOLDOWN_HOURS", "12"))),
            db_path=os.getenv("DB_PATH", "data/lego_prices.db").strip(),
            has_amazon_prime=os.getenv("HAS_AMAZON_PRIME", "false").lower() == "true",
            prefer_click_and_collect=os.getenv("PREFER_CLICK_AND_COLLECT", "false").lower() == "true",
            keepa_api_key=os.getenv("KEEPA_API_KEY", "").strip() or None,
        )
