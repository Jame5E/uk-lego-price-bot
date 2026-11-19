from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from .deal_filter import is_valid_lego_set_listing
from .scraper import DEFAULT_HEADERS, fetch_price_for_url, parse_price_from_html
from .shipping import (
    APPROVED_UK_RETAILERS,
    UKRetailer,
    estimate_uk_shipping,
    match_approved_uk_retailer,
)

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    CurlAsyncSession = None  # type: ignore[assignment]
    HAS_CURL_CFFI = False

OUT_OF_STOCK_PHRASES = (
    "out of stock",
    "currently unavailable",
    "sold out",
    "temporarily out of stock",
    "no longer available",
)

GENERIC_BRICKSET_SUBTHEMES = {
    "licensed",
    "miscellaneous",
    "seasonal",
    "promotional",
    "general",
    "retail",
    "special",
}

# Per-source circuit breaker for any direct store that returns repeated 403/429
_SOURCE_COOLDOWN_TIERS: tuple[float, ...] = (180.0, 600.0, 900.0)
_SOURCE_OUTAGE_ALERT_SECONDS = 7200.0
_SOURCE_FAIL_STREAK: dict[str, int] = {}
_SOURCE_COOLDOWN_TIER: dict[str, int] = {}
_SOURCE_COOLDOWN_UNTIL: dict[str, float] = {}
_SOURCE_FIRST_FAIL_AT: dict[str, float] = {}
_SOURCE_OUTAGE_ALERTED: set[str] = set()
_COOLDOWN_EXPIRY_PENDING: set[str] = set()

_SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "amazon_uk": "Amazon UK",
    "john_lewis": "John Lewis",
    "argos": "Argos",
    "disney_store": "Disney Store UK",
    "lego_uk": "LEGO.com UK",
}


def _is_source_cooled_down(source_key: str) -> bool:
    until = _SOURCE_COOLDOWN_UNTIL.get(source_key, 0.0)
    if until > time.monotonic():
        return True
    if source_key in _SOURCE_COOLDOWN_UNTIL:
        _SOURCE_COOLDOWN_UNTIL.pop(source_key, None)
        _SOURCE_FAIL_STREAK.pop(source_key, None)
        _COOLDOWN_EXPIRY_PENDING.add(source_key)
    return False


def _record_source_block(source_key: str) -> None:
    now = time.monotonic()
    _SOURCE_FIRST_FAIL_AT.setdefault(source_key, now)
    _rotate_curl_profile()
    streak = _SOURCE_FAIL_STREAK.get(source_key, 0) + 1
    _SOURCE_FAIL_STREAK[source_key] = streak
    if streak >= 3:
        tier = _SOURCE_COOLDOWN_TIER.get(source_key, 0)
        duration = _SOURCE_COOLDOWN_TIERS[min(tier, len(_SOURCE_COOLDOWN_TIERS) - 1)]
        _SOURCE_COOLDOWN_UNTIL[source_key] = now + duration
        _SOURCE_COOLDOWN_TIER[source_key] = tier + 1
        _SOURCE_FAIL_STREAK[source_key] = 0
        _COOLDOWN_EXPIRY_PENDING.discard(source_key)


def _record_source_ok(source_key: str) -> None:
    _SOURCE_FAIL_STREAK.pop(source_key, None)
    _SOURCE_COOLDOWN_TIER.pop(source_key, None)
    _SOURCE_COOLDOWN_UNTIL.pop(source_key, None)
    _SOURCE_FIRST_FAIL_AT.pop(source_key, None)
    _SOURCE_OUTAGE_ALERTED.discard(source_key)
    _COOLDOWN_EXPIRY_PENDING.discard(source_key)


def pop_long_outage_sources(min_outage_seconds: float = _SOURCE_OUTAGE_ALERT_SECONDS) -> list[str]:
    """Return source keys that have been continuously failing for >= min_outage_seconds (once per outage)."""
    now = time.monotonic()
    outages: list[str] = []
    for key, started_at in list(_SOURCE_FIRST_FAIL_AT.items()):
        if key not in _SOURCE_OUTAGE_ALERTED and (now - started_at) >= min_outage_seconds:
            _SOURCE_OUTAGE_ALERTED.add(key)
            outages.append(key)
    return outages


def pop_finished_cooldowns() -> list[str]:
    """Return human-readable store names whose cooldowns just expired (once per cooldown)."""
    now = time.monotonic()
    for key, until in list(_SOURCE_COOLDOWN_UNTIL.items()):
        if now >= until:
            _SOURCE_COOLDOWN_UNTIL.pop(key, None)
            _SOURCE_FAIL_STREAK.pop(key, None)
            _COOLDOWN_EXPIRY_PENDING.add(key)
    if not _COOLDOWN_EXPIRY_PENDING:
        return []
    finished = sorted(_COOLDOWN_EXPIRY_PENDING)
    _COOLDOWN_EXPIRY_PENDING.clear()
    return [_SOURCE_DISPLAY_NAMES.get(k, k) for k in finished]


def clear_cooldowns() -> list[str]:
    """Manually reset all active store cooldowns/streaks and return cleared display names."""
    now = time.monotonic()
    active_keys = sorted(k for k, until in _SOURCE_COOLDOWN_UNTIL.items() if until > now)
    _SOURCE_COOLDOWN_UNTIL.clear()
    _SOURCE_FAIL_STREAK.clear()
    _SOURCE_COOLDOWN_TIER.clear()
    _SOURCE_FIRST_FAIL_AT.clear()
    _SOURCE_OUTAGE_ALERTED.clear()
    _COOLDOWN_EXPIRY_PENDING.clear()
    return [_SOURCE_DISPLAY_NAMES.get(k, k) for k in active_keys]


def export_cooldown_state() -> dict[str, dict[str, float]]:
    """Serialize active cooldowns + escalation tiers using wall-clock time (for SQLite persistence)."""
    mono_now = time.monotonic()
    wall_now = time.time()
    state: dict[str, dict[str, float]] = {}
    for key in set(_SOURCE_COOLDOWN_TIER) | set(_SOURCE_COOLDOWN_UNTIL):
        entry: dict[str, float] = {"tier": float(_SOURCE_COOLDOWN_TIER.get(key, 0))}
        until = _SOURCE_COOLDOWN_UNTIL.get(key)
        if until is not None and until > mono_now:
            entry["until_epoch"] = wall_now + (until - mono_now)
        state[key] = entry
    return state


def import_cooldown_state(state: dict[str, Any] | None) -> None:
    """Restore cooldowns saved by export_cooldown_state (expired cooldowns are dropped silently)."""
    if not isinstance(state, dict):
        return
    mono_now = time.monotonic()
    wall_now = time.time()
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        try:
            tier = int(entry.get("tier", 0))
        except (TypeError, ValueError):
            tier = 0
        if tier > 0:
            _SOURCE_COOLDOWN_TIER[key] = tier
        until_epoch = entry.get("until_epoch")
        if isinstance(until_epoch, (int, float)) and until_epoch > wall_now:
            _SOURCE_COOLDOWN_UNTIL[key] = mono_now + (float(until_epoch) - wall_now)


# In-memory price cache (30 minutes) and Amazon /dp/{asin} URL cache
_PRICE_CACHE_TTL_SECONDS = 1800.0
_PRICE_CACHE: dict[str, tuple[float, "UKOffer", list[str]]] = {}
_AMAZON_DP_URL_CACHE: dict[str, str] = {}


def get_source_health() -> dict[str, int]:
    """Return remaining cooldown seconds for any source currently cooled down."""
    now = time.monotonic()
    active: dict[str, int] = {}
    for key, until in list(_SOURCE_COOLDOWN_UNTIL.items()):
        if until > now:
            active[key] = int(until - now)
        else:
            _SOURCE_COOLDOWN_UNTIL.pop(key, None)
            _SOURCE_FAIL_STREAK.pop(key, None)
            _COOLDOWN_EXPIRY_PENDING.add(key)
    return active


def get_price_cache_stats() -> tuple[int, int]:
    """Return (active_cached_entries, ttl_seconds)."""
    now = time.monotonic()
    for key, (ts, _, _) in list(_PRICE_CACHE.items()):
        if now - ts >= _PRICE_CACHE_TTL_SECONDS:
            _PRICE_CACHE.pop(key, None)
    return len(_PRICE_CACHE), int(_PRICE_CACHE_TTL_SECONDS)


def clear_price_cache() -> int:
    """Clear all cached price entries and Amazon ASIN URLs, returning removed price entries."""
    count = len(_PRICE_CACHE)
    _PRICE_CACHE.clear()
    _AMAZON_DP_URL_CACHE.clear()
    return count


def load_amazon_dp_urls(urls: dict[str, str]) -> None:
    """Preload persisted Amazon /dp/{asin} URLs from SQLite on bot startup."""
    for set_num, url in urls.items():
        if set_num and url:
            _AMAZON_DP_URL_CACHE[set_num.strip().split("-")[0]] = url


@dataclass(frozen=True)
class SetMetadata:
    name: str | None
    rrp_gbp: float | None
    is_retiring: bool
    image_url: str | None
    source: str
    category: str | None = None
    is_retired: bool = False


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


def clean_set_display_name(name: str | None, set_number: str | None = None) -> str | None:
    """Clean redundant set numbers or generic Brickset prefixes like 'Licensed: 42171 ...'."""
    if not name:
        return None
    cleaned = name.strip()
    if ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        if prefix.strip().lower() in GENERIC_BRICKSET_SUBTHEMES:
            cleaned = rest.strip()
    if set_number:
        clean_num = set_number.strip().split("-")[0]
        cleaned = re.sub(rf"^(?:lego\s+)?#?{re.escape(clean_num)}(?:-\d+)?\s*[:\-–]?\s*", "", cleaned, flags=re.I)
    return cleaned.strip() or name.strip()


def _format_category(theme: str | None) -> str | None:
    if not theme:
        return None
    t = theme.strip()
    return t if t.lower().startswith("lego ") else f"LEGO {t}"


def _enrich_name_with_subtheme(name: str | None, subtheme: str | None, set_number: str | None = None) -> str | None:
    base = clean_set_display_name(name, set_number)
    if not base:
        return None
    if subtheme and subtheme.strip():
        sub = subtheme.strip()
        if sub.lower() not in GENERIC_BRICKSET_SUBTHEMES and sub.lower() not in base.lower():
            return f"{sub}: {base}"
    return base


def _parse_exit_date_status(exit_date_text: str) -> tuple[bool, bool]:
    """Return (is_retiring, is_retired) from a Brickset exit date or 'Launch - Exit' range string."""
    raw = exit_date_text.strip()
    if not raw:
        return False, False
    # Brickset's "Launch/exit" field is often formatted as "01 Mar 25 - 31 Dec 26"
    for sep in (" - ", " – ", " to "):
        if sep in raw:
            raw = raw.split(sep)[-1].strip()
            break
    if not raw or raw.lower() in ("tbd", "tba", "?", "n/a", "unknown"):
        return False, False
    for fmt in (
        "%d %b %Y",
        "%d %b %y",
        "%d %B %Y",
        "%d %B %y",
        "%b %Y",
        "%b %y",
        "%B %Y",
        "%B %y",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            days_left = (dt - datetime.now(timezone.utc)).days
            if days_left < 0:
                return False, True
            if days_left <= 180:
                return True, False
            return False, False
        except ValueError:
            continue
    return False, False


_CURL_SESSIONS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()
_CURL_PROFILES: tuple[str, ...] = ("chrome124", "chrome120", "safari17_0", "edge101", "chrome")
_CURL_PROFILE_IDX = 0


def _rotate_curl_profile() -> None:
    """Advance to the next browser TLS fingerprint for the next session without aborting in-flight requests."""
    global _CURL_PROFILE_IDX
    _CURL_PROFILE_IDX = (_CURL_PROFILE_IDX + 1) % len(_CURL_PROFILES)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _CURL_SESSIONS.pop(loop, None)


def _get_curl_session() -> Any:
    assert CurlAsyncSession is not None
    loop = asyncio.get_running_loop()
    csession = _CURL_SESSIONS.get(loop)
    if csession is None:
        profile = _CURL_PROFILES[_CURL_PROFILE_IDX % len(_CURL_PROFILES)]
        csession = CurlAsyncSession(impersonate=profile)
        _CURL_SESSIONS[loop] = csession
    return csession


async def close_http_sessions() -> None:
    """Close any shared CurlAsyncSession bound to the current event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    csession = _CURL_SESSIONS.pop(loop, None)
    if csession is not None:
        try:
            await csession.close()
        except Exception:
            pass


async def _http_get_html(
    session: aiohttp.ClientSession | None,
    url: str,
    *,
    cookies: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, str]:
    """Fetch HTML using shared Chrome TLS impersonation (curl_cffi) when available, else aiohttp."""
    uk_headers = {
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.co.uk/",
        "Upgrade-Insecure-Requests": "1",
    }
    if HAS_CURL_CFFI and CurlAsyncSession is not None:
        csession = _get_curl_session()
        try:
            resp = await csession.get(
                url,
                headers=uk_headers,
                cookies=cookies,
                timeout=timeout_seconds,
                allow_redirects=True,
            )
            return int(resp.status_code), str(resp.text)
        except Exception:
            # Discard and recreate session once if the underlying connection went stale
            loop = asyncio.get_running_loop()
            stale = _CURL_SESSIONS.pop(loop, None)
            if stale is not None:
                try:
                    await stale.close()
                except Exception:
                    pass
            raise

    if session is None:
        raise RuntimeError("No HTTP session available")
    merged_headers = {
        **DEFAULT_HEADERS,
        **uk_headers,
    }
    async with session.get(
        url,
        headers=merged_headers,
        cookies=cookies,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    ) as resp:
        return resp.status, await resp.text()


def _parse_lego_uk_retiring(html: str) -> bool:
    """Detect LEGO.com UK 'Retiring soon' badge or JSON availability flag."""
    if not html:
        return False
    lower = html.lower()
    if '"retiringsoon":true' in lower or '"availabilitystatus":"retiringsoon"' in lower:
        return True
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(["span", "div", "p", "strong"], limit=1500):
        txt = el.get_text(" ", strip=True).lower()
        if txt in ("retiring soon", "retiring soon!"):
            soup.decompose()
            return True
    soup.decompose()
    return False


async def _lookup_lego_uk_retiring(
    session: aiohttp.ClientSession | None,
    set_number: str,
    name_hint: str | None = None,
) -> bool:
    """Rate-limit-safe check of LEGO.com UK product page for 'Retiring soon' status."""
    if _is_source_cooled_down("lego_uk"):
        return False
    url = build_lego_uk_product_url(name_hint, set_number)
    try:
        status, html = await _http_get_html(session, url, timeout_seconds=10.0)
        if status in (202, 403, 429, 503):
            _record_source_block("lego_uk")
            return False
        if status != 200:
            return False
        _record_source_ok("lego_uk")
        return _parse_lego_uk_retiring(html)
    except Exception:
        _record_source_block("lego_uk")
        return False


async def lookup_set_metadata(
    session: aiohttp.ClientSession,
    set_number: str,
    *,
    brickset_api_key: str | None = None,
    proxy_url: str | None = None,
) -> SetMetadata:
    """Auto-discover set name, category, UK RRP (£), box art image, and retiring status."""
    clean_num = set_number.strip().split("-")[0]

    meta: SetMetadata | None = None
    if brickset_api_key:
        candidate = await _lookup_brickset(session, clean_num, brickset_api_key)
        if candidate.rrp_gbp is not None or candidate.name is not None:
            meta = candidate

    if meta is None:
        public_meta = await _lookup_brickset_public(session, clean_num)
        if public_meta.name or public_meta.rrp_gbp or public_meta.is_retired:
            meta = public_meta

    if meta is not None:
        if not meta.is_retiring and not meta.is_retired:
            if await _lookup_lego_uk_retiring(session, clean_num, meta.name):
                meta = SetMetadata(
                    name=meta.name,
                    rrp_gbp=meta.rrp_gbp,
                    is_retiring=True,
                    image_url=meta.image_url,
                    source=meta.source,
                    category=meta.category,
                    is_retired=meta.is_retired,
                )
        return meta

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
        status, html = await _http_get_html(session, url, timeout_seconds=10.0)
        if status != 200:
            return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_web_err")
    except Exception:
        return SetMetadata(None, None, False, default_box_art_url(set_number), "brickset_web_err")

    soup = BeautifulSoup(html, "html.parser")
    name: str | None = None
    h1 = soup.find("h1")
    if h1:
        raw_title = h1.get_text(" ", strip=True)
        name = clean_set_display_name(raw_title, set_number)

    theme: str | None = None
    subtheme: str | None = None
    availability_val: str = ""
    exit_date_retiring = False
    exit_date_retired = False
    for dt in soup.find_all("dt"):
        label = dt.get_text(" ", strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        val = dd.get_text(" ", strip=True)
        if label == "theme":
            theme = val
        elif label == "subtheme":
            subtheme = val
        elif label == "availability":
            availability_val += " " + val.lower()
        elif label in ("exit date", "launch/exit"):
            availability_val += " " + val.lower()
            exit_part = val.split("-")[-1].strip() if "-" in val else val
            r_soon, r_done = _parse_exit_date_status(exit_part)
            if r_soon:
                exit_date_retiring = True
            if r_done:
                exit_date_retired = True

    name = _enrich_name_with_subtheme(name, subtheme, set_number)
    category = _format_category(theme)

    text = soup.get_text(" ", strip=True)
    soup.decompose()
    rrp_gbp: float | None = None
    m = re.search(r"RRP[^£]{0,30}£\s*(\d{1,4}(?:\.\d{2})?)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"£\s*(\d{1,4}\.\d{2})", text)
    if m:
        rrp_gbp = round(float(m.group(1)), 2)

    is_retired = exit_date_retired or ("retired" in availability_val and "retiring" not in availability_val)
    retiring = (not is_retired) and (exit_date_retiring or ("retiring soon" in availability_val))
    return SetMetadata(
        name=name,
        rrp_gbp=rrp_gbp,
        is_retiring=retiring,
        image_url=default_box_art_url(set_number),
        source="brickset_web",
        category=category,
        is_retired=is_retired,
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
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
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
    enriched_name = _enrich_name_with_subtheme(s.get("name"), s.get("subtheme"), set_number)
    category = _format_category(s.get("theme"))

    is_retiring = False
    is_retired = False
    if exit_date_str:
        try:
            exit_dt = datetime.fromisoformat(str(exit_date_str).replace("Z", "+00:00"))
            days_left = (exit_dt - datetime.now(timezone.utc)).days
            if days_left < 0:
                is_retired = True
            elif days_left <= 180:
                is_retiring = True
        except ValueError:
            pass

    return SetMetadata(
        name=enriched_name,
        rrp_gbp=round(float(rrp), 2) if rrp else None,
        is_retiring=is_retiring,
        image_url=image_url,
        source="brickset",
        category=category,
        is_retired=is_retired,
    )


def build_lego_uk_product_url(name: str | None, set_number: str) -> str:
    """Build a LEGO.com UK product URL using the set name slug when available."""
    clean_num = set_number.strip().split("-")[0]
    if not name or name.strip().startswith("LEGO Set #"):
        return f"https://www.lego.com/en-gb/product/{clean_num}"
    cleaned = clean_set_display_name(name, clean_num) or name.strip()
    cleaned = re.sub(r"^lego\s+(?:icons|technic|star\s+wars|ideas|creator|city|marvel|harry\s+potter|disney)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^lego\s+", "", cleaned, flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    if not slug or slug == clean_num:
        return f"https://www.lego.com/en-gb/product/{clean_num}"
    if not slug.endswith(f"-{clean_num}"):
        slug = f"{slug}-{clean_num}"
    return f"https://www.lego.com/en-gb/product/{slug}"


def build_uk_search_urls(name: str, set_number: str | None) -> list[tuple[UKRetailer, str]]:
    """Build candidate search URLs across the 8 approved UK online retailers."""
    query = quote_plus(f"LEGO {set_number}" if set_number else name)
    clean_num = set_number.strip().split("-")[0] if set_number else ""
    urls: list[tuple[UKRetailer, str]] = [
        (UKRetailer.AMAZON_UK, f"https://www.amazon.co.uk/s?k={query}"),
    ]
    if set_number:
        urls.append((UKRetailer.LEGO_UK, build_lego_uk_product_url(name, set_number)))
        urls.append((UKRetailer.ARGOS, f"https://www.argos.co.uk/search/lego-{clean_num}/"))
    urls.extend(
        [
            (UKRetailer.JOHN_LEWIS, f"https://www.johnlewis.com/search?search-term={query}"),
            (UKRetailer.SMYTHS, f"https://www.smythstoys.com/uk/en-gb/search/?text={query}"),
            (UKRetailer.CURRYS, f"https://www.currys.co.uk/search?q={query}"),
            (UKRetailer.ENTERTAINER, f"https://www.thetoyshop.com/search?q={query}"),
            (UKRetailer.DISNEY_STORE, f"https://www.disneystore.co.uk/search?q={query}"),
        ]
    )
    return urls


def extract_first_gbp_price_from_search(html: str, set_number: str | None) -> float | None:
    """Extract an in-stock GBP price from a UK retailer search page (filters out-of-stock)."""
    if "£" not in html and "GBP" not in html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    try:
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
                    if isinstance(offers, list):
                        offers = offers[0] if offers else None
                    if isinstance(offers, dict) and str(offers.get("priceCurrency", "GBP")).upper() == "GBP":
                        availability = str(offers.get("availability", "")).lower()
                        if "outofstock" in availability:
                            continue
                        raw_val = offers.get("price") or offers.get("lowPrice")
                        if raw_val is not None:
                            try:
                                val = float(str(raw_val).replace(",", ""))
                                if val > 0:
                                    return round(val, 2)
                            except ValueError:
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
    finally:
        soup.decompose()


# --- Layer 1: Zero-Token Direct UK Retailer Parsers ---


def parse_amazon_uk_search_html(
    html: str,
    set_number: str,
    *,
    rrp_gbp: float | None = None,
    has_amazon_prime: bool = False,
) -> list[UKOffer]:
    """Parse Amazon UK search HTML for genuine brand-new LEGO set offers."""
    clean_num = set_number.strip().split("-")[0]
    if clean_num not in html or "s-search-result" not in html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    try:
        loc_el = soup.find(id="glow-ingress-line2")
        loc_text = loc_el.get_text(" ", strip=True).lower() if loc_el else ""
        # If Amazon rendered export ex-VAT prices due to a non-UK server IP, restore 20% UK VAT
        ex_vat_export = bool(loc_text) and not any(
            uk_kw in loc_text for uk_kw in ("united kingdom", "select your address", "update location", "uk")
        )

        offers: list[UKOffer] = []
        for card in soup.find_all("div", attrs={"data-component-type": "s-search-result"})[:8]:
            asin = str(card.get("data-asin") or "").strip()
            if not asin:
                continue
            card_text = card.get_text(" ", strip=True)
            if any(p in card_text.lower() for p in OUT_OF_STOCK_PHRASES):
                continue

            title_recipe = card.find("div", attrs={"data-cy": "title-recipe"})
            h2 = card.find("h2")
            if title_recipe:
                title = title_recipe.get_text(" ", strip=True)
            elif h2:
                title = h2.get_text(" ", strip=True)
            else:
                title = card_text
            if len(title) < 12 or clean_num not in title:
                title = card_text[:280]

            price_spans = card.select("span.a-price:not([data-a-strike]) > span.a-offscreen")
            if not price_spans:
                continue
            raw_price = price_spans[0].get_text(strip=True)
            m = re.search(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", raw_price)
            if not m:
                continue
            price = float(m.group(1).replace(",", ""))
            if ex_vat_export:
                price = round(price * 1.20, 2)
            else:
                price = round(price, 2)

            if not is_valid_lego_set_listing(title, clean_num, price, rrp_gbp):
                continue

            ship = estimate_uk_shipping(price, UKRetailer.AMAZON_UK, has_amazon_prime=has_amazon_prime)
            offers.append(
                UKOffer(
                    retailer=UKRetailer.AMAZON_UK,
                    url=f"https://www.amazon.co.uk/dp/{asin}",
                    item_price_gbp=price,
                    shipping_gbp=ship.shipping_gbp,
                    total_gbp=ship.total_gbp,
                    shipping_note=ship.note,
                )
            )
        return offers
    finally:
        soup.decompose()


def parse_amazon_uk_product_html(
    html: str,
    url: str,
    set_number: str,
    *,
    rrp_gbp: float | None = None,
    has_amazon_prime: bool = False,
) -> tuple[UKOffer | None, bool]:
    """Parse an Amazon UK /dp/{asin} product page to verify stock, exact price, and seller/delivery.

    Returns (verified_offer, is_definitively_invalid_or_oos).
    """
    clean_num = set_number.strip().split("-")[0]
    soup = BeautifulSoup(html, "html.parser")
    try:
        loc_el = soup.find(id="glow-ingress-line2")
        loc_text = loc_el.get_text(" ", strip=True).lower() if loc_el else ""
        ex_vat_export = bool(loc_text) and not any(
            uk_kw in loc_text for uk_kw in ("united kingdom", "select your address", "update location", "uk")
        )

        avail_el = soup.find(id="availability")
        avail_text = avail_el.get_text(" ", strip=True).lower() if avail_el else ""
        if any(phrase in avail_text for phrase in OUT_OF_STOCK_PHRASES):
            return None, True

        title_el = soup.find(id="productTitle")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            return None, False

        price_el = (
            soup.select_one("#corePriceDisplay_desktop_feature_div span.a-price:not(.a-text-price) span.a-offscreen")
            or soup.select_one("#corePrice_feature_div span.a-price:not(.a-text-price) span.a-offscreen")
            or soup.select_one("#apex_desktop span.a-price:not(.a-text-price) span.a-offscreen")
        )
        if not price_el:
            return None, False
        raw_price = price_el.get_text(strip=True)
        m = re.search(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", raw_price)
        if not m:
            return None, False

        price = float(m.group(1).replace(",", ""))
        price = round(price * 1.20, 2) if ex_vat_export else round(price, 2)

        if not is_valid_lego_set_listing(title, clean_num, price, rrp_gbp):
            return None, True

        explicit_delivery: str | None = None
        if not ex_vat_export and not has_amazon_prime:
            deliv_el = soup.find(id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE")
            if deliv_el:
                deliv_text = deliv_el.get_text(" ", strip=True)
                if "£" in deliv_text or "free" in deliv_text.lower():
                    explicit_delivery = deliv_text

        ship = estimate_uk_shipping(
            price,
            UKRetailer.AMAZON_UK,
            has_amazon_prime=has_amazon_prime,
            explicit_delivery_text=explicit_delivery,
        )
        note = ship.note
        return (
            UKOffer(
                retailer=UKRetailer.AMAZON_UK,
                url=url,
                item_price_gbp=price,
                shipping_gbp=ship.shipping_gbp,
                total_gbp=ship.total_gbp,
                shipping_note=note,
            ),
            False,
        )
    finally:
        soup.decompose()


def _extract_current_card_price(
    card: Any,
    *,
    preferred_selectors: tuple[str, ...],
    set_number: str,
    title_text: str,
    rrp_gbp: float | None = None,
) -> float | None:
    """Extract the current (sale) GBP price from a retailer card, ignoring Was/RRP/Save/finance text."""
    # 1. Try explicit current-price CSS selectors first
    for sel in preferred_selectors:
        for el in card.select(sel):
            el_txt = el.get_text(" ", strip=True)
            if any(bad in el_txt.lower() for bad in ("was", "rrp", "save", "per month")):
                continue
            m = re.search(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", el_txt)
            if m:
                val = round(float(m.group(1).replace(",", "")), 2)
                if is_valid_lego_set_listing(title_text, set_number, val, rrp_gbp):
                    return val

    # 2. Fallback: strip strikethrough tags & Was/Save/RRP/finance phrases, then pick lowest valid price
    for strike in card.select("s, del, [class*='was'], [data-test*='was']"):
        strike.decompose()

    cleaned = card.get_text(" ", strip=True)
    cleaned = re.sub(r"from\s*£\s*\d+(?:\.\d{2})?\s*per\s*month", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:was|rrp|save)\s*:?\s*£\s*\d{1,4}(?:,\d{3})*(?:\.\d{2})?", " ", cleaned, flags=re.I)

    candidates: list[float] = []
    for raw in re.findall(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", cleaned):
        val = round(float(raw.replace(",", "")), 2)
        if is_valid_lego_set_listing(title_text, set_number, val, rrp_gbp):
            candidates.append(val)
    return min(candidates) if candidates else None


def parse_john_lewis_search_html(
    html: str,
    set_number: str,
    *,
    rrp_gbp: float | None = None,
    prefer_click_and_collect: bool = False,
) -> list[UKOffer]:
    clean_num = set_number.strip().split("-")[0]
    if clean_num not in html or "£" not in html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    try:
        offers: list[UKOffer] = []
        for article in soup.find_all("article")[:10]:
            text = article.get_text(" ", strip=True)
            if clean_num not in text or "£" not in text:
                continue
            if any(p in text.lower() for p in OUT_OF_STOCK_PHRASES):
                continue
            price = _extract_current_card_price(
                article,
                preferred_selectors=(
                    "span[class*='price_price__now']",
                    "[data-test='product-card-price']",
                    "p[class*='Price_price']",
                ),
                set_number=clean_num,
                title_text=text,
                rrp_gbp=rrp_gbp,
            )
            if price is None:
                continue
            link_tag = article.find("a", href=True)
            href = str(link_tag["href"]).strip() if link_tag else ""
            if href.startswith("/"):
                href = f"https://www.johnlewis.com{href}"
            url = href if href.startswith("http") else f"https://www.johnlewis.com/search?search-term=lego+{clean_num}"
            ship = estimate_uk_shipping(price, UKRetailer.JOHN_LEWIS, prefer_click_and_collect=prefer_click_and_collect)
            offers.append(
                UKOffer(
                    retailer=UKRetailer.JOHN_LEWIS,
                    url=url,
                    item_price_gbp=price,
                    shipping_gbp=ship.shipping_gbp,
                    total_gbp=ship.total_gbp,
                    shipping_note=ship.note,
                )
            )
        return offers
    finally:
        soup.decompose()


def parse_argos_search_html(
    html: str,
    set_number: str,
    *,
    rrp_gbp: float | None = None,
    prefer_click_and_collect: bool = False,
) -> list[UKOffer]:
    clean_num = set_number.strip().split("-")[0]
    if clean_num not in html or "£" not in html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    try:
        offers: list[UKOffer] = []
        seen_urls: set[str] = set()
        for card in soup.find_all("div", attrs={"data-test": re.compile("component-product-card")})[:10]:
            text = card.get_text(" ", strip=True)
            if clean_num not in text or "£" not in text:
                continue
            if any(p in text.lower() for p in OUT_OF_STOCK_PHRASES):
                continue
            price = _extract_current_card_price(
                card,
                preferred_selectors=(
                    "span.ds-c-price__price",
                    "[data-test='component-product-card-price']",
                    "div.ds-c-price",
                ),
                set_number=clean_num,
                title_text=text,
                rrp_gbp=rrp_gbp,
            )
            if price is None:
                continue
            link_tag = card.find("a", href=re.compile(r"^/product/\d+")) or card.find("a", href=True)
            href = str(link_tag["href"]).strip() if link_tag else ""
            if href.startswith("/"):
                href = f"https://www.argos.co.uk{href}"
            url = href if href.startswith("http") else f"https://www.argos.co.uk/search/lego-{clean_num}/"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            ship = estimate_uk_shipping(price, UKRetailer.ARGOS, prefer_click_and_collect=prefer_click_and_collect)
            offers.append(
                UKOffer(
                    retailer=UKRetailer.ARGOS,
                    url=url,
                    item_price_gbp=price,
                    shipping_gbp=ship.shipping_gbp,
                    total_gbp=ship.total_gbp,
                    shipping_note=ship.note,
                )
            )
        return offers
    finally:
        soup.decompose()


def parse_disney_store_search_html(
    html: str,
    set_number: str,
    *,
    rrp_gbp: float | None = None,
) -> list[UKOffer]:
    clean_num = set_number.strip().split("-")[0]
    if clean_num not in html or "£" not in html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    try:
        offers: list[UKOffer] = []
        for tile in soup.select("div.product, div.product-tile")[:10]:
            text = tile.get_text(" ", strip=True)
            if clean_num not in text or "£" not in text:
                continue
            if any(p in text.lower() for p in OUT_OF_STOCK_PHRASES):
                continue
            m = re.search(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", text)
            if not m:
                continue
            price = round(float(m.group(1).replace(",", "")), 2)
            if not is_valid_lego_set_listing(text, clean_num, price, rrp_gbp):
                continue
            link_tag = tile.find("a", href=True)
            href = str(link_tag["href"]).strip() if link_tag else ""
            if href.startswith("/"):
                href = f"https://www.disneystore.co.uk{href}"
            url = href.split("?")[0] if href.startswith("http") else f"https://www.disneystore.co.uk/search?q=lego+{clean_num}"
            ship = estimate_uk_shipping(price, UKRetailer.DISNEY_STORE)
            offers.append(
                UKOffer(
                    retailer=UKRetailer.DISNEY_STORE,
                    url=url,
                    item_price_gbp=price,
                    shipping_gbp=ship.shipping_gbp,
                    total_gbp=ship.total_gbp,
                    shipping_note=ship.note,
                )
            )
        return offers
    finally:
        soup.decompose()


async def _fetch_direct_uk_store(
    session: aiohttp.ClientSession | None,
    *,
    source_key: str,
    label: str,
    url: str,
    parser_fn: Callable[[str], list[UKOffer]],
    cookies: dict[str, str] | None = None,
) -> tuple[list[UKOffer], str | None]:
    if _is_source_cooled_down(source_key):
        return [], f"{label}: skipped (cooldown)"

    for attempt in range(2):
        try:
            status, html = await _http_get_html(session, url, cookies=cookies, timeout_seconds=10.0)
            if status != 200:
                if status in (403, 429, 503) and attempt == 0:
                    _rotate_curl_profile()
                    await asyncio.sleep(2.0)
                    continue
                if status in (202, 403, 429, 503):
                    _record_source_block(source_key)
                return [], f"{label}: HTTP {status}"

            html_lower = html.lower()
            is_soft_block = (
                "validatecaptcha" in html_lower
                or "enter the characters you see below" in html_lower
                or "api-services-support@amazon" in html_lower
                or "sorry, we just need to make sure you're not a robot" in html_lower
                or (source_key == "amazon_uk" and 'data-component-type="s-search-result"' not in html)
            )
            if is_soft_block:
                if attempt == 0:
                    _rotate_curl_profile()
                    await asyncio.sleep(2.0)
                    continue
                _record_source_block(source_key)
                return [], f"{label}: rate-limited"

            _record_source_ok(source_key)
            found = parser_fn(html)
            return found, (None if found else f"{label}: not in stock / not listed")
        except Exception as exc:
            if attempt == 0:
                _rotate_curl_profile()
                await asyncio.sleep(2.0)
                continue
            _record_source_block(source_key)
            return [], f"{label}: {type(exc).__name__}"

    return [], f"{label}: rate-limited"


# --- Layer 2: SerpApi Google Shopping UK (Covers all 8 Approved UK Stores) ---


def parse_serpapi_shopping_payload(
    payload: dict[str, Any],
    set_number: str,
    *,
    rrp_gbp: float | None = None,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
) -> list[UKOffer]:
    """Extract offers strictly from the 8 approved UK retailers in a SerpApi Google Shopping UK response."""
    clean_num = set_number.strip().split("-")[0]
    offers: list[UKOffer] = []

    for item in payload.get("shopping_results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        link = str(item.get("product_link") or item.get("link") or "").strip()

        retailer = match_approved_uk_retailer(source) or match_approved_uk_retailer(link)
        if retailer is None or retailer not in APPROVED_UK_RETAILERS:
            continue

        # Reject explicitly second-hand / refurbished condition tags from Google Shopping
        second_hand = str(item.get("second_hand_condition") or "").strip().lower()
        if second_hand:
            continue

        extracted = item.get("extracted_price")
        price: float | None = None
        if isinstance(extracted, (int, float)) and extracted > 0:
            price_str = str(item.get("price") or "£")
            if "£" in price_str or "gbp" in price_str.lower():
                price = round(float(extracted), 2)
        if price is None:
            m = re.search(r"£\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", str(item.get("price") or ""))
            if m:
                price = round(float(m.group(1).replace(",", "")), 2)

        if price is None or not is_valid_lego_set_listing(title, clean_num, price, rrp_gbp):
            continue

        delivery_str = str(item.get("delivery") or "").strip() or None
        ship = estimate_uk_shipping(
            price,
            retailer,
            has_amazon_prime=has_amazon_prime,
            prefer_click_and_collect=prefer_click_and_collect,
            explicit_delivery_text=delivery_str,
        )
        offers.append(
            UKOffer(
                retailer=retailer,
                url=link or f"https://www.google.co.uk/search?tbm=shop&q=LEGO+{clean_num}",
                item_price_gbp=price,
                shipping_gbp=ship.shipping_gbp,
                total_gbp=ship.total_gbp,
                shipping_note=ship.note,
            )
        )

    return offers


async def _fetch_serpapi_google_shopping_uk(
    session: aiohttp.ClientSession,
    set_number: str,
    api_key: str,
    *,
    rrp_gbp: float | None = None,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
    on_credit_used: Callable[[], None] | None = None,
) -> tuple[list[UKOffer], str | None]:
    clean_num = set_number.strip().split("-")[0]
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_shopping",
        "q": f"LEGO {clean_num}",
        "gl": "uk",
        "hl": "en",
        "location": "United Kingdom",
        "api_key": api_key,
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return [], f"Google Shopping UK (SerpApi): HTTP {resp.status}"
            payload = await resp.json()
    except Exception as exc:
        return [], f"Google Shopping UK (SerpApi): {type(exc).__name__}"

    if "error" in payload:
        return [], f"Google Shopping UK: {payload['error']}"

    # SerpApi cached responses are free (0 credits charged)
    meta_status = str((payload.get("search_metadata") or {}).get("status") or "")
    was_cached = bool((payload.get("search_parameters") or {}).get("cached")) or "cached" in meta_status.lower()
    if not was_cached and on_credit_used is not None:
        on_credit_used()

    offers = parse_serpapi_shopping_payload(
        payload,
        clean_num,
        rrp_gbp=rrp_gbp,
        has_amazon_prime=has_amazon_prime,
        prefer_click_and_collect=prefer_click_and_collect,
    )
    return offers, (None if offers else "Google Shopping UK: no approved UK retailer offer found")


async def find_best_uk_offer(
    session: aiohttp.ClientSession,
    *,
    name: str,
    set_number: str | None,
    direct_url: str | None = None,
    rrp_gbp: float | None = None,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
    serpapi_key: str | None = None,
    allow_serpapi: bool = True,
    on_serpapi_credit_used: Callable[[], None] | None = None,
    on_amazon_dp_url_update: Callable[[str, str | None], None] | None = None,
    proxy_url: str | None = None,
    force_refresh: bool = False,
) -> tuple[UKOffer | None, list[str]]:
    """Check approved UK retailers concurrently (0-token direct stores + blocked-only Google Shopping UK fallback)
    and return the cheapest genuine in-stock landed GBP offer."""
    clean_key_id = set_number.strip().split("-")[0] if set_number else name.strip().lower()
    cache_key = f"{clean_key_id}|{int(has_amazon_prime)}|{int(prefer_click_and_collect)}|{direct_url or ''}"
    now = time.monotonic()
    if not force_refresh and cache_key in _PRICE_CACHE:
        cached_at, cached_offer, cached_notes = _PRICE_CACHE[cache_key]
        if now - cached_at < _PRICE_CACHE_TTL_SECONDS:
            return cached_offer, [*cached_notes, "Served from 30m cache"]
        _PRICE_CACHE.pop(cache_key, None)

    offers: list[UKOffer] = []
    notes: list[str] = []
    tasks = []

    used_direct_dp = False
    clean_num: str | None = None
    if set_number:
        clean_num = set_number.strip().split("-")[0]
        cached_dp_url = _AMAZON_DP_URL_CACHE.get(clean_num)
        if not cached_dp_url and direct_url and "amazon.co.uk" in direct_url.lower() and "/dp/" in direct_url:
            cached_dp_url = direct_url

        if cached_dp_url:
            used_direct_dp = True
            target_clean_num = clean_num

            async def _fetch_amazon_dp_direct() -> tuple[list[UKOffer], str | None]:
                if _is_source_cooled_down("amazon_uk"):
                    return [], "Amazon UK: skipped (cooldown)"
                for attempt in range(2):
                    try:
                        status, dp_html = await _http_get_html(
                            session,
                            cached_dp_url,
                            cookies={"i18n-prefs": "GBP"},
                            timeout_seconds=10.0,
                        )
                        if status == 404:
                            _AMAZON_DP_URL_CACHE.pop(target_clean_num, None)
                            if on_amazon_dp_url_update is not None:
                                on_amazon_dp_url_update(target_clean_num, None)
                            return [], "Amazon UK: product page 404"
                        if status != 200:
                            if status in (429, 503) and attempt == 0:
                                _rotate_curl_profile()
                                await asyncio.sleep(2.0)
                                continue
                            if status in (202, 403, 429, 503):
                                _record_source_block("amazon_uk")
                            return [], f"Amazon UK: HTTP {status}"
                        dp_lower = dp_html.lower()
                        if (
                            "validatecaptcha" in dp_lower
                            or "enter the characters you see below" in dp_lower
                            or "api-services-support@amazon" in dp_lower
                        ):
                            if attempt == 0:
                                _rotate_curl_profile()
                                await asyncio.sleep(2.0)
                                continue
                            _record_source_block("amazon_uk")
                            return [], "Amazon UK: rate-limited"
                        _record_source_ok("amazon_uk")
                        verified, definitively_bad = parse_amazon_uk_product_html(
                            dp_html,
                            cached_dp_url,
                            target_clean_num,
                            rrp_gbp=rrp_gbp,
                            has_amazon_prime=has_amazon_prime,
                        )
                        if verified is not None:
                            return [verified], None
                        if definitively_bad:
                            _AMAZON_DP_URL_CACHE.pop(target_clean_num, None)
                            if on_amazon_dp_url_update is not None:
                                on_amazon_dp_url_update(target_clean_num, None)
                            return [], "Amazon UK: out of stock"
                        return [], "Amazon UK: not in stock / not listed"
                    except Exception as exc:
                        if attempt == 0:
                            _rotate_curl_profile()
                            await asyncio.sleep(2.0)
                            continue
                        _record_source_block("amazon_uk")
                        return [], f"Amazon UK: {type(exc).__name__}"
                return [], "Amazon UK: rate-limited"

            tasks.append(_fetch_amazon_dp_direct())
        else:
            tasks.append(
                _fetch_direct_uk_store(
                    session,
                    source_key="amazon_uk",
                    label="Amazon UK",
                    url=f"https://www.amazon.co.uk/s?k=LEGO+{clean_num}",
                    cookies={"i18n-prefs": "GBP"},
                    parser_fn=lambda html: parse_amazon_uk_search_html(
                        html, clean_num, rrp_gbp=rrp_gbp, has_amazon_prime=has_amazon_prime
                    ),
                )
            )
        tasks.append(
            _fetch_direct_uk_store(
                session,
                source_key="john_lewis",
                label="John Lewis",
                url=f"https://www.johnlewis.com/search?search-term=lego+{clean_num}",
                parser_fn=lambda html: parse_john_lewis_search_html(
                    html, clean_num, rrp_gbp=rrp_gbp, prefer_click_and_collect=prefer_click_and_collect
                ),
            )
        )

    if direct_url and match_approved_uk_retailer(direct_url) is not None:
        async def _check_direct() -> tuple[list[UKOffer], str | None]:
            res = await fetch_price_for_url(session, direct_url)
            if res.ok and res.quote is not None and res.quote.retailer in APPROVED_UK_RETAILERS:
                if not res.quote.in_stock:
                    return [], f"Direct URL ({res.quote.retailer.display_name}): out of stock"
                if not is_valid_lego_set_listing(name, set_number, res.quote.price_gbp, rrp_gbp):
                    return [], f"Direct URL ({res.quote.retailer.display_name}): price outside RRP bounds"
                ship = estimate_uk_shipping(
                    res.quote.price_gbp,
                    res.quote.retailer,
                    has_amazon_prime=has_amazon_prime,
                    prefer_click_and_collect=prefer_click_and_collect,
                )
                return [
                    UKOffer(
                        retailer=res.quote.retailer,
                        url=direct_url,
                        item_price_gbp=res.quote.price_gbp,
                        shipping_gbp=ship.shipping_gbp,
                        total_gbp=ship.total_gbp,
                        shipping_note=ship.note,
                    )
                ], None
            return [], f"Direct URL: {res.error}"

        tasks.append(_check_direct())

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item in results:
        if isinstance(item, Exception):
            notes.append(f"Check error: {type(item).__name__}")
            continue
        found_offers, err = item
        offers.extend(o for o in found_offers if o.retailer in APPROVED_UK_RETAILERS)
        if err:
            notes.append(err)

    # SerpApi blocked-only fallback: only spend a credit when NO direct offers were found AND a store was blocked/rate-limited
    any_blocked = any(
        any(tok in n.lower() for tok in ("rate-limited", "cooldown", "http 503", "http 403", "http 429"))
        for n in notes
    )
    if clean_num and serpapi_key and allow_serpapi and (not offers and any_blocked):
        serp_offers, serp_err = await _fetch_serpapi_google_shopping_uk(
            session,
            clean_num,
            serpapi_key,
            rrp_gbp=rrp_gbp,
            has_amazon_prime=has_amazon_prime,
            prefer_click_and_collect=prefer_click_and_collect,
            on_credit_used=on_serpapi_credit_used,
        )
        offers.extend(o for o in serp_offers if o.retailer in APPROVED_UK_RETAILERS)
        if serp_err:
            notes.append(serp_err)

    if not offers:
        return None, notes

    offers.sort(key=lambda o: o.total_gbp)
    best = offers[0]

    # Verify winning Amazon UK /dp/ offer on its product page (skipped if already fetched directly)
    if (
        not used_direct_dp
        and best.retailer == UKRetailer.AMAZON_UK
        and "/dp/" in best.url
        and clean_num
        and not _is_source_cooled_down("amazon_uk")
    ):
        try:
            status, dp_html = await _http_get_html(
                session,
                best.url,
                cookies={"i18n-prefs": "GBP"},
                timeout_seconds=10.0,
            )
            if status == 200 and "validateCaptcha" not in dp_html:
                verified, definitively_bad = parse_amazon_uk_product_html(
                    dp_html,
                    best.url,
                    clean_num,
                    rrp_gbp=rrp_gbp,
                    has_amazon_prime=has_amazon_prime,
                )
                if verified is not None:
                    offers[0] = verified
                    offers.sort(key=lambda o: o.total_gbp)
                    best = offers[0]
                elif definitively_bad:
                    offers.pop(0)
                    if not offers:
                        return None, [*notes, "Amazon UK: out of stock on product page"]
                    best = offers[0]
        except Exception:
            pass  # Fail open to search-card offer if /dp/ check times out

    if clean_num and best.retailer == UKRetailer.AMAZON_UK and "/dp/" in best.url:
        _AMAZON_DP_URL_CACHE[clean_num] = best.url
        if on_amazon_dp_url_update is not None:
            on_amazon_dp_url_update(clean_num, best.url)

    _PRICE_CACHE[cache_key] = (time.monotonic(), best, list(notes))
    return best, notes
