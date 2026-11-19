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
    wishlist_path: str
    has_amazon_prime: bool
    prefer_click_and_collect: bool
    brickset_api_key: str | None
    serpapi_key: str | None = None
    serpapi_monthly_limit: int = 250
    extra_notify_user_ids: tuple[str, ...] = ("942902769319292948",)
    proxy_url: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        channel_raw = os.getenv("DISCORD_CHANNEL_ID", "0").strip()
        user_raw = os.getenv("DISCORD_USER_ID", "0").strip()
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        extra_raw = os.getenv("DISCORD_EXTRA_NOTIFY_USER_IDS", "942902769319292948").strip()
        extra_ids = tuple(u.strip() for u in extra_raw.split(",") if u.strip().isdigit())

        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required in .env")
        if not channel_raw.isdigit() or int(channel_raw) == 0:
            raise ValueError("DISCORD_CHANNEL_ID must be a valid Discord channel ID")
        if not user_raw.isdigit() or int(user_raw) == 0:
            raise ValueError("DISCORD_USER_ID must be a valid Discord user ID")

        raw_limit = os.getenv("SERPAPI_MONTHLY_LIMIT", "250").strip()
        serp_limit = max(10, int(raw_limit)) if raw_limit.isdigit() else 250

        return cls(
            discord_token=token,
            channel_id=int(channel_raw),
            user_id=int(user_raw),
            guild_id=int(guild_raw) if guild_raw.isdigit() else None,
            check_interval_minutes=max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))),
            alert_cooldown_hours=max(1, int(os.getenv("ALERT_COOLDOWN_HOURS", "12"))),
            db_path=os.getenv("DB_PATH", "data/lego_prices.db").strip(),
            wishlist_path=os.getenv("WISHLIST_PATH", "wishlist.json").strip(),
            has_amazon_prime=os.getenv("HAS_AMAZON_PRIME", "false").lower() == "true",
            prefer_click_and_collect=os.getenv("PREFER_CLICK_AND_COLLECT", "false").lower() == "true",
            brickset_api_key=os.getenv("BRICKSET_API_KEY", "").strip() or None,
            serpapi_key=os.getenv("SERPAPI_KEY", "").strip() or None,
            serpapi_monthly_limit=serp_limit,
            extra_notify_user_ids=extra_ids,
            proxy_url=os.getenv("PROXY_URL", "").strip() or None,
        )
