from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from .shipping import UKRetailer, detect_retailer_from_url


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class PriceQuote:
    price_gbp: float
    retailer: UKRetailer
    in_stock: bool = True
    source: str = "html"
    historical_low_gbp: float | None = None
    historical_avg_gbp: float | None = None


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    quote: PriceQuote | None = None
    error: str | None = None


def extract_asin_from_amazon_url(url: str) -> str | None:
    match = ASIN_RE.search(url)
    return match.group(1).upper() if match else None


def parse_price_from_html(html: str, url: str) -> PriceQuote | None:
    """Extract GBP price from schema.org JSON-LD or OpenGraph meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    retailer = detect_retailer_from_url(url)

    # 1. Try JSON-LD Product / Offer blocks
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        quote = _extract_from_jsonld(data, retailer)
        if quote is not None:
            return quote

    # 2. Fallback: OpenGraph / product meta tags
    amount_meta = soup.find("meta", attrs={"property": "product:price:amount"}) or soup.find(
        "meta", attrs={"property": "og:price:amount"}
    )
    currency_meta = soup.find("meta", attrs={"property": "product:price:currency"}) or soup.find(
        "meta", attrs={"property": "og:price:currency"}
    )

    if amount_meta and amount_meta.get("content"):
        currency = (currency_meta.get("content") if currency_meta else "GBP") or "GBP"
        if currency.upper() == "GBP":
            parsed = _parse_float(amount_meta["content"])
            if parsed is not None and parsed > 0:
                return PriceQuote(price_gbp=parsed, retailer=retailer, source="meta")

    return None


def _extract_from_jsonld(data: Any, retailer: UKRetailer) -> PriceQuote | None:
    nodes: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            nodes.extend(n for n in data["@graph"] if isinstance(n, dict))
        nodes.append(data)
    elif isinstance(data, list):
        nodes.extend(n for n in data if isinstance(n, dict))

    for node in nodes:
        offers = node.get("offers")
        if not offers and node.get("@type") == "Offer":
            offers = node
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            continue

        currency = str(offers.get("priceCurrency", "GBP")).upper()
        if currency != "GBP":
            continue

        raw_price = offers.get("price") or offers.get("lowPrice")
        price = _parse_float(str(raw_price)) if raw_price is not None else None
        if price is None or price <= 0:
            continue

        availability = str(offers.get("availability", "")).lower()
        in_stock = "outofstock" not in availability
        return PriceQuote(
            price_gbp=price,
            retailer=retailer,
            in_stock=in_stock,
            source="json-ld",
        )

    return None


def _parse_float(value: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
    if not cleaned:
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


async def fetch_amazon_uk_keepa(
    session: aiohttp.ClientSession,
    asin: str,
    api_key: str,
) -> FetchResult:
    """Fetch Amazon UK (domain=2) price and stats via Keepa API."""
    endpoint = "https://api.keepa.com/product"
    params = {"key": api_key, "domain": "2", "asin": asin, "stats": "90"}
    try:
        async with session.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return FetchResult(ok=False, error=f"Keepa API returned HTTP {resp.status}")
            payload = await resp.json()
    except Exception as exc:
        return FetchResult(ok=False, error=f"Keepa request failed: {exc}")

    products = payload.get("products") or []
    if not products:
        return FetchResult(ok=False, error="Product not found on Keepa UK")

    stats = products[0].get("stats") or {}
    current = stats.get("current") or []
    # Keepa index 0 = Amazon price, index 1 = New 3rd-party price (in pence)
    pence = current[0] if len(current) > 0 and current[0] > 0 else (
        current[1] if len(current) > 1 and current[1] > 0 else -1
    )
    if pence <= 0:
        return FetchResult(ok=False, error="No active Amazon UK price in Keepa")

    avg90 = stats.get("avg90") or []
    min_stats = stats.get("min") or []
    avg_gbp = round(avg90[0] / 100, 2) if len(avg90) > 0 and avg90[0] > 0 else None
    low_gbp = (
        round(min_stats[0][1] / 100, 2)
        if len(min_stats) > 0 and isinstance(min_stats[0], list) and len(min_stats[0]) > 1 and min_stats[0][1] > 0
        else None
    )

    return FetchResult(
        ok=True,
        quote=PriceQuote(
            price_gbp=round(pence / 100, 2),
            retailer=UKRetailer.AMAZON_UK,
            in_stock=True,
            source="keepa",
            historical_low_gbp=low_gbp,
            historical_avg_gbp=avg_gbp,
        ),
    )


async def fetch_price_for_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    keepa_api_key: str | None = None,
) -> FetchResult:
    """Fetch current GBP price for a UK retailer URL."""
    retailer = detect_retailer_from_url(url)

    if retailer == UKRetailer.AMAZON_UK and keepa_api_key:
        asin = extract_asin_from_amazon_url(url)
        if asin:
            keepa_res = await fetch_amazon_uk_keepa(session, asin, keepa_api_key)
            if keepa_res.ok:
                return keepa_res

    try:
        async with session.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (403, 429, 503):
                return FetchResult(
                    ok=False,
                    error=f"{retailer.display_name} blocked automated request (HTTP {resp.status})",
                )
            if resp.status != 200:
                return FetchResult(
                    ok=False,
                    error=f"{retailer.display_name} returned HTTP {resp.status}",
                )
            html = await resp.text()
    except Exception as exc:
        return FetchResult(ok=False, error=f"Request error: {exc}")

    quote = parse_price_from_html(html, url)
    if quote is None:
        return FetchResult(
            ok=False,
            error=f"Could not find GBP price metadata on {retailer.display_name} page",
        )
    return FetchResult(ok=True, quote=quote)
