from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .chart import generate_price_chart
from .config import Config
from .db import PriceDatabase, TrackedItem
from .predictor import PriceAnalysis, analyze_price
from .shipping import APPROVED_UK_RETAILERS, UKRetailer, detect_retailer_from_url
from .uk_finder import (
    HAS_CURL_CFFI,
    build_uk_search_urls,
    clean_set_display_name,
    clear_cooldowns,
    clear_price_cache,
    default_box_art_url,
    find_best_uk_offer,
    get_price_cache_stats,
    get_source_health,
    load_amazon_dp_urls,
    lookup_set_metadata,
    pop_finished_cooldowns,
    pop_long_outage_sources,
)
from .wishlist_store import load_wishlist_entries, remove_wishlist_entry, upsert_wishlist_entry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lego_bot")

COMMAND_TIMEOUT_SECONDS = 300.0


def format_pings(
    default_user_id: int,
    item: TrackedItem,
    extra_user_ids: tuple[str, ...] = ("942902769319292948",),
) -> str:
    ids = list(item.notify_user_ids) if item.notify_user_ids else [str(default_user_id), *extra_user_ids]
    return " ".join(f"<@{uid}>" for uid in dict.fromkeys(ids))


def format_item_title(item: TrackedItem) -> str:
    display_name = clean_set_display_name(item.name, item.set_number) or item.name
    if item.set_number and item.set_number not in display_name:
        return f"{display_name} (#{item.set_number})"
    return display_name


def make_status_embed(
    title: str,
    description: str,
    *,
    color: discord.Color | None = None,
    image_url: str | None = None,
    footer: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blue(),
    )
    if image_url:
        embed.set_image(url=image_url)
    if footer:
        embed.set_footer(text=footer)
    return embed


def make_error_embed(title: str, detail: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌ {title}",
        description=detail,
        color=discord.Color.red(),
    )


def build_alert_embeds(
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    shipping_note: str,
    offer_url: str | None = None,
    *,
    is_all_time_low: bool = False,
    has_chart: bool = False,
    simulated: bool = False,
) -> list[discord.Embed]:
    if "BUY NOW" in analysis.verdict:
        color = discord.Color.green()
        verdict_icon = "🟢"
    elif "GOOD DEAL" in analysis.verdict or "BUY SOON" in analysis.verdict:
        color = discord.Color.gold()
        verdict_icon = "🟡"
    else:
        color = discord.Color.orange()
        verdict_icon = "🟠"

    title_prefix = "🧪 [TEST] " if simulated else ""
    atl_badge = "🔥 NEW ALL-TIME LOW! " if is_all_time_low else ""
    link_url = offer_url or item.url
    title = format_item_title(item)

    headline = (
        f"## 🏷️ Deal Price: £{analysis.total_gbp:.2f}\n"
        f"### ~~RRP: £{analysis.rrp_gbp:.2f}~~  •  **{analysis.discount_pct:.1f}% OFF** *(Save £{analysis.savings_gbp:.2f})*\n"
        f"📍 Best in-stock UK price at **{retailer.display_name}**"
    )

    embed = discord.Embed(
        title=f"{title_prefix}{atl_badge}{title}",
        url=link_url or None,
        description=headline,
        color=color,
    )

    if item.category:
        embed.add_field(name="📂 Category", value=f"**{item.category}**", inline=False)

    embed.add_field(
        name="💷 Price Breakdown",
        value=(
            f"• **Item Price:** £{analysis.item_price_gbp:.2f}\n"
            f"• **UK Delivery:** £{analysis.shipping_gbp:.2f} *({shipping_note})*\n"
            f"• **Total Landed:** **£{analysis.total_gbp:.2f}**\n"
            f"• **Normal RRP:** £{analysis.rrp_gbp:.2f}"
        ),
        inline=True,
    )

    history_lines = []
    if is_all_time_low:
        history_lines.append("🔥 **Lowest price ever tracked!**")
    if analysis.historical_low_gbp is not None:
        history_lines.append(f"• **Tracked Low:** £{analysis.historical_low_gbp:.2f}")
    if analysis.historical_avg_gbp is not None:
        history_lines.append(f"• **Tracked Avg:** £{analysis.historical_avg_gbp:.2f}")
    history_lines.append(f"• **Retiring Soon:** {'⚠️ Yes' if item.is_retiring else 'No'}")

    embed.add_field(
        name="📊 Price Context",
        value="\n".join(history_lines),
        inline=True,
    )

    embed.add_field(
        name=f"{verdict_icon} Verdict: {analysis.verdict}",
        value=analysis.forecast,
        inline=False,
    )

    if link_url:
        embed.add_field(
            name="🛒 Where to Buy",
            value=f"**[Open {retailer.display_name} Deal Page →]({link_url})**",
            inline=False,
        )

    box_art = item.image_url or default_box_art_url(item.set_number)
    if box_art:
        embed.set_image(url=box_art)

    embed.set_footer(text=f"Set #{item.set_number or item.id} • UK GBP (£) inc. delivery")

    embeds = [embed]
    if has_chart:
        chart_embed = discord.Embed(
            title="📈 Tracked Price History (GBP)",
            color=color,
        )
        chart_embed.set_image(url="attachment://chart.png")
        embeds.append(chart_embed)

    return embeds


class _PingUserSelect(discord.ui.UserSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="1. Pick a user to manage their pings...",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, PingManagerView)
        view.target_user = self.values[0]
        view.rebuild_items()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _PingSetSelect(discord.ui.Select):
    def __init__(
        self,
        options: list[discord.SelectOption],
        chunk_keys: set[str],
        *,
        placeholder: str,
        row: int,
    ) -> None:
        self.chunk_keys = chunk_keys
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, PingManagerView)
        if not view.target_user:
            await interaction.response.send_message(
                embed=make_error_embed("Pick a User", "Please pick a user first."),
                ephemeral=True,
            )
            return
        selected_keys = set(self.values)
        uid = str(view.target_user.id)
        for item in view.bot.db.list_items():
            key = item.set_number or str(item.id)
            if key not in self.chunk_keys:
                continue
            current = (
                list(item.notify_user_ids)
                if item.notify_user_ids
                else list(dict.fromkeys([str(view.bot.config.user_id), *view.bot.config.extra_notify_user_ids]))
            )
            want = key in selected_keys
            changed = False
            if want and uid not in current:
                current.append(uid)
                changed = True
            elif not want and uid in current:
                current.remove(uid)
                changed = True
            elif not item.notify_user_ids:
                changed = True

            if changed:
                updated = view.bot.db.update_item(item.id, notify_user_ids=current)
                if updated and updated.set_number:
                    upsert_wishlist_entry(
                        view.bot.config.wishlist_path,
                        {
                            "name": updated.name,
                            "set_number": updated.set_number,
                            "notify_user_ids": list(updated.notify_user_ids),
                        },
                    )
        view.rebuild_items()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PingManagerView(discord.ui.View):
    def __init__(self, bot: "LegoTrackerBot", target_user: discord.abc.User | None = None) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.target_user = target_user
        self.rebuild_items()

    def rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(_PingUserSelect())

        items = self.bot.db.list_items()
        if self.target_user and items:
            uid = str(self.target_user.id)
            chunks = [items[i : i + 25] for i in range(0, min(len(items), 75), 25)]
            for idx, chunk in enumerate(chunks):
                options: list[discord.SelectOption] = []
                chunk_keys: set[str] = set()
                for item in chunk:
                    key = item.set_number or str(item.id)
                    chunk_keys.add(key)
                    current = (
                        list(item.notify_user_ids)
                        if item.notify_user_ids
                        else list(dict.fromkeys([str(self.bot.config.user_id), *self.bot.config.extra_notify_user_ids]))
                    )
                    label = format_item_title(item)[:100]
                    desc = (item.category or "LEGO Set")[:100]
                    options.append(
                        discord.SelectOption(
                            label=label,
                            value=key,
                            description=desc,
                            default=(uid in current),
                        )
                    )
                start_n = idx * 25 + 1
                end_n = idx * 25 + len(chunk)
                placeholder = (
                    "2. Check/uncheck sets for this user..."
                    if len(chunks) == 1
                    else f"2. Check/uncheck sets ({start_n}–{end_n})..."
                )
                self.add_item(_PingSetSelect(options, chunk_keys, placeholder=placeholder, row=1 + idx))

            btn_row = 1 + len(chunks)
            add_all_btn = discord.ui.Button(
                label="Add to ALL",
                emoji="✅",
                style=discord.ButtonStyle.success,
                row=btn_row,
            )
            remove_all_btn = discord.ui.Button(
                label="Remove from ALL",
                emoji="🔕",
                style=discord.ButtonStyle.danger,
                row=btn_row,
            )

            async def _on_add_all(interaction: discord.Interaction) -> None:
                await self._bulk_set(interaction, add=True)

            async def _on_remove_all(interaction: discord.Interaction) -> None:
                await self._bulk_set(interaction, add=False)

            add_all_btn.callback = _on_add_all
            remove_all_btn.callback = _on_remove_all
            self.add_item(add_all_btn)
            self.add_item(remove_all_btn)

    async def _bulk_set(self, interaction: discord.Interaction, *, add: bool) -> None:
        if not self.target_user:
            return
        uid = str(self.target_user.id)
        for item in self.bot.db.list_items():
            current = (
                list(item.notify_user_ids)
                if item.notify_user_ids
                else list(dict.fromkeys([str(self.bot.config.user_id), *self.bot.config.extra_notify_user_ids]))
            )
            if add and uid not in current:
                current.append(uid)
            elif not add and uid in current:
                current.remove(uid)
            updated = self.bot.db.update_item(item.id, notify_user_ids=current)
            if updated and updated.set_number:
                upsert_wishlist_entry(
                    self.bot.config.wishlist_path,
                    {
                        "name": updated.name,
                        "set_number": updated.set_number,
                        "notify_user_ids": list(updated.notify_user_ids),
                    },
                )
        self.rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        items = self.bot.db.list_items()
        if not items:
            return make_status_embed("🔔 Manage Pings", "Your wishlist is currently empty.")
        if not self.target_user:
            return make_status_embed(
                "🔔 Interactive Ping Manager",
                "Pick a Discord user from the dropdown below to check/uncheck which LEGO sets they get pinged for.",
                color=discord.Color.blue(),
            )
        uid = str(self.target_user.id)
        lines = []
        for item in items:
            current = (
                list(item.notify_user_ids)
                if item.notify_user_ids
                else list(dict.fromkeys([str(self.bot.config.user_id), *self.bot.config.extra_notify_user_ids]))
            )
            mark = "✅" if uid in current else "⬜"
            lines.append(f"{mark} **{format_item_title(item)}**")
        return make_status_embed(
            "🔔 Interactive Ping Manager",
            (
                f"Managing pings for <@{uid}>\n"
                f"Use the dropdown below to check/uncheck sets, or click **Add to ALL** / **Remove from ALL**:\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.blue(),
            footer="Changes save immediately • Menu stays active for 3 minutes",
        )


class CheckResultView(discord.ui.View):
    """Interactive buttons attached to live price-check embeds."""

    def __init__(self, bot: "LegoTrackerBot", set_number: str | None) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.set_number = set_number
        if not set_number:
            self.recheck_btn.disabled = True

    @discord.ui.button(label="Re-check", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def recheck_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.set_number:
            await interaction.response.send_message("Set number unavailable.", ephemeral=True)
            return
        await interaction.response.defer()
        embeds = await self.bot.run_price_checks(set_number=self.set_number, force_refresh=True)
        if embeds:
            await interaction.edit_original_response(embed=embeds[0], view=self)

    @discord.ui.button(label="Manage Pings", emoji="🔔", style=discord.ButtonStyle.primary)
    async def pings_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = PingManagerView(self.bot, target_user=interaction.user)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


class RetiredSetView(discord.ui.View):
    """Undo button attached to retired-set auto-removal notifications."""

    def __init__(self, bot: "LegoTrackerBot", item: TrackedItem) -> None:
        super().__init__(timeout=86400)
        self.bot = bot
        self.item = item

    @discord.ui.button(label="Re-add Set", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def readd_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.item.set_number and self.bot.db.get_item_by_set_number(self.item.set_number) is None:
            self.bot.db.add_item(
                name=self.item.name,
                rrp_gbp=self.item.rrp_gbp,
                set_number=self.item.set_number,
                category=self.item.category,
                target_price_gbp=self.item.target_price_gbp,
                min_discount_pct=self.item.min_discount_pct,
                url=self.item.url,
                retailer=self.item.retailer,
                is_retiring=self.item.is_retiring,
                image_url=self.item.image_url,
                notify_user_ids=self.item.notify_user_ids,
            )
            upsert_wishlist_entry(
                self.bot.config.wishlist_path,
                {
                    "name": self.item.name,
                    "set_number": self.item.set_number,
                    "category": self.item.category,
                    "rrp_gbp": self.item.rrp_gbp if self.item.rrp_gbp > 0 else None,
                    "target_price_gbp": self.item.target_price_gbp,
                    "min_discount_pct": self.item.min_discount_pct,
                    "is_retiring": self.item.is_retiring,
                    "image_url": self.item.image_url,
                    "notify_user_ids": list(self.item.notify_user_ids),
                },
            )
        button.disabled = True
        button.label = "Re-added"
        await interaction.response.edit_message(view=self)


def is_admin_user(interaction: discord.Interaction, owner_user_id: int) -> bool:
    """Return True if the user is a Discord server Administrator or the configured bot owner."""
    if interaction.user.id == owner_user_id:
        return True
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


class LegoTrackerBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.db = PriceDatabase(config.db_path)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self._last_retiring_meta_check: dict[int, float] = {}
        env_dev = os.getenv("DEV_MODE", "false").lower() == "true"
        self.dev_mode: bool = self.db.get_dev_mode(default=env_dev)

    def _startup_sync_and_clean(self) -> None:
        """Sync wishlist.json into SQLite, clean legacy set titles, and purge poisoned/old history."""
        load_amazon_dp_urls(self.db.get_amazon_dp_urls())
        entries = load_wishlist_entries(self.config.wishlist_path)
        for entry in entries:
            set_num = str(entry.get("set_number") or "").strip() or None
            if not set_num or self.db.get_item_by_set_number(set_num) is not None:
                continue
            raw_name = str(entry.get("name") or f"LEGO Set #{set_num}").strip()
            clean_name = clean_set_display_name(raw_name, set_num) or raw_name
            notify_ids = [str(u) for u in (entry.get("notify_user_ids") or [])]
            self.db.add_item(
                name=clean_name,
                rrp_gbp=float(entry["rrp_gbp"]) if entry.get("rrp_gbp") is not None else 0.0,
                set_number=set_num,
                category=str(entry.get("category") or "").strip() or None,
                target_price_gbp=float(entry["target_price_gbp"]) if entry.get("target_price_gbp") is not None else None,
                min_discount_pct=float(entry.get("min_discount_pct", 15.0)),
                url=str(entry.get("url") or "").strip() or None,
                retailer=detect_retailer_from_url(str(entry.get("url") or "").strip() or None).value,
                is_retiring=bool(entry.get("is_retiring", False)),
                image_url=str(entry.get("image_url") or "").strip() or default_box_art_url(set_num),
                notify_user_ids=notify_ids,
            )

        for item in self.db.list_items():
            cleaned = clean_set_display_name(item.name, item.set_number)
            if cleaned and cleaned != item.name:
                self.db.update_item(item.id, name=cleaned)
                if item.set_number:
                    upsert_wishlist_entry(
                        self.config.wishlist_path,
                        {"name": cleaned, "set_number": item.set_number},
                    )

        deleted_rows, reset_items = self.db.purge_implausible_history()
        pruned_old = self.db.prune_old_history(retention_days=180)
        if deleted_rows or reset_items or pruned_old:
            logger.info(
                "Startup DB cleanup removed %d implausible row(s), pruned %d old row(s), reset %d alert(s)",
                deleted_rows,
                pruned_old,
                reset_items,
            )

    async def setup_hook(self) -> None:
        self._startup_sync_and_clean()
        connector = aiohttp.TCPConnector(limit=16, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(connector=connector)
        self._register_commands()

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced slash commands to guild %s", self.config.guild_id)
        else:
            await self.tree.sync()
            logger.info("Synced global slash commands")

        self.price_check_loop.change_interval(minutes=self.config.check_interval_minutes)
        self.price_check_loop.start()
        if not self.cooldown_watcher_loop.is_running():
            self.cooldown_watcher_loop.start()

    async def close(self) -> None:
        if self.price_check_loop.is_running():
            self.price_check_loop.cancel()
        if self.cooldown_watcher_loop.is_running():
            self.cooldown_watcher_loop.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        from .uk_finder import close_http_sessions

        await close_http_sessions()
        self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "?")

    @tasks.loop(minutes=1)
    async def cooldown_watcher_loop(self) -> None:
        finished = pop_finished_cooldowns()
        if not finished:
            return
        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.channel_id)
            except Exception:
                channel = None
        if isinstance(channel, discord.abc.Messageable):
            stores = ", ".join(f"**{s}**" for s in finished)
            noun = "this store" if len(finished) == 1 else "these stores"
            content = f"@everyone ✅ Cooldown has finished for {stores} — checks for {noun} have resumed."
            allowed = discord.AllowedMentions.none() if self.dev_mode else discord.AllowedMentions(everyone=True)
            await channel.send(content=content, allowed_mentions=allowed)

    @cooldown_watcher_loop.before_loop
    async def before_cooldown_watcher_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=30)
    async def price_check_loop(self) -> None:
        try:
            await asyncio.wait_for(self.run_price_checks(scheduled=True), timeout=600.0)
            outages = pop_long_outage_sources()
            if outages:
                channel = self.get_channel(self.config.channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(self.config.channel_id)
                    except Exception:
                        channel = None
                if isinstance(channel, discord.abc.Messageable):
                    stores = ", ".join(f"`{s}`" for s in outages)
                    await channel.send(
                        content=f"⚠️ **Store Health Notice:** {stores} has been returning errors/blocks for >2 hours.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except Exception as exc:
            logger.error("Scheduled price check failed: %s", exc)

    @price_check_loop.before_loop
    async def before_price_check_loop(self) -> None:
        await self.wait_until_ready()

    async def _maybe_send_weekly_summary(self, channel: object) -> None:
        if not self.db.should_send_weekly_summary() or not isinstance(channel, discord.abc.Messageable):
            return
        self.db.prune_old_history(retention_days=180)
        lines = []
        for item in self.db.list_items():
            hist = self.db.get_price_history(item.id, limit=1)
            low = self.db.get_historical_low(item.id)
            t = format_item_title(item)
            if hist:
                low_str = f" | Low £{low:.2f}" if low is not None else ""
                lines.append(f"• **{t}**: **£{hist[0].total_gbp:.2f}**{low_str}")
            else:
                lines.append(f"• **{t}**: *No price recorded yet*")
        summary_embed = make_status_embed(
            "📊 Weekly UK LEGO Wishlist Summary",
            "\n".join(lines) if lines else "Your wishlist is empty.",
            color=discord.Color.blue(),
        )
        if self.dev_mode:
            summary_content = "🛠️ [DEV MODE] Weekly LEGO summary! *(pings suppressed)*"
            allowed = discord.AllowedMentions.none()
        else:
            summary_pings = " ".join(
                f"<@{uid}>"
                for uid in dict.fromkeys([str(self.config.user_id), *self.config.extra_notify_user_ids])
            )
            summary_content = f"{summary_pings} Weekly LEGO summary!"
            allowed = discord.AllowedMentions.all()
        await channel.send(content=summary_content, embed=summary_embed, allowed_mentions=allowed)
        self.db.mark_weekly_summary_sent()

    async def run_price_checks(
        self,
        set_number: str | None = None,
        *,
        on_item_embed: Callable[[discord.Embed, Optional[discord.ui.View]], Awaitable[None]] | None = None,
        force_refresh: bool = False,
        scheduled: bool = False,
    ) -> list[discord.Embed]:
        assert self.http_session is not None
        if set_number:
            found = self.db.get_item_by_set_number(set_number)
            items = [found] if found else []
        elif scheduled:
            all_items = self.db.list_items()
            if not all_items:
                return []
            items = self.db.get_items_due_for_check(min_age_hours=4.0)[:1]
        else:
            items = self.db.list_items()

        if not items and not scheduled:
            empty_embed = make_status_embed("🧩 Your LEGO Wishlist", "No tracked LEGO items found.")
            if on_item_embed is not None:
                await on_item_embed(empty_embed, None)
            return [empty_embed]

        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.channel_id)
            except Exception as exc:
                logger.error("Could not fetch alert channel %s: %s", self.config.channel_id, exc)
                if not scheduled:
                    err_embed = make_error_embed(
                        "Channel Error", f"Could not access alert channel {self.config.channel_id}."
                    )
                    if on_item_embed is not None:
                        await on_item_embed(err_embed, None)
                    return [err_embed]
                return []

        if scheduled and not items:
            await self._maybe_send_weekly_summary(channel)
            return []

        now_mono = time.monotonic()
        result_embeds: list[discord.Embed] = []
        emit_lock = asyncio.Lock()
        sem = asyncio.Semaphore(1 if scheduled else 3)
        interactive_serp_budget = [5 if set_number is None else 1]

        async def _emit(embed: discord.Embed, item_set_number: str | None = None) -> None:
            async with emit_lock:
                result_embeds.append(embed)
                if on_item_embed is not None:
                    view = CheckResultView(self, item_set_number) if item_set_number else None
                    await on_item_embed(embed, view)

        async def _check_one(idx: int, item: TrackedItem) -> None:
            async with sem:
                if len(items) > 1 and idx > 0:
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                status_msg: Optional[discord.Message] = None
                if scheduled and isinstance(channel, discord.abc.Messageable):
                    try:
                        status_msg = await channel.send(
                            content=f"🔍 **Scheduled Check:** Checking **{format_item_title(item)}** for UK deals...",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception:
                        status_msg = None

                async def _update_status(text: str) -> None:
                    if status_msg is not None:
                        try:
                            await status_msg.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
                        except Exception:
                            pass

                rrp = item.rrp_gbp
                is_retiring = item.is_retiring
                image_url = item.image_url
                resolved_name = clean_set_display_name(item.name, item.set_number) or item.name
                category = item.category

                meta_ttl = 86400.0 if is_retiring else 604800.0
                meta_refresh_due = (
                    now_mono - self._last_retiring_meta_check.get(item.id, 0.0) >= meta_ttl
                )
                needs_meta = (
                    rrp <= 0
                    or not image_url
                    or not category
                    or not resolved_name
                    or resolved_name.startswith("LEGO Set #")
                    or meta_refresh_due
                )
                if needs_meta and item.set_number:
                    meta = await lookup_set_metadata(
                        self.http_session,  # type: ignore[arg-type]
                        item.set_number,
                        brickset_api_key=self.config.brickset_api_key,
                        proxy_url=self.config.proxy_url,
                    )
                    self._last_retiring_meta_check[item.id] = now_mono
                    if meta.is_retired:
                        title = format_item_title(item)
                        box_art = item.image_url or default_box_art_url(item.set_number)
                        self.db.remove_item(item.id)
                        if item.set_number:
                            remove_wishlist_entry(self.config.wishlist_path, item.set_number)
                        retired_embed = make_status_embed(
                            f"📦 Removed Retired Set: {title}",
                            "This LEGO set has now **retired** and was automatically removed from your wishlist.",
                            color=discord.Color.dark_grey(),
                            image_url=box_art,
                        )
                        if isinstance(channel, discord.abc.Messageable):
                            if self.dev_mode:
                                retired_content = f"🛠️ [DEV MODE] **{title}** has been removed from your wishlist because it has retired. *(pings suppressed)*"
                                allowed = discord.AllowedMentions.none()
                            else:
                                pings = format_pings(
                                    self.config.user_id,
                                    item,
                                    extra_user_ids=self.config.extra_notify_user_ids,
                                )
                                retired_content = f"{pings} **{title}** has been removed from your wishlist as it has **retired**."
                                allowed = discord.AllowedMentions.all()
                            retired_view = RetiredSetView(self, item)
                            await channel.send(
                                content=retired_content,
                                embed=retired_embed,
                                view=retired_view,
                                allowed_mentions=allowed,
                            )
                        await _emit(retired_embed)
                        return

                    if meta.name and (resolved_name.startswith("LEGO Set #") or len(meta.name) >= len(resolved_name) - 4):
                        resolved_name = clean_set_display_name(meta.name, item.set_number) or meta.name
                    if not category and meta.category:
                        category = meta.category
                    if rrp <= 0 and meta.rrp_gbp:
                        rrp = meta.rrp_gbp
                    if meta.source in ("brickset", "brickset_web", "lego_uk"):
                        is_retiring = meta.is_retiring
                    if not image_url and meta.image_url:
                        image_url = meta.image_url

                    self.db.update_item(
                        item.id,
                        name=resolved_name,
                        category=category,
                        rrp_gbp=rrp if rrp > 0 else None,
                        is_retiring=is_retiring,
                        image_url=image_url,
                    )
                    item = self.db.get_item(item.id) or item
                    if item.set_number:
                        upsert_wishlist_entry(
                            self.config.wishlist_path,
                            {
                                "name": item.name,
                                "set_number": item.set_number,
                                "category": item.category,
                                "rrp_gbp": item.rrp_gbp if item.rrp_gbp > 0 else None,
                                "is_retiring": item.is_retiring,
                                "image_url": item.image_url,
                            },
                        )

                allow_serpapi = False
                if self.config.serpapi_key:
                    if scheduled:
                        allow_serpapi, _ = self.db.can_spend_token(
                            "serpapi", self.config.serpapi_monthly_limit, manual_force=False
                        )
                    elif force_refresh and interactive_serp_budget[0] > 0:
                        allow_serpapi, _ = self.db.can_spend_token(
                            "serpapi", self.config.serpapi_monthly_limit, manual_force=True
                        )
                        if allow_serpapi:
                            interactive_serp_budget[0] -= 1

                best_offer, notes = await find_best_uk_offer(
                    self.http_session,  # type: ignore[arg-type]
                    name=resolved_name,
                    set_number=item.set_number,
                    direct_url=item.url,
                    rrp_gbp=rrp if rrp > 0 else None,
                    has_amazon_prime=self.config.has_amazon_prime,
                    prefer_click_and_collect=self.config.prefer_click_and_collect,
                    serpapi_key=self.config.serpapi_key,
                    allow_serpapi=allow_serpapi,
                    on_serpapi_credit_used=lambda: self.db.increment_monthly_usage("serpapi"),
                    on_amazon_dp_url_update=self.db.set_amazon_dp_url,
                    proxy_url=self.config.proxy_url,
                    force_refresh=force_refresh,
                )

                title = format_item_title(item)
                box_art = item.image_url or default_box_art_url(item.set_number)
                cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""

                if best_offer is None:
                    # During 0-token interactive checks, fall back to latest verified UK retailer price if recorded
                    latest = self.db.get_latest_price(item.id) if not scheduled and not force_refresh else None
                    rec_retailer = UKRetailer.OTHER
                    if latest is not None:
                        try:
                            rec_retailer = UKRetailer(latest.retailer)
                        except ValueError:
                            rec_retailer = UKRetailer.OTHER

                    if latest is not None and rec_retailer in APPROVED_UK_RETAILERS:
                        eff_rrp = rrp if rrp > 0 else latest.price_gbp
                        hist_totals = [h.total_gbp for h in self.db.get_price_history(item.id, limit=90)]
                        cached_analysis = analyze_price(
                            rrp_gbp=eff_rrp,
                            item_price_gbp=latest.price_gbp,
                            shipping_gbp=latest.shipping_gbp,
                            historical_totals_gbp=hist_totals,
                            is_retiring=is_retiring,
                        )
                        icon = (
                            "🟢"
                            if "BUY NOW" in cached_analysis.verdict
                            else ("🟡" if "GOOD" in cached_analysis.verdict or "SOON" in cached_analysis.verdict else "🟠")
                        )
                        fallback_url: Optional[str] = (
                            item.url
                            if item.url and detect_retailer_from_url(item.url) == rec_retailer
                            else None
                        )
                        if not fallback_url and rec_retailer == UKRetailer.AMAZON_UK and item.set_number:
                            clean_num = item.set_number.strip().split("-")[0]
                            fallback_url = self.db.get_amazon_dp_urls().get(clean_num)
                        if not fallback_url and item.set_number:
                            fallback_url = dict(build_uk_search_urls(resolved_name, item.set_number)).get(rec_retailer)
                        if not fallback_url:
                            fallback_url = item.url
                        verdict_line = (
                            f"{icon} **{cached_analysis.verdict}** — [View Deal →]({fallback_url})"
                            if fallback_url
                            else f"{icon} **{cached_analysis.verdict}**"
                        )
                        await _emit(
                            make_status_embed(
                                f"🧩 {title}",
                                (
                                    f"{cat_line}"
                                    f"## 🏷️ Last Verified Price: £{cached_analysis.total_gbp:.2f}\n"
                                    f"⚠️ *Live stores were unavailable — this saved price may not be up-to-date. Click **Re-check** to try again.*\n"
                                    f"### Normal RRP: £{cached_analysis.rrp_gbp:.2f}  •  **{cached_analysis.discount_pct:.1f}% OFF**\n"
                                    f"📍 **Seller:** {rec_retailer.display_name} *(recorded `{latest.recorded_at[:10]}`)*\n"
                                    f"{verdict_line}"
                                ),
                                color=discord.Color.blue(),
                                image_url=box_art,
                                footer="Cached DB price • Click Re-check for a fresh scan",
                            ),
                            item.set_number,
                        )
                        return

                    rrp_line = f"💷 **Normal RRP:** £{rrp:.2f}\n" if rrp > 0 else ""
                    diag = "\n".join(f"• {n}" for n in notes[:5]) if notes else "No approved UK retailers had stock."
                    await _emit(
                        make_status_embed(
                            f"🧩 {title}",
                            (
                                f"{cat_line}{rrp_line}"
                                f"⚠️ **No live in-stock UK offers found right now**\n"
                                f"*Sources checked:*\n{diag}"
                            ),
                            color=discord.Color.orange(),
                            image_url=box_art,
                            footer="Checked approved UK retailers • GBP (£) inc. delivery",
                        ),
                        item.set_number,
                    )
                    await _update_status(f"ℹ️ **{title}** had no good deals *(no in-stock UK offers found)*.")
                    return

                if rrp <= 0:
                    rrp = best_offer.item_price_gbp

                prev_low = self.db.get_historical_low(item.id)
                is_all_time_low = prev_low is not None and best_offer.total_gbp < prev_low

                history = self.db.get_price_history(item.id, limit=90)
                hist_totals = [h.total_gbp for h in history]
                if best_offer.historical_low_gbp is not None:
                    hist_totals.append(best_offer.historical_low_gbp)
                if best_offer.historical_avg_gbp is not None:
                    hist_totals.append(best_offer.historical_avg_gbp)

                self.db.record_price(
                    item.id,
                    best_offer.item_price_gbp,
                    best_offer.shipping_gbp,
                    best_offer.total_gbp,
                    best_offer.retailer.value,
                )

                analysis = analyze_price(
                    rrp_gbp=rrp,
                    item_price_gbp=best_offer.item_price_gbp,
                    shipping_gbp=best_offer.shipping_gbp,
                    historical_totals_gbp=hist_totals,
                    is_retiring=is_retiring,
                )

                icon = "🟢" if "BUY NOW" in analysis.verdict else ("🟡" if "GOOD" in analysis.verdict or "SOON" in analysis.verdict else "🟠")
                color = discord.Color.green() if "BUY NOW" in analysis.verdict else (discord.Color.gold() if "GOOD" in analysis.verdict or "SOON" in analysis.verdict else discord.Color.blue())
                ship_line = (
                    f"*(incl. £{best_offer.shipping_gbp:.2f} delivery — £{best_offer.item_price_gbp:.2f} item)*\n"
                    if best_offer.shipping_gbp > 0
                    else ""
                )
                await _emit(
                    make_status_embed(
                        f"🧩 {title}",
                        (
                            f"{cat_line}"
                            f"## 🏷️ Deal Price: £{analysis.total_gbp:.2f}\n"
                            f"{ship_line}"
                            f"### Normal RRP: £{analysis.rrp_gbp:.2f}  •  **{analysis.discount_pct:.1f}% OFF**\n"
                            f"📍 **Seller:** {best_offer.retailer.display_name}\n"
                            f"{icon} **{analysis.verdict}** — [View Deal →]({best_offer.url})"
                        ),
                        color=color,
                        image_url=box_art,
                        footer="Prices in GBP (£) including estimated UK delivery",
                    ),
                    item.set_number,
                )

                hit_target = item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
                hit_discount = analysis.discount_pct >= item.min_discount_pct
                if not (hit_target or hit_discount):
                    if item.last_alert_price_gbp is not None:
                        self.db.reset_alert_state(item.id)
                    await _update_status(
                        f"ℹ️ **{title}** had no good deals "
                        f"*(best: £{analysis.total_gbp:.2f} at {best_offer.retailer.display_name}, {analysis.discount_pct:.1f}% off)*."
                    )
                elif self.db.should_alert(
                    item, analysis.total_gbp, self.config.alert_cooldown_hours
                ):
                    await _update_status(f"🔥 Good deal spotted for **{title}**!")
                    series = list(reversed([h.total_gbp for h in self.db.get_price_history(item.id, limit=30)]))
                    chart_bytes = generate_price_chart(series, rrp) if len(series) >= 2 else None
                    embeds = build_alert_embeds(
                        item,
                        analysis,
                        best_offer.retailer,
                        best_offer.shipping_note,
                        offer_url=best_offer.url,
                        is_all_time_low=is_all_time_low,
                        has_chart=chart_bytes is not None,
                    )
                    if isinstance(channel, discord.abc.Messageable):
                        if self.dev_mode:
                            alert_content = "🛠️ [DEV MODE] LEGO sale spotted! *(pings suppressed)*"
                            allowed = discord.AllowedMentions.none()
                        else:
                            pings = format_pings(
                                self.config.user_id,
                                item,
                                extra_user_ids=self.config.extra_notify_user_ids,
                            )
                            alert_content = f"{pings} LEGO sale spotted!"
                            allowed = discord.AllowedMentions.all()
                        if chart_bytes:
                            file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
                            await channel.send(
                                content=alert_content, embeds=embeds, file=file, allowed_mentions=allowed
                            )
                        else:
                            await channel.send(content=alert_content, embeds=embeds, allowed_mentions=allowed)
                        self.db.mark_alerted(item.id, analysis.total_gbp)
                else:
                    await _update_status(
                        f"ℹ️ **{title}** is still on deal (**£{analysis.total_gbp:.2f}** at "
                        f"{best_offer.retailer.display_name} — alert cooldown active)."
                    )

        await asyncio.gather(*(_check_one(idx, it) for idx, it in enumerate(items)))

        if scheduled and not set_number:
            await self._maybe_send_weekly_summary(channel)

        return result_embeds

    def _register_commands(self) -> None:
        wishlist_group = app_commands.Group(name="wishlist", description="Manage your UK LEGO wishlist")
        item_group = app_commands.Group(name="item", description="Item actions and notifications")
        bot_group = app_commands.Group(name="bot", description="Bot management commands")

        @wishlist_group.command(name="add", description="Add a LEGO set to your wishlist using just its set number")
        @app_commands.describe(
            set_number="LEGO set number (e.g. 10316)",
            name="Optional custom name (auto-fetched if left blank)",
            min_discount_pct="Alert when total discount is at least this % off RRP (default 15%)",
            target_price_gbp="Optional target price in £ (including shipping)",
        )
        async def wishlist_add(
            interaction: discord.Interaction,
            set_number: str,
            name: Optional[str] = None,
            min_discount_pct: float = 15.0,
            target_price_gbp: Optional[float] = None,
        ) -> None:
            await interaction.response.defer()
            try:
                assert self.http_session is not None
                clean_num = set_number.strip().split("-")[0]
                existing = self.db.get_item_by_set_number(clean_num)
                if existing:
                    embed = make_status_embed(
                        "Already on Wishlist",
                        f"**{format_item_title(existing)}** is already tracked.",
                        color=discord.Color.gold(),
                        image_url=existing.image_url or default_box_art_url(clean_num),
                    )
                    await interaction.followup.send(embed=embed)
                    return

                meta = await asyncio.wait_for(
                    lookup_set_metadata(
                        self.http_session,
                        clean_num,
                        brickset_api_key=self.config.brickset_api_key,
                        proxy_url=self.config.proxy_url,
                    ),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
                resolved_name = name or meta.name or f"LEGO Set #{clean_num}"
                rrp = meta.rrp_gbp or 0.0
                notify_ids = list(
                    dict.fromkeys([str(self.config.user_id), *self.config.extra_notify_user_ids, str(interaction.user.id)])
                )
                box_art = meta.image_url or default_box_art_url(clean_num)

                item = self.db.add_item(
                    name=resolved_name,
                    rrp_gbp=rrp,
                    set_number=clean_num,
                    category=meta.category,
                    target_price_gbp=target_price_gbp,
                    min_discount_pct=min_discount_pct,
                    is_retiring=meta.is_retiring,
                    image_url=box_art,
                    notify_user_ids=notify_ids,
                )
                upsert_wishlist_entry(
                    self.config.wishlist_path,
                    {
                        "name": item.name,
                        "set_number": clean_num,
                        "category": item.category,
                        "rrp_gbp": rrp if rrp > 0 else None,
                        "target_price_gbp": target_price_gbp,
                        "min_discount_pct": min_discount_pct,
                        "is_retiring": meta.is_retiring,
                        "image_url": box_art,
                        "notify_user_ids": notify_ids,
                    },
                )
                rrp_msg = f"£{rrp:.2f}" if rrp > 0 else "Auto-detect on next check"
                cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""
                embed = make_status_embed(
                    f"✅ Added: {format_item_title(item)}",
                    (
                        f"{cat_line}"
                        f"💷 **Normal RRP:** {rrp_msg}\n"
                        f"⚠️ **Retiring Soon:** {'Yes' if meta.is_retiring else 'No'}\n"
                        f"🔔 **Notify:** {format_pings(self.config.user_id, item, self.config.extra_notify_user_ids)}"
                    ),
                    color=discord.Color.green(),
                    image_url=box_art,
                    footer="Saved to wishlist.json",
                )
                await interaction.followup.send(embed=embed)
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Timed Out", "Looking up set details took too long. Please try again."),
                    ephemeral=True,
                )
            except Exception as exc:
                logger.exception("wishlist add failed")
                await interaction.followup.send(
                    embed=make_error_embed("Failed to Add Set", str(exc)),
                    ephemeral=True,
                )

        @wishlist_group.command(name="list", description="List all LEGO sets on your wishlist")
        async def wishlist_list(interaction: discord.Interaction) -> None:
            items = self.db.list_items()
            if not items:
                embed = make_status_embed(
                    "🧩 Your LEGO Wishlist",
                    "Your wishlist is empty. Use `/wishlist add <set_number>` to add a set!",
                    color=discord.Color.blue(),
                )
                await interaction.response.send_message(embed=embed)
                return

            item_embeds: list[discord.Embed] = []
            for item in items:
                hist = self.db.get_price_history(item.id, limit=1)
                latest_line = f"🏷️ **Latest Price:** £{hist[0].total_gbp:.2f}\n" if hist else "🏷️ **Latest Price:** *Not checked yet*\n"
                rrp_str = f"£{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "Auto-detect"
                cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""
                target_str = f"£{item.target_price_gbp:.2f}" if item.target_price_gbp else "None"
                retire_line = "⚠️ **Retiring Soon:** Yes\n" if item.is_retiring else ""
                pings = format_pings(
                    self.config.user_id,
                    item,
                    extra_user_ids=self.config.extra_notify_user_ids,
                )
                box_art = item.image_url or default_box_art_url(item.set_number)
                item_embeds.append(
                    make_status_embed(
                        f"🧩 {format_item_title(item)}",
                        (
                            f"{cat_line}"
                            f"💷 **Normal RRP:** {rrp_str}\n"
                            f"{latest_line}"
                            f"🎯 **Alert:** ≥{item.min_discount_pct:.0f}% off | Target {target_str}\n"
                            f"{retire_line}"
                            f"🔔 **Notify:** {pings}"
                        ),
                        color=discord.Color.blue(),
                        image_url=box_art,
                        footer=f"Set #{item.set_number or item.id} • Run /wishlist check for live prices",
                    )
                )

            batches = [item_embeds[i : i + 5] for i in range(0, len(item_embeds), 5)]
            await interaction.response.send_message(embeds=batches[0])
            for batch in batches[1:]:
                await interaction.followup.send(embeds=batch)

        async def _set_number_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            query = current.lower().strip()
            choices: list[app_commands.Choice[str]] = []
            for item in self.db.list_items():
                if not item.set_number:
                    continue
                label = format_item_title(item)[:100]
                if query and query not in label.lower() and query not in item.set_number.lower():
                    continue
                choices.append(app_commands.Choice(name=label, value=item.set_number))
                if len(choices) >= 25:
                    break
            return choices

        @wishlist_group.command(name="check", description="Check the current UK price of all items (or one specific set) on your wishlist")
        @app_commands.describe(
            set_number="Optional LEGO set number to check (leave blank to check all)",
            force="Bypass the 30-minute price cache and fetch fresh UK prices",
        )
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def wishlist_check(
            interaction: discord.Interaction,
            set_number: Optional[str] = None,
            force: bool = False,
        ) -> None:
            if not set_number and not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed(
                        "Admin Only",
                        "Checking the **entire** wishlist at once is restricted to server admins. Please pick a specific `set_number` instead!",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                async def _send_one(embed: discord.Embed, view: Optional[discord.ui.View] = None) -> None:
                    if view is not None:
                        await interaction.followup.send(embed=embed, view=view)
                    else:
                        await interaction.followup.send(embed=embed)

                await asyncio.wait_for(
                    self.run_price_checks(set_number=set_number, on_item_embed=_send_one, force_refresh=force),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Timed Out", "UK retailers took too long to respond (>5m). Please try again.")
                )
            except Exception as exc:
                logger.exception("wishlist check failed")
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Error", str(exc))
                )

        @wishlist_group.command(name="edit", description="Edit alert settings for a tracked LEGO set")
        @app_commands.describe(
            set_number="LEGO set number (e.g. 10316)",
            target_price_gbp="New target price in £ (enter 0 to clear)",
            min_discount_pct="New minimum discount %",
            is_retiring="Mark as retiring soon (True/False)",
        )
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def wishlist_edit(
            interaction: discord.Interaction,
            set_number: str,
            target_price_gbp: Optional[float] = None,
            min_discount_pct: Optional[float] = None,
            is_retiring: Optional[bool] = None,
        ) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            updated = self.db.update_item(
                item.id,
                target_price_gbp=target_price_gbp,
                min_discount_pct=min_discount_pct,
                is_retiring=is_retiring,
            )
            assert updated is not None
            upsert_wishlist_entry(
                self.config.wishlist_path,
                {
                    "name": updated.name,
                    "set_number": updated.set_number,
                    "target_price_gbp": updated.target_price_gbp,
                    "min_discount_pct": updated.min_discount_pct,
                    "is_retiring": updated.is_retiring,
                },
            )
            embed = make_status_embed(
                f"✏️ Updated: {format_item_title(updated)}",
                (
                    f"• **Target Price:** {f'£{updated.target_price_gbp:.2f}' if updated.target_price_gbp else 'None'}\n"
                    f"• **Min Discount:** {updated.min_discount_pct:.0f}%\n"
                    f"• **Retiring Soon:** {'Yes' if updated.is_retiring else 'No'}"
                ),
                color=discord.Color.green(),
                image_url=updated.image_url or default_box_art_url(updated.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @wishlist_group.command(name="remove", description="Remove a LEGO set from your wishlist")
        @app_commands.describe(set_number="LEGO set number to remove (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def wishlist_remove(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            self.db.remove_item(item.id)
            remove_wishlist_entry(self.config.wishlist_path, set_number)
            embed = make_status_embed(
                "🗑️ Removed from Wishlist",
                f"Removed **{format_item_title(item)}** from your wishlist.",
                color=discord.Color.orange(),
                image_url=item.image_url or default_box_art_url(item.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @item_group.command(name="notify", description="Toggle who gets pinged when a LEGO set goes on sale")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)", user="Discord user to add/remove from pings")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_notify(interaction: discord.Interaction, set_number: str, user: discord.User) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            uid = str(user.id)
            current = (
                list(item.notify_user_ids)
                if item.notify_user_ids
                else list(dict.fromkeys([str(self.config.user_id), *self.config.extra_notify_user_ids]))
            )
            if uid in current:
                current.remove(uid)
                action = f"Removed <@{uid}> from"
            else:
                current.append(uid)
                action = f"Added <@{uid}> to"
            updated = self.db.update_item(item.id, notify_user_ids=current)
            assert updated is not None
            upsert_wishlist_entry(
                self.config.wishlist_path,
                {"name": updated.name, "set_number": updated.set_number, "notify_user_ids": list(updated.notify_user_ids)},
            )
            pings_text = format_pings(self.config.user_id, updated) if updated.notify_user_ids else "None (no one will be pinged)"
            embed = make_status_embed(
                f"🔔 Notifications Updated: {format_item_title(updated)}",
                f"{action} sale alerts.\n**Current pings:** {pings_text}",
                color=discord.Color.blue(),
                image_url=updated.image_url or default_box_art_url(updated.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @item_group.command(name="notify_remove", description="Remove a user from sale pings for a LEGO set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)", user="Discord user to remove from pings")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_notify_remove(interaction: discord.Interaction, set_number: str, user: discord.User) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            uid = str(user.id)
            current = (
                list(item.notify_user_ids)
                if item.notify_user_ids
                else list(dict.fromkeys([str(self.config.user_id), *self.config.extra_notify_user_ids]))
            )
            if uid not in current:
                await interaction.response.send_message(
                    embed=make_status_embed(
                        f"🔔 Not Listed: {format_item_title(item)}",
                        f"<@{uid}> is not currently on the ping list for this set.\n**Current pings:** {format_pings(self.config.user_id, item)}",
                        color=discord.Color.gold(),
                    )
                )
                return
            current.remove(uid)
            updated = self.db.update_item(item.id, notify_user_ids=current)
            assert updated is not None
            upsert_wishlist_entry(
                self.config.wishlist_path,
                {"name": updated.name, "set_number": updated.set_number, "notify_user_ids": list(updated.notify_user_ids)},
            )
            pings_text = format_pings(self.config.user_id, updated) if updated.notify_user_ids else "None"
            embed = make_status_embed(
                f"🔕 Removed Ping: {format_item_title(updated)}",
                f"Removed <@{uid}> from sale alerts.\n**Current pings:** {pings_text}",
                color=discord.Color.orange(),
                image_url=updated.image_url or default_box_art_url(updated.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @item_group.command(name="notify_list", description="See who gets pinged when a LEGO set goes on sale")
        @app_commands.describe(set_number="Optional LEGO set number (e.g. 10316). Leave blank to see all sets.")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_notify_list(interaction: discord.Interaction, set_number: Optional[str] = None) -> None:
            if set_number:
                item = self.db.get_item_by_set_number(set_number)
                if not item:
                    await interaction.response.send_message(
                        embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                        ephemeral=True,
                    )
                    return
                items = [item]
            else:
                items = self.db.list_items()
                if not items:
                    await interaction.response.send_message(
                        embed=make_status_embed("🔔 Notification List", "Your wishlist is currently empty.")
                    )
                    return

            embeds: list[discord.Embed] = []
            for item in items:
                pings = format_pings(
                    self.config.user_id,
                    item,
                    extra_user_ids=self.config.extra_notify_user_ids,
                )
                cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""
                box_art = item.image_url or default_box_art_url(item.set_number)
                embeds.append(
                    make_status_embed(
                        f"🔔 Notifications: {format_item_title(item)}",
                        f"{cat_line}• **Pinged on sale:** {pings}",
                        color=discord.Color.blue(),
                        image_url=box_art,
                        footer=f"Set #{item.set_number or item.id} • Use /item notify or /item notify_remove to edit",
                    )
                )

            batches = [embeds[i : i + 5] for i in range(0, len(embeds), 5)]
            await interaction.response.send_message(embeds=batches[0])
            for batch in batches[1:]:
                await interaction.followup.send(embeds=batch)

        @item_group.command(name="notify_all", description="Add or remove a user from sale pings on ALL wishlist items at once")
        @app_commands.describe(user="Discord user to add or remove", action="Whether to add or remove this user (default: add)")
        async def item_notify_all(
            interaction: discord.Interaction,
            user: discord.User,
            action: Literal["add", "remove"] = "add",
        ) -> None:
            items = self.db.list_items()
            if not items:
                await interaction.response.send_message(
                    embed=make_status_embed("🔔 Notification List", "Your wishlist is currently empty.")
                )
                return

            uid = str(user.id)
            updated_count = 0
            for item in items:
                current = (
                    list(item.notify_user_ids)
                    if item.notify_user_ids
                    else list(dict.fromkeys([str(self.config.user_id), *self.config.extra_notify_user_ids]))
                )
                changed = False
                if action == "add" and uid not in current:
                    current.append(uid)
                    changed = True
                elif action == "remove" and uid in current:
                    current.remove(uid)
                    changed = True
                elif not item.notify_user_ids:
                    changed = True

                if changed:
                    updated = self.db.update_item(item.id, notify_user_ids=current)
                    if updated and updated.set_number:
                        upsert_wishlist_entry(
                            self.config.wishlist_path,
                            {
                                "name": updated.name,
                                "set_number": updated.set_number,
                                "notify_user_ids": list(updated.notify_user_ids),
                            },
                        )
                    updated_count += 1

            verb = "Added" if action == "add" else "Removed"
            prep = "to" if action == "add" else "from"
            embed = make_status_embed(
                f"🔔 Updated All Wishlist Pings",
                f"{verb} <@{uid}> {prep} sale alerts across **{len(items)}** wishlist set(s) ({updated_count} updated).",
                color=discord.Color.green() if action == "add" else discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed)

        @item_group.command(name="pings", description="Interactive menu to check/uncheck which sets a user is pinged on")
        @app_commands.describe(user="Optional Discord user (you can also pick from the dropdown)")
        async def item_pings(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
            view = PingManagerView(self, target_user=user)
            await interaction.response.send_message(embed=view.build_embed(), view=view)

        @item_group.command(name="check", description="Check UK prices right now for one set or all sets")
        @app_commands.describe(set_number="Optional specific LEGO set number to check")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_check(interaction: discord.Interaction, set_number: Optional[str] = None) -> None:
            if not set_number and not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed(
                        "Admin Only",
                        "Checking **all** sets at once is restricted to server admins. Please pick a specific `set_number` instead!",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                async def _send_one(embed: discord.Embed, view: Optional[discord.ui.View] = None) -> None:
                    if view is not None:
                        await interaction.followup.send(embed=embed, view=view)
                    else:
                        await interaction.followup.send(embed=embed)

                await asyncio.wait_for(
                    self.run_price_checks(set_number=set_number, on_item_embed=_send_one),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Timed Out", "The check took longer than 10m. Please try again.")
                )
            except Exception as exc:
                logger.exception("item check failed")
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Error", str(exc))
                )

        @item_group.command(name="history", description="View recent UK price history for a set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_history(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            records = self.db.get_price_history(item.id, limit=10)
            img = item.image_url or default_box_art_url(item.set_number)
            if not records:
                embed = make_status_embed(
                    f"📈 Price History: {format_item_title(item)}",
                    "No price history recorded yet. Run `/wishlist check` to record today's price!",
                    color=discord.Color.gold(),
                    image_url=img,
                )
                await interaction.response.send_message(embed=embed)
                return
            lowest = min(r.total_gbp for r in records)
            lines = [f"💷 **Normal RRP:** £{item.rrp_gbp:.2f}  •  🔥 **Recent Low:** £{lowest:.2f}\n"]
            for r in records:
                lines.append(f"• `{r.recorded_at[:10]}` — **£{r.total_gbp:.2f}** at {r.retailer}")
            embed = make_status_embed(
                f"📈 Price History: {format_item_title(item)}",
                "\n".join(lines),
                color=discord.Color.blue(),
                image_url=img,
            )
            await interaction.response.send_message(embed=embed)

        @item_group.command(name="chart", description="View a GBP price history chart for a tracked LEGO set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def item_chart(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            records = self.db.get_price_history(item.id, limit=30)
            if len(records) < 2:
                await interaction.response.send_message(
                    embed=make_status_embed(
                        f"📈 Price Chart: {format_item_title(item)}",
                        "Need at least **2** recorded price checks to draw a chart. Run `/wishlist check` to record today's price!",
                        color=discord.Color.gold(),
                        image_url=item.image_url or default_box_art_url(item.set_number),
                    )
                )
                return

            series = list(reversed([r.total_gbp for r in records]))
            rrp = item.rrp_gbp if item.rrp_gbp > 0 else max(series)
            chart_bytes = generate_price_chart(series, rrp)
            low = min(series)
            latest = series[-1]
            embed = make_status_embed(
                f"📈 Price Chart: {format_item_title(item)}",
                (
                    f"💷 **Normal RRP:** £{rrp:.2f}  •  "
                    f"🏷️ **Latest:** £{latest:.2f}  •  "
                    f"🔥 **Low:** £{low:.2f}  *(last {len(series)} checks)*"
                ),
                color=discord.Color.blue(),
                image_url="attachment://chart.png",
                footer=f"Set #{item.set_number or item.id} • Green = tracked price, Grey dashed = RRP",
            )
            file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
            await interaction.response.send_message(embed=embed, file=file)

        @bot_group.command(name="status", description="Show bot schedule, UK retailer engine, monthly token quota, and source health (Admin only)")
        async def bot_status(interaction: discord.Interaction) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            items = self.db.list_items()
            retiring_count = sum(1 for i in items if i.is_retiring)
            cached_count, cache_ttl = get_price_cache_stats()
            cooldowns = get_source_health()

            next_run = self.price_check_loop.next_iteration
            if next_run is not None:
                delta = next_run - datetime.now(timezone.utc)
                total_mins = max(0, int(delta.total_seconds() // 60))
                hours, mins = divmod(total_mins, 60)
                next_str = f"in **{hours}h {mins}m**" if hours else f"in **{mins}m**"
            else:
                next_str = "Not scheduled"

            if cooldowns:
                health_lines = "\n".join(
                    f"• ⚠️ `{src}`: cooldown ({secs // 60}m {secs % 60}s left)"
                    for src, secs in sorted(cooldowns.items())
                )
            else:
                health_lines = "• ✅ All direct UK stores healthy"

            engine_str = "Chrome TLS (`curl_cffi`)" if HAS_CURL_CFFI else "Standard HTTP (`aiohttp`)"
            if self.config.serpapi_key:
                used = self.db.get_monthly_usage("serpapi")
                _, today_cap = self.db.can_spend_token("serpapi", self.config.serpapi_monthly_limit, manual_force=False)
                quota_str = f"**{used} / {self.config.serpapi_monthly_limit}** used this month *(auto pace cap today: {today_cap})*"
            else:
                quota_str = "Not configured *(0-signup Direct UK Store mode)*"

            stores_str = ", ".join(r.display_name for r in APPROVED_UK_RETAILERS)
            dev_str = "ON (pings suppressed)" if self.dev_mode else "OFF"
            embed = make_status_embed(
                "🤖 LEGO Bot Status",
                (
                    f"⏱️ **Check Tick:** Every {self.config.check_interval_minutes} min (next {next_str} • ≥4h staleness queue)\n"
                    f"🧩 **Tracked Sets:** {len(items)} ({retiring_count} retiring soon)\n"
                    f"💾 **Price Cache:** {cached_count} set(s) cached ({cache_ttl // 60}m TTL)\n"
                    f"🛠️ **Dev Mode:** {dev_str}\n"
                    f"🌐 **Direct Engine:** {engine_str}\n"
                    f"🎟️ **Google Shopping Quota:** {quota_str}\n"
                    f"🛒 **Approved UK Stores ({len(APPROVED_UK_RETAILERS)}):** {stores_str}\n\n"
                    f"**Source Health:**\n{health_lines}"
                ),
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed)

        @bot_group.command(name="devmode", description="Toggle dev mode (suppresses user pings on alerts and summaries) (Admin only)")
        @app_commands.describe(enabled="Set True to enable or False to disable (leave blank to toggle)")
        async def bot_devmode(interaction: discord.Interaction, enabled: Optional[bool] = None) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            self.dev_mode = (not self.dev_mode) if enabled is None else enabled
            self.db.set_dev_mode(self.dev_mode)
            state = "ON (pings suppressed)" if self.dev_mode else "OFF (normal pings enabled)"
            color = discord.Color.orange() if self.dev_mode else discord.Color.green()
            embed = make_status_embed(
                f"🛠️ Dev Mode: {'ON' if self.dev_mode else 'OFF'}",
                f"Dev mode is now **{state}** *(saved across restarts)*.",
                color=color,
            )
            await interaction.response.send_message(embed=embed)

        @bot_group.command(name="clean_history", description="Purge bogus/outlier historical prices and reset poisoned alert states (Admin only)")
        async def bot_clean_history(interaction: discord.Interaction) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            deleted_rows, reset_items = self.db.purge_implausible_history()
            pruned_old = self.db.prune_old_history(retention_days=180)
            embed = make_status_embed(
                "🧹 Price History Cleaned",
                (
                    f"• **Removed outlier/unverified history rows:** {deleted_rows}\n"
                    f"• **Pruned rows older than 180 days:** {pruned_old}\n"
                    f"• **Reset poisoned alert states:** {reset_items}"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)

        @bot_group.command(name="clear_cache", description="Clear the 30-minute in-memory price cache (Admin only)")
        async def bot_clear_cache(interaction: discord.Interaction) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            cleared = clear_price_cache()
            embed = make_status_embed(
                "🧹 Price Cache Cleared",
                f"Removed **{cleared}** cached set(s) from the 30-minute memory cache.",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)

        @bot_group.command(name="clear_cooldowns", description="Reset all active store cooldowns immediately (Admin only)")
        async def bot_clear_cooldowns(interaction: discord.Interaction) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            cleared_stores = clear_cooldowns()
            msg = (
                f"Reset active cooldowns for: {', '.join(f'**{s}**' for s in cleared_stores)}."
                if cleared_stores
                else "No stores were in cooldown (all block streaks reset)."
            )
            await interaction.response.send_message(
                embed=make_status_embed("🔄 Store Cooldowns Cleared", msg, color=discord.Color.green())
            )

        @bot_group.command(name="update", description="Pull the latest code from GitHub, sync dependencies, and restart the bot (Admin only)")
        async def bot_update(interaction: discord.Interaction) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        "git", "pull", "--ff-only", "origin", "main",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    ),
                    timeout=20.0,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
                output = (out or b"").decode().strip() or (err or b"").decode().strip()
                if proc.returncode != 0:
                    await interaction.followup.send(
                        embed=make_error_embed("Git Pull Failed", f"```{output[:1500]}```"),
                        ephemeral=True,
                    )
                    return
                if "Already up to date." in output:
                    await interaction.followup.send(
                        embed=make_status_embed("Up to Date", "The bot is already running the latest code.", color=discord.Color.green()),
                        ephemeral=True,
                    )
                    return

                pip_note = ""
                try:
                    pip_proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, pip_err = await asyncio.wait_for(pip_proc.communicate(), timeout=45.0)
                    if pip_proc.returncode != 0:
                        err_txt = (pip_err or b"").decode()
                        if "externally-managed-environment" in err_txt:
                            pip_proc2 = await asyncio.create_subprocess_exec(
                                sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "-q", "-r", "requirements.txt",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await asyncio.wait_for(pip_proc2.communicate(), timeout=45.0)
                            pip_note = "\n✅ Synced Python packages (`--user`)." if pip_proc2.returncode == 0 else "\n⚠️ `pip install` warning (continuing restart)."
                        else:
                            pip_note = "\n⚠️ `pip install` warning (continuing restart)."
                    else:
                        pip_note = "\n✅ Synced Python packages (`requirements.txt`)."
                except Exception as pip_exc:
                    logger.warning("pip install during /bot update failed: %s", pip_exc)
                    pip_note = "\n⚠️ Skipped package sync (restarting with updated code)."

                await interaction.followup.send(
                    embed=make_status_embed(
                        "🔄 Updating & Restarting",
                        f"Pulled latest code from GitHub:\n```{output[:1000]}```{pip_note}",
                        color=discord.Color.green(),
                    ),
                    ephemeral=True,
                )
                await self.close()
                os._exit(0)
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Update Timed Out", "Update command took too long."),
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.followup.send(
                    embed=make_error_embed("Update Error", str(exc)),
                    ephemeral=True,
                )

        self.tree.add_command(wishlist_group)
        self.tree.add_command(item_group)
        self.tree.add_command(bot_group)


def main() -> None:
    config = Config.from_env()
    bot = LegoTrackerBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
