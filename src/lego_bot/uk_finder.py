from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from .scraper import DEFAULT_HEADERS, fetch_price_for_url, parse_price_from_html
from .shipping import UKRetailer, estimate_uk_shipping

OUT_OF_STOCK_PHRASES = (
    "out of stock",
    "currently unavailable",
    "sold out",
    "temporarily out of stock",
    "no longer available",
)


@dataclass(frozen=True)
class SetMetadata:
    name: str | None
    rrp_gbp: float | None
    is_retiring: bool
    image_url: str | None
    source: str


@dataclass(frozen=True)
class UKOffer:
    retailer: UKRetailer
    url: str
    item_price_gbp: float
    shipping_gbp: float
    total_gbp: float
    shipping_note: str
    historical_low_gbp: float | None = None
    historical_avg_gbp: float | None = None


def default_box_art_url(set_number: str | None) -> str | None:
    if not set_number:
        return None
    clean = set_number.strip().split("-")[0]
    if not clean.isdigit():
        return None
    return f"https://images.brickset.com/sets/images/{clean}-1.jpg"


async def lookup_set_metadata(
    session: aiohttp.ClientSession,
    set_number: str,
    *,
    brickset_api_key: str | None = None,
) -> SetMetadata:
    """Auto-discover set name, UK RRP (£), box art image, and retiring status."""
    clean_num = set_number.strip().split("-")[0]

    if brickset_api_key:
        meta = await _lookup_brickset(session, clean_num, brickset_api_key)
        if meta.rrp_gbp is not None or meta.name is not None:
            return meta

    lego_url = f"https://www.lego.com/en-gb/product/{clean_num}"
    try:
        async with session.get(
            lego_url, headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                quote = parse_price_from_html(html, lego_url)
                retiring = "retiring soon" in html.lower()

                og_title = soup.find("meta", attrs={"property": "og:title"})
                og_image = soup.find("meta", attrs={"property": "og:image"})
                name = og_title["content"].split("|")[0].strip() if og_title and og_title.get("content") else None
                image_url = og_image["content"].strip() if og_image and og_image.get("content") else default_box_art_url(clean_num)

                if quote is not None or name is not None:
                    return SetMetadata(
                        name=name,
                        rrp_gbp=quote.price_gbp if quote else None,
                        is_retiring=retiring,
                        image_url=image_url,
                        source="lego_uk",
                    )
    except Exception:
        pass

    # Fallback 2: public Brickset set page (great for set name + UK RRP £)
    public_meta = await _lookup_brickset_public(session, clean_num)
    if public_meta.name or public_meta.rrp_gbp:
        return public_meta

    return SetMetadata(
        name=None,
        rrp_gbp=None,
        is_retiring=False,
        image_url=default_box_art_url(clean_num),
        source="fallback",
    )


async def _lookup_brickset_public(
    session: aiohttp.ClientSession,
    set_number: str,
) -> SetMetadata:
    url = f"https://brickset.com/sets/{set_number}-1"
    try:
        async with session.get(url, headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_web_err")
            html = await resp.text()
    except Exception:
        return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_web_err")

    soup = BeautifulSoup(html, "html.parser")
    name: str | None = None
    h1 = soup.find("h1")
    if h1:
        raw_title = h1.get_text(" ", strip=True)
        # Brickset h1 is often "10316-1: The Lord of the Rings: Rivendell"
        if ":" in raw_title and raw_title.split(":", 1)[0].strip().startswith(set_number):
            name = raw_title.split(":", 1)[1].strip()
        else:
            name = raw_title

    text = soup.get_text(" ", strip=True)
    rrp_gbp: float | None = None
    m = re.search(r"RRP[^£]{0,30}£\s*(\d{1,4}(?:\.\d{2})?)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"£\s*(\d{1,4}\.\d{2})", text)
    if m:
        rrp_gbp = round(float(m.group(1)), 2)

    retiring = "retiring soon" in text.lower() or "retired" in text.lower()
    return SetMetadata(
        name=name,
        rrp_gbp=rrp_gbp,
        is_retiring=retiring,
        image_url=default_box_art_url(set_number),
        source="brickset_web",
    )


async def _lookup_brickset(
    session: aiohttp.ClientSession,
    set_number: str,
    api_key: str,
) -> SetMetadata:
    url = "https://brickset.com/api/v3.asmx/getSets"
    params = {
        "apiKey": api_key,
        "userHash": "",
        "params": json.dumps({"setNumber": f"{set_number}-1"}),
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_error")
            data = await resp.json()
    except Exception:
        return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_error")

    sets = data.get("sets") or []
    if not sets:
        return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_empty")

    s = sets[0]
    uk_data = s.get("LEGOCom", {}).get("UK", {})
    rrp = uk_data.get("retailPrice")
    exit_date_str = uk_data.get("dateLastAvailable") or s.get("exitDate")
    image_url = s.get("image", {}).get("imageURL") or default_box_art_url(set_number)

    is_retiring = False
    if exit_date_str:
        try:
            exit_dt = datetime.fromisoformat(str(exit_date_str).replace("Z", "+00:00"))
            days_left = (exit_dt - datetime.now(timezone.utc)).days
            if days_left <= 180:
                is_retiring = True
        except ValueError:
            pass

    return SetMetadata(
        name=s.get("name"),
        rrp_gbp=round(float(rrp), 2) if rrp else None,
        is_retiring=is_retiring,
        image_url=image_url,
        source="brickset",
    )


def build_uk_search_urls(name: str, set_number: str | None) -> list[tuple[UKRetailer, str]]:
    """Build candidate UK retailer URLs for a LEGO set number."""
    query = quote_plus(f"LEGO {set_number}" if set_number else name)
    urls: list[tuple[UKRetailer, str]] = []
    if set_number:
        clean_num = set_number.strip().split("-")[0]
        urls.append((UKRetailer.LEGO_UK, f"https://www.lego.com/en-gb/product/{clean_num}"))
    urls.extend(
        [
            (UKRetailer.ARGOS, f"https://www.argos.co.uk/search/{query}/"),
            (UKRetailer.SMYTHS, f"https://www.smythstoys.com/uk/en-gb/search/?text={query}"),
            (UKRetailer.JOHN_LEWIS, f"https://www.johnlewis.com/search?search-term={query}"),
            (UKRetailer.AMAZON_UK, f"https://www.amazon.co.uk/s?k={query}"),
        ]
    )
    return urls


def extract_first_gbp_price_from_search(html: str, set_number: str | None) -> float | None:
    """Extract an in-stock GBP price from a UK retailer search page (filters out-of-stock)."""
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data.get("itemListElement") if isinstance(data, dict) else None
        if isinstance(items, list):
            for entry in items:
                item = entry.get("item", entry) if isinstance(entry, dict) else None
                if not isinstance(item, dict):
                    continue
                if set_number and set_number not in json.dumps(item):
                    continue
                offers = item.get("offers")
                if isinstance(offers, dict) and str(offers.get("priceCurrency", "GBP")).upper() == "GBP":
                    availability = str(offers.get("availability", "")).lower()
                    if "outofstock" in availability:
                        continue
                    try:
                        val = float(offers["price"])
                        if val > 0:
                            return round(val, 2)
                    except (KeyError, TypeError, ValueError):
                        pass

    text = soup.get_text(" ", strip=True)
    if set_number and set_number in text:
        idx = text.find(set_number)
        snippet = text[max(0, idx - 100) : idx + 300]
        lower_snip = snippet.lower()
        if any(phrase in lower_snip for phrase in OUT_OF_STOCK_PHRASES):
            return None
        match = re.search(r"£\s*(\d{1,4}(?:\.\d{2})?)", snippet)
        if match:
            return round(float(match.group(1)), 2)

    return None


async def find_best_uk_offer(
    session: aiohttp.ClientSession,
    *,
    name: str,
    set_number: str | None,
    direct_url: str | None = None,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
    keepa_api_key: str | None = None,
) -> tuple[UKOffer | None, list[str]]:
    """Check direct URL or multiple UK retailers and return the cheapest in-stock landed offer."""
    offers: list[UKOffer] = []
    notes: list[str] = []

    if direct_url:
        res = await fetch_price_for_url(session, direct_url, keepa_api_key=keepa_api_key)
        if res.ok and res.quote is not None:
            if not res.quote.in_stock:
                notes.append(f"Direct URL ({res.quote.retailer.display_name}): out of stock (skipped)")
            else:
                ship = estimate_uk_shipping(
                    res.quote.price_gbp,
                    res.quote.retailer,
                    has_amazon_prime=has_amazon_prime,
                    prefer_click_and_collect=prefer_click_and_collect,
                )
                offers.append(
                    UKOffer(
                        retailer=res.quote.retailer,
                        url=direct_url,
                        item_price_gbp=res.quote.price_gbp,
                        shipping_gbp=ship.shipping_gbp,
                        total_gbp=ship.total_gbp,
                        shipping_note=ship.note,
                        historical_low_gbp=res.quote.historical_low_gbp,
                        historical_avg_gbp=res.quote.historical_avg_gbp,
                    )
                )
        else:
            notes.append(f"Direct URL: {res.error}")

    for retailer, url in build_uk_search_urls(name, set_number):
        if retailer == UKRetailer.LEGO_UK:
            res = await fetch_price_for_url(session, url, keepa_api_key=keepa_api_key)
            if res.ok and res.quote is not None:
                if not res.quote.in_stock:
                    notes.append("LEGO.com UK: out of stock (skipped)")
                else:
                    ship = estimate_uk_shipping(
                        res.quote.price_gbp,
                        retailer,
                        has_amazon_prime=has_amazon_prime,
                        prefer_click_and_collect=prefer_click_and_collect,
                    )
                    offers.append(
                        UKOffer(
                            retailer=retailer,
                            url=url,
                            item_price_gbp=res.quote.price_gbp,
                            shipping_gbp=ship.shipping_gbp,
                            total_gbp=ship.total_gbp,
                            shipping_note=ship.note,
                        )
                    )
            else:
                notes.append(f"{retailer.display_name}: {res.error}")
            continue

        try:
            async with session.get(
                url, headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                if resp.status != 200:
                    notes.append(f"{retailer.display_name}: HTTP {resp.status}")
                    continue
                html = await resp.text()
        except Exception as exc:
            notes.append(f"{retailer.display_name}: {exc}")
            continue

        price = extract_first_gbp_price_from_search(html, set_number)
        if price is None:
            notes.append(f"{retailer.display_name}: no in-stock GBP price found")
            continue

        ship = estimate_uk_shipping(
            price,
            retailer,
            has_amazon_prime=has_amazon_prime,
            prefer_click_and_collect=prefer_click_and_collect,
        )
        offers.append(
            UKOffer(
                retailer=retailer,
                url=url,
                item_price_gbp=price,
                shipping_gbp=ship.shipping_gbp,
                total_gbp=ship.total_gbp,
                shipping_note=ship.note,
            )
        )

    if not offers:
        return None, notes

    best = min(offers, key=lambda o: o.total_gbp)
    return best, notes
