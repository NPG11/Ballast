"""The interface Ballast needs from any broker. Alpaca implements it; so does FakeBroker for tests."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from engine import Portfolio, Trade

FILLED = "filled"
PENDING = "pending"     # accepted, waiting (e.g. market closed)
PARTIAL = "partially_filled"
FAILED = "failed"       # rejected / canceled / expired


@dataclass(frozen=True)
class OrderInfo:
    id: str
    symbol: str
    side: str
    status: str                  # one of FILLED / PENDING / PARTIAL / FAILED
    filled_notional: Decimal
    requested_notional: Decimal | None = None


class Broker(Protocol):
    def get_portfolio(self) -> Portfolio: ...
    def is_market_open(self) -> bool: ...
    def buying_power(self) -> Decimal: ...
    def submit_order(self, trade: Trade, client_order_id: str) -> str: ...
    def get_order(self, order_id: str) -> OrderInfo: ...
