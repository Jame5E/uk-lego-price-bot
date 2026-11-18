from __future__ import annotations

import io
import logging
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


def format_pings(default_user_id: int, item: TrackedItem) -> str:
    ids = list(item.notify_user_ids) if item.notify_user_ids else [str(default_user_id)]
    return " ".join(f"<@{uid}>" for uid in dict.fromkeys(ids))


def build_alert_embed(
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    shipping_note: str,
    offer_url: str | None = None,
    *,
    is_all_time_low: bool = False,
    has_chart: bool = False,
    simulated: bool = False,
) -> discord.Embed:
    if "BUY NOW" in analysis.verdict:
        color = discord.Color.green()
    elif "GOOD DEAL" in analysis.verdict or "BUY SOON" in analysis.verdict:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    title_prefix = "[TEST] " if simulated else ""
    atl_badge = "🔥 NEW ALL-TIME LOW! " if is_all_time_low else ""
    set_tag = f" (#{item.set_number})" if item.set_number else ""
    link_url = offer_url or item.url

    embed = discord.Embed(
        title=f"{title_prefix}{atl_badge}LEGO Deal Alert: {item.name}{set_tag}",
        url=link_url or discord.Embed.Empty,
        description=f"Best in-stock UK price spotted at **{retailer.display_name}**!",
        color=color,
    )

    thumb = item.image_url or default_box_art_url(item.set_number)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.add_field(
        name="Price Breakdown (GBP)",
        value=(
            f"**Item:** £{analysis.item_price_gbp:.2f}\n"
            f"**UK Shipping:** £{analysis.shipping_gbp:.2f} *({shipping_note})*\n"
            f"**Total Cost:** **£{analysis.total_gbp:.2f}** (RRP £{analysis.rrp_gbp:.2f})"
        ),
        inline=True,
    )

    embed.add_field(
        name="Reduction",
        value=(
            f"**Saved:** £{analysis.savings_gbp:.2f}\n"
            f"**Total Discount:** {analysis.discount_pct:.1f}% off\n"
            f"**Item-only Discount:** {analysis.item_discount_pct:.1f}% off"
        ),
        inline=True,
    )

    history_lines = []
    if is_all_time_low:
        history_lines.append("🔥 **NEW ALL-TIME LOW** in your tracked history!")
    if analysis.historical_low_gbp is not None:
        history_lines.append(f"**Tracked Low:** £{analysis.historical_low_gbp:.2f}")
    if analysis.historical_avg_gbp is not None:
        history_lines.append(f"**Tracked Avg:** £{analysis.historical_avg_gbp:.2f}")
    if item.is_retiring:
        history_lines.append("**Status:** Retiring Soon")
    if history_lines:
        embed.add_field(name="Price Context", value="\n".join(history_lines), inline=False)

    embed.add_field(
        name=f"Verdict: {analysis.verdict}",
        value=analysis.forecast,
        inline=False,
    )

    if link_url:
        embed.add_field(name="Link", value=f"[View on {retailer.display_name}]({link_url})", inline=False)
    if has_chart:
        embed.set_image(url="attachment://chart.png")

    embed.set_footer(text=f"Wishlist ID #{item.id} • Prices in GBP (£) including UK shipping")
    return embed


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
        await self.run_price_checks()

    @price_check_loop.before_loop
    async def before_price_check_loop(self) -> None:
        await self.wait_until_ready()

    async def run_price_checks(self, set_number: str | None = None) -> list[str]:
        assert self.http_session is not None
        if set_number:
            found = self.db.get_item_by_set_number(set_number)
            items = [found] if found else []
        else:
            items = self.db.list_items()

        if not items:
            return ["No tracked LEGO items found."]

        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.channel_id)
            except Exception as exc:
                logger.error("Could not fetch alert channel %s: %s", self.config.channel_id, exc)
                return [f"Error: Could not access alert channel {self.config.channel_id}."]

        summaries: list[str] = []
        for item in items:
            rrp = item.rrp_gbp
            is_retiring = item.is_retiring
            image_url = item.image_url

            if (rrp <= 0 or not is_retiring or not image_url) and item.set_number:
                meta = await lookup_set_metadata(
                    self.http_session, item.set_number, brickset_api_key=self.config.brickset_api_key
                )
                if rrp <= 0 and meta.rrp_gbp:
                    rrp = meta.rrp_gbp
                if meta.is_retiring:
                    is_retiring = True
                if not image_url and meta.image_url:
                    image_url = meta.image_url
                self.db.update_item(
                    item.id,
                    rrp_gbp=rrp if rrp > 0 else None,
                    is_retiring=is_retiring,
                    image_url=image_url,
                )
                item = self.db.get_item(item.id) or item

            best_offer, notes = await find_best_uk_offer(
                self.http_session,
                name=item.name,
                set_number=item.set_number,
                direct_url=item.url,
                has_amazon_prime=self.config.has_amazon_prime,
                prefer_click_and_collect=self.config.prefer_click_and_collect,
                keepa_api_key=self.config.keepa_api_key,
            )
            if best_offer is None:
                summaries.append(f"#{item.set_number or item.id} {item.name}: no in-stock UK offers ({'; '.join(notes)})")
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

            summaries.append(
                f"#{item.set_number or item.id} {item.name}: £{analysis.total_gbp:.2f} at "
                f"{best_offer.retailer.display_name} ({analysis.discount_pct:.1f}% off) -> {analysis.verdict}"
            )

            hit_target = item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
            hit_discount = analysis.discount_pct >= item.min_discount_pct
            if (hit_target or hit_discount) and self.db.should_alert(
                item, analysis.total_gbp, self.config.alert_cooldown_hours
            ):
                series = list(reversed([h.total_gbp for h in self.db.get_price_history(item.id, limit=30)]))
                chart_bytes = generate_price_chart(series, rrp) if len(series) >= 2 else None
                embed = build_alert_embed(
                    item,
                    analysis,
                    best_offer.retailer,
                    best_offer.shipping_note,
                    offer_url=best_offer.url,
                    is_all_time_low=is_all_time_low,
                    has_chart=chart_bytes is not None,
                )
                if isinstance(channel, discord.abc.Messageable):
                    pings = format_pings(self.config.user_id, item)
                    if chart_bytes:
                        file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
                        await channel.send(content=f"{pings} LEGO sale spotted!", embed=embed, file=file)
                    else:
                        await channel.send(content=f"{pings} LEGO sale spotted!", embed=embed)
                    self.db.mark_alerted(item.id, analysis.total_gbp)

        if not set_number and self.db.should_send_weekly_summary() and isinstance(channel, discord.abc.Messageable):
            lines = []
            for item in self.db.list_items():
                hist = self.db.get_price_history(item.id, limit=1)
                low = self.db.get_historical_low(item.id)
                if hist:
                    low_str = f" | Low £{low:.2f}" if low is not None else ""
                    lines.append(f"• **{item.name}** (#{item.set_number or item.id}): **£{hist[0].total_gbp:.2f}**{low_str}")
                else:
                    lines.append(f"• **{item.name}** (#{item.set_number or item.id}): *No price recorded yet*")
            summary_embed = discord.Embed(
                title="Weekly UK LEGO Wishlist Summary",
                description="\n".join(lines) if lines else "Your wishlist is empty.",
                color=discord.Color.blue(),
            )
            await channel.send(content=f"<@{self.config.user_id}> Weekly LEGO summary!", embed=summary_embed)
            self.db.mark_weekly_summary_sent()

        return summaries

    def _register_commands(self) -> None:
        wishlist_group = app_commands.Group(name="wishlist", description="Manage your UK LEGO wishlist")
        item_group = app_commands.Group(name="item", description="Item actions and notifications")

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
            await interaction.response.defer(ephemeral=True)
            assert self.http_session is not None
            clean_num = set_number.strip().split("-")[0]
            meta = await lookup_set_metadata(
                self.http_session, clean_num, brickset_api_key=self.config.brickset_api_key
            )
            resolved_name = name or meta.name or f"LEGO Set #{clean_num}"
            rrp = meta.rrp_gbp or 0.0
            notify_ids = [str(interaction.user.id)]

            existing = self.db.get_item_by_set_number(clean_num)
            if existing:
                await interaction.followup.send(
                    f"Set **#{clean_num} {existing.name}** is already on your wishlist!", ephemeral=True
                )
                return

            item = self.db.add_item(
                name=resolved_name,
                rrp_gbp=rrp,
                set_number=clean_num,
                target_price_gbp=target_price_gbp,
                min_discount_pct=min_discount_pct,
                is_retiring=meta.is_retiring,
                image_url=meta.image_url,
                notify_user_ids=notify_ids,
            )
            upsert_wishlist_entry(
                self.config.wishlist_path,
                {
                    "name": item.name,
                    "set_number": clean_num,
                    "rrp_gbp": rrp if rrp > 0 else None,
                    "target_price_gbp": target_price_gbp,
                    "min_discount_pct": min_discount_pct,
                    "is_retiring": meta.is_retiring,
                    "image_url": meta.image_url,
                    "notify_user_ids": notify_ids,
                },
            )
            rrp_msg = f"£{rrp:.2f}" if rrp > 0 else "Auto-detect on next check"
            retire_msg = "Yes" if meta.is_retiring else "No"
            await interaction.followup.send(
                f"Added **#{clean_num} {item.name}** to `wishlist.json`!\n"
                f"RRP: **{rrp_msg}** | Retiring Soon: **{retire_msg}** | Notify: <@{interaction.user.id}>",
                ephemeral=True,
            )

        @wishlist_group.command(name="list", description="List all LEGO sets on your wishlist")
        async def wishlist_list(interaction: discord.Interaction) -> None:
            items = self.db.list_items()
            if not items:
                await interaction.response.send_message(
                    "Your wishlist is empty. Use `/wishlist add <set_number>`!", ephemeral=True
                )
                return
            lines = []
            for item in items:
                hist = self.db.get_price_history(item.id, limit=1)
                latest = f"Latest: £{hist[0].total_gbp:.2f}" if hist else "Not checked yet"
                rrp_str = f"£{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "Auto"
                pings = format_pings(self.config.user_id, item)
                lines.append(
                    f"**#{item.set_number or item.id}** {item.name} — RRP {rrp_str} | "
                    f"Alert ≥{item.min_discount_pct:.0f}% off | {latest} | Pings: {pings}"
                )
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
                await interaction.response.send_message(f"Set #{set_number} not found.", ephemeral=True)
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
            await interaction.response.send_message(
                f"Updated **#{updated.set_number} {updated.name}** "
                f"(Min discount: {updated.min_discount_pct:.0f}%, Target: "
                f"{f'£{updated.target_price_gbp:.2f}' if updated.target_price_gbp else 'None'}, "
                f"Retiring: {updated.is_retiring}).",
                ephemeral=True,
            )

        @wishlist_group.command(name="remove", description="Remove a LEGO set from your wishlist")
        @app_commands.describe(set_number="LEGO set number to remove (e.g. 10316)")
        async def wishlist_remove(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(f"Set #{set_number} not found.", ephemeral=True)
                return
            self.db.remove_item(item.id)
            remove_wishlist_entry(self.config.wishlist_path, set_number)
            await interaction.response.send_message(f"Removed **#{set_number} {item.name}** from your wishlist.", ephemeral=True)

        @item_group.command(name="notify", description="Toggle who gets pinged when a LEGO set goes on sale")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)", user="Discord user to add/remove from pings")
        async def item_notify(interaction: discord.Interaction, set_number: str, user: discord.User) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(f"Set #{set_number} not found.", ephemeral=True)
                return
            uid = str(user.id)
            current = list(item.notify_user_ids) if item.notify_user_ids else [str(self.config.user_id)]
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
            await interaction.response.send_message(
                f"{action} notifications for **#{set_number} {item.name}**. "
                f"Current pings: {format_pings(self.config.user_id, updated)}",
                ephemeral=True,
            )

        @item_group.command(name="check", description="Check UK prices right now")
        @app_commands.describe(set_number="Optional specific LEGO set number to check")
        async def item_check(interaction: discord.Interaction, set_number: Optional[str] = None) -> None:
            await interaction.response.defer(ephemeral=True)
            results = await self.run_price_checks(set_number=set_number)
            await interaction.followup.send("\n".join(results), ephemeral=True)

        @item_group.command(name="history", description="View recent UK price history for a set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
        async def item_history(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(f"Set #{set_number} not found.", ephemeral=True)
                return
            records = self.db.get_price_history(item.id, limit=10)
            if not records:
                await interaction.response.send_message(f"No price history recorded yet for **#{set_number} {item.name}**.", ephemeral=True)
                return
            lowest = min(r.total_gbp for r in records)
            lines = [f"**#{set_number} {item.name}** (RRP £{item.rrp_gbp:.2f} | Recent Low £{lowest:.2f}):"]
            for r in records:
                lines.append(f"- {r.recorded_at[:10]}: £{r.total_gbp:.2f} at {r.retailer}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @item_group.command(name="simulate_sale", description="Send a test deal alert with chart & box art in your alert channel")
        @app_commands.describe(set_number="LEGO set number", sale_price_gbp="Simulated UK item price in £")
        async def item_simulate(interaction: discord.Interaction, set_number: str, sale_price_gbp: float) -> None:
            await interaction.response.defer(ephemeral=True)
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.followup.send(f"Set #{set_number} not found.", ephemeral=True)
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
            embed = build_alert_embed(
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
                await channel.send(content=f"{pings} LEGO sale spotted!", embed=embed, file=file)
                await interaction.followup.send("Sent test alert with chart!", ephemeral=True)
            else:
                await interaction.followup.send("Alert channel is not messageable.", ephemeral=True)

        self.tree.add_command(wishlist_group)
        self.tree.add_command(item_group)


def main() -> None:
    config = Config.from_env()
    bot = LegoTrackerBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
