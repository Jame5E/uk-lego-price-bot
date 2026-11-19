from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class PriceAnalysis:
    rrp_gbp: float
    item_price_gbp: float
    shipping_gbp: float
    total_gbp: float
    savings_gbp: float
    discount_pct: float
    item_discount_pct: float
    historical_low_gbp: float | None
    historical_avg_gbp: float | None
    verdict: str
    forecast: str


def analyze_price(
    *,
    rrp_gbp: float,
    item_price_gbp: float,
    shipping_gbp: float,
    historical_totals_gbp: list[float] | None = None,
    is_retiring: bool = False,
) -> PriceAnalysis:
    """Analyze a UK LEGO price in GBP and estimate whether it will drop more or go back up."""
    total_gbp = round(item_price_gbp + shipping_gbp, 2)
    savings_gbp = round(rrp_gbp - total_gbp, 2)
    discount_pct = round((savings_gbp / rrp_gbp) * 100, 1) if rrp_gbp > 0 else 0.0
    item_discount_pct = (
        round(((rrp_gbp - item_price_gbp) / rrp_gbp) * 100, 1) if rrp_gbp > 0 else 0.0
    )

    history = [p for p in (historical_totals_gbp or []) if p > 0]
    hist_low = round(min(history), 2) if history else None
    hist_avg = round(mean(history), 2) if history else None

    near_all_time_low = hist_low is not None and total_gbp <= hist_low * 1.03
    below_avg = hist_avg is not None and total_gbp < hist_avg * 0.95

    if is_retiring:
        if discount_pct > 0.0:
            verdict = "BUY NOW (RETIRING)"
            forecast = (
                "This set is retiring soon and is below RRP. "
                "Waiting risks it selling out and jumping in price on the secondary market."
            )
        else:
            verdict = "WATCH (RETIRING)"
            forecast = (
                "Currently at full RRP, but because it is retiring soon, "
                "grab it on any dip before UK retail stock sells out."
            )
    else:
        if discount_pct >= 30.0 or (near_all_time_low and discount_pct >= 20.0):
            verdict = "BUY NOW"
            forecast = (
                "Rare/all-time low UK price range (20-30%+ off after shipping). "
                "Retailers like Amazon UK and Argos usually bounce back up after this level of discount."
            )
        elif discount_pct >= 20.0 or (below_avg and discount_pct >= 15.0):
            verdict = "GOOD DEAL"
            forecast = (
                "Solid UK discount (20%+ off or clearly below recent average). "
                "Could occasionally hit 25-30% during major sales (Black Friday/Prime Day), "
                "but likely to go back up once the promo ends."
            )
        elif discount_pct >= 10.0:
            verdict = "WAIT"
            forecast = (
                "Only a mild reduction after UK shipping. LEGO sets at UK retailers regularly "
                "hit 20-25% off, so this is likely to drop more in the future."
            )
        else:
            verdict = "WAIT"
            forecast = (
                "Close to RRP once UK delivery is included. Likely to drop more during upcoming retailer promos."
            )

    return PriceAnalysis(
        rrp_gbp=round(rrp_gbp, 2),
        item_price_gbp=round(item_price_gbp, 2),
        shipping_gbp=round(shipping_gbp, 2),
        total_gbp=total_gbp,
        savings_gbp=savings_gbp,
        discount_pct=discount_pct,
        item_discount_pct=item_discount_pct,
        historical_low_gbp=hist_low,
        historical_avg_gbp=hist_avg,
        verdict=verdict,
        forecast=forecast,
    )
