from src.lego_bot.shipping import UKRetailer
from src.lego_bot.uk_finder import build_uk_search_urls, extract_first_gbp_price_from_search


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
