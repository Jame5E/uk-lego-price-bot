from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .chart import generate_price_chart
from .config import Config
from .db import PriceDatabase, TrackedItem
from .embeds import (
    build_alert_embeds,
    build_price_card,
    effective_notify_user_ids,
    format_discount_badge,
    format_discount_short,
    format_item_title,
    format_pings,
    make_error_embed,
    make_status_embed,
)
from .predictor import PriceAnalysis, analyze_price
from .scheduler import build_weekly_summary_embed, format_pinned_dashboard, is_quiet_hours
from .shipping import APPROVED_UK_RETAILERS, UKRetailer, detect_retailer_from_url
from .uk_finder import (
    HAS_CURL_CFFI,
    UKOffer,
    build_uk_search_urls,
    clean_set_display_name,
    clear_cooldowns,
    clear_price_cache,
    default_box_art_url,
    export_cooldown_state,
    find_best_uk_offer,
    get_price_cache_stats,
    get_source_health,
    import_cooldown_state,
    load_amazon_dp_urls,
    lookup_set_metadata,
    pop_finished_cooldowns,
    pop_long_outage_sources,
)
from .views import (
    CheckResultView,
    DealLinkView,
    PingManagerView,
    RetiredSetView,
    SetDetailView,
    WishlistView,
)
from .wishlist_store import load_wishlist_entries, remove_wishlist_entry, upsert_wishlist_entry

__all__ = [
    "LegoTrackerBot",
    "RetiredSetView",
    "WishlistView",
    "SetDetailView",
    "CheckResultView",
    "DealLinkView",
    "PingManagerView",
    "build_alert_embeds",
    "format_discount_badge",
    "format_discount_short",
    "is_admin_user",
    "main",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lego_bot")

COMMAND_TIMEOUT_SECONDS = 300.0


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
        import_cooldown_state(self.db.get_state_json("source_cooldowns", {}))
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

    def _save_cooldowns(self) -> None:
        """Persist store cooldowns to SQLite so they survive /bot update restarts."""
        try:
            self.db.set_state_json("source_cooldowns", export_cooldown_state())
        except Exception as exc:
            logger.warning("Could not save store cooldowns: %s", exc)

    def _format_status_text(self, text: str) -> str:
        return format_pinned_dashboard(self.db, self.config, text)

    async def _get_status_message(self, channel: discord.abc.Messageable) -> Optional[discord.Message]:
        """Return the single pinned status message, creating (and pinning) it if it doesn't exist yet."""
        saved = self.db.get_state_json("status_message", {}) or {}
        msg_id = saved.get("message_id") if isinstance(saved, dict) else None
        channel_id = getattr(channel, "id", None)
        if msg_id and saved.get("channel_id") == channel_id:
            try:
                return await channel.fetch_message(int(msg_id))
            except Exception:
                pass
        try:
            msg = await channel.send(
                content=self._format_status_text("⏳ Waiting for the first scheduled check..."),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            logger.warning("Could not create status message: %s", exc)
            return None
        try:
            await msg.pin(reason="LEGO bot scheduled check status")
        except Exception as exc:
            logger.info("Could not pin status message (grant 'Manage Messages' to enable): %s", exc)
        self.db.set_state_json("status_message", {"message_id": msg.id, "channel_id": channel_id})
        return msg

    async def close(self) -> None:
        if self.price_check_loop.is_running():
            self.price_check_loop.cancel()
        if self.cooldown_watcher_loop.is_running():
            self.cooldown_watcher_loop.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        from .uk_finder import close_http_sessions

        await close_http_sessions()
        self._save_cooldowns()
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
            status_msg = await self._get_status_message(channel)
            if status_msg is not None:
                stores = ", ".join(f"**{s}**" for s in finished)
                try:
                    await status_msg.edit(
                        content=self._format_status_text(f"✅ Cooldown finished for {stores} — checks resumed."),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass

    @cooldown_watcher_loop.before_loop
    async def before_cooldown_watcher_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=30)
    async def price_check_loop(self) -> None:
        try:
            await asyncio.sleep(random.uniform(5.0, 90.0))
            await asyncio.wait_for(self.run_price_checks(scheduled=True), timeout=600.0)
            outages = pop_long_outage_sources()
            if outages:
                logger.warning("Long outage (>2h) detected for sources: %s", ", ".join(outages))
        except Exception as exc:
            logger.error("Scheduled price check failed: %s", exc)

    @price_check_loop.before_loop
    async def before_price_check_loop(self) -> None:
        await self.wait_until_ready()

    async def _maybe_send_weekly_summary(self, channel: object) -> None:
        if not self.db.should_send_weekly_summary() or not isinstance(channel, discord.abc.Messageable):
            return
        self.db.prune_old_history(retention_days=180)
        items = self.db.list_items()
        summary_embed = build_weekly_summary_embed(self.db, items)
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

    async def _refresh_item_metadata(
        self,
        item: TrackedItem,
        channel: object,
        now_mono: float,
    ) -> tuple[TrackedItem | None, bool]:
        """Refresh Brickset metadata when due; archive retired sets instead of deleting."""
        rrp = item.rrp_gbp
        is_retiring = item.is_retiring
        image_url = item.image_url
        resolved_name = clean_set_display_name(item.name, item.set_number) or item.name
        category = item.category

        meta_ttl = 86400.0 if is_retiring else 604800.0
        meta_refresh_due = now_mono - self._last_retiring_meta_check.get(item.id, 0.0) >= meta_ttl
        needs_meta = (
            rrp <= 0
            or not image_url
            or not category
            or not resolved_name
            or resolved_name.startswith("LEGO Set #")
            or meta_refresh_due
        )
        if not (needs_meta and item.set_number):
            return item, False

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
            self.db.archive_item(item.id)
            if item.set_number:
                remove_wishlist_entry(self.config.wishlist_path, item.set_number)
            retired_embed = make_status_embed(
                f"📦 Archived Retired Set: {title}",
                "This LEGO set has now **retired** and was archived from active checks *(price history preserved)*.",
                color=discord.Color.dark_grey(),
                thumbnail_url=box_art,
            )
            if isinstance(channel, discord.abc.Messageable):
                if self.dev_mode:
                    retired_content = f"🛠️ [DEV MODE] **{title}** has retired and was archived. *(pings suppressed)*"
                    allowed = discord.AllowedMentions.none()
                else:
                    pings = format_pings(
                        self.config.user_id,
                        item,
                        extra_user_ids=self.config.extra_notify_user_ids,
                    )
                    retired_content = f"{pings} **{title}** has **retired** and was archived."
                    allowed = discord.AllowedMentions.all()
                await channel.send(
                    content=retired_content,
                    embed=retired_embed,
                    view=RetiredSetView(self, item),
                    allowed_mentions=allowed,
                )
            return None, True

        new_rrp = meta.rrp_gbp if (meta.rrp_gbp and meta.rrp_gbp > 0) else rrp
        new_name = meta.name or resolved_name
        new_img = meta.image_url or image_url or default_box_art_url(item.set_number)
        new_cat = meta.category or category
        new_retiring = meta.is_retiring or is_retiring
        if (
            new_rrp != item.rrp_gbp
            or new_name != item.name
            or new_img != item.image_url
            or new_cat != item.category
            or new_retiring != item.is_retiring
        ):
            updated = self.db.update_item(
                item.id,
                name=new_name,
                rrp_gbp=new_rrp,
                category=new_cat,
                is_retiring=new_retiring,
                image_url=new_img,
            )
            if updated:
                item = updated
                upsert_wishlist_entry(
                    self.config.wishlist_path,
                    {
                        "name": updated.name,
                        "set_number": item.set_number,
                        "category": new_cat,
                        "rrp_gbp": new_rrp if new_rrp > 0 else None,
                        "is_retiring": new_retiring,
                        "image_url": new_img,
                    },
                )
        return item, False

    async def _fetch_offer(
        self,
        item: TrackedItem,
        *,
        scheduled: bool,
        force_refresh: bool,
        interactive_serp_budget: list[int],
    ) -> tuple[UKOffer | None, list[str]]:
        resolved_name = clean_set_display_name(item.name, item.set_number) or item.name
        can_use_serp = bool(self.config.serpapi_key) and (
            self.db.can_spend_token("serpapi", self.config.serpapi_monthly_limit, manual_force=force_refresh)[0]
            if scheduled or force_refresh
            else (interactive_serp_budget[0] > 0 and self.db.can_spend_token("serpapi", self.config.serpapi_monthly_limit, manual_force=False)[0])
        )

        def _on_serp_spent() -> None:
            self.db.record_token_spend("serpapi")
            if not scheduled and not force_refresh and interactive_serp_budget[0] > 0:
                interactive_serp_budget[0] -= 1

        return await find_best_uk_offer(
            self.http_session,  # type: ignore[arg-type]
            name=resolved_name,
            set_number=item.set_number,
            direct_url=item.url,
            rrp_gbp=item.rrp_gbp if item.rrp_gbp > 0 else None,
            has_amazon_prime=self.config.has_amazon_prime,
            prefer_click_and_collect=self.config.prefer_click_and_collect,
            serpapi_key=self.config.serpapi_key,
            allow_serpapi=can_use_serp,
            on_serpapi_credit_used=_on_serp_spent,
            on_amazon_dp_url_update=self.db.set_amazon_dp_url,
            proxy_url=self.config.proxy_url,
            force_refresh=force_refresh,
        )

    def _matching_ping_users(self, item: TrackedItem, analysis: PriceAnalysis) -> list[str]:
        """Return user IDs whose personal or global threshold is met by this offer."""
        base_users = effective_notify_user_ids(item, self.config.user_id, self.config.extra_notify_user_ids)
        custom = self.db.get_user_thresholds(item.id)
        matched: list[str] = []
        for uid in base_users:
            t_gbp, m_pct = custom.get(uid, (item.target_price_gbp, item.min_discount_pct))
            eff_target = t_gbp if t_gbp is not None else item.target_price_gbp
            eff_disc = m_pct if m_pct is not None else item.min_discount_pct
            hit_target = eff_target is not None and analysis.total_gbp <= eff_target
            hit_disc = analysis.discount_pct >= eff_disc
            if hit_target or hit_disc:
                matched.append(uid)
        return matched

    async def _notify_if_deal(
        self,
        item: TrackedItem,
        analysis: PriceAnalysis,
        best_offer: UKOffer,
        *,
        is_all_time_low: bool,
        channel: object,
        update_status: Callable[[str], Awaitable[None]],
    ) -> None:
        title = format_item_title(item)
        matched_users = self._matching_ping_users(item, analysis)
        if not matched_users:
            if item.last_alert_price_gbp is not None:
                self.db.reset_alert_state(item.id)
            await update_status(
                f"ℹ️ **{title}** had no good deals "
                f"*(best: £{analysis.total_gbp:.2f} at {best_offer.retailer.display_name}, {format_discount_short(analysis.discount_pct)})*."
            )
            return

        if not self.db.should_alert(item, analysis.total_gbp, self.config.alert_cooldown_hours):
            await update_status(
                f"ℹ️ **{title}** is still on deal (**£{analysis.total_gbp:.2f}** at "
                f"{best_offer.retailer.display_name} — alert cooldown active)."
            )
            return

        if (
            is_quiet_hours(self.config.quiet_hours_start, self.config.quiet_hours_end)
            and not is_all_time_low
            and not item.is_retiring
        ):
            await update_status(
                f"🌙 **{title}** is on deal (**£{analysis.total_gbp:.2f}**) — holding ping until morning quiet hours end."
            )
            return

        await update_status(f"🔥 Good deal spotted for **{title}**!")
        series = list(reversed([h.total_gbp for h in self.db.get_price_history(item.id, limit=30)]))
        rrp = item.rrp_gbp if item.rrp_gbp > 0 else analysis.rrp_gbp
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
            deal_summary = (
                f"🔥 **{title}** — **£{analysis.total_gbp:.2f}** "
                f"at {best_offer.retailer.display_name} ({format_discount_short(analysis.discount_pct)})"
            )
            if self.dev_mode:
                alert_content = f"🛠️ [DEV MODE] {deal_summary} *(pings suppressed)*"
                allowed = discord.AllowedMentions.none()
            else:
                pings = " ".join(f"<@{uid}>" for uid in matched_users)
                alert_content = f"{pings} {deal_summary}"
                allowed = discord.AllowedMentions.all()
            view = DealLinkView(best_offer.url, best_offer.retailer.display_name)
            if chart_bytes:
                file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
                await channel.send(
                    content=alert_content, embeds=embeds, file=file, view=view, allowed_mentions=allowed
                )
            else:
                await channel.send(content=alert_content, embeds=embeds, view=view, allowed_mentions=allowed)
            self.db.mark_alerted(item.id, analysis.total_gbp)

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
            items = self.db.get_items_due_for_check(min_age_hours=self.config.stale_check_hours)[:1]
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

        async def _emit(
            embed: discord.Embed,
            item_set_number: str | None = None,
            *,
            deal_url: str | None = None,
            retailer_name: str | None = None,
        ) -> None:
            async with emit_lock:
                result_embeds.append(embed)
                if on_item_embed is not None:
                    view = (
                        CheckResultView(
                            self,
                            item_set_number,
                            deal_url=deal_url,
                            retailer_name=retailer_name,
                        )
                        if item_set_number
                        else None
                    )
                    await on_item_embed(embed, view)

        async def _check_one(idx: int, item: TrackedItem) -> None:
            async with sem:
                if len(items) > 1 and idx > 0:
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                status_msg: Optional[discord.Message] = None
                if scheduled and isinstance(channel, discord.abc.Messageable):
                    status_msg = await self._get_status_message(channel)

                async def _update_status(text: str) -> None:
                    if status_msg is not None:
                        try:
                            await status_msg.edit(
                                content=self._format_status_text(text),
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except Exception:
                            pass

                await _update_status(f"🔍 Checking **{format_item_title(item)}** for UK deals...")

                refreshed, was_retired = await self._refresh_item_metadata(item, channel, now_mono)
                if was_retired or refreshed is None:
                    return
                item = refreshed

                best_offer, notes = await self._fetch_offer(
                    item,
                    scheduled=scheduled,
                    force_refresh=force_refresh,
                    interactive_serp_budget=interactive_serp_budget,
                )
                self.db.mark_checked(item.id)

                title = format_item_title(item)
                box_art = item.image_url or default_box_art_url(item.set_number)
                blocked_notes = [
                    n for n in notes
                    if any(tok in n.lower() for tok in ("rate-limited", "cooldown", "http ", "timeout", "error"))
                ]

                if best_offer is None:
                    latest = self.db.get_latest_price(item.id) if not scheduled and not force_refresh else None
                    rec_retailer = UKRetailer.OTHER
                    if latest is not None:
                        try:
                            rec_retailer = UKRetailer(latest.retailer)
                        except ValueError:
                            rec_retailer = UKRetailer.OTHER

                    if latest is not None and rec_retailer in APPROVED_UK_RETAILERS:
                        eff_rrp = item.rrp_gbp if item.rrp_gbp > 0 else latest.price_gbp
                        hist_totals = [h.total_gbp for h in self.db.get_price_history(item.id, limit=90)]
                        cached_analysis = analyze_price(
                            rrp_gbp=eff_rrp,
                            item_price_gbp=latest.price_gbp,
                            shipping_gbp=latest.shipping_gbp,
                            historical_totals_gbp=hist_totals,
                            is_retiring=item.is_retiring,
                        )
                        fallback_url = item.url if item.url and detect_retailer_from_url(item.url) == rec_retailer else None
                        if not fallback_url and rec_retailer == UKRetailer.AMAZON_UK and item.set_number:
                            fallback_url = self.db.get_amazon_dp_urls().get(item.set_number.strip().split("-")[0])
                        if not fallback_url and item.set_number:
                            resolved_name = clean_set_display_name(item.name, item.set_number) or item.name
                            fallback_url = dict(build_uk_search_urls(resolved_name, item.set_number)).get(rec_retailer)
                        embeds = build_price_card(
                            item,
                            cached_analysis,
                            rec_retailer,
                            mode="cached",
                            offer_url=fallback_url or item.url,
                            recorded_at=latest.recorded_at,
                            excluded_notes=blocked_notes,
                        )
                        await _emit(
                            embeds[0],
                            item.set_number,
                            deal_url=fallback_url or item.url,
                            retailer_name=rec_retailer.display_name,
                        )
                        return

                    rrp_line = f"💷 **Normal RRP:** £{item.rrp_gbp:.2f}\n" if item.rrp_gbp > 0 else ""
                    diag = "\n".join(f"• {n}" for n in notes[:5]) if notes else "No approved UK retailers had stock."
                    await _emit(
                        make_status_embed(
                            f"🧩 {title}",
                            f"{rrp_line}⚠️ **No live in-stock UK offers found right now**\n*Sources checked:*\n{diag}",
                            color=discord.Color.orange(),
                            thumbnail_url=box_art,
                            footer="Checked approved UK retailers • GBP (£) inc. delivery",
                        ),
                        item.set_number,
                    )
                    await _update_status(f"ℹ️ **{title}** had no good deals *(no in-stock UK offers found)*.")
                    return

                rrp = item.rrp_gbp if item.rrp_gbp > 0 else best_offer.item_price_gbp
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
                    is_retiring=item.is_retiring,
                )

                embeds = build_price_card(
                    item,
                    analysis,
                    best_offer.retailer,
                    mode="check",
                    shipping_note=best_offer.shipping_note,
                    offer_url=best_offer.url,
                    excluded_notes=blocked_notes,
                    is_all_time_low=is_all_time_low,
                )
                await _emit(
                    embeds[0],
                    item.set_number,
                    deal_url=best_offer.url,
                    retailer_name=best_offer.retailer.display_name,
                )

                await self._notify_if_deal(
                    item,
                    analysis,
                    best_offer,
                    is_all_time_low=is_all_time_low,
                    channel=channel,
                    update_status=_update_status,
                )

        await asyncio.gather(*(_check_one(idx, it) for idx, it in enumerate(items)))
        self._save_cooldowns()

        if scheduled and not set_number:
            await self._maybe_send_weekly_summary(channel)

        return result_embeds

    def _build_history_embed(self, item: TrackedItem) -> tuple[discord.Embed, discord.File | None]:
        records = self.db.get_price_history(item.id, limit=30)
        img = item.image_url or default_box_art_url(item.set_number)
        if not records:
            return (
                make_status_embed(
                    f"📈 Price History: {format_item_title(item)}",
                    "No price history recorded yet. Run `/check` to record today's price!",
                    color=discord.Color.gold(),
                    thumbnail_url=img,
                ),
                None,
            )

        series = list(reversed([r.total_gbp for r in records]))
        rrp = item.rrp_gbp if item.rrp_gbp > 0 else max(series)
        low = min(series)
        latest = series[-1]
        lines = [
            f"💷 **Normal RRP:** £{rrp:.2f}  •  🏷️ **Latest:** £{latest:.2f}  •  🔥 **Low:** £{low:.2f}\n"
        ]
        for r in records[:10]:
            lines.append(f"• `{r.recorded_at[:10]}` — **£{r.total_gbp:.2f}** at {r.retailer}")

        file: discord.File | None = None
        embed_image = img
        if len(series) >= 2:
            chart_bytes = generate_price_chart(series, rrp)
            file = discord.File(io.BytesIO(chart_bytes), filename="chart.png")
            embed_image = "attachment://chart.png"

        embed = make_status_embed(
            f"📈 Price History: {format_item_title(item)}",
            "\n".join(lines),
            color=discord.Color.blue(),
            image_url=embed_image,
            footer=f"Set #{item.set_number or item.id} • Last {len(records)} check(s)",
        )
        return embed, file

    def _register_commands(self) -> None:
        bot_group = app_commands.Group(
            name="bot",
            description="Bot management commands (Admin only)",
            default_permissions=discord.Permissions(administrator=True),
        )

        async def _set_number_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            query = current.lower().strip()
            choices: list[app_commands.Choice[str]] = []
            for item in self.db.list_items(include_retired=True):
                if not item.set_number:
                    continue
                label = format_item_title(item)[:100]
                if query and query not in label.lower() and query not in item.set_number.lower():
                    continue
                choices.append(app_commands.Choice(name=label, value=item.set_number))
                if len(choices) >= 25:
                    break
            return choices

        @self.tree.command(name="add", description="Add a LEGO set to your wishlist using its set number")
        @app_commands.describe(
            set_number="LEGO set number (e.g. 10316)",
            name="Optional custom name (auto-fetched if left blank)",
            min_discount_pct="Alert when total discount is at least this % off RRP (default 15%)",
            target_price_gbp="Optional target price in £ (including shipping)",
        )
        async def cmd_add(
            interaction: discord.Interaction,
            set_number: str,
            name: Optional[str] = None,
            min_discount_pct: app_commands.Range[float, 0.0, 90.0] = 15.0,
            target_price_gbp: Optional[app_commands.Range[float, 0.0, 5000.0]] = None,
        ) -> None:
            await interaction.response.defer()
            try:
                assert self.http_session is not None
                clean_num = set_number.strip().split("-")[0]
                existing = self.db.get_item_by_set_number(clean_num)
                if existing:
                    if existing.status == "retired":
                        self.db.restore_item(existing.id)
                    embed = make_status_embed(
                        "Already on Wishlist",
                        f"**{format_item_title(existing)}** is already tracked.",
                        color=discord.Color.gold(),
                        thumbnail_url=existing.image_url or default_box_art_url(clean_num),
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
                    thumbnail_url=box_art,
                    footer="Saved to wishlist",
                )
                await interaction.followup.send(embed=embed)
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=make_error_embed("Timed Out", "Looking up set details took too long. Please try again."),
                    ephemeral=True,
                )
            except Exception:
                logger.exception("add command failed")
                await interaction.followup.send(
                    embed=make_error_embed("Failed to Add Set", "Could not add that set right now. Please try again."),
                    ephemeral=True,
                )

        @self.tree.command(name="list", description="View your tracked LEGO wishlist")
        async def cmd_list(interaction: discord.Interaction) -> None:
            view = WishlistView(self)
            await interaction.response.send_message(embed=view.build_embed(), view=view)

        @self.tree.command(name="set", description="Open the detail card & quick actions for a tracked LEGO set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def cmd_set(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            view = SetDetailView(self, item)
            await interaction.response.send_message(embed=view.build_embed(), view=view)

        @self.tree.command(name="deals", description="Show tracked sets currently at or below target / on sale")
        async def cmd_deals(interaction: discord.Interaction) -> None:
            items = self.db.list_items()
            deals: list[tuple[float, str]] = []
            for item in items:
                latest = self.db.get_latest_price(item.id)
                if not latest or item.rrp_gbp <= 0:
                    continue
                disc_pct = ((item.rrp_gbp - latest.total_gbp) / item.rrp_gbp) * 100
                hit_target = item.target_price_gbp is not None and latest.total_gbp <= item.target_price_gbp
                hit_disc = disc_pct >= item.min_discount_pct
                if hit_target or hit_disc:
                    tag = " 🎯" if hit_target else ""
                    ret = " ⚠️" if item.is_retiring else ""
                    deals.append(
                        (
                            disc_pct,
                            f"🟢 **{format_item_title(item)}** — **£{latest.total_gbp:.2f}** "
                            f"`{format_discount_short(disc_pct)}` at {latest.retailer}{tag}{ret}",
                        )
                    )
            if not deals:
                await interaction.response.send_message(
                    embed=make_status_embed(
                        "🏷️ Current UK LEGO Deals",
                        "No tracked sets are currently hitting your target price or minimum discount.",
                        color=discord.Color.blue(),
                    )
                )
                return
            deals.sort(key=lambda d: d[0], reverse=True)
            await interaction.response.send_message(
                embed=make_status_embed(
                    f"🔥 Active Deals ({len(deals)})",
                    "\n".join(line for _, line in deals[:20]),
                    color=discord.Color.green(),
                    footer="Sorted by highest % off RRP",
                )
            )

        @self.tree.command(name="check", description="Check live UK prices for one set or all sets")
        @app_commands.describe(
            set_number="LEGO set number to check (leave blank to check all — Admin only)",
            force="Bypass the 30-minute price cache and fetch fresh UK prices",
        )
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def cmd_check(
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
            except Exception:
                logger.exception("check command failed")
                await interaction.followup.send(
                    embed=make_error_embed("Price Check Error", "Something went wrong while checking UK stores. Please try again.")
                )

        @self.tree.command(name="edit", description="Edit alert settings for a tracked LEGO set")
        @app_commands.describe(
            set_number="LEGO set number (e.g. 10316)",
            target_price_gbp="New target price in £ (enter 0 to clear)",
            min_discount_pct="New minimum discount %",
            is_retiring="Mark as retiring soon (True/False)",
        )
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def cmd_edit(
            interaction: discord.Interaction,
            set_number: str,
            target_price_gbp: Optional[app_commands.Range[float, 0.0, 5000.0]] = None,
            min_discount_pct: Optional[app_commands.Range[float, 0.0, 90.0]] = None,
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
                thumbnail_url=updated.image_url or default_box_art_url(updated.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="remove", description="Remove a LEGO set from your wishlist")
        @app_commands.describe(set_number="LEGO set number to remove (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def cmd_remove(interaction: discord.Interaction, set_number: str) -> None:
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
                thumbnail_url=item.image_url or default_box_art_url(item.set_number),
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="pings", description="Interactive menu to choose which sets a user gets pinged for")
        @app_commands.describe(user="Optional Discord user (defaults to picking from dropdown)")
        async def cmd_pings(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
            view = PingManagerView(self, target_user=user)
            await interaction.response.send_message(embed=view.build_embed(), view=view)

        @self.tree.command(name="history", description="View GBP price history and chart for a tracked LEGO set")
        @app_commands.describe(set_number="LEGO set number (e.g. 10316)")
        @app_commands.autocomplete(set_number=_set_number_autocomplete)
        async def cmd_history(interaction: discord.Interaction, set_number: str) -> None:
            item = self.db.get_item_by_set_number(set_number)
            if not item:
                await interaction.response.send_message(
                    embed=make_error_embed("Not Found", f"Set #{set_number} is not on your wishlist."),
                    ephemeral=True,
                )
                return
            embed, file = self._build_history_embed(item)
            if file is not None:
                await interaction.response.send_message(embed=embed, file=file)
            else:
                await interaction.response.send_message(embed=embed)

        @bot_group.command(name="status", description="Show bot schedule, UK retailer engine, monthly token quota, and source health")
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
                    f"⏱️ **Check Tick:** Every {self.config.check_interval_minutes} min (next {next_str} • ≥{self.config.stale_check_hours:g}h staleness queue)\n"
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

        @bot_group.command(name="devmode", description="Toggle dev mode (suppresses user pings on alerts and summaries)")
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

        @bot_group.command(name="maintenance", description="Clear price cache, reset store cooldowns, clean history, or export wishlist")
        @app_commands.describe(action="Which maintenance task to run")
        async def bot_maintenance(
            interaction: discord.Interaction,
            action: Literal["clear_cache", "clear_cooldowns", "clean_history", "export_wishlist"],
        ) -> None:
            if not is_admin_user(interaction, self.config.user_id):
                await interaction.response.send_message(
                    embed=make_error_embed("Admin Only", "Only server admins can use `/bot` commands."),
                    ephemeral=True,
                )
                return
            if action == "clear_cache":
                cleared = clear_price_cache()
                embed = make_status_embed(
                    "🧹 Price Cache Cleared",
                    f"Removed **{cleared}** cached set(s) from the 30-minute memory cache.",
                    color=discord.Color.green(),
                )
                await interaction.response.send_message(embed=embed)
            elif action == "clear_cooldowns":
                cleared_stores = clear_cooldowns()
                self._save_cooldowns()
                msg = (
                    f"Reset active cooldowns for: {', '.join(f'**{s}**' for s in cleared_stores)}."
                    if cleared_stores
                    else "No stores were in cooldown (all block streaks reset)."
                )
                await interaction.response.send_message(
                    embed=make_status_embed("🔄 Store Cooldowns Cleared", msg, color=discord.Color.green())
                )
            elif action == "export_wishlist":
                data = [
                    {
                        "name": it.name,
                        "set_number": it.set_number,
                        "category": it.category,
                        "rrp_gbp": it.rrp_gbp if it.rrp_gbp > 0 else None,
                        "target_price_gbp": it.target_price_gbp,
                        "min_discount_pct": it.min_discount_pct,
                        "is_retiring": it.is_retiring,
                        "image_url": it.image_url,
                        "notify_user_ids": list(it.notify_user_ids),
                    }
                    for it in self.db.list_items()
                ]
                raw = json.dumps(data, indent=2)
                Path(self.config.wishlist_path).write_text(raw, encoding="utf-8")
                await interaction.response.send_message(
                    embed=make_status_embed(
                        "📤 Wishlist Exported",
                        f"Exported **{len(data)}** active set(s) to `{self.config.wishlist_path}`.",
                        color=discord.Color.green(),
                    ),
                    file=discord.File(io.BytesIO(raw.encode("utf-8")), filename="wishlist.json"),
                )
            else:
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

        @bot_group.command(name="update", description="Pull the latest code from GitHub, sync dependencies, and restart (Owner only)")
        async def bot_update(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.config.user_id:
                await interaction.response.send_message(
                    embed=make_error_embed("Owner Only", "Only the configured bot owner can run `/bot update`."),
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
            except Exception:
                logger.exception("bot update failed")
                await interaction.followup.send(
                    embed=make_error_embed("Update Error", "Could not complete update. Check bot logs for details."),
                    ephemeral=True,
                )

        self.tree.add_command(bot_group)


def main() -> None:
    config = Config.from_env()
    bot = LegoTrackerBot(config)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()
