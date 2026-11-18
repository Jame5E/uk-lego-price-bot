from __future__ import annotations

import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .db import PriceDatabase, TrackedItem
from .predictor import PriceAnalysis, analyze_price
from .scraper import fetch_price_for_url
from .shipping import UKRetailer, detect_retailer_from_url, estimate_uk_shipping

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lego_bot")


def build_alert_embed(
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    shipping_note: str,
    *,
    simulated: bool = False,
) -> discord.Embed:
    if "BUY NOW" in analysis.verdict:
        color = discord.Color.green()
    elif "GOOD DEAL" in analysis.verdict or "BUY SOON" in analysis.verdict:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    title_prefix = "[TEST] " if simulated else ""
    set_tag = f" (#{item.set_number})" if item.set_number else ""
    embed = discord.Embed(
        title=f"{title_prefix}LEGO Deal Alert: {item.name}{set_tag}",
        url=item.url or discord.Embed.Empty,
        description=f"Spotted on sale at **{retailer.display_name}**!",
        color=color,
    )

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

    if item.url:
        embed.add_field(name="Link", value=f"[View on {retailer.display_name}]({item.url})", inline=False)

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

    async def run_price_checks(self, item_id: int | None = None) -> list[str]:
        assert self.http_session is not None
        items = [self.db.get_item(item_id)] if item_id is not None else self.db.list_items()
        items = [i for i in items if i is not None]

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
            if not item.url:
                summaries.append(f"#{item.id} {item.name}: skipped (no URL)")
                continue

            result = await fetch_price_for_url(
                self.http_session,
                item.url,
                keepa_api_key=self.config.keepa_api_key,
            )
            if not result.ok or result.quote is None:
                summaries.append(f"#{item.id} {item.name}: {result.error}")
                continue

            quote = result.quote
            shipping = estimate_uk_shipping(
                quote.price_gbp,
                quote.retailer,
                has_amazon_prime=self.config.has_amazon_prime,
                prefer_click_and_collect=self.config.prefer_click_and_collect,
            )

            history = self.db.get_price_history(item.id, limit=90)
            hist_totals = [h.total_gbp for h in history]
            if quote.historical_low_gbp is not None:
                hist_totals.append(quote.historical_low_gbp)
            if quote.historical_avg_gbp is not None:
                hist_totals.append(quote.historical_avg_gbp)

            self.db.record_price(
                item.id,
                quote.price_gbp,
                shipping.shipping_gbp,
                shipping.total_gbp,
                quote.retailer.value,
            )

            analysis = analyze_price(
                rrp_gbp=item.rrp_gbp,
                item_price_gbp=quote.price_gbp,
                shipping_gbp=shipping.shipping_gbp,
                historical_totals_gbp=hist_totals,
                is_retiring=item.is_retiring,
            )

            hit_target = (
                item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
            )
            hit_discount = analysis.discount_pct >= item.min_discount_pct

            summaries.append(
                f"#{item.id} {item.name}: £{analysis.total_gbp:.2f} "
                f"({analysis.discount_pct:.1f}% off) -> {analysis.verdict}"
            )

            if (hit_target or hit_discount) and self.db.should_alert(
                item, analysis.total_gbp, self.config.alert_cooldown_hours
            ):
                embed = build_alert_embed(item, analysis, quote.retailer, shipping.note)
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(
                        content=f"<@{self.config.user_id}> LEGO sale spotted!",
                        embed=embed,
                    )
                    self.db.mark_alerted(item.id, analysis.total_gbp)

        return summaries

    def _register_commands(self) -> None:
        @self.tree.command(name="track", description="Add a LEGO set or piece to your UK price watchlist")
        @app_commands.describe(
            name="Name of the LEGO set or piece",
            rrp_gbp="Normal UK retail price (RRP) in GBP (£)",
            url="UK retailer URL (Amazon UK, Argos, LEGO.com, Smyths, John Lewis)",
            set_number="Optional LEGO set or part number (e.g. 10316)",
            target_price_gbp="Alert when total cost (inc. shipping) is at or below this £ amount",
            min_discount_pct="Alert when total discount is at least this % off RRP (default 15%)",
            is_retiring="Set True if this LEGO set is retiring soon",
        )
        async def track_cmd(
            interaction: discord.Interaction,
            name: str,
            rrp_gbp: float,
            url: Optional[str] = None,
            set_number: Optional[str] = None,
            target_price_gbp: Optional[float] = None,
            min_discount_pct: float = 15.0,
            is_retiring: bool = False,
        ) -> None:
            retailer = detect_retailer_from_url(url)
            item = self.db.add_item(
                name=name,
                rrp_gbp=rrp_gbp,
                set_number=set_number,
                target_price_gbp=target_price_gbp,
                min_discount_pct=min_discount_pct,
                url=url,
                retailer=retailer.value,
                is_retiring=is_retiring,
            )
            target_str = f"£{item.target_price_gbp:.2f}" if item.target_price_gbp else "None"
            await interaction.response.send_message(
                f"Added **#{item.id} {item.name}** (RRP £{item.rrp_gbp:.2f}, "
                f"Retailer: {retailer.display_name}, Min Discount: {item.min_discount_pct:.0f}%, "
                f"Target: {target_str}).",
                ephemeral=True,
            )

        @self.tree.command(name="wishlist", description="Show all LEGO items you are tracking")
        async def wishlist_cmd(interaction: discord.Interaction) -> None:
            items = self.db.list_items()
            if not items:
                await interaction.response.send_message("Your LEGO wishlist is empty. Use `/track` to add one!", ephemeral=True)
                return

            lines = []
            for item in items:
                history = self.db.get_price_history(item.id, limit=1)
                latest = f"Latest: £{history[0].total_gbp:.2f}" if history else "No price checked yet"
                retiring = " [Retiring]" if item.is_retiring else ""
                lines.append(
                    f"**#{item.id}** {item.name}{retiring} — RRP £{item.rrp_gbp:.2f} | "
                    f"Alert ≥{item.min_discount_pct:.0f}% off | {latest}"
                )
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="check", description="Check UK prices right now")
        @app_commands.describe(item_id="Optional specific wishlist ID to check")
        async def check_cmd(
            interaction: discord.Interaction,
            item_id: Optional[int] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            results = await self.run_price_checks(item_id=item_id)
            await interaction.followup.send("\n".join(results), ephemeral=True)

        @self.tree.command(name="history", description="View recent UK price history for a tracked item")
        @app_commands.describe(item_id="Wishlist ID from /wishlist")
        async def history_cmd(interaction: discord.Interaction, item_id: int) -> None:
            item = self.db.get_item(item_id)
            if not item:
                await interaction.response.send_message(f"Item #{item_id} not found.", ephemeral=True)
                return
            records = self.db.get_price_history(item_id, limit=10)
            if not records:
                await interaction.response.send_message(f"No price history recorded yet for **#{item.id} {item.name}**.", ephemeral=True)
                return
            lowest = min(r.total_gbp for r in records)
            lines = [f"**#{item.id} {item.name}** (RRP £{item.rrp_gbp:.2f} | Recent Low £{lowest:.2f}):"]
            for r in records:
                lines.append(f"- {r.recorded_at[:10]}: £{r.total_gbp:.2f} (£{r.price_gbp:.2f} + £{r.shipping_gbp:.2f} ship)")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="remove", description="Remove a LEGO item from your wishlist")
        @app_commands.describe(item_id="Wishlist ID from /wishlist")
        async def remove_cmd(interaction: discord.Interaction, item_id: int) -> None:
            removed = self.db.remove_item(item_id)
            msg = f"Removed item #{item_id}." if removed else f"Item #{item_id} not found."
            await interaction.response.send_message(msg, ephemeral=True)

        @self.tree.command(name="simulate_sale", description="Send a test deal alert ping in your alert channel")
        @app_commands.describe(
            item_id="Wishlist ID from /wishlist",
            sale_price_gbp="Simulated UK item price in £ (before shipping)",
        )
        async def simulate_sale_cmd(
            interaction: discord.Interaction,
            item_id: int,
            sale_price_gbp: float,
        ) -> None:
            item = self.db.get_item(item_id)
            if not item:
                await interaction.response.send_message(f"Item #{item_id} not found.", ephemeral=True)
                return

            retailer = detect_retailer_from_url(item.url)
            if retailer == UKRetailer.OTHER:
                try:
                    retailer = UKRetailer(item.retailer)
                except ValueError:
                    retailer = UKRetailer.ARGOS

            shipping = estimate_uk_shipping(
                sale_price_gbp,
                retailer,
                has_amazon_prime=self.config.has_amazon_prime,
                prefer_click_and_collect=self.config.prefer_click_and_collect,
            )
            hist = [h.total_gbp for h in self.db.get_price_history(item.id, limit=90)]
            analysis = analyze_price(
                rrp_gbp=item.rrp_gbp,
                item_price_gbp=sale_price_gbp,
                shipping_gbp=shipping.shipping_gbp,
                historical_totals_gbp=hist,
                is_retiring=item.is_retiring,
            )
            embed = build_alert_embed(item, analysis, retailer, shipping.note, simulated=True)

            channel = self.get_channel(self.config.channel_id) or await self.fetch_channel(self.config.channel_id)
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    content=f"<@{self.config.user_id}> LEGO sale spotted!",
                    embed=embed,
                )
                await interaction.response.send_message("Sent test alert to your alert channel!", ephemeral=True)
            else:
                await interaction.response.send_message("Alert channel is not messageable.", ephemeral=True)


def main() -> None:
    config = Config.from_env()
    bot = LegoTrackerBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
