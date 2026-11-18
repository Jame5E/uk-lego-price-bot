from src.lego_bot.shipping import UKRetailer, detect_retailer_from_url, estimate_uk_shipping


def test_detect_retailer_from_url():
    assert detect_retailer_from_url("https://www.argos.co.uk/product/12345") == UKRetailer.ARGOS
    assert detect_retailer_from_url("https://www.amazon.co.uk/dp/B09ABC1234") == UKRetailer.AMAZON_UK
    assert detect_retailer_from_url("https://www.lego.com/en-gb/product/10316") == UKRetailer.LEGO_UK
    assert detect_retailer_from_url("https://www.smythstoys.com/uk/en-gb/p/123") == UKRetailer.SMYTHS
    assert detect_retailer_from_url("https://www.johnlewis.com/lego/p123") == UKRetailer.JOHN_LEWIS


def test_amazon_uk_shipping():
    # Under £35 without Prime -> £4.49 shipping
    res = estimate_uk_shipping(25.00, UKRetailer.AMAZON_UK, has_amazon_prime=False)
    assert res.shipping_gbp == 4.49
    assert res.total_gbp == 29.49

    # Over £35 -> free
    res_free = estimate_uk_shipping(40.00, UKRetailer.AMAZON_UK, has_amazon_prime=False)
    assert res_free.shipping_gbp == 0.0
    assert res_free.total_gbp == 40.00

    # Under £35 with Prime -> free
    res_prime = estimate_uk_shipping(25.00, UKRetailer.AMAZON_UK, has_amazon_prime=True)
    assert res_prime.shipping_gbp == 0.0


def test_argos_shipping():
    delivery = estimate_uk_shipping(50.00, UKRetailer.ARGOS, prefer_click_and_collect=False)
    assert delivery.shipping_gbp == 3.95
    assert delivery.total_gbp == 53.95

    cc = estimate_uk_shipping(50.00, UKRetailer.ARGOS, prefer_click_and_collect=True)
    assert cc.shipping_gbp == 0.0
    assert cc.total_gbp == 50.00
