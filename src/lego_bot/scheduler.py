from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

from .config import Config
from .db import PriceDatabase, TrackedItem
from .embeds import format_discount_short, format_item_title, make_status_embed
from .shipping import APPROVED_UK_RETAILERS
from .uk_finder import get_source_health

UK_TZ = ZoneInfo("Europe/London")


def is_quiet_hours(
    start_hour: int,
    end_hour: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if current UK local time is within [start_hour, end_hour)."""
    if start_hour == end_hour:
        return False
    dt = (now or datetime.now(timezone.utc)).astimezone(UK_TZ)
    hour = dt.hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def format_pinned_dashboard(
    db: PriceDatabase,
    config: Config,
    status_text: str,
) -> str:
    """Format the pinned status message as a compact dashboard with store health."""
    now_ts = int(time.time())
    next_ts = now_ts + config.check_interval_minutes * 60
    items = db.list_items()
    due = db.get_items_due_for_check(min_age_hours=config.stale_check_hours)
    next_item_str = f"Next: **{format_item_title(due[0])}**" if due else "Queue caught up"

    total_stores = len(APPROVED_UK_RETAILERS)
    cooldowns = get_source_health()
    healthy_count = max(0, total_stores - len(cooldowns))
    if not cooldowns:
        store_str = f"✅ {healthy_count}/{total_stores} healthy"
    else:
        cd_list = ", ".join(f"`{src}` ({max(1, secs // 60)}m)" for src, secs in sorted(cooldowns.items()))
        store_str = f"⚠️ {healthy_count}/{total_stores} healthy (cooldown: {cd_list})"

    if config.serpapi_key:
        used = db.get_monthly_usage("serpapi")
        quota_part = f" · SerpApi {used}/{config.serpapi_monthly_limit}"
    else:
        quota_part = ""

    return (
        f"📌 **Scheduled Check Status**\n"
        f"{status_text}\n"
        f"📊 Tracked **{len(items)}** · {next_item_str}{quota_part}\n"
        f"🛒 **Stores:** {store_str}\n"
        f"🕒 Updated <t:{now_ts}:R> • Next tick ~<t:{next_ts}:R>"
    )


def build_weekly_summary_embed(db: PriceDatabase, items: list[TrackedItem]) -> discord.Embed:
    """Build a structured, discount-sorted weekly summary with week-over-week deltas."""
    if not items:
        return make_status_embed(
            "📊 Weekly UK LEGO Wishlist Summary",
            "Your wishlist is empty.",
            color=discord.Color.blue(),
        )

    on_deal: list[tuple[float, str]] = []
    dropped: list[tuple[float, str]] = []
    retiring: list[tuple[float, str]] = []
    other_count = 0

    for item in items:
        latest = db.get_latest_price(item.id)
        if not latest:
            other_count += 1
            continue

        disc_pct = ((item.rrp_gbp - latest.total_gbp) / item.rrp_gbp) * 100 if item.rrp_gbp > 0 else 0.0
        prev = db.get_price_days_ago(item.id, days_ago=7)
        delta_val = round(latest.total_gbp - prev.total_gbp, 2) if prev else 0.0
        if prev and delta_val <= -1.0:
            delta_str = f" (↓ £{abs(delta_val):.2f})"
        elif prev and delta_val >= 1.0:
            delta_str = f" (↑ £{delta_val:.2f})"
        elif prev:
            delta_str = " (—)"
        else:
            delta_str = ""

        low = db.get_historical_low(item.id)
        low_str = f" | Low £{low:.2f}" if low is not None else ""
        disc_str = f" `{format_discount_short(disc_pct)}`" if item.rrp_gbp > 0 else ""
        line = f"• **{format_item_title(item)}**: **£{latest.total_gbp:.2f}**{disc_str}{delta_str}{low_str}"

        hit_target = item.target_price_gbp is not None and latest.total_gbp <= item.target_price_gbp
        hit_disc = disc_pct >= item.min_discount_pct
        if hit_target or hit_disc:
            on_deal.append((disc_pct, line))
        elif prev and delta_val <= -1.0:
            dropped.append((disc_pct, line))
        elif item.is_retiring:
            retiring.append((disc_pct, line))
        else:
            other_count += 1

    sections: list[str] = []
    if on_deal:
        on_deal.sort(key=lambda r: r[0], reverse=True)
        sections.append("**🔥 On Deal Now**\n" + "\n".join(line for _, line in on_deal))
    if dropped:
        dropped.sort(key=lambda r: r[0], reverse=True)
        sections.append("**📉 Dropped This Week**\n" + "\n".join(line for _, line in dropped))
    if retiring:
        retiring.sort(key=lambda r: r[0], reverse=True)
        sections.append("**⚠️ Retiring Soon**\n" + "\n".join(line for _, line in retiring))
    if other_count:
        sections.append(f"**📦 Everything Else:** {other_count} set(s) steady near RRP/no deals")

    body = "\n\n".join(sections) if sections else "No prices recorded yet."
    return make_status_embed(
        "📊 Weekly UK LEGO Wishlist Summary",
        body,
        color=discord.Color.blue(),
        footer=f"{len(items)} active set(s) • Sorted by discount",
    )
