import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import discord
import pytest

from src.lego_bot.bot import LegoTrackerBot
from src.lego_bot.config import Config
from src.lego_bot.uk_finder import SetMetadata
from src.lego_bot.wishlist_store import load_wishlist_entries, upsert_wishlist_entry


@pytest.mark.asyncio
async def test_retired_set_auto_cleanup_and_ping():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        wishlist_path = str(Path(tmp) / "wishlist.json")
        upsert_wishlist_entry(
            wishlist_path,
            {"name": "Old Retired Set", "set_number": "10299", "notify_user_ids": ["111111", "222222"]},
        )
        cfg = Config(
            discord_token="test-token",
            channel_id=123456,
            user_id=111111,
            guild_id=None,
            check_interval_minutes=30,
            alert_cooldown_hours=12,
            db_path=db_path,
            wishlist_path=wishlist_path,
            has_amazon_prime=False,
            prefer_click_and_collect=False,
            brickset_api_key=None,
        )
        bot = LegoTrackerBot(cfg)
        bot._startup_sync_and_clean()
        assert bot.db.get_item_by_set_number("10299") is not None

        sent_messages: list[dict] = []

        class FakeChannel(discord.abc.Messageable):
            async def _get_channel(self):  # type: ignore[override]
                return self

            async def send(self, content=None, *, embed=None, allowed_mentions=None, **kwargs):  # type: ignore[override]
                sent_messages.append(
                    {"content": content, "embed": embed, "allowed_mentions": allowed_mentions}
                )

        bot.get_channel = lambda cid: FakeChannel()  # type: ignore[method-assign]
        bot.http_session = aiohttp.ClientSession()

        retired_meta = SetMetadata(
            name="Old Retired Set",
            rrp_gbp=149.99,
            is_retiring=False,
            image_url=None,
            source="brickset_web",
            is_retired=True,
        )
        try:
            with patch("src.lego_bot.bot.lookup_set_metadata", new=AsyncMock(return_value=retired_meta)):
                await bot.run_price_checks(set_number="10299")
            assert bot.db.get_item_by_set_number("10299") is None
        finally:
            await bot.http_session.close()
            bot.db.close()

        assert load_wishlist_entries(wishlist_path) == []
        assert len(sent_messages) == 1
        msg = sent_messages[0]
        assert "<@111111>" in msg["content"]
        assert "<@222222>" in msg["content"]
        assert "retired" in msg["content"].lower()
        assert "Removed Retired Set" in msg["embed"].title
