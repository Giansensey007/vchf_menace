from src.vnx.auth import auth_headers, sort_object_deep


def test_sort_object_deep():
    assert sort_object_deep({"b": 1, "a": {"d": 2, "c": 3}}) == {"a": {"c": 3, "d": 2}, "b": 1}


def test_sign_deterministic_payload():
    h = auth_headers("/api/v1/private/accountBalance", {})
    assert "x-app-public-key" in h
    assert "x-app-nonce" in h
    assert "x-app-signed-data" in h


def test_auth_headers_shape():
    h = auth_headers("/api/v1/private/depositAddress", {"asset": "VCHF", "blockchain": "BASE"})
    assert h["Content-Type"] == "application/json"


def test_add_order_body_matches_signature():
    from src.vnx.auth import canonical_vnx_body, sort_object_deep

    payload = {
        "timestamp": "2026-01-01T00:00:00Z",
        "clordid": "test-abc",
        "symbol": "USDC/CHF",
        "side": "Buy",
        "ordtype": "Limit",
        "timeinforce": "FOK",
        "orderqty": 30.0,
        "price": 0.92,
    }
    body = canonical_vnx_body(payload)
    assert '"orderqty":30,' in body or body.endswith('"orderqty":30}')
    h = auth_headers("/api/v1/private/addOrder", sort_object_deep(payload), nonce=1234567890)
    assert "x-app-signed-data" in h


def test_vnx_min_order():
    from src.quotes.vnx import VNX_MIN_ORDER

    assert VNX_MIN_ORDER["VCHF"] == 30.0
