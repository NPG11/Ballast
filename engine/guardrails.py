"""Step 4: check a set of trades against the user's own rules before anything runs.

Ballast only ever executes rebalance plans it generated itself. There is no
"buy X for me" path. These checks run at planning time AND again right before execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .analysis import compute_allocation
from .categories import category_of, is_fund
from .models import CASH, Portfolio, Rules, Side, Trade, money, pct


@dataclass
class GuardrailResult:
    violations: list[str] = field(default_factory=list)  # any of these blocks execution
    warnings: list[str] = field(default_factory=list)    # informational
    queue_for_open: bool = False                         # market closed: orders wait for the open

    @property
    def ok(self) -> bool:
        return not self.violations


def check_trades(
    portfolio: Portfolio,
    trades: list[Trade],
    rules: Rules,
    market_open: bool = True,
    buying_power: Decimal | None = None,
    overrides: dict[str, str] | None = None,
) -> GuardrailResult:
    rules.validate()
    c = rules.constraints
    result = GuardrailResult()
    alloc = compute_allocation(portfolio, overrides)
    total = alloc.total_value
    max_trade = money(total * c.max_trade_pct)

    # 1. No single order bigger than the user's limit.
    for t in trades:
        if t.notional > max_trade:
            result.violations.append(
                f"{t.side.value.title()} ${t.notional:,} of {t.symbol} is {pct(t.notional / total)} "
                f"of your portfolio. Your per-order limit is {pct(c.max_trade_pct)} (${max_trade:,})."
            )

    # 2. Total traded stays under the user's rebalance cap.
    sells = sum((t.notional for t in trades if t.side == Side.SELL), Decimal("0"))
    buys = sum((t.notional for t in trades if t.side == Side.BUY), Decimal("0"))
    if c.max_rebalance_usd is not None and max(sells, buys) > c.max_rebalance_usd:
        result.violations.append(
            f"This rebalance moves ${max(sells, buys):,}. Your limit per rebalance is ${c.max_rebalance_usd:,}."
        )

    # 3. Can't sell more than you own.
    held = {h.symbol: h.market_value for h in portfolio.holdings}
    sold: dict[str, Decimal] = {}
    for t in trades:
        if t.side == Side.SELL:
            sold[t.symbol] = sold.get(t.symbol, Decimal("0")) + t.notional
    for sym, amt in sold.items():
        if amt > held.get(sym, Decimal("0")):
            result.violations.append(f"You only hold ${held.get(sym, 0):,} of {sym}, can't sell ${amt:,}.")

    # 4. Can't spend more than you'll have.
    available = money(portfolio.cash if buying_power is None else min(portfolio.cash, buying_power)) + sells
    if buys > available:
        result.violations.append(f"These buys need ${buys:,} but only ${available:,} would be available.")

    # 5. Simulate the result and check it against targets and caps.
    after_cat = dict(alloc.values)
    after_sym = dict(held)
    for t in trades:
        sign = -1 if t.side == Side.SELL else 1
        after_cat[t.category] = after_cat.get(t.category, Decimal("0")) + sign * t.notional
        after_cat[CASH] = after_cat.get(CASH, Decimal("0")) - sign * t.notional
        after_sym[t.symbol] = after_sym.get(t.symbol, Decimal("0")) + sign * t.notional

    for cat in sorted({t.category for t in trades if t.side == Side.BUY}):
        target = rules.targets.get(cat, Decimal("0"))
        weight = after_cat.get(cat, Decimal("0")) / total
        if weight > target + rules.drift_limit:
            result.violations.append(
                f"This would put {cat.replace('_', ' ')} at {pct(weight)}. "
                f"Your target is {pct(target)} with a {pct(rules.drift_limit)} drift limit."
            )

    if c.max_single_stock_pct is not None:
        for sym in sorted({t.symbol for t in trades if t.side == Side.BUY and not is_fund(t.symbol)}):
            weight = after_sym.get(sym, Decimal("0")) / total
            if weight > c.max_single_stock_pct:
                result.violations.append(
                    f"{sym} would be {pct(weight)} of your portfolio. "
                    f"Your cap on any single stock is {pct(c.max_single_stock_pct)}."
                )

    # 6. Market hours.
    if not market_open:
        result.queue_for_open = True
        result.warnings.append("The market is closed, so orders would wait for the next open.")

    return result
