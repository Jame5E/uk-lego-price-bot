from src.lego_bot.shipping import (
    APPROVED_UK_RETAILERS,
    UKRetailer,
    detect_retailer_from_url,
    estimate_uk_shipping,
    match_approved_uk_retailer,
)


def test_approved_uk_retailers_exact_eight():
    assert len(APPROVED_UK_RETAILERS) == 8
    assert set(APPROVED_UK_RETAILERS) == {
        UKRetailer.AMAZON_UK,
        UKRetailer.LEGO_UK,
        UKRetailer.JOHN_LEWIS,
        UKRetailer.SMYTHS,
        UKRetailer.ARGOS,
        UKRetailer.CURRYS,
        UKRetailer.ENTERTAINER,
        UKRetailer.DISNEY_STORE,
    }


def test_detect_and_match_approved_retailers_and_reject_others():
    assert detect_retailer_from_url("https://www.argos.co.uk/product/12345") == UKRetailer.ARGOS
    assert detect_retailer_from_url("https://www.amazon.co.uk/dp/B09ABC1234") == UKRetailer.AMAZON_UK
    assert detect_retailer_from_url("https://www.lego.com/en-gb/product/10316") == UKRetailer.LEGO_UK
    assert detect_retailer_from_url("https://www.smythstoys.com/uk/en-gb/p/123") == UKRetailer.SMYTHS
    assert detect_retailer_from_url("https://www.johnlewis.com/lego/p123") == UKRetailer.JOHN_LEWIS
    assert detect_retailer_from_url("https://www.currys.co.uk/products/lego-123.html") == UKRetailer.CURRYS
    assert detect_retailer_from_url("https://www.thetoyshop.com/lego/p/123") == UKRetailer.ENTERTAINER
    assert detect_retailer_from_url("https://www.disneystore.co.uk/lego-75379.html") == UKRetailer.DISNEY_STORE

    # Unapproved / marketplace / foreign stores must be rejected
    assert match_approved_uk_retailer("Very") is None
    assert match_approved_uk_retailer("Zatu Games") is None
    assert match_approved_uk_retailer("Zavvi") is None
    assert match_approved_uk_retailer("Argos on eBay") is None
    assert match_approved_uk_retailer("https://www.amazon.com/dp/B09ABC1234") is None
    assert match_approved_uk_retailer("https://www.ebay.co.uk/itm/12345") is None


def test_amazon_uk_shipping():
    res = estimate_uk_shipping(25.00, UKRetailer.AMAZON_UK, has_amazon_prime=False)
    assert res.shipping_gbp == 4.49
    assert res.total_gbp == 29.49

    res_free = estimate_uk_shipping(40.00, UKRetailer.AMAZON_UK, has_amazon_prime=False)
    assert res_free.shipping_gbp == 0.0
    assert res_free.total_gbp == 40.00

    res_prime = estimate_uk_shipping(25.00, UKRetailer.AMAZON_UK, has_amazon_prime=True)
    assert res_prime.shipping_gbp == 0.0


def test_argos_currys_entertainer_disney_and_explicit_shipping():
    delivery = estimate_uk_shipping(50.00, UKRetailer.ARGOS, prefer_click_and_collect=False)
    assert delivery.shipping_gbp == 3.95
    assert delivery.total_gbp == 53.95

    cc = estimate_uk_shipping(50.00, UKRetailer.ARGOS, prefer_click_and_collect=True)
    assert cc.shipping_gbp == 0.0
    assert cc.total_gbp == 50.00

    currys_free = estimate_uk_shipping(45.00, UKRetailer.CURRYS)
    assert currys_free.shipping_gbp == 0.0

    ent_small = estimate_uk_shipping(25.00, UKRetailer.ENTERTAINER)
    assert ent_small.shipping_gbp == 3.99

    disney_over_50 = estimate_uk_shipping(59.99, UKRetailer.DISNEY_STORE)
    assert disney_over_50.shipping_gbp == 0.0

    explicit = estimate_uk_shipping(
        149.99,
        UKRetailer.ARGOS,
        prefer_click_and_collect=False,
        explicit_delivery_text="£3.95 delivery",
    )
    assert explicit.shipping_gbp == 3.95
    assert explicit.total_gbp == 153.94
