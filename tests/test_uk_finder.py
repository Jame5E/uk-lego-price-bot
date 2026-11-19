import asyncio
import time

from src.lego_bot import bot as _bot_mod  # noqa: F401
from src.lego_bot.deal_filter import is_valid_lego_set_listing
from src.lego_bot.shipping import APPROVED_UK_RETAILERS, UKRetailer
from src.lego_bot.uk_finder import (
    _PRICE_CACHE,
    _SOURCE_FIRST_FAIL_AT,
    _SOURCE_OUTAGE_ALERTED,
    UKOffer,
    _record_source_block,
    _record_source_ok,
    build_lego_uk_product_url,
    build_uk_search_urls,
    clean_set_display_name,
    extract_first_gbp_price_from_search,
    find_best_uk_offer,
    get_price_cache_stats,
    get_source_health,
    parse_amazon_uk_product_html,
    parse_amazon_uk_search_html,
    parse_argos_search_html,
    parse_john_lewis_search_html,
    parse_serpapi_shopping_payload,
    pop_long_outage_sources,
)


def test_clean_set_display_name_and_lego_url():
    assert (
        clean_set_display_name("Licensed: 42171 Mercedes-AMG F1 W14 E Performance", "42171")
        == "Mercedes-AMG F1 W14 E Performance"
    )
    assert (
        build_lego_uk_product_url("Licensed: 42171 Mercedes-AMG F1 W14 E Performance", "42171")
        == "https://www.lego.com/en-gb/product/mercedes-amg-f1-w14-e-performance-42171"
    )
    assert (
        build_lego_uk_product_url("LEGO Icons The Lord of the Rings: Rivendell", "10316")
        == "https://www.lego.com/en-gb/product/the-lord-of-the-rings-rivendell-10316"
    )
    assert build_lego_uk_product_url("LEGO Set #42171", "42171") == "https://www.lego.com/en-gb/product/42171"


def test_deal_filter_blocks_wall_mounts_lights_and_outliers():
    # Real set passes
    assert is_valid_lego_set_listing("LEGO 42171 Technic Mercedes-AMG F1 W14 E Performance", "42171", 154.99, 189.99)
    # Wall mounts, LED kits, cases, used items, and <40% RRP outliers are rejected
    assert not is_valid_lego_set_listing("3D Wall Mount for Lego UCS Millennium Falcon Model 75192", "75192", 23.11, 734.99)
    assert not is_valid_lego_set_listing("LED Light Kit Compatible with LEGO 42171", "42171", 29.99, 189.99)
    assert not is_valid_lego_set_listing("Acrylic Display Case for 42171", "42171", 89.99, 189.99)
    assert not is_valid_lego_set_listing("LEGO 42171 Technic Set - Pre-owned Incomplete", "42171", 120.00, 189.99)
    assert not is_valid_lego_set_listing("LEGO 42171 Technic Set", "42171", 45.00, 189.99)


def test_build_uk_search_urls_covers_approved_eight_retailers():
    urls = build_uk_search_urls("LEGO Rivendell", "10316")
    retailers = {r for r, _ in urls}
    assert retailers == set(APPROVED_UK_RETAILERS)


def test_direct_uk_store_html_parsers():
    amz_uk_html = """
    <html><body>
      <span id="glow-ingress-line2">United Kingdom</span>
      <div data-component-type="s-search-result" data-asin="B0CFW12345">
        <h2>3D Wall Mount for Lego 42171</h2>
        <span class="a-price"><span class="a-offscreen">£19.99</span></span>
      </div>
      <div data-component-type="s-search-result" data-asin="B0CFVY5L22">
        <h2>LEGO 42171 Technic Mercedes-AMG F1 W14 E Performance Race Car Set</h2>
        <span class="a-price"><span class="a-offscreen">£154.99</span></span>
      </div>
    </body></html>
    """
    amz_offers = parse_amazon_uk_search_html(amz_uk_html, "42171", rrp_gbp=189.99)
    assert len(amz_offers) == 1
    assert amz_offers[0].item_price_gbp == 154.99
    assert amz_offers[0].url == "https://www.amazon.co.uk/dp/B0CFVY5L22"

    # Non-UK server IP ex-VAT restoration (129.16 * 1.20 = 154.99)
    amz_export_html = amz_uk_html.replace("United Kingdom", "United States").replace("£154.99", "£129.16")
    amz_export_offers = parse_amazon_uk_search_html(amz_export_html, "42171", rrp_gbp=189.99)
    assert len(amz_export_offers) == 1
    assert amz_export_offers[0].item_price_gbp == 154.99

    jl_html = """
    <html><body>
      <article>
        <a href="/lego-technic-42171-mercedes/p111524143">LEGO Technic 42171 Mercedes-AMG F1 £170.99</a>
      </article>
    </body></html>
    """
    jl_offers = parse_john_lewis_search_html(jl_html, "42171", rrp_gbp=189.99)
    assert len(jl_offers) == 1
    assert jl_offers[0].total_gbp == 170.99

    # Argos parser ignores monthly finance text ("from £7.63 per month")
    argos_html = """
    <html><body>
      <div data-test="component-product-card">
        <a href="/product/5257195">LEGO Technic Mercedes-AMG F1 W14 Set 42171</a>
        <span>from £7.63 per month</span>
        <strong>£165.00</strong>
      </div>
    </body></html>
    """
    argos_offers = parse_argos_search_html(argos_html, "42171", rrp_gbp=189.99, prefer_click_and_collect=True)
    assert len(argos_offers) == 1
    assert argos_offers[0].item_price_gbp == 165.00
    assert argos_offers[0].url == "https://www.argos.co.uk/product/5257195"


def test_serpapi_shopping_parser_strictly_filters_to_eight_uk_stores():
    payload = {
        "shopping_results": [
            {
                "title": "LEGO Technic 42171 Mercedes-AMG F1 W14",
                "source": "Zatu Games",
                "price": "£139.99",
                "extracted_price": 139.99,
                "link": "https://www.board-game.co.uk/product/42171",
            },
            {
                "title": "LEGO Technic 42171 Mercedes-AMG F1 W14",
                "source": "Smyths Toys Superstores",
                "price": "£149.99",
                "extracted_price": 149.99,
                "delivery": "Free delivery",
                "link": "https://www.smythstoys.com/uk/en-gb/toys/lego/p/234567",
            },
            {
                "title": "LEGO Technic 42171 Mercedes-AMG F1 W14",
                "source": "Argos",
                "price": "£145.00",
                "extracted_price": 145.00,
                "second_hand_condition": "refurbished",
                "link": "https://www.argos.co.uk/product/5257195",
            },
        ]
    }
    offers = parse_serpapi_shopping_payload(payload, "42171", rrp_gbp=189.99)
    assert len(offers) == 1
    assert offers[0].retailer == UKRetailer.SMYTHS
    assert offers[0].total_gbp == 149.99


def test_extract_first_gbp_price_and_price_cache():
    html = "<html><body><div>LEGO Icons Rivendell 10316 - £349.99</div></body></html>"
    assert extract_first_gbp_price_from_search(html, "10316") == 349.99

    _PRICE_CACHE.clear()
    _PRICE_CACHE["42171|0|0|"] = (
        time.monotonic(),
        UKOffer(
            retailer=UKRetailer.JOHN_LEWIS,
            url="https://www.johnlewis.com/lego-42171/p111524143",
            item_price_gbp=170.99,
            shipping_gbp=0.0,
            total_gbp=170.99,
            shipping_note="Free UK delivery",
        ),
        [],
    )
    count, ttl = get_price_cache_stats()
    assert count == 1
    assert ttl == 1800
    assert isinstance(get_source_health(), dict)

    offer, notes = asyncio.run(
        find_best_uk_offer(None, name="Mercedes-AMG F1 W14", set_number="42171")  # type: ignore[arg-type]
    )
    assert offer is not None
    assert offer.total_gbp == 170.99
    assert any("cache" in n.lower() for n in notes)
    _PRICE_CACHE.clear()


def test_amazon_uk_product_page_verification():
    dp_3p_html = """
    <html><body>
      <span id="glow-ingress-line2">United Kingdom</span>
      <span id="productTitle">LEGO Technic Mercedes-AMG F1 W14 E Performance 42171</span>
      <div id="availability">In stock</div>
      <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">£145.00</span></span></div>
      <div id="merchantInfoFeature_feature_div">Dispatches from and sold by BrickDealsUK</div>
      <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">£5.99 delivery Friday</div>
    </body></html>
    """
    offer, bad = parse_amazon_uk_product_html(
        dp_3p_html, "https://www.amazon.co.uk/dp/B0CFVZ74KT", "42171", rrp_gbp=189.99, has_amazon_prime=True
    )
    assert bad is False
    assert offer is not None
    assert offer.item_price_gbp == 145.00
    assert offer.shipping_gbp == 5.99
    assert offer.total_gbp == 150.99
    assert "3rd-party seller" in offer.shipping_note

    dp_oos_html = dp_3p_html.replace("In stock", "Currently unavailable.")
    oos_offer, oos_bad = parse_amazon_uk_product_html(
        dp_oos_html, "https://www.amazon.co.uk/dp/B0CFVZ74KT", "42171", rrp_gbp=189.99
    )
    assert oos_offer is None
    assert oos_bad is True


def test_john_lewis_and_argos_ignore_was_rrp_save_and_finance():
    jl_sale_html = """
    <html><body>
      <article>
        <a href="/lego-technic-42171-mercedes/p111524143">
          LEGO Technic 42171 Mercedes-AMG F1 W14
          <s>Was £189.99</s>
          <span>Save £40.00</span>
          <p class="Price_price__abc">
            <span class="price_price__now__xyz">£149.99</span>
          </p>
        </a>
      </article>
    </body></html>
    """
    jl_offers = parse_john_lewis_search_html(jl_sale_html, "42171", rrp_gbp=189.99)
    assert len(jl_offers) == 1
    assert jl_offers[0].item_price_gbp == 149.99

    argos_sale_html = """
    <html><body>
      <div data-test="component-product-card">
        <a href="/product/5257195">LEGO Technic Mercedes-AMG F1 W14 Set 42171</a>
        <span>RRP £189.99</span>
        <span>Save £40.00</span>
        <span>from £7.63 per month</span>
        <div class="ds-c-price">
          <span class="ds-c-price__price">£149.99</span>
        </div>
      </div>
    </body></html>
    """
    argos_offers = parse_argos_search_html(argos_sale_html, "42171", rrp_gbp=189.99, prefer_click_and_collect=True)
    assert len(argos_offers) == 1
    assert argos_offers[0].item_price_gbp == 149.99


def test_long_outage_alert_fires_once_and_resets():
    _SOURCE_FIRST_FAIL_AT.clear()
    _SOURCE_OUTAGE_ALERTED.clear()

    _record_source_block("argos_direct")
    _SOURCE_FIRST_FAIL_AT["argos_direct"] = time.monotonic() - 7500.0
    first = pop_long_outage_sources(min_outage_seconds=7200.0)
    assert first == ["argos_direct"]

    # Second call for the same ongoing outage must not re-alert
    second = pop_long_outage_sources(min_outage_seconds=7200.0)
    assert second == []

    # Recovery resets state so a future outage can alert again
    _record_source_ok("argos_direct")
    _record_source_block("argos_direct")
    _SOURCE_FIRST_FAIL_AT["argos_direct"] = time.monotonic() - 7500.0
    third = pop_long_outage_sources(min_outage_seconds=7200.0)
    assert third == ["argos_direct"]

    _SOURCE_FIRST_FAIL_AT.clear()
    _SOURCE_OUTAGE_ALERTED.clear()
