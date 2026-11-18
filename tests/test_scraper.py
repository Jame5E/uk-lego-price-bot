from src.lego_bot.scraper import extract_asin_from_amazon_url, parse_price_from_html
from src.lego_bot.shipping import UKRetailer


def test_extract_asin():
    assert extract_asin_from_amazon_url("https://www.amazon.co.uk/LEGO-Set/dp/B09G3HRMVB/ref=sr_1_1") == "B09G3HRMVB"
    assert extract_asin_from_amazon_url("https://www.argos.co.uk/product/12345") is None


def test_parse_jsonld_gbp():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org/",
      "@type": "Product",
      "name": "LEGO Rivendell",
      "offers": {
        "@type": "Offer",
        "priceCurrency": "GBP",
        "price": "369.99",
        "availability": "https://schema.org/InStock"
      }
    }
    </script>
    </head></html>
    """
    quote = parse_price_from_html(html, "https://www.argos.co.uk/product/99999")
    assert quote is not None
    assert quote.price_gbp == 369.99
    assert quote.retailer == UKRetailer.ARGOS
    assert quote.in_stock is True


def test_parse_meta_gbp():
    html = """
    <html><head>
      <meta property="product:price:amount" content="44.99" />
      <meta property="product:price:currency" content="GBP" />
    </head></html>
    """
    quote = parse_price_from_html(html, "https://www.smythstoys.com/uk/en-gb/p/12345")
    assert quote is not None
    assert quote.price_gbp == 44.99
    assert quote.retailer == UKRetailer.SMYTHS
