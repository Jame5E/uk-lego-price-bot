from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import aiohttp
from dotenv import load_dotenv

from src.lego_bot.chart import generate_price_chart
from src.lego_bot.db import PriceDatabase, TrackedItem
from src.lego_bot.predictor import PriceAnalysis, analyze_price
from src.lego_bot.shipping import UKRetailer, detect_retailer_from_url, estimate_uk_shipping
from src.lego_bot.uk_finder import default_box_art_url, find_best_uk_offer, lookup_set_metadata
from src.lego_bot.wishlist_store import load_wishlist_entries


def sync_wishlist_to_db(db: PriceDatabase, entries: list[dict[str, Any]]) -> list[TrackedItem]:
    """Sync wishlist.json entries into SQLite and return the active TrackedItems."""
    existing = db.list_items()
    by_set = {i.set_number: i for i in existing if i.set_number}
    by_name_url = {(i.name, i.url): i for i in existing}
    synced: list[TrackedItem] = []

    for entry in entries:
        name = str(entry["name"]).strip()
        set_number = str(entry["set_number"]).strip() if entry.get("set_number") else None
        url = str(entry.get("url") or "").strip() or None
        notify_ids = [str(u) for u in (entry.get("notify_user_ids") or [])]

        match = by_set.get(set_number) if set_number else by_name_url.get((name, url))
        if match is not None:
            updated = db.update_item(
                match.id,
                name=name,
                rrp_gbp=float(entry["rrp_gbp"]) if entry.get("rrp_gbp") is not None else None,
                target_price_gbp=float(entry["target_price_gbp"]) if entry.get("target_price_gbp") is not None else None,
                min_discount_pct=float(entry["min_discount_pct"]) if entry.get("min_discount_pct") is not None else None,
                is_retiring=bool(entry["is_retiring"]) if entry.get("is_retiring") is not None else None,
                notify_user_ids=notify_ids if notify_ids else None,
            )
            synced.append(updated or match)
            continue

        retailer = detect_retailer_from_url(url)
        rrp = float(entry["rrp_gbp"]) if entry.get("rrp_gbp") is not None else 0.0
        item = db.add_item(
            name=name,
            rrp_gbp=rrp,
            set_number=set_number,
            target_price_gbp=float(entry["target_price_gbp"]) if entry.get("target_price_gbp") is not None else None,
            min_discount_pct=float(entry.get("min_discount_pct", 15.0)),
            url=url,
            retailer=retailer.value,
            is_retiring=bool(entry.get("is_retiring", False)),
            image_url=str(entry.get("image_url") or "") or default_box_art_url(set_number),
            notify_user_ids=notify_ids,
        )
        synced.append(item)
    return synced


def format_pings(default_user_id: str, item: TrackedItem) -> str:
    ids = list(item.notify_user_ids) if item.notify_user_ids else [default_user_id]
    if default_user_id not in ids and not item.notify_user_ids:
        ids.append(default_user_id)
    return " ".join(f"<@{uid}>" for uid in dict.fromkeys(ids))


def build_discord_payload(
    default_user_id: str,
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    shipping_note: str,
    offer_url: str | None = None,
    *,
    is_all_time_low: bool = False,
    has_chart: bool = False,
    simulated: bool = False,
) -> dict[str, Any]:
    if "BUY NOW" in analysis.verdict:
        color = 0x2ECC71
    elif "GOOD DEAL" in analysis.verdict or "BUY SOON" in analysis.verdict:
        color = 0xF1C40F
    else:
        color = 0xE67E22

    prefix = "[TEST] " if simulated else ""
    atl_badge = "🔥 NEW ALL-TIME LOW! " if is_all_time_low else ""
    set_tag = f" (#{item.set_number})" if item.set_number else ""
    link_url = offer_url or item.url

    fields: list[dict[str, Any]] = [
        {
            "name": "Price Breakdown (GBP)",
            "value": (
                f"**Item:** £{analysis.item_price_gbp:.2f}\n"
                f"**UK Shipping:** £{analysis.shipping_gbp:.2f} *({shipping_note})*\n"
                f"**Total Cost:** **£{analysis.total_gbp:.2f}** (RRP £{analysis.rrp_gbp:.2f})"
            ),
            "inline": True,
        },
        {
            "name": "Reduction",
            "value": (
                f"**Saved:** £{analysis.savings_gbp:.2f}\n"
                f"**Total Discount:** {analysis.discount_pct:.1f}% off\n"
                f"**Item-only Discount:** {analysis.item_discount_pct:.1f}% off"
            ),
            "inline": True,
        },
    ]

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
        fields.append({"name": "Price Context", "value": "\n".join(history_lines), "inline": False})

    fields.append({"name": f"Verdict: {analysis.verdict}", "value": analysis.forecast, "inline": False})
    if link_url:
        fields.append({"name": "Link", "value": f"[View on {retailer.display_name}]({link_url})", "inline": False})

    embed: dict[str, Any] = {
        "title": f"{prefix}{atl_badge}LEGO Deal Alert: {item.name}{set_tag}",
        "description": f"Best in-stock UK price spotted at **{retailer.display_name}**!",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Wishlist ID #{item.id} • Prices in GBP (£) including UK shipping"},
    }
    if link_url:
        embed["url"] = link_url
    thumb = item.image_url or default_box_art_url(item.set_number)
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    if has_chart:
        embed["image"] = {"url": "attachment://chart.png"}

    return {
        "content": f"{format_pings(default_user_id, item)} LEGO sale spotted!",
        "embeds": [embed],
    }


def build_weekly_summary_payload(
    default_user_id: str,
    db: PriceDatabase,
    items: list[TrackedItem],
) -> dict[str, Any]:
    lines: list[str] = []
    for item in items:
        hist = db.get_price_history(item.id, limit=1)
        low = db.get_historical_low(item.id)
        if hist:
            latest = hist[0]
            rrp_str = f"RRP £{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "RRP unknown"
            low_str = f" | Low £{low:.2f}" if low is not None else ""
            lines.append(
                f"• **{item.name}** (#{item.set_number or item.id}): "
                f"**£{latest.total_gbp:.2f}** at {latest.retailer} ({rrp_str}{low_str})"
            )
        else:
            lines.append(f"• **{item.name}** (#{item.set_number or item.id}): *No price recorded yet*")

    embed = {
        "title": "Weekly UK LEGO Wishlist Summary",
        "description": "\n".join(lines) if lines else "Your wishlist is empty.",
        "color": 0x3498DB,
        "footer": {"text": "cPanel tracker is active • Checks run twice daily"},
    }
    return {
        "content": f"<@{default_user_id}> Here is your weekly LEGO price summary!",
        "embeds": [embed],
    }


async def send_discord_message(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
    *,
    webhook_url: str | None = None,
    bot_token: str | None = None,
    channel_id: str | None = None,
    chart_png: bytes | None = None,
) -> None:
    if webhook_url:
        url = webhook_url
        headers: dict[str, str] = {}
    elif bot_token and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}
    else:
        raise ValueError("Set DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID in .env")

    if chart_png:
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps(payload), content_type="application/json")
        form.add_field("files[0]", chart_png, filename="chart.png", content_type="image/png")
        async with session.post(url, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Discord multipart post failed ({resp.status}): {body}")
        return

    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"Discord post failed ({resp.status}): {body}")


async def run_once(*, test_mode: bool = False, force_weekly: bool = False) -> None:
    load_dotenv()
    user_id = os.getenv("DISCORD_USER_ID", "").strip()
    if not user_id.isdigit():
        raise ValueError("DISCORD_USER_ID must be set in .env")

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or None
    channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip() or None

    db_path = os.getenv("DB_PATH", "data/lego_prices.db").strip()
    wishlist_path = os.getenv("WISHLIST_PATH", "wishlist.json").strip()
    cooldown_hours = max(1, int(os.getenv("ALERT_COOLDOWN_HOURS", "12")))
    has_prime = os.getenv("HAS_AMAZON_PRIME", "false").lower() == "true"
    prefer_cc = os.getenv("PREFER_CLICK_AND_COLLECT", "false").lower() == "true"
    brickset_key = os.getenv("BRICKSET_API_KEY", "").strip() or None

    entries = load_wishlist_entries(wishlist_path)
    db = PriceDatabase(db_path)

    try:
        items = sync_wishlist_to_db(db, entries)
        if not items:
            print("Wishlist is empty.")
            return

        async with aiohttp.ClientSession() as session:
            if test_mode:
                first = items[0]
                rrp = first.rrp_gbp if first.rrp_gbp > 0 else 429.99
                retailer = detect_retailer_from_url(first.url) if first.url else UKRetailer.ARGOS
                sim_price = round(rrp * 0.72, 2)
                shipping = estimate_uk_shipping(
                    sim_price, retailer, has_amazon_prime=has_prime, prefer_click_and_collect=prefer_cc
                )
                sample_series = [rrp, round(rrp * 0.92, 2), round(rrp * 0.85, 2), shipping.total_gbp]
                chart_bytes = generate_price_chart(sample_series, rrp)
                analysis = analyze_price(
                    rrp_gbp=rrp,
                    item_price_gbp=sim_price,
                    shipping_gbp=shipping.shipping_gbp,
                    historical_totals_gbp=sample_series[:-1],
                    is_retiring=first.is_retiring,
                )
                payload = build_discord_payload(
                    user_id,
                    first,
                    analysis,
                    retailer,
                    shipping.note,
                    is_all_time_low=True,
                    has_chart=True,
                    simulated=True,
                )
                await send_discord_message(
                    session,
                    payload,
                    webhook_url=webhook_url,
                    bot_token=bot_token,
                    channel_id=channel_id,
                    chart_png=chart_bytes,
                )
                print(f"Sent test alert with chart for {first.name} (£{analysis.total_gbp:.2f})")
                return

            for item in items:
                rrp = item.rrp_gbp
                is_retiring = item.is_retiring
                image_url = item.image_url

                if (rrp <= 0 or not is_retiring or not image_url) and item.set_number:
                    meta = await lookup_set_metadata(
                        session, item.set_number, brickset_api_key=brickset_key
                    )
                    if rrp <= 0 and meta.rrp_gbp:
                        rrp = meta.rrp_gbp
                    if meta.is_retiring:
                        is_retiring = True
                    if not image_url and meta.image_url:
                        image_url = meta.image_url
                    db.update_item(
                        item.id,
                        rrp_gbp=rrp if rrp > 0 else None,
                        is_retiring=is_retiring,
                        image_url=image_url,
                    )
                    item = db.get_item(item.id) or item

                best_offer, notes = await find_best_uk_offer(
                    session,
                    name=item.name,
                    set_number=item.set_number,
                    direct_url=item.url,
                    rrp_gbp=rrp if rrp > 0 else None,
                    has_amazon_prime=has_prime,
                    prefer_click_and_collect=prefer_cc,
                )
                if best_offer is None:
                    print(f"No in-stock UK offers found for {item.name}: {'; '.join(notes)}")
                    continue

                if rrp <= 0:
                    rrp = best_offer.item_price_gbp

                prev_low = db.get_historical_low(item.id)
                is_all_time_low = prev_low is not None and best_offer.total_gbp < prev_low

                history = db.get_price_history(item.id, limit=90)
                hist_totals = [h.total_gbp for h in history]
                if best_offer.historical_low_gbp is not None:
                    hist_totals.append(best_offer.historical_low_gbp)
                if best_offer.historical_avg_gbp is not None:
                    hist_totals.append(best_offer.historical_avg_gbp)

                db.record_price(
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
                print(
                    f"{item.name}: best £{analysis.total_gbp:.2f} at {best_offer.retailer.display_name} "
                    f"({analysis.discount_pct:.1f}% off RRP £{rrp:.2f}) -> {analysis.verdict}"
                )

                hit_target = item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
                hit_discount = analysis.discount_pct >= item.min_discount_pct
                if (hit_target or hit_discount) and db.should_alert(item, analysis.total_gbp, cooldown_hours):
                    series = list(reversed([h.total_gbp for h in db.get_price_history(item.id, limit=30)]))
                    chart_bytes = generate_price_chart(series, rrp) if len(series) >= 2 else None
                    payload = build_discord_payload(
                        user_id,
                        item,
                        analysis,
                        best_offer.retailer,
                        best_offer.shipping_note,
                        offer_url=best_offer.url,
                        is_all_time_low=is_all_time_low,
                        has_chart=chart_bytes is not None,
                    )
                    await send_discord_message(
                        session,
                        payload,
                        webhook_url=webhook_url,
                        bot_token=bot_token,
                        channel_id=channel_id,
                        chart_png=chart_bytes,
                    )
                    db.mark_alerted(item.id, analysis.total_gbp)
                    print(f"Alert sent for {item.name}!")

            if force_weekly or db.should_send_weekly_summary():
                summary_payload = build_weekly_summary_payload(user_id, db, db.list_items())
                await send_discord_message(
                    session,
                    summary_payload,
                    webhook_url=webhook_url,
                    bot_token=bot_token,
                    channel_id=channel_id,
                )
                db.mark_weekly_summary_sent()
                print("Sent weekly summary!")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot UK LEGO price check for cPanel Cron Jobs")
    parser.add_argument("--test", action="store_true", help="Send a simulated sale alert to verify Discord setup")
    parser.add_argument("--weekly", action="store_true", help="Force sending the weekly summary now")
    args = parser.parse_args()
    asyncio.run(run_once(test_mode=args.test, force_weekly=args.weekly))


if __name__ == "__main__":
    main()
