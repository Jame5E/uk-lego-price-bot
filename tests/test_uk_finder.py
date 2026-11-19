from src.lego_bot.shipping import UKRetailer
from src.lego_bot.uk_finder import (
    build_lego_uk_product_url,
    build_uk_search_urls,
    extract_first_gbp_price_from_search,
)


def test_build_lego_uk_product_url():
    assert (
        build_lego_uk_product_url("Licensed: 42171 Mercedes-AMG F1 W14 E Performance", "42171")
        == "https://www.lego.com/en-gb/product/mercedes-amg-f1-w14-e-performance-42171"
    )
    assert (
        build_lego_uk_product_url("LEGO Icons The Lord of the Rings: Rivendell", "10316")
        == "https://www.lego.com/en-gb/product/the-lord-of-the-rings-rivendell-10316"
    )
    assert build_lego_uk_product_url("LEGO Set #42171", "42171") == "https://www.lego.com/en-gb/product/42171"


def test_build_uk_search_urls_includes_major_uk_retailers():
    urls = build_uk_search_urls("LEGO Rivendell", "10316")
    retailers = {r for r, _ in urls}
    assert UKRetailer.LEGO_UK in retailers
    assert UKRetailer.SMYTHS in retailers
    assert UKRetailer.JOHN_LEWIS in retailers
    assert UKRetailer.ARGOS not in retailers
    assert UKRetailer.AMAZON_UK not in retailers


def test_extract_first_gbp_price_from_search():
    html = """
    <html><body>
      <div>LEGO Icons The Lord of the Rings: Rivendell 10316 Building Set - £349.99</div>
    </body></html>
    """
    price = extract_first_gbp_price_from_search(html, "10316")
    assert price == 349.99

    jsonld_html = """
    <html><head><script type="application/ld+json">
    {"itemListElement": [{"item": {"name": "LEGO 42171", "offers": {"@type": "AggregateOffer", "priceCurrency": "GBP", "lowPrice": "154.95"}}}]}
    </script></head></html>
    """
    assert extract_first_gbp_price_from_search(jsonld_html, "42171") == 154.95


def test_price_cache_and_bot_imports():
    import asyncio
    import time
    from src.lego_bot import bot as _bot_mod  # noqa: F401
    from src.lego_bot.uk_finder import (
        _PRICE_CACHE,
        UKOffer,
        find_best_uk_offer,
        get_price_cache_stats,
        get_source_health,
    )

    _PRICE_CACHE.clear()
    _PRICE_CACHE["42171|0|0|"] = (
        time.monotonic(),
        UKOffer(
            retailer=UKRetailer.LEGO_UK,
            url="https://www.brickwatch.net/en-GB/set/42171/",
            item_price_gbp=154.95,
            shipping_gbp=0.0,
            total_gbp=154.95,
            shipping_note="Free UK delivery",
        ),
        [],
    )
    count, ttl = get_price_cache_stats()
    assert count == 1
    assert ttl == 600
    assert isinstance(get_source_health(), dict)

    offer, notes = asyncio.run(
        find_best_uk_offer(None, name="LEGO 42171", set_number="42171")  # type: ignore[arg-type]
    )
    assert offer is not None
    assert offer.total_gbp == 154.95
    assert any("cache" in n.lower() for n in notes)
    _PRICE_CACHE.clear()
