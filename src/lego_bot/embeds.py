from __future__ import annotations

from typing import Literal, Optional

import discord

from .db import TrackedItem
from .predictor import PriceAnalysis, Verdict
from .shipping import UKRetailer
from .uk_finder import UKOffer, clean_set_display_name, default_box_art_url


def effective_notify_user_ids(
    item: TrackedItem,
    default_user_id: int,
    extra_user_ids: tuple[str, ...] = ("942902769319292948",),
) -> list[str]:
    """Return the deduplicated list of Discord user IDs to ping for an item."""
    if item.notify_user_ids:
        return list(dict.fromkeys(str(u) for u in item.notify_user_ids))
    return list(dict.fromkeys([str(default_user_id), *(str(u) for u in extra_user_ids)]))


def format_pings(
    default_user_id: int,
    item: TrackedItem,
    extra_user_ids: tuple[str, ...] = ("942902769319292948",),
) -> str:
    ids = effective_notify_user_ids(item, default_user_id, extra_user_ids)
    return " ".join(f"<@{uid}>" for uid in ids)


def format_item_title(item: TrackedItem) -> str:
    display_name = clean_set_display_name(item.name, item.set_number) or item.name
    if item.set_number and item.set_number not in display_name:
        return f"{display_name} (#{item.set_number})"
    return display_name


def format_discount_short(discount_pct: float) -> str:
    """Format a discount percentage cleanly, avoiding '-5% off' when above RRP."""
    rounded = round(discount_pct)
    if rounded > 0:
        return f"-{rounded}%"
    if rounded == 0:
        return "at RRP"
    return f"+{abs(rounded)}% above RRP"


def format_discount_badge(discount_pct: float, savings_gbp: float | None = None) -> str:
    """Format a discount headline badge cleanly for embeds."""
    rounded = round(discount_pct)
    if rounded > 0:
        save_part = f" *(Save £{savings_gbp:.2f})*" if savings_gbp is not None and savings_gbp > 0 else ""
        return f"**{rounded}% OFF**{save_part}"
    if rounded == 0:
        return "**At RRP**"
    over_part = f" *(+£{abs(savings_gbp):.2f})*" if savings_gbp is not None and savings_gbp < 0 else ""
    return f"**{abs(rounded)}% above RRP**{over_part}"


def verdict_style(verdict: Verdict, discount_pct: float = 0.0) -> tuple[str, discord.Color]:
    """Consistent colour & emoji language across all embeds."""
    if round(discount_pct) < 0:
        return "🟠", discord.Color.orange()
    if verdict in (Verdict.BUY_NOW, Verdict.BUY_NOW_RETIRING):
        return "🟢", discord.Color.green()
    if verdict in (Verdict.GOOD_DEAL, Verdict.WATCH_RETIRING, Verdict.LOWEST_SEEN):
        return "🟡", discord.Color.gold()
    return "⚪", discord.Color.blue()


def make_status_embed(
    title: str,
    description: str,
    *,
    color: discord.Color | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
    footer: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blue(),
    )
    if image_url:
        embed.set_image(url=image_url)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if footer:
        embed.set_footer(text=footer)
    return embed


def make_error_embed(title: str, detail: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌ {title}",
        description=detail,
        color=discord.Color.red(),
    )


def build_price_card(
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    *,
    mode: Literal["alert", "check", "cached"] = "check",
    shipping_note: str = "",
    offer_url: str | None = None,
    recorded_at: str | None = None,
    excluded_notes: list[str] | None = None,
    is_all_time_low: bool = False,
    has_chart: bool = False,
    simulated: bool = False,
    second_offer: Optional[UKOffer] = None,
) -> list[discord.Embed]:
    """Unified price card builder for alerts, live checks, and cached DB checks."""
    verdict_icon, color = verdict_style(analysis.verdict, analysis.discount_pct)
    if mode == "cached":
        color = discord.Color.blue()

    link_url = offer_url or item.url
    title = format_item_title(item)
    rrp_part = (
        f"~~RRP: £{analysis.rrp_gbp:.2f}~~"
        if round(analysis.discount_pct) > 0
        else f"Normal RRP: £{analysis.rrp_gbp:.2f}"
    )
    disc_badge = format_discount_badge(
        analysis.discount_pct,
        analysis.savings_gbp if mode == "alert" else None,
    )

    badges: list[str] = []
    if simulated:
        badges.append("🧪 **TEST ALERT**")
    if is_all_time_low:
        badges.append("🔥 **NEW ALL-TIME LOW**")
    badge_line = f"{'  •  '.join(badges)}\n" if badges else ""

    cat_line = f"📂 {item.category}\n" if item.category else ""
    price_label = "Last Verified Price" if mode == "cached" else "Deal Price"
    seller_suffix = f" *(recorded `{recorded_at[:10]}`)*" if mode == "cached" and recorded_at else ""
    cached_warn = (
        "⚠️ *Live stores unavailable — saved price may be stale. Click **Re-check** to retry.*\n"
        if mode == "cached"
        else ""
    )

    if analysis.shipping_gbp > 0:
        ship_txt = f"£{analysis.item_price_gbp:.2f} + £{analysis.shipping_gbp:.2f} delivery"
        if shipping_note:
            ship_txt += f" *({shipping_note})*"
    else:
        ship_txt = f"£{analysis.item_price_gbp:.2f} *(free delivery)*"

    ppp_part = ""
    if item.piece_count and item.piece_count > 0:
        ppp = (analysis.total_gbp * 100.0) / item.piece_count
        ppp_part = f"  •  🧱 **{ppp:.1f}p/piece**"

    headline = (
        f"{badge_line}"
        f"{cat_line}"
        f"## 🏷️ {price_label}: £{analysis.total_gbp:.2f}\n"
        f"{cached_warn}"
        f"### {rrp_part}  •  {disc_badge}{ppp_part}\n"
        f"📍 **{retailer.display_name}**{seller_suffix} — {ship_txt}"
    )

    embed = discord.Embed(
        title=f"🧩 {title}",
        url=link_url or None,
        description=headline,
        color=color,
    )

    if mode == "alert":
        context_lines: list[str] = []
        if analysis.historical_low_gbp is not None:
            context_lines.append(f"• **Tracked Low:** £{analysis.historical_low_gbp:.2f}")
        if analysis.historical_avg_gbp is not None:
            context_lines.append(f"• **Tracked Avg:** £{analysis.historical_avg_gbp:.2f}")
        if item.last_alert_price_gbp is not None:
            diff = round(analysis.total_gbp - item.last_alert_price_gbp, 2)
            if diff < 0:
                context_lines.append(f"• **vs. Last Alert:** ↓ £{abs(diff):.2f} cheaper")
            elif diff > 0:
                context_lines.append(f"• **vs. Last Alert:** ↑ £{diff:.2f} higher")
        if second_offer is not None:
            context_lines.append(
                f"• **Next Best UK:** £{second_offer.total_gbp:.2f} at {second_offer.retailer.display_name}"
            )
        context_lines.append(f"• **Retiring Soon:** {'⚠️ Yes' if item.is_retiring else 'No'}")

        embed.add_field(
            name="📊 Price Context",
            value="\n".join(context_lines),
            inline=False,
        )
        embed.add_field(
            name=f"{verdict_icon} Verdict: {analysis.verdict.value}",
            value=analysis.forecast,
            inline=False,
        )
    else:
        retire_tag = "  •  ⚠️ **Retiring Soon**" if item.is_retiring else ""
        embed.add_field(
            name=f"{verdict_icon} {analysis.verdict.value}{retire_tag}",
            value=analysis.forecast,
            inline=False,
        )
        if excluded_notes:
            set_tag = f"#{item.set_number}" if item.set_number else title
            embed.add_field(
                name=f"⚠️ Excluded from {set_tag} check",
                value=", ".join(excluded_notes),
                inline=False,
            )

    box_art = item.image_url or default_box_art_url(item.set_number)
    if box_art:
        if mode == "alert":
            embed.set_image(url=box_art)
        else:
            embed.set_thumbnail(url=box_art)

    footer_text = (
        "Cached DB price • Click Re-check for a fresh scan"
        if mode == "cached"
        else f"Set #{item.set_number or item.id} • UK GBP (£) inc. delivery"
    )
    embed.set_footer(text=footer_text)

    embeds = [embed]
    if mode == "alert" and has_chart:
        chart_embed = discord.Embed(title="📈 Tracked Price History (GBP)", color=color)
        chart_embed.set_image(url="attachment://chart.png")
        embeds.append(chart_embed)
    return embeds


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
    second_offer: Optional[UKOffer] = None,
) -> list[discord.Embed]:
    return build_price_card(
        item,
        analysis,
        retailer,
        mode="alert",
        shipping_note=shipping_note,
        offer_url=offer_url,
        is_all_time_low=is_all_time_low,
        has_chart=has_chart,
        simulated=simulated,
        second_offer=second_offer,
    )
