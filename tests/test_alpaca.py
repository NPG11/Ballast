"""Alpaca adapter tests against a fake Alpaca API (no network)."""
import json
from decimal import Decimal as D

import pytest

import adapters.alpaca as alpaca
from adapters.broker import FAILED, FILLED, PARTIAL, PENDING
from engine import Side, Trade


class FakeAlpacaAPI:
    def __init__(self):
        self.calls, self.orders = [], {}

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith("https://paper-api.alpaca.markets/v2/")      # always paper
        assert headers["APCA-API-KEY-ID"] == "PKTEST"
        path = url.split("alpaca.markets")[1]
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        if path == "/v2/positions":
            return 200, [{"symbol": "VTI", "qty": "30", "current_price": "280.00"},
                         {"symbol": "OLD", "qty": "0", "current_price": "1"}]
        if path == "/v2/account":
            return 200, {"cash": "2000.004", "non_marginable_buying_power": "1999.50"}
        if path == "/v2/clock":
            return 200, {"is_open": False}
        if path == "/v2/orders" and method == "POST":
            if data["client_order_id"] in self.orders:
                return 422, {"message": "client_order_id must be unique"}
            self.orders[data["client_order_id"]] = data
            return 200, {"id": "oid-" + data["client_order_id"]}
        if path.startswith("/v2/orders:by_client_order_id"):
            return 200, {"id": "oid-" + path.split("=")[1]}
        if path.startswith("/v2/orders/"):
            status = {"oid-a": "filled", "oid-b": "partially_filled", "oid-c": "rejected"}.get(path[11:], "new")
            return 200, {"id": path[11:], "symbol": "VTI", "side": "sell", "status": status,
                         "filled_qty": "2", "filled_avg_price": "280.5", "notional": "561"}
        if path == "/v2/bad":
            return 403, {"message": "forbidden"}
        raise AssertionError(path)


@pytest.fixture
def api(monkeypatch):
    fake = FakeAlpacaAPI()
    monkeypatch.setattr(alpaca, "_http", fake)
    return fake


def broker():
    return alpaca.AlpacaBroker("PKTEST", "secret")


def test_portfolio_and_account(api):
    p = broker().get_portfolio()
    assert [(h.symbol, h.qty, h.price) for h in p.holdings] == [("VTI", D("30"), D("280.00"))]
    assert p.cash == D("2000.00")
    assert broker().buying_power() == D("1999.50")
    assert broker().is_market_open() is False


def test_orders_are_dollar_based_market_day_orders(api):
    oid = broker().submit_order(Trade("VTI", Side.SELL, D("960"), "us_stocks"), "BL-1-0")
    assert oid == "oid-BL-1-0"
    assert api.calls[-1][2] == {"symbol": "VTI", "notional": "960.00", "side": "sell", "type": "market",
                                "time_in_force": "day", "client_order_id": "BL-1-0"}


def test_retrying_an_order_never_duplicates_it(api):
    t = Trade("BND", Side.BUY, D("480"), "bonds")
    first = broker().submit_order(t, "BL-1-1")
    again = broker().submit_order(t, "BL-1-1")
    assert first == again and len(api.orders) == 1


def test_order_status_mapping(api):
    b = broker()
    assert b.get_order("oid-a").status == FILLED and b.get_order("oid-a").filled_notional == D("561.00")
    assert b.get_order("oid-b").status == PARTIAL
    assert b.get_order("oid-c").status == FAILED
    assert b.get_order("oid-x").status == PENDING


def test_errors_are_raised_clearly(api):
    with pytest.raises(alpaca.AlpacaError, match="403: forbidden"):
        broker()._call("GET", "/v2/bad")


def test_keys_from_secrets_manager(monkeypatch):
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws
    from adapters.secrets import load_alpaca_keys
    for k in ("ALPACA_KEY", "ALPACA_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.setenv("ALPACA_SECRET_ID", "ballast/alpaca")
    load_alpaca_keys.cache_clear()
    with mock_aws():
        boto3.client("secretsmanager").create_secret(
            Name="ballast/alpaca", SecretString=json.dumps({"key": "PKFROMAWS", "secret": "s"}))
        assert load_alpaca_keys() == ("PKFROMAWS", "s")
    load_alpaca_keys.cache_clear()
