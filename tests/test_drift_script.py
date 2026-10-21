"""The demo 'drift' script, against a fake Alpaca API."""
import sys

import pytest

import adapters.alpaca as alpaca


class API:
    def __init__(self, open_=True):
        self.open, self.posted = open_, []

    def __call__(self, method, url, headers, body, timeout):
        import json
        path = url.split("alpaca.markets")[1]
        if path == "/v2/clock":
            return 200, {"is_open": self.open}
        if path == "/v2/positions":
            return 200, [{"symbol": "VTI", "qty": "200", "current_price": "300"},
                         {"symbol": "BND", "qty": "250", "current_price": "80"}]
        if path == "/v2/account":
            return 200, {"cash": "20000"}
        if path == "/v2/orders":
            d = json.loads(body); self.posted.append(d)
            return 200, {"id": d["client_order_id"]}
        if path.startswith("/v2/orders/"):
            oid = path[len("/v2/orders/"):]
            side = "sell" if oid.endswith("sell") else "buy"
            return 200, {"id": oid, "symbol": "X", "side": side, "status": "filled",
                         "filled_qty": "1", "filled_avg_price": "8000", "notional": "8000"}
        raise AssertionError(path)


def run(monkeypatch, api, *args):
    monkeypatch.setattr(alpaca, "_http", api)
    monkeypatch.setenv("ALPACA_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET", "s")
    from adapters.secrets import load_alpaca_keys
    load_alpaca_keys.cache_clear()
    monkeypatch.setattr(sys, "argv", ["drift_alpaca", *args])
    from scripts import drift_alpaca
    try:
        drift_alpaca.main()
    finally:
        load_alpaca_keys.cache_clear()


def test_sells_bonds_then_buys_stocks_with_unique_ids(monkeypatch):
    api = API()
    run(monkeypatch, api)
    assert [(o["symbol"], o["side"], o["notional"]) for o in api.posted] == [
        ("BND", "sell", "8000.00"), ("VTI", "buy", "8000.00")]
    assert api.posted[0]["client_order_id"].startswith("drift-")


def test_refuses_when_market_closed(monkeypatch):
    with pytest.raises(SystemExit, match="Market is closed"):
        run(monkeypatch, API(open_=False))


def test_refuses_to_sell_more_than_held(monkeypatch):
    api = API()
    with pytest.raises(SystemExit, match="only hold"):
        run(monkeypatch, api, "--amount", "50000")
    assert api.posted == []
