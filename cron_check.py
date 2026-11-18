from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from src.lego_bot.db import PriceDatabase, TrackedItem
from src.lego_bot.predictor import PriceAnalysis, analyze_price
from src.lego_bot.scraper import fetch_price_for_url
from src.lego_bot.shipping import UKRetailer, detect_retailer_from_url, estimate_uk_shipping


def load_wishlist(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(
            f"Wishlist file not found: {path}. Copy wishlist.example.json to wishlist.json and edit it."
        )
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("wishlist.json must be a JSON array of LEGO items")
    return data


def sync_wishlist_to_db(db: PriceDatabase, entries: list[dict[str, Any]]) -> list[TrackedItem]:
    """Ensure every item in wishlist.json exists in SQLite and return them."""
    existing = db.list_items()
    by_key = {(i.name, i.url): i for i in existing}
    synced: list[TrackedItem] = []

    for entry in entries:
        name = str(entry["name"]).strip()
        url = str(entry.get("url") or "").strip() or None
        key = (name, url)
        if key in by_key:
            synced.append(by_key[key])
            continue

        retailer = detect_retailer_from_url(url)
        item = db.add_item(
            name=name,
            rrp_gbp=float(entry["rrp_gbp"]),
            set_number=str(entry["set_number"]) if entry.get("set_number") else None,
            target_price_gbp=float(entry["target_price_gbp"]) if entry.get("target_price_gbp") is not None else None,
            min_discount_pct=float(entry.get("min_discount_pct", 15.0)),
            url=url,
            retailer=retailer.value,
            is_retiring=bool(entry.get("is_retiring", False)),
        )
        synced.append(item)
    return synced


def build_discord_payload(
    user_id: str,
    item: TrackedItem,
    analysis: PriceAnalysis,
    retailer: UKRetailer,
    shipping_note: str,
    *,
    simulated: bool = False,
) -> dict[str, Any]:
    if "BUY NOW" in analysis.verdict:
        color = 0x2ECC71
    elif "GOOD DEAL" in analysis.verdict or "BUY SOON" in analysis.verdict:
        color = 0xF1C40F
    else:
        color = 0xE67E22

    prefix = "[TEST] " if simulated else ""
    set_tag = f" (#{item.set_number})" if item.set_number else ""

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
    if analysis.historical_low_gbp is not None:
        history_lines.append(f"**Tracked Low:** £{analysis.historical_low_gbp:.2f}")
    if analysis.historical_avg_gbp is not None:
        history_lines.append(f"**Tracked Avg:** £{analysis.historical_avg_gbp:.2f}")
    if item.is_retiring:
        history_lines.append("**Status:** Retiring Soon")
    if history_lines:
        fields.append({"name": "Price Context", "value": "\n".join(history_lines), "inline": False})

    fields.append({"name": f"Verdict: {analysis.verdict}", "value": analysis.forecast, "inline": False})
    if item.url:
        fields.append({"name": "Link", "value": f"[View on {retailer.display_name}]({item.url})", "inline": False})

    embed: dict[str, Any] = {
        "title": f"{prefix}LEGO Deal Alert: {item.name}{set_tag}",
        "description": f"Spotted on sale at **{retailer.display_name}**!",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Wishlist ID #{item.id} • Prices in GBP (£) including UK shipping"},
    }
    if item.url:
        embed["url"] = item.url

    return {
        "content": f"<@{user_id}> LEGO sale spotted!",
        "embeds": [embed],
    }


async def send_discord_message(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
    *,
    webhook_url: str | None = None,
    bot_token: str | None = None,
    channel_id: str | None = None,
) -> None:
    if webhook_url:
        async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Discord webhook failed ({resp.status}): {body}")
        return

    if bot_token and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Discord REST API failed ({resp.status}): {body}")
        return

    raise ValueError("Set DISCORD_WEBHOOK_URL (recommended for cPanel) or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID in .env")


async def run_once(*, test_mode: bool = False) -> None:
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
    keepa_key = os.getenv("KEEPA_API_KEY", "").strip() or None

    entries = load_wishlist(wishlist_path)
    db = PriceDatabase(db_path)

    try:
        items = sync_wishlist_to_db(db, entries)
        if not items:
            print("Wishlist is empty.")
            return

        async with aiohttp.ClientSession() as session:
            if test_mode:
                first = items[0]
                retailer = detect_retailer_from_url(first.url)
                sim_price = round(first.rrp_gbp * 0.72, 2)
                shipping = estimate_uk_shipping(
                    sim_price, retailer, has_amazon_prime=has_prime, prefer_click_and_collect=prefer_cc
                )
                analysis = analyze_price(
                    rrp_gbp=first.rrp_gbp,
                    item_price_gbp=sim_price,
                    shipping_gbp=shipping.shipping_gbp,
                    historical_totals_gbp=[first.rrp_gbp, round(first.rrp_gbp * 0.85, 2)],
                    is_retiring=first.is_retiring,
                )
                payload = build_discord_payload(
                    user_id, first, analysis, retailer, shipping.note, simulated=True
                )
                await send_discord_message(
                    session, payload, webhook_url=webhook_url, bot_token=bot_token, channel_id=channel_id
                )
                print(f"Sent test alert for {first.name} (£{analysis.total_gbp:.2f})")
                return

            for item in items:
                if not item.url:
                    print(f"Skipping {item.name}: no URL")
                    continue

                res = await fetch_price_for_url(session, item.url, keepa_api_key=keepa_key)
                if not res.ok or res.quote is None:
                    print(f"Failed {item.name}: {res.error}")
                    continue

                quote = res.quote
                shipping = estimate_uk_shipping(
                    quote.price_gbp,
                    quote.retailer,
                    has_amazon_prime=has_prime,
                    prefer_click_and_collect=prefer_cc,
                )
                history = db.get_price_history(item.id, limit=90)
                hist_totals = [h.total_gbp for h in history]
                if quote.historical_low_gbp is not None:
                    hist_totals.append(quote.historical_low_gbp)
                if quote.historical_avg_gbp is not None:
                    hist_totals.append(quote.historical_avg_gbp)

                db.record_price(
                    item.id, quote.price_gbp, shipping.shipping_gbp, shipping.total_gbp, quote.retailer.value
                )
                analysis = analyze_price(
                    rrp_gbp=item.rrp_gbp,
                    item_price_gbp=quote.price_gbp,
                    shipping_gbp=shipping.shipping_gbp,
                    historical_totals_gbp=hist_totals,
                    is_retiring=item.is_retiring,
                )
                print(f"{item.name}: £{analysis.total_gbp:.2f} ({analysis.discount_pct:.1f}% off) -> {analysis.verdict}")

                hit_target = item.target_price_gbp is not None and analysis.total_gbp <= item.target_price_gbp
                hit_discount = analysis.discount_pct >= item.min_discount_pct
                if (hit_target or hit_discount) and db.should_alert(item, analysis.total_gbp, cooldown_hours):
                    payload = build_discord_payload(user_id, item, analysis, quote.retailer, shipping.note)
                    await send_discord_message(
                        session, payload, webhook_url=webhook_url, bot_token=bot_token, channel_id=channel_id
                    )
                    db.mark_alerted(item.id, analysis.total_gbp)
                    print(f"Alert sent for {item.name}!")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot UK LEGO price check for cPanel Cron Jobs")
    parser.add_argument("--test", action="store_true", help="Send a simulated sale alert to verify Discord setup")
    args = parser.parse_args()
    asyncio.run(run_once(test_mode=args.test))


if __name__ == "__main__":
    main()
