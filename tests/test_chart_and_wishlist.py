from pathlib import Path

from src.lego_bot.chart import generate_price_chart
from src.lego_bot.uk_finder import default_box_art_url, extract_first_gbp_price_from_search
from src.lego_bot.wishlist_store import load_wishlist_entries, remove_wishlist_entry, upsert_wishlist_entry


def test_generate_price_chart_returns_valid_png():
    png = generate_price_chart([429.99, 389.99, 349.99, 319.99], 429.99)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 500


def test_out_of_stock_is_filtered():
    html = "<div>LEGO Rivendell 10316 - £299.99 Out of Stock</div>"
    assert extract_first_gbp_price_from_search(html, "10316") is None


def test_box_art_url():
    assert default_box_art_url("10316") == "https://images.brickset.com/sets/images/10316-1.jpg"


def test_wishlist_store_upsert_and_remove(tmp_path: Path):
    wl = str(tmp_path / "wishlist.json")
    upsert_wishlist_entry(wl, {"name": "LEGO Rivendell", "set_number": "10316"})
    upsert_wishlist_entry(wl, {"name": "LEGO Rivendell", "set_number": "10316", "notify_user_ids": ["111", "222"]})
    entries = load_wishlist_entries(wl)
    assert len(entries) == 1
    assert entries[0]["notify_user_ids"] == ["111", "222"]

    assert remove_wishlist_entry(wl, "10316") is True
    assert load_wishlist_entries(wl) == []
