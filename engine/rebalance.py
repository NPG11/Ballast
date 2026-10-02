"""Step 3: work out the trades that bring the portfolio back to the user's targets.

Principles:
- Spare cash is used before selling anything (cash-first).
- Sells come before buys, so the money is there when buying.
- Sell from the largest holding in a category first, to keep the number of trades low.
- Trades smaller than min_trade_usd are skipped.
- Buys never spend more cash than is actually available.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .analysis import Allocation, compute_allocation
from .categories import DEFAULT_BUY_SYMBOL, category_of
from .models import CASH, Portfolio, Rules, RulesError, Side, Trade, money


@dataclass(frozen=True)
class RebalancePlan:
    trades: list[Trade]                 # sells first, then buys
    before: Allocation
    after_values: dict[str, Decimal]    # estimated dollars per category after trades
    skipped: dict[str, Decimal]         # category -> amount too small to trade

    @property
    def total_sells(self) -> Decimal:
        return sum((t.notional for t in self.trades if t.side == Side.SELL), Decimal("0"))

    @property
    def total_buys(self) -> Decimal:
        return sum((t.notional for t in self.trades if t.side == Side.BUY), Decimal("0"))


def _buy_symbol(category: str, portfolio: Portfolio, rules: Rules, overrides) -> str:
    if category in rules.buy_symbols:
        return rules.buy_symbols[category]
    held = [h for h in portfolio.holdings if category_of(h.symbol, overrides) == category]
    if held:
        return max(held, key=lambda h: h.market_value).symbol
    if category in DEFAULT_BUY_SYMBOL:
        return DEFAULT_BUY_SYMBOL[category]
    raise RulesError(f"I don't know which fund to buy for '{category}'. Please pick one.")


def plan_rebalance(portfolio: Portfolio, rules: Rules, overrides: dict[str, str] | None = None) -> RebalancePlan:
    rules.validate()
    alloc = compute_allocation(portfolio, overrides)
    total = alloc.total_value

    # How far each (non-cash) category is from its target, in dollars.
    categories = (set(rules.targets) | set(alloc.values)) - {CASH}
    deltas = {
        c: money(total * rules.targets.get(c, Decimal("0"))) - alloc.values.get(c, Decimal("0"))
        for c in categories
    }

    skipped = {c: d for c, d in deltas.items() if d != 0 and abs(d) < rules.min_trade_usd}

    # --- Sells: overweight categories, largest holding first ---
    sells: list[Trade] = []
    for c in sorted(c for c, d in deltas.items() if d <= -rules.min_trade_usd):
        remaining = -deltas[c]
        held = sorted(
            (h for h in portfolio.holdings if category_of(h.symbol, overrides) == c),
            key=lambda h: h.market_value,
            reverse=True,
        )
        for h in held:
            if remaining <= 0:
                break
            amount = min(remaining, h.market_value)
            sells.append(Trade(h.symbol, Side.SELL, money(amount), c))
            remaining -= amount

    # --- Buys: underweight categories, capped by the cash actually available ---
    wanted = {c: d for c, d in deltas.items() if d >= rules.min_trade_usd}
    spendable = money(portfolio.cash) + sum((t.notional for t in sells), Decimal("0"))
    want_total = sum(wanted.values(), Decimal("0"))
    scale = min(Decimal("1"), spendable / want_total) if want_total else Decimal("1")

    buys: list[Trade] = []
    for c in sorted(wanted):
        amount = money(wanted[c] * scale)
        if amount >= rules.min_trade_usd:
            buys.append(Trade(_buy_symbol(c, portfolio, rules, overrides), Side.BUY, amount, c))

    trades = sells + buys

    # Estimated result, so Alexa can say "you'll be back at 60/20/20".
    after = dict(alloc.values)
    for t in trades:
        sign = -1 if t.side == Side.SELL else 1
        after[t.category] = after.get(t.category, Decimal("0")) + sign * t.notional
        after[CASH] = after.get(CASH, Decimal("0")) - sign * t.notional

    return RebalancePlan(trades=trades, before=alloc, after_values=after, skipped=skipped)
