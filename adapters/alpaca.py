"""Alpaca paper-trading adapter. Paper mode is forced: this code cannot trade real money.

Env vars: ALPACA_KEY, ALPACA_SECRET
"""
from __future__ import annotations

import os
from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from engine import Holding, Portfolio, Side, Trade, money

from .broker import FAILED, FILLED, PARTIAL, PENDING, OrderInfo

_FAILED = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
           OrderStatus.SUSPENDED, OrderStatus.STOPPED, OrderStatus.DONE_FOR_DAY}


class AlpacaBroker:
    def __init__(self, key: str | None = None, secret: str | None = None):
        self.client = TradingClient(
            key or os.environ["ALPACA_KEY"],
            secret or os.environ["ALPACA_SECRET"],
            paper=True,  # never change this
        )

    def get_portfolio(self) -> Portfolio:
        holdings = tuple(
            Holding(p.symbol, Decimal(p.qty), Decimal(p.current_price))
            for p in self.client.get_all_positions()
            if Decimal(p.qty) > 0
        )
        return Portfolio(holdings, money(self.client.get_account().cash))

    def is_market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def buying_power(self) -> Decimal:
        # Use non-marginable buying power so rebalances never rely on margin.
        acct = self.client.get_account()
        return money(acct.non_marginable_buying_power or acct.cash)

    def submit_order(self, trade: Trade, client_order_id: str) -> str:
        order = self.client.submit_order(MarketOrderRequest(
            symbol=trade.symbol,
            notional=float(trade.notional),        # dollar-based (fractional) order
            side=OrderSide.SELL if trade.side == Side.SELL else OrderSide.BUY,
            time_in_force=TimeInForce.DAY,         # notional orders must be DAY
            client_order_id=client_order_id,       # makes retries safe (no duplicate orders)
        ))
        return str(order.id)

    def get_order(self, order_id: str) -> OrderInfo:
        o = self.client.get_order_by_id(order_id)
        filled = Decimal(o.filled_qty or "0") * Decimal(o.filled_avg_price or "0")
        if o.status == OrderStatus.FILLED:
            status = FILLED
        elif o.status == OrderStatus.PARTIALLY_FILLED:
            status = PARTIAL
        elif o.status in _FAILED:
            status = FAILED
        else:
            status = PENDING
        return OrderInfo(str(o.id), o.symbol, o.side.value, status, money(filled),
                         Decimal(str(o.notional)) if o.notional else None)
