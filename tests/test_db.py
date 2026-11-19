from datetime import datetime, timedelta, timezone

from src.lego_bot.db import PriceDatabase


def test_db_crud_and_cooldown():
    db = PriceDatabase(":memory:")
    item = db.add_item(
        name="LEGO Star Wars Millennium Falcon",
        rrp_gbp=734.99,
        set_number="75192",
        target_price_gbp=550.00,
        min_discount_pct=20.0,
        url="https://www.argos.co.uk/product/12345",
        retailer="argos",
    )
    assert item.id == 1
    assert len(db.list_items()) == 1

    db.record_price(item.id, 540.00, 0.00, 540.00, "argos")
    history = db.get_price_history(item.id)
    assert len(history) == 1
    assert history[0].total_gbp == 540.00

    assert db.should_alert(item, 540.00, cooldown_hours=12) is True
    db.mark_alerted(item.id, 540.00)

    updated = db.get_item(item.id)
    assert updated is not None
    assert db.should_alert(updated, 540.00, cooldown_hours=12) is False
    assert db.should_alert(updated, 535.00, cooldown_hours=12) is True

    db.reset_alert_state(item.id)
    reset_item = db.get_item(item.id)
    assert reset_item is not None
    assert db.should_alert(reset_item, 540.00, cooldown_hours=12) is True

    assert db.remove_item(item.id) is True
    assert db.list_items() == []
    db.close()


def test_monthly_token_quota_governor_never_exhausts_early():
    db = PriceDatabase(":memory:")
    day_one = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    # 250 monthly limit -> 85% auto pool = 212 across 30 days -> ceil(212/30 * 1) = 8 on day 1
    allowed, cap = db.can_spend_token("serpapi", 250, manual_force=False, now=day_one)
    assert allowed is True
    assert cap == 8

    db.increment_monthly_usage("serpapi", count=8, now=day_one)
    allowed_after, _ = db.can_spend_token("serpapi", 250, manual_force=False, now=day_one)
    assert allowed_after is False

    # Manual force can still use the 15% manual reserve pool!
    manual_ok, safe_ceil = db.can_spend_token("serpapi", 250, manual_force=True, now=day_one)
    assert manual_ok is True
    assert safe_ceil == 248

    # Next day (Nov 2) unlocks another daily slice automatically
    day_two = day_one + timedelta(days=1)
    allowed_day2, cap_day2 = db.can_spend_token("serpapi", 250, manual_force=False, now=day_two)
    assert allowed_day2 is True
    assert cap_day2 >= 15
    db.close()


def test_purge_implausible_history_and_persisted_dev_mode():
    db = PriceDatabase(":memory:")
    assert db.get_dev_mode(default=False) is False
    db.set_dev_mode(True)
    assert db.get_dev_mode(default=False) is True
    db.set_dev_mode(False)
    assert db.get_dev_mode(default=True) is False

    item = db.add_item(name="Mercedes-AMG F1 W14", rrp_gbp=189.99, set_number="42171")
    # Bogus £19.99 accessory row + bogus 'other' row + genuine £154.99 Amazon UK row
    db.record_price(item.id, 19.99, 0.0, 19.99, "amazon_uk")
    db.record_price(item.id, 150.00, 0.0, 150.00, "other")
    db.record_price(item.id, 154.99, 0.0, 154.99, "amazon_uk")
    db.mark_alerted(item.id, 19.99)

    deleted, reset_count = db.purge_implausible_history()
    assert deleted == 2
    assert reset_count == 1
    assert db.get_historical_low(item.id) == 154.99
    refreshed = db.get_item(item.id)
    assert refreshed is not None and refreshed.last_alert_price_gbp is None
    db.close()


def test_items_due_for_check_prioritizes_unchecked_and_retiring():
    db = PriceDatabase(":memory:")
    now = datetime.now(timezone.utc)
    fresh = db.add_item(name="Fresh Set", rrp_gbp=99.99, set_number="10001")
    unchecked = db.add_item(name="Unchecked Set", rrp_gbp=99.99, set_number="10002")
    db.record_price(fresh.id, 89.99, 0.0, 89.99, "argos")

    due = db.get_items_due_for_check(min_age_hours=4.0, now=now)
    assert [i.set_number for i in due] == ["10002"]
    db.close()
