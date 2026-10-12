"""A demo portfolio that lives in the store (DynamoDB when deployed).

Each call loads the portfolio, and placing an order saves it back, so it behaves the same
no matter which Lambda instance handles the request. Every website visitor gets their own.
"""
from __future__ import annotations

from decimal import Decimal

from engine import Portfolio, Trade

from .broker import OrderInfo
from .fake import FakeBroker

# A drifted starting point: stocks ran up, so US stocks are ~68% against a 60% target.
DEMO_POSITIONS = {"VTI": ("30", "280"), "BND": ("25", "80")}
DEMO_CASH = "2000"


def demo_broker() -> FakeBroker:
    return FakeBroker(dict(DEMO_POSITIONS), cash=DEMO_CASH)


class StoredDemoBroker:
    def __init__(self, store, key: str):
        self.store, self.key = store, f"broker:{key}"

    def _load(self) -> FakeBroker:
        state = self.store.get_state(self.key)
        return FakeBroker.from_state(state) if state else demo_broker()

    def get_portfolio(self) -> Portfolio:
        return self._load().get_portfolio()

    def is_market_open(self) -> bool:
        return self._load().is_market_open()       # the demo market is always open

    def buying_power(self) -> Decimal:
        return self._load().buying_power()

    def submit_order(self, trade: Trade, client_order_id: str) -> str:
        b = self._load()
        oid = b.submit_order(trade, client_order_id)
        self.store.put_state(self.key, b.to_state())
        return oid

    def get_order(self, order_id: str) -> OrderInfo:
        return self._load().get_order(order_id)
