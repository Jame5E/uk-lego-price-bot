from src.lego_bot.shipping import UKRetailer
from src.lego_bot.uk_finder import (
    build_uk_search_urls,
    extract_first_gbp_price_from_search,
    parse_hotukdeals_rss,
    parse_hotukdeals_search_html,
)


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


def test_parse_hotukdeals_rss():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>LEGO Icons 10316 Rivendell - £349.99 at Argos</title>
        <link>https://www.hotukdeals.com/deals/lego-10316-test</link>
        <description>In stock at Argos</description>
      </item>
      <item>
        <title>[EXPIRED] LEGO 10316 Rivendell - £299.99 at Amazon</title>
        <link>https://www.hotukdeals.com/deals/lego-10316-expired</link>
        <description>Expired</description>
      </item>
    </channel></rss>
    """
    offers = parse_hotukdeals_rss(rss, "10316", prefer_click_and_collect=True)
    assert len(offers) == 1
    assert offers[0].retailer == UKRetailer.ARGOS
    assert offers[0].item_price_gbp == 349.99
    assert offers[0].total_gbp == 349.99
    assert "hotukdeals.com" in offers[0].url


def test_parse_hotukdeals_search_html():
    html = """
    <html><body>
      <article>
        <a href="/deals/lego-42171-smyths">LEGO Technic 42171 Mercedes F1 - £149.99 at Smyths</a>
      </article>
      <article>
        <a href="/deals/lego-42171-old">Expired LEGO 42171 - £129.99 at Argos</a>
      </article>
    </body></html>
    """
    offers = parse_hotukdeals_search_html(html, "42171", prefer_click_and_collect=True)
    assert len(offers) == 1
    assert offers[0].retailer == UKRetailer.SMYTHS
    assert offers[0].item_price_gbp == 149.99
    assert offers[0].url == "https://www.hotukdeals.com/deals/lego-42171-smyths"


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
