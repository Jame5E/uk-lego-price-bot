from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class UKRetailer(str, Enum):
    AMAZON_UK = "amazon_uk"
    ARGOS = "argos"
    LEGO_UK = "lego_uk"
    SMYTHS = "smyths"
    JOHN_LEWIS = "john_lewis"
    CURRYS = "currys"
    ENTERTAINER = "entertainer"
    DISNEY_STORE = "disney_store"
    OTHER = "other"

    @property
    def display_name(self) -> str:
        names = {
            UKRetailer.AMAZON_UK: "Amazon UK",
            UKRetailer.ARGOS: "Argos",
            UKRetailer.LEGO_UK: "LEGO.com UK",
            UKRetailer.SMYTHS: "Smyths Toys",
            UKRetailer.JOHN_LEWIS: "John Lewis",
            UKRetailer.CURRYS: "Currys",
            UKRetailer.ENTERTAINER: "The Entertainer",
            UKRetailer.DISNEY_STORE: "Disney Store UK",
            UKRetailer.OTHER: "Other UK Retailer",
        }
        return names[self]


APPROVED_UK_RETAILERS: tuple[UKRetailer, ...] = (
    UKRetailer.AMAZON_UK,
    UKRetailer.LEGO_UK,
    UKRetailer.JOHN_LEWIS,
    UKRetailer.SMYTHS,
    UKRetailer.ARGOS,
    UKRetailer.CURRYS,
    UKRetailer.ENTERTAINER,
    UKRetailer.DISNEY_STORE,
)


@dataclass(frozen=True)
class ShippingEstimate:
    shipping_gbp: float
    total_gbp: float
    note: str


def match_approved_uk_retailer(merchant_or_url: str | None) -> UKRetailer | None:
    """Strictly match ONLY the 8 approved UK online retailers from a URL or merchant name."""
    if not merchant_or_url:
        return None
    lower = merchant_or_url.strip().lower()

    # Reject non-UK Amazon domains or eBay/marketplace wrappers (e.g. "Argos on eBay")
    if any(bad in lower for bad in ("ebay", "onbuy", "aliexpress", "temu", "fruugo", "wish", "vinted", "stockx")):
        return None
    if any(foreign_amz in lower for foreign_amz in ("amazon.com", "amazon.de", "amazon.fr", "amazon.es", "amazon.it", "amazon us")):
        return None

    if "amazon.co.uk" in lower or lower in ("amazon", "amazon uk", "amazon.co.uk - retail"):
        return UKRetailer.AMAZON_UK
    if lower.startswith("amazon") and "marketplace" not in lower:
        return UKRetailer.AMAZON_UK
    if "argos.co.uk" in lower or lower == "argos" or lower.startswith("argos "):
        return UKRetailer.ARGOS
    if "lego.com" in lower or lower in ("lego", "lego uk", "lego store", "lego.com uk", "official lego shop"):
        return UKRetailer.LEGO_UK
    if "smythstoys.com" in lower or "smyths" in lower:
        return UKRetailer.SMYTHS
    if "johnlewis.com" in lower or "john lewis" in lower:
        return UKRetailer.JOHN_LEWIS
    if "currys.co.uk" in lower or lower == "currys" or lower.startswith("currys "):
        return UKRetailer.CURRYS
    if "thetoyshop.com" in lower or "entertainer" in lower:
        return UKRetailer.ENTERTAINER
    if "disneystore.co.uk" in lower or "shopdisney.co.uk" in lower or "disney store" in lower:
        return UKRetailer.DISNEY_STORE
    return None


def detect_retailer_from_url(url: str | None) -> UKRetailer:
    matched = match_approved_uk_retailer(url)
    return matched if matched is not None else UKRetailer.OTHER


def parse_explicit_shipping_gbp(delivery_text: str | None) -> tuple[float, str] | None:
    """Extract explicit GBP delivery price from a Google Shopping delivery label when present."""
    if not delivery_text:
        return None
    clean = delivery_text.strip()
    lower = clean.lower()
    if "free" in lower and ("delivery" in lower or "shipping" in lower or "postage" in lower):
        return 0.0, "Free UK delivery"
    m = re.search(r"£\s*(\d{1,2}(?:\.\d{2})?)", clean)
    if m and ("delivery" in lower or "shipping" in lower or "postage" in lower):
        cost = round(float(m.group(1)), 2)
        return cost, f"£{cost:.2f} UK delivery"
    return None


def estimate_uk_shipping(
    item_price_gbp: float,
    retailer: UKRetailer,
    *,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
    explicit_delivery_text: str | None = None,
) -> ShippingEstimate:
    """Estimate UK delivery cost in GBP for the 8 approved UK LEGO retailers."""
    price = round(item_price_gbp, 2)

    parsed_explicit = parse_explicit_shipping_gbp(explicit_delivery_text)
    if parsed_explicit is not None:
        ship_cost, ship_note = parsed_explicit
        if retailer == UKRetailer.AMAZON_UK and has_amazon_prime:
            return ShippingEstimate(0.0, price, "Free (Amazon Prime UK)")
        if prefer_click_and_collect and retailer in (
            UKRetailer.ARGOS,
            UKRetailer.SMYTHS,
            UKRetailer.CURRYS,
            UKRetailer.ENTERTAINER,
        ):
            return ShippingEstimate(0.0, price, f"Free ({retailer.display_name} Click & Collect)")
        return ShippingEstimate(ship_cost, round(price + ship_cost, 2), ship_note)

    if retailer == UKRetailer.AMAZON_UK:
        if has_amazon_prime:
            return ShippingEstimate(0.0, price, "Free (Amazon Prime UK)")
        if price >= 35.0:
            return ShippingEstimate(0.0, price, "Free UK delivery (over £35)")
        return ShippingEstimate(4.49, round(price + 4.49, 2), "£4.49 standard UK delivery (free over £35)")

    if retailer == UKRetailer.ARGOS:
        if prefer_click_and_collect:
            return ShippingEstimate(0.0, price, "Free (Argos Click & Collect)")
        return ShippingEstimate(3.95, round(price + 3.95, 2), "£3.95 standard UK delivery (or free Click & Collect)")

    if retailer == UKRetailer.LEGO_UK:
        if price >= 50.0:
            return ShippingEstimate(0.0, price, "Free UK delivery (over £50)")
        return ShippingEstimate(3.95, round(price + 3.95, 2), "£3.95 standard UK delivery (free over £50)")

    if retailer == UKRetailer.SMYTHS:
        if prefer_click_and_collect:
            return ShippingEstimate(0.0, price, "Free (Smyths Click & Collect)")
        if price >= 20.0:
            return ShippingEstimate(0.0, price, "Free UK home delivery (over £20)")
        return ShippingEstimate(4.99, round(price + 4.99, 2), "£4.99 UK delivery (free over £20 or Click & Collect)")

    if retailer == UKRetailer.JOHN_LEWIS:
        if prefer_click_and_collect and price >= 30.0:
            return ShippingEstimate(0.0, price, "Free Click & Collect (over £30)")
        if price >= 50.0:
            return ShippingEstimate(0.0, price, "Free UK delivery (over £50)")
        return ShippingEstimate(4.50, round(price + 4.50, 2), "£4.50 UK delivery (free over £50)")

    if retailer == UKRetailer.CURRYS:
        if prefer_click_and_collect or price >= 40.0:
            return ShippingEstimate(0.0, price, "Free UK delivery / Click & Collect")
        return ShippingEstimate(3.99, round(price + 3.99, 2), "£3.99 UK delivery (free over £40 or Click & Collect)")

    if retailer == UKRetailer.ENTERTAINER:
        if prefer_click_and_collect:
            return ShippingEstimate(0.0, price, "Free (The Entertainer Click & Collect)")
        if price >= 39.99:
            return ShippingEstimate(0.0, price, "Free UK delivery (over £39.99)")
        return ShippingEstimate(3.99, round(price + 3.99, 2), "£3.99 UK delivery (free over £39.99)")

    if retailer == UKRetailer.DISNEY_STORE:
        if price >= 50.0:
            return ShippingEstimate(0.0, price, "Free UK delivery (over £50)")
        return ShippingEstimate(3.99, round(price + 3.99, 2), "£3.99 UK delivery (free over £50)")

    return ShippingEstimate(0.0, price, "Shipping not estimated")
