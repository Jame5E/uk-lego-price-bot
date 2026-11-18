from src.lego_bot.shipping import UKRetailer
from src.lego_bot.uk_finder import build_uk_search_urls, extract_first_gbp_price_from_search, parse_hotukdeals_rss


def test_build_uk_search_urls_includes_major_uk_retailers():
    urls = build_uk_search_urls("LEGO Rivendell", "10316")
    retailers = {r for r, _ in urls}
    assert UKRetailer.LEGO_UK in retailers
    assert UKRetailer.ARGOS in retailers
    assert UKRetailer.SMYTHS in retailers
    assert UKRetailer.JOHN_LEWIS in retailers
    assert UKRetailer.AMAZON_UK in retailers


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
