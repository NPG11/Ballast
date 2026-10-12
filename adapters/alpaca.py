"""Alpaca paper-trading adapter, using Alpaca's REST API directly (no extra packages).

Paper trading is forced: the base URL is the paper endpoint and can't be changed.

Keys come from (in order): the constructor, ALPACA_KEY / ALPACA_SECRET, or AWS Secrets Manager
when ALPACA_SECRET_ID is set (that's how the deployed Lambda gets them).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from engine import Holding, Portfolio, Side, Trade, money

from .broker import FAILED, FILLED, PARTIAL, PENDING, OrderInfo
from .secrets import load_alpaca_keys

PAPER_URL = "https://paper-api.alpaca.markets"
_FAILED = {"canceled", "expired", "rejected", "suspended", "stopped", "done_for_day"}


class AlpacaError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Alpaca {status}: {message}")


def _http(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    """One HTTP call. Returns (status, parsed JSON). Split out so tests can replace it."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"message": raw.decode(errors="replace")}
        return e.code, data


class AlpacaBroker:
    def __init__(self, key: str | None = None, secret: str | None = None, timeout: float = 15.0):
        if not (key and secret):
            key, secret = load_alpaca_keys()
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                         "Content-Type": "application/json", "Accept": "application/json"}
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = PAPER_URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
        status, data = _http(method, url, self._headers,
                             json.dumps(body).encode() if body is not None else None, self.timeout)
        if status >= 400:
            msg = (data or {}).get("message", "request failed") if isinstance(data, dict) else str(data)
            raise AlpacaError(status, msg)
        return data

    def get_portfolio(self) -> Portfolio:
        holdings = tuple(
            Holding(p["symbol"], Decimal(p["qty"]), Decimal(p["current_price"]))
            for p in self._call("GET", "/v2/positions")
            if Decimal(p["qty"]) > 0
        )
        return Portfolio(holdings, money(self._call("GET", "/v2/account")["cash"]))

    def is_market_open(self) -> bool:
        return bool(self._call("GET", "/v2/clock")["is_open"])

    def buying_power(self) -> Decimal:
        # Non-marginable buying power, so a rebalance never relies on margin.
        acct = self._call("GET", "/v2/account")
        return money(acct.get("non_marginable_buying_power") or acct["cash"])

    def submit_order(self, trade: Trade, client_order_id: str) -> str:
        body = {
            "symbol": trade.symbol,
            "notional": f"{trade.notional:.2f}",          # dollar-based (fractional) order
            "side": "sell" if trade.side == Side.SELL else "buy",
            "type": "market",
            "time_in_force": "day",                        # required for notional orders
            "client_order_id": client_order_id,            # makes retries safe
        }
        try:
            return str(self._call("POST", "/v2/orders", body)["id"])
        except AlpacaError as e:
            # A retry of an order Alpaca already has: return the existing one, never place a duplicate.
            if e.status == 422 and "client_order_id" in str(e):
                existing = self._call("GET", "/v2/orders:by_client_order_id",
                                      query={"client_order_id": client_order_id})
                return str(existing["id"])
            raise

    def get_order(self, order_id: str) -> OrderInfo:
        o = self._call("GET", f"/v2/orders/{order_id}")
        status = o["status"]
        filled = Decimal(o.get("filled_qty") or "0") * Decimal(o.get("filled_avg_price") or "0")
        mapped = (FILLED if status == "filled" else PARTIAL if status == "partially_filled"
                  else FAILED if status in _FAILED else PENDING)
        return OrderInfo(str(o["id"]), o["symbol"], o["side"], mapped, money(filled),
                         Decimal(o["notional"]) if o.get("notional") else None)
