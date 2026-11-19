from src.lego_bot.predictor import analyze_price


def test_strong_discount_buy_now():
    analysis = analyze_price(
        rrp_gbp=100.00,
        item_price_gbp=65.00,
        shipping_gbp=0.00,
        historical_totals_gbp=[95.0, 90.0, 80.0, 68.0],
        is_retiring=False,
    )
    assert analysis.savings_gbp == 35.00
    assert analysis.discount_pct == 35.0
    assert analysis.verdict == "BUY NOW"
    assert "bounce back up" in analysis.forecast.lower()


def test_mild_discount_wait():
    analysis = analyze_price(
        rrp_gbp=50.00,
        item_price_gbp=42.00,
        shipping_gbp=3.95,
        historical_totals_gbp=[50.0, 48.0],
        is_retiring=False,
    )
    # Total is 45.95 -> 8.1% off RRP
    assert analysis.total_gbp == 45.95
    assert analysis.verdict == "WAIT"
    assert "likely to drop more" in analysis.forecast.lower()


def test_retiring_set_verdict():
    # Any discount below RRP on a retiring set should say BUY NOW (RETIRING)
    analysis = analyze_price(
        rrp_gbp=100.00,
        item_price_gbp=92.00,
        shipping_gbp=0.00,
        historical_totals_gbp=[100.0, 95.0],
        is_retiring=True,
    )
    assert analysis.verdict == "BUY NOW (RETIRING)"

    full_rrp = analyze_price(
        rrp_gbp=100.00,
        item_price_gbp=100.00,
        shipping_gbp=0.00,
        historical_totals_gbp=[100.0],
        is_retiring=True,
    )
    assert full_rrp.verdict == "WATCH (RETIRING)"
