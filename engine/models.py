"""Core data types for the Ballast engine.

All money is Decimal so amounts are exact (no float rounding errors).
Percentages are stored as fractions: 60% -> Decimal("0.60").

The user's policy has two layers:
- Targets: the split they want (can be changed with a normal confirmation).
- Constraints: hard limits that protect them (changing these needs a stronger confirmation).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

CASH = "cash"
CENT = Decimal("0.01")


def money(x) -> Decimal:
    """Round any number to cents."""
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def pct(x: Decimal) -> str:
    return f"{x * 100:.1f}%"


class RulesError(ValueError):
    """The user's rules are invalid or break their own constraints."""


class UnclassifiedHoldingError(ValueError):
    """A holding has no category, so the engine refuses to touch the portfolio."""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        super().__init__(f"Don't know which category these belong to: {', '.join(symbols)}")


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: Decimal
    price: Decimal

    @property
    def market_value(self) -> Decimal:
        return money(self.qty * self.price)


@dataclass(frozen=True)
class Portfolio:
    holdings: tuple[Holding, ...]
    cash: Decimal

    @property
    def total_value(self) -> Decimal:
        return money(sum((h.market_value for h in self.holdings), Decimal("0")) + self.cash)


@dataclass(frozen=True)
class Constraints:
    """Hard limits. Every plan is checked against these before anything runs."""

    max_trade_pct: Decimal = Decimal("0.25")         # largest single order, as share of portfolio
    max_rebalance_usd: Decimal | None = None         # largest total amount traded in one rebalance
    max_category_pct: Decimal | None = None          # no category target above this
    max_single_stock_pct: Decimal | None = None      # no individual stock (not a fund) above this

    def validate(self) -> None:
        if not (0 < self.max_trade_pct <= 1):
            raise RulesError("Max trade size must be between 0% and 100%.")
        if self.max_rebalance_usd is not None and self.max_rebalance_usd <= 0:
            raise RulesError("Max rebalance amount must be positive.")
        for name in ("max_category_pct", "max_single_stock_pct"):
            v = getattr(self, name)
            if v is not None and not (0 < v <= 1):
                raise RulesError(f"{name} must be between 0% and 100%.")


@dataclass(frozen=True)
class Rules:
    """The user's full policy: targets plus constraints. Any number of categories."""

    targets: dict[str, Decimal]                     # category -> fraction, must sum to 1
    drift_limit: Decimal = Decimal("0.05")          # rebalance when any category is off by more than this
    min_trade_usd: Decimal = Decimal("50")          # skip trades too small to bother with
    buy_symbols: dict[str, str] = field(default_factory=dict)  # category -> fund to buy
    constraints: Constraints = field(default_factory=Constraints)

    def validate(self) -> None:
        from .categories import is_fund  # local import avoids a cycle

        self.constraints.validate()
        if not self.targets:
            raise RulesError("You need at least one category.")
        for cat, p in self.targets.items():
            if p < 0 or p > 1:
                raise RulesError(f"Target for {cat} must be between 0% and 100%.")
        total = sum(self.targets.values(), Decimal("0"))
        if abs(total - 1) > Decimal("0.0001"):
            raise RulesError(f"Targets add up to {total * 100:.1f}%, not 100%.")
        if not (0 < self.drift_limit < 1):
            raise RulesError("Drift limit must be between 0% and 100%.")
        if self.min_trade_usd < 0:
            raise RulesError("Minimum trade size can't be negative.")

        c = self.constraints
        if c.max_single_stock_pct is not None:
            for cat, sym in self.buy_symbols.items():
                p = self.targets.get(cat, Decimal("0"))
                if not is_fund(sym) and p > c.max_single_stock_pct:
                    raise RulesError(
                        f"Putting {pct(p)} into {sym} breaks your {pct(c.max_single_stock_pct)} "
                        f"cap on any single stock."
                    )
        if c.max_category_pct is not None:
            for cat, p in self.targets.items():
                if cat != CASH and p > c.max_category_pct:
                    raise RulesError(
                        f"A {pct(p)} target for {cat.replace('_', ' ')} breaks your "
                        f"{pct(c.max_category_pct)} cap on any one category."
                    )

    @property
    def version(self) -> str:
        """Short fingerprint of the rules. Proposals made under old rules won't execute."""
        c = self.constraints
        blob = json.dumps({
            "targets": {k: str(v) for k, v in sorted(self.targets.items())},
            "drift": str(self.drift_limit), "min": str(self.min_trade_usd),
            "buy": dict(sorted(self.buy_symbols.items())),
            "c": [str(c.max_trade_pct), str(c.max_rebalance_usd),
                  str(c.max_category_pct), str(c.max_single_stock_pct)],
        }, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: Side
    notional: Decimal  # dollar amount
    category: str
