import tempfile
from pathlib import Path

import discord
import pytest

from src.lego_bot.bot import (
    LegoTrackerBot,
    WishlistView,
    build_alert_embeds,
    format_discount_badge,
    format_discount_short,
)
from src.lego_bot.config import Config
from src.lego_bot.db import TrackedItem
from src.lego_bot.predictor import analyze_price
from src.lego_bot.shipping import UKRetailer


def _make_config(tmp: str) -> Config:
    return Config(
        discord_token="test-token",
        channel_id=123456,
        user_id=111111,
        guild_id=None,
        check_interval_minutes=30,
        alert_cooldown_hours=12,
        db_path=str(Path(tmp) / "test.db"),
        wishlist_path=str(Path(tmp) / "wishlist.json"),
        has_amazon_prime=False,
        prefer_click_and_collect=False,
        brickset_api_key=None,
    )


def test_discount_formatting_handles_above_rrp():
    assert format_discount_short(31.4) == "-31%"
    assert format_discount_short(0.0) == "at RRP"
    assert format_discount_short(-5.2) == "+5% above RRP"

    assert format_discount_badge(25.0, 50.0) == "**25% OFF** *(Save £50.00)*"
    assert format_discount_badge(0.0, 0.0) == "**At RRP**"
    assert format_discount_badge(-10.0, -20.0) == "**10% above RRP** *(+£20.00)*"


def test_alert_embed_above_rrp_has_no_negative_percent():
    item = TrackedItem(
        id=1,
        name="Test Set",
        rrp_gbp=100.0,
        set_number="10316",
        category="Icons",
        target_price_gbp=None,
        min_discount_pct=15.0,
        url=None,
        retailer="amazon_uk",
        is_retiring=False,
        image_url=None,
        last_alert_price_gbp=None,
        last_alert_at=None,
        notify_user_ids=(),
        created_at="2026-01-01T00:00:00",
    )
    analysis = analyze_price(rrp_gbp=100.0, item_price_gbp=105.0, shipping_gbp=0.0)
    embeds = build_alert_embeds(item, analysis, UKRetailer.AMAZON_UK, "Free delivery")
    desc = embeds[0].description or ""
    assert "-5" not in desc
    assert "5% above RRP" in desc
    assert "~~RRP" not in desc


def test_simplified_commands_and_admin_permissions():
    with tempfile.TemporaryDirectory() as tmp:
        bot = LegoTrackerBot(_make_config(tmp))
        try:
            bot._register_commands()
            names = {cmd.name for cmd in bot.tree.get_commands()}
            assert names == {"add", "list", "set", "deals", "check", "edit", "remove", "pings", "history", "bot"}

            bot_cmd = bot.tree.get_command("bot")
            assert bot_cmd is not None
            assert bot_cmd.default_permissions is not None
            assert bot_cmd.default_permissions.administrator is True

            sub_names = {c.name for c in bot_cmd.commands}  # type: ignore[attr-defined]
            assert sub_names == {"status", "devmode", "maintenance", "update"}
        finally:
            bot.db.close()


def test_pinned_status_includes_store_health():
    with tempfile.TemporaryDirectory() as tmp:
        bot = LegoTrackerBot(_make_config(tmp))
        try:
            text = bot._format_status_text("Checked **Rivendell**.")
            assert "Stores:" in text
            assert "Scheduled Check Status" in text
        finally:
            bot.db.close()


@pytest.mark.asyncio
async def test_wishlist_view_paginates_compactly():
    with tempfile.TemporaryDirectory() as tmp:
        bot = LegoTrackerBot(_make_config(tmp))
        try:
            for i in range(12):
                bot.db.add_item(name=f"Set {i:02d}", rrp_gbp=50.0, set_number=str(10000 + i))

            view = WishlistView(bot, page=0)
            embed_p1 = view.build_embed()
            assert "(12 sets)" in (embed_p1.title or "")
            assert "Page 1/2" in (embed_p1.title or "")
            assert "Set 00" in (embed_p1.description or "")
            assert "Set 11" not in (embed_p1.description or "")

            view_p2 = WishlistView(bot, page=1)
            embed_p2 = view_p2.build_embed()
            assert "Page 2/2" in (embed_p2.title or "")
            assert "Set 11" in (embed_p2.description or "")
        finally:
            bot.db.close()


def test_quiet_hours_and_link_buttons():
    from datetime import datetime, timezone
    from src.lego_bot.scheduler import is_quiet_hours
    from src.lego_bot.views import DealLinkView

    late = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    morning = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(23, 7, now=late) is True
    assert is_quiet_hours(23, 7, now=morning) is False

    @pytest.mark.asyncio
    async def _check_btn():
        v = DealLinkView("https://www.amazon.co.uk/dp/B001", "Amazon UK")
        assert len(v.children) == 1
        btn = v.children[0]
        assert isinstance(btn, discord.ui.Button)
        assert btn.style == discord.ButtonStyle.link
        assert btn.url == "https://www.amazon.co.uk/dp/B001"
