from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UKRetailer(str, Enum):
    AMAZON_UK = "amazon_uk"
    ARGOS = "argos"
    LEGO_UK = "lego_uk"
    SMYTHS = "smyths"
    JOHN_LEWIS = "john_lewis"
    BRICKLINK_UK = "bricklink_uk"
    OTHER = "other"

    @property
    def display_name(self) -> str:
        names = {
            UKRetailer.AMAZON_UK: "Amazon UK",
            UKRetailer.ARGOS: "Argos",
            UKRetailer.LEGO_UK: "LEGO.com UK",
            UKRetailer.SMYTHS: "Smyths Toys",
            UKRetailer.JOHN_LEWIS: "John Lewis",
            UKRetailer.BRICKLINK_UK: "BrickLink UK",
            UKRetailer.OTHER: "Other UK Retailer",
        }
        return names[self]


@dataclass(frozen=True)
class ShippingEstimate:
    shipping_gbp: float
    total_gbp: float
    note: str


def detect_retailer_from_url(url: str | None) -> UKRetailer:
    if not url:
        return UKRetailer.OTHER
    lower = url.lower()
    if "amazon.co.uk" in lower:
        return UKRetailer.AMAZON_UK
    if "argos.co.uk" in lower:
        return UKRetailer.ARGOS
    if "lego.com" in lower:
        return UKRetailer.LEGO_UK
    if "smythstoys.com" in lower:
        return UKRetailer.SMYTHS
    if "johnlewis.com" in lower:
        return UKRetailer.JOHN_LEWIS
    if "bricklink.com" in lower:
        return UKRetailer.BRICKLINK_UK
    return UKRetailer.OTHER


def estimate_uk_shipping(
    item_price_gbp: float,
    retailer: UKRetailer,
    *,
    has_amazon_prime: bool = False,
    prefer_click_and_collect: bool = False,
) -> ShippingEstimate:
    """Estimate UK delivery cost in GBP for major UK LEGO retailers."""
    price = round(item_price_gbp, 2)

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

    if retailer == UKRetailer.BRICKLINK_UK:
        return ShippingEstimate(3.50, round(price + 3.50, 2), "~£3.50 est. Royal Mail UK postage")

    return ShippingEstimate(0.0, price, "Shipping not estimated")
