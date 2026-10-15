from iowait import fetch_price, total_price


def test_fetch_price():
    assert fetch_price("ab") == 19.5


def test_total_price():
    assert total_price(["ab", "ab"]) == 39.0
    assert total_price([]) == 0.0
