from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from statistics import mean


class Verdict(str, Enum):
    BUY_NOW = "BUY NOW"
    BUY_NOW_RETIRING = "BUY NOW (RETIRING)"
    WATCH_RETIRING = "WATCH (RETIRING)"
    LOWEST_SEEN = "LOWEST SEEN"
    GOOD_DEAL = "GOOD DEAL"
    WAIT = "WAIT"


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
    verdict: Verdict
    forecast: str


def _dynamic_context(total_gbp: float, hist_low: float | None, hist_avg: float | None) -> str:
    parts: list[str] = []
    if hist_avg is not None and hist_avg > 0:
        diff = round(hist_avg - total_gbp, 2)
        if diff >= 1.0:
            parts.append(f"£{diff:.2f} below its tracked average (£{hist_avg:.2f})")
        elif diff <= -1.0:
            parts.append(f"£{abs(diff):.2f} above its tracked average (£{hist_avg:.2f})")
    if hist_low is not None:
        if total_gbp <= hist_low:
            parts.append(f"matches/beats its tracked low of £{hist_low:.2f}")
        else:
            parts.append(f"tracked low is £{hist_low:.2f}")
    return "; ".join(parts)


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
    at_all_time_low = hist_low is not None and total_gbp <= hist_low
    below_avg = hist_avg is not None and total_gbp < hist_avg * 0.95
    ctx = _dynamic_context(total_gbp, hist_low, hist_avg)
    ctx_suffix = f" ({ctx}.)" if ctx else ""

    if is_retiring:
        if discount_pct > 0.0:
            verdict = Verdict.BUY_NOW_RETIRING
            forecast = (
                "Retiring soon and below RRP — waiting risks UK retail stock selling out."
                + ctx_suffix
            )
        else:
            verdict = Verdict.WATCH_RETIRING
            forecast = (
                "At/above RRP, but retiring soon — grab it on any dip before stock disappears."
                + ctx_suffix
            )
    else:
        if discount_pct >= 30.0 or (near_all_time_low and discount_pct >= 20.0):
            verdict = Verdict.BUY_NOW
            forecast = (
                "Rare/all-time low UK price range (20–30%+ off after shipping); retailers usually bounce back up."
                + ctx_suffix
            )
        elif discount_pct >= 20.0 or (below_avg and discount_pct >= 15.0):
            verdict = Verdict.GOOD_DEAL
            forecast = (
                "Solid UK discount — likely to rise again once this promo ends."
                + ctx_suffix
            )
        elif at_all_time_low and discount_pct > 0.0:
            verdict = Verdict.LOWEST_SEEN
            forecast = (
                "Lowest tracked UK price so far, though only a modest discount off RRP."
                + ctx_suffix
            )
        elif discount_pct >= 10.0:
            verdict = Verdict.WAIT
            forecast = (
                "Mild reduction after UK delivery; UK retailers regularly hit 20–25% off."
                + ctx_suffix
            )
        elif discount_pct < 0.0:
            verdict = Verdict.WAIT
            forecast = (
                f"Currently £{abs(savings_gbp):.2f} above RRP after delivery — wait for a restock at RRP or better."
                + ctx_suffix
            )
        else:
            verdict = Verdict.WAIT
            forecast = (
                "Close to RRP once UK delivery is included — likely to drop during upcoming promos."
                + ctx_suffix
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
