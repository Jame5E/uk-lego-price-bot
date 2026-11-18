from cron_check import build_discord_payload, sync_wishlist_to_db
from src.lego_bot.db import PriceDatabase
from src.lego_bot.predictor import analyze_price
from src.lego_bot.shipping import UKRetailer


def test_sync_wishlist_idempotent():
    db = PriceDatabase(":memory:")
    entries = [
        {
            "name": "LEGO Rivendell",
            "set_number": "10316",
            "rrp_gbp": 429.99,
            "target_price_gbp": 340.00,
            "min_discount_pct": 15.0,
            "url": "https://www.amazon.co.uk/dp/B0BV7M5R6Q",
            "is_retiring": False,
        }
    ]
    first = sync_wishlist_to_db(db, entries)
    second = sync_wishlist_to_db(db, entries)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].id == second[0].id
    assert len(db.list_items()) == 1
    db.close()


def test_sync_minimal_wishlist_only_name_and_set_number():
    db = PriceDatabase(":memory:")
    entries = [{"name": "LEGO Rivendell", "set_number": "10316"}]
    items = sync_wishlist_to_db(db, entries)
    assert len(items) == 1
    assert items[0].set_number == "10316"
    assert items[0].rrp_gbp == 0.0
    assert items[0].min_discount_pct == 15.0
    db.close()


def test_build_discord_payload():
    db = PriceDatabase(":memory:")
    item = db.add_item(
        name="LEGO Rivendell",
        rrp_gbp=429.99,
        set_number="10316",
        url="https://www.argos.co.uk/product/12345",
        retailer="argos",
    )
    analysis = analyze_price(rrp_gbp=429.99, item_price_gbp=299.99, shipping_gbp=0.0)
    payload = build_discord_payload("987654321", item, analysis, UKRetailer.ARGOS, "Free Click & Collect", simulated=True)
    assert "<@987654321>" in payload["content"]
    assert "[TEST] LEGO Deal Alert: LEGO Rivendell (#10316)" == payload["embeds"][0]["title"]
    assert "£299.99" in payload["embeds"][0]["fields"][0]["value"]
    db.close()
