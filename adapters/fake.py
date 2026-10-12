"""An in-memory broker for tests and offline demos. Fills instantly when the market is open."""
from __future__ import annotations

import itertools
from decimal import Decimal

from engine import Holding, Portfolio, Side, Trade, money

from .broker import FAILED, FILLED, PENDING, OrderInfo


class FakeBroker:
    def __init__(self, positions: dict[str, tuple[str, str]], cash: str, market_open: bool = True):
        """positions: symbol -> (qty, price)."""
        self.qty = {s: Decimal(q) for s, (q, _) in positions.items()}
        self.prices = {s: Decimal(p) for s, (_, p) in positions.items()}
        self.cash = Decimal(cash)
        self.market_open = market_open
        self.orders: dict[str, OrderInfo] = {}
        self._by_client_id: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.reject_symbols: set[str] = set()

    def set_price(self, symbol: str, price: str) -> None:
        self.prices[symbol] = Decimal(price)

    def get_portfolio(self) -> Portfolio:
        holdings = tuple(Holding(s, q, self.prices[s]) for s, q in sorted(self.qty.items()) if q > 0)
        return Portfolio(holdings, money(self.cash))

    def is_market_open(self) -> bool:
        return self.market_open

    def buying_power(self) -> Decimal:
        return money(self.cash)

    def submit_order(self, trade: Trade, client_order_id: str) -> str:
        if client_order_id in self._by_client_id:          # idempotent: same request, same order
            return self._by_client_id[client_order_id]
        oid = f"ord-{next(self._ids)}"
        self._by_client_id[client_order_id] = oid
        if trade.symbol in self.reject_symbols:
            self.orders[oid] = OrderInfo(oid, trade.symbol, trade.side.value, FAILED, Decimal("0"), trade.notional)
        elif not self.market_open:
            self.orders[oid] = OrderInfo(oid, trade.symbol, trade.side.value, PENDING, Decimal("0"), trade.notional)
        else:
            self._fill(trade)
            self.orders[oid] = OrderInfo(oid, trade.symbol, trade.side.value, FILLED, trade.notional, trade.notional)
        return oid

    def get_order(self, order_id: str) -> OrderInfo:
        return self.orders[order_id]

    # ---- save / load, so a demo portfolio can live in a database between requests ----

    def to_state(self) -> dict:
        return {
            "qty": {s: str(q) for s, q in self.qty.items()},
            "prices": {s: str(p) for s, p in self.prices.items()},
            "cash": str(self.cash),
            "market_open": self.market_open,
            "orders": {oid: {"symbol": o.symbol, "side": o.side, "status": o.status,
                             "filled": str(o.filled_notional),
                             "requested": None if o.requested_notional is None else str(o.requested_notional)}
                       for oid, o in self.orders.items()},
            "by_client_id": dict(self._by_client_id),
            "next_id": len(self.orders) + 1,
        }

    @classmethod
    def from_state(cls, state: dict) -> "FakeBroker":
        b = cls({}, cash=state["cash"], market_open=state.get("market_open", True))
        b.qty = {s: Decimal(q) for s, q in state["qty"].items()}
        b.prices = {s: Decimal(p) for s, p in state["prices"].items()}
        b.orders = {oid: OrderInfo(oid, o["symbol"], o["side"], o["status"], Decimal(o["filled"]),
                                   None if o["requested"] is None else Decimal(o["requested"]))
                    for oid, o in state.get("orders", {}).items()}
        b._by_client_id = dict(state.get("by_client_id", {}))
        b._ids = itertools.count(state.get("next_id", len(b.orders) + 1))
        return b

    def _fill(self, trade: Trade) -> None:
        price = self.prices.setdefault(trade.symbol, Decimal("100"))
        shares = trade.notional / price
        if trade.side == Side.SELL:
            self.qty[trade.symbol] = self.qty.get(trade.symbol, Decimal("0")) - shares
            self.cash += trade.notional
        else:
            self.qty[trade.symbol] = self.qty.get(trade.symbol, Decimal("0")) + shares
            self.cash -= trade.notional
