from src.bridge.wormhole_vaa import _extract_vaa_bytes


def test_extract_vaa_from_data_array_filters_sequence():
    body = {
        "data": [
            {"sequence": 6702, "vaa": "AQID"},
            {"sequence": 193618, "vaa": "0xdeadbeef"},
        ]
    }
    # Root extract should not pick wrong row
    assert _extract_vaa_bytes(body) is None
    match = next(r for r in body["data"] if r["sequence"] == 193618)
    assert _extract_vaa_bytes(match) == bytes.fromhex("deadbeef")
