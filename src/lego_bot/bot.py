from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .chart import generate_price_chart
from .config import Config
from .db import PriceDatabase, TrackedItem
from .predictor import PriceAnalysis, analyze_price
from .shipping import UKRetailer, detect_retailer_from_url, estimate_uk_shipping
from .uk_finder import default_box_art_url, find_best_uk_offer, lookup_set_metadata
from .wishlist_store import remove_wishlist_entry, upsert_wishlist_entry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lego_bot")

COMMAND_TIMEOUT_SECONDS = 45.0


def format_pings(
    default_user_id: int,
    item: TrackedItem,
    extra_user_ids: tuple[str, ...] = ("942902769319292948",),
) -> str:
    ids = list(item.notify_user_ids) if item.notify_user_ids else [str(default_user_id), *extra_user_ids]
    return " ".join(f"<@{uid}>" for uid in dict.fromkeys(ids))


def format_item_title(item: TrackedItem) -> str:
    if item.set_number and item.set_number not in item.name:
        return f"{item.name} (#{item.set_number})"
    return item.name


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
        url=link_url or discord.Embed.Empty,
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


class LegoTrackerBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.db = PriceDatabase(config.db_path)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
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

    async def close(self) -> None:
        if self.price_check_loop.is_running():
            self.price_check_loop.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "?")

    @tasks.loop(minutes=30)
    async def price_check_loop(self) -> None:
        try:
            await asyncio.wait_for(self.run_price_checks(), timeout=120.0)
        except Exception as exc:
            logger.error("Scheduled price check failed: %s", exc)

    @price_check_loop.before_loop
    async def before_price_check_loop(self) -> None:
        await self.wait_until_ready()

    async def run_price_checks(self, set_number: str | None = None) -> list[discord.Embed]:
        assert self.http_session is not None
        if set_number:
            found = self.db.get_item_by_set_number(set_number)
            items = [found] if found else []
        else:
            items = self.db.list_items()

        if not items:
            return [make_status_embed("🧩 Your LEGO Wishlist", "No tracked LEGO items found.")]

        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.channel_id)
            except Exception as exc:
                logger.error("Could not fetch alert channel %s: %s", self.config.channel_id, exc)
                return [make_error_embed("Channel Error", f"Could not access alert channel {self.config.channel_id}.")]

        result_embeds: list[discord.Embed] = []
        for item in items:
            rrp = item.rrp_gbp
            is_retiring = item.is_retiring
            image_url = item.image_url
            resolved_name = item.name
            category = item.category

            needs_meta = (
                rrp <= 0
                or not image_url
                or not category
                or resolved_name.startswith("LEGO Set #")
                or ":" not in resolved_name
                or is_retiring
            )
            if needs_meta and item.set_number:
                meta = await lookup_set_metadata(
                    self.http_session, item.set_number, brickset_api_key=self.config.brickset_api_key
                )
                if meta.name and (resolved_name.startswith("LEGO Set #") or len(meta.name) > len(resolved_name)):
                    resolved_name = meta.name
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

            best_offer, _notes = await find_best_uk_offer(
                self.http_session,
                name=item.name,
                set_number=item.set_number,
                direct_url=item.url,
                has_amazon_prime=self.config.has_amazon_prime,
                prefer_click_and_collect=self.config.prefer_click_and_collect,
                keepa_api_key=self.config.keepa_api_key,
            )
            title = format_item_title(item)
            box_art = item.image_url or default_box_art_url(item.set_number)
            cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""

            if best_offer is None:
                rrp_line = f"💷 **Normal RRP:** £{rrp:.2f}\n" if rrp > 0 else ""
                result_embeds.append(
                    make_status_embed(
                        f"🧩 {title}",
                        f"{cat_line}{rrp_line}⚠️ No live UK offers found right now *(retailers blocked automated check)*",
                        color=discord.Color.orange(),
                        image_url=box_art,
                        footer="Prices in GBP (£) including estimated UK delivery",
                    )
                )
                continue

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
            result_embeds.append(
                make_status_embed(
                    f"🧩 {title}",
                    (
                        f"{cat_line}"
                        f"## 🏷️ Deal Price: £{analysis.total_gbp:.2f}\n"
                        f"### Normal RRP: £{analysis.rrp_gbp:.2f}  •  **{analysis.discount_pct:.1f}% OFF**\n"
                        f"📍 **Seller:** {best_offer.retailer.display_name}\n"
                        f"{icon} **{analysis.verdict}** — [View Deal →]({best_offer.url})"
                    ),
                    color=color,
                    image_url=box_art,
                    footer="Prices in GBP (£) including estimated UK delivery",
                )
            )

            hit_target = item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
            hit_discount = analysis.discount_pct >= item.min_discount_pct
            if (hit_target or hit_discount) and self.db.should_alert(
                item, analysis.total_gbp, self.config.alert_cooldown_hours
            ):
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
                    pings = format_pings(
                        self.config.user_id,
                        item,
                        extra_user_ids=self.config.extra_notify_user_ids,
                    )
                    if chart_bytes:
                        file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
                        await channel.send(content=f"{pings} LEGO sale spotted!", embeds=embeds, file=file)
                    else:
                        await channel.send(content=f"{pings} LEGO sale spotted!", embeds=embeds)
                    self.db.mark_alerted(item.id, analysis.total_gbp)

        if not set_number and self.db.should_send_weekly_summary() and isinstance(channel, discord.abc.Messageable):
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
            await channel.send(content=f"<@{self.config.user_id}> Weekly LEGO summary!", embed=summary_embed)
            self.db.mark_weekly_summary_sent()

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
                    lookup_set_metadata(self.http_session, clean_num, brickset_api_key=self.config.brickset_api_key),
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
            for item in items[:10]:
                hist = self.db.get_price_history(item.id, limit=1)
                latest_line = f"🏷️ **Latest Price:** £{hist[0].total_gbp:.2f}\n" if hist else "🏷️ **Latest Price:** *Not checked yet*\n"
                rrp_str = f"£{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "Auto-detect"
                cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""
                pings = format_pings(self.config.user_id, item)
                box_art = item.image_url or default_box_art_url(item.set_number)
                item_embeds.append(
                    make_status_embed(
                        f"🧩 {format_item_title(item)}",
                        (
                            f"{cat_line}"
                            f"💷 **Normal RRP:** {rrp_str}\n"
                            f"{latest_line}"
                            f"🔔 **Notify:** {pings}"
                        ),
                        color=discord.Color.blue(),
                        image_url=box_art,
                        footer=f"Set #{item.set_number or item.id} • Run /wishlist check for live prices",
                    )
                )

            await interaction.response.send_message(embeds=item_embeds)

        @wishlist_group.command(name="check", description="Check the current UK price of all items on your wishlist right now")
        async def wishlist_check(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            try:
                embeds = await asyncio.wait_for(
                    self.run_price_checks(set_number=None),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
                await interaction.followup.send(embeds=embeds[:10])
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Timed Out", "UK retailers took too long to respond (>45s). Please try again.")
                )
            except Exception as exc:
                logger.exception("wishlist check failed")
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Error", str(exc))
                )

        @wishlist_group.command(name="edit", description="Edit alert settings for a tracked LEGO set")
        @app_commands.describe(
            set_number="LEGO set number (e.g. 10316)",
            target_price_gbp="New target price in £",
            min_discount_pct="New minimum discount %",
            is_retiring="Mark as retiring soon (True/False)",
        )
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

        @item_group.command(name="check", description="Check UK prices right now for one set or all sets")
        @app_commands.describe(set_number="Optional specific LEGO set number to check")
        async def item_check(interaction: discord.Interaction, set_number: Optional[str] = None) -> None:
            await interaction.response.defer()
            try:
                embeds = await asyncio.wait_for(
                    self.run_price_checks(set_number=set_number),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
                await interaction.followup.send(embeds=embeds[:10])
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Timed Out", "The check took longer than 45s. Please try again.")
                )
            except Exception as exc:
                logger.exception("item check failed")
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Error", str(exc))
                )

        @item_group.command(name="history", description="View recent UK price history for a set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
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

        @item_group.command(name="simulate_sale", description="Send a test deal alert with chart & full-width box art")
        @app_commands.describe(set_number="LEGO set number", sale_price_gbp="Simulated UK item price in £")
        async def item_simulate(interaction: discord.Interaction, set_number: str, sale_price_gbp: float) -> None:
            await interaction.response.defer()
            try:
                item = self.db.get_item_by_set_number(set_number)
                if not item:
                    await interaction.followup.send(
                        embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                        ephemeral=True,
                    )
                    return
                rrp = item.rrp_gbp if item.rrp_gbp > 0 else round(sale_price_gbp * 1.3, 2)
                retailer = detect_retailer_from_url(item.url) if item.url else UKRetailer.ARGOS
                shipping = estimate_uk_shipping(
                    sale_price_gbp,
                    retailer,
                    has_amazon_prime=self.config.has_amazon_prime,
                    prefer_click_and_collect=self.config.prefer_click_and_collect,
                )
                series = [rrp, round(rrp * 0.9, 2), round(rrp * 0.82, 2), shipping.total_gbp]
                chart_bytes = generate_price_chart(series, rrp)
                analysis = analyze_price(
                    rrp_gbp=rrp,
                    item_price_gbp=sale_price_gbp,
                    shipping_gbp=shipping.shipping_gbp,
                    historical_totals_gbp=series[:-1],
                    is_retiring=item.is_retiring,
                )
                embeds = build_alert_embeds(
                    item,
                    analysis,
                    retailer,
                    shipping.note,
                    is_all_time_low=True,
                    has_chart=True,
                    simulated=True,
                )
                channel = self.get_channel(self.config.channel_id) or await self.fetch_channel(self.config.channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    pings = format_pings(self.config.user_id, item)
                    file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
                    await channel.send(content=f"{pings} LEGO sale spotted!", embeds=embeds, file=file)
                    await interaction.followup.send(
                        embed=make_status_embed(
                            "✅ Test Alert Sent",
                            f"Posted simulated deal alert for **{format_item_title(item)}** in <#{self.config.channel_id}>.",
                            color=discord.Color.green(),
                            image_url=item.image_url or default_box_art_url(item.set_number),
                        ),
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        embed=make_error_embed("Channel Error", "Alert channel is not messageable."),
                        ephemeral=True,
                    )
            except Exception as exc:
                logger.exception("simulate_sale failed")
                await interaction.followup.send(
                    embed=make_error_embed("Simulation Failed", str(exc)),
                    ephemeral=True,
                )

        @bot_group.command(name="update", description="Pull the latest code from GitHub and restart the bot")
        async def bot_update(interaction: discord.Interaction) -> None:
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
                await interaction.followup.send(
                    embed=make_status_embed("🔄 Updating & Restarting", f"Pulled latest code from GitHub:\n```{output[:1000]}```", color=discord.Color.green()),
                    ephemeral=True,
                )
                await self.close()
                os._exit(0)
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Update Timed Out", "Git pull took too long (>20s)."),
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
