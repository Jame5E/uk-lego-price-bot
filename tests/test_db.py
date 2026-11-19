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
    # Same price within cooldown should not re-alert
    assert db.should_alert(updated, 540.00, cooldown_hours=12) is False
    # Further drop by >= £1 should re-alert immediately
    assert db.should_alert(updated, 535.00, cooldown_hours=12) is True

    # When deal ends and returns to RRP, reset allows future deals to alert again
    db.reset_alert_state(item.id)
    reset_item = db.get_item(item.id)
    assert reset_item is not None
    assert db.should_alert(reset_item, 540.00, cooldown_hours=12) is True

    assert db.remove_item(item.id) is True
    assert db.list_items() == []
    db.close()
