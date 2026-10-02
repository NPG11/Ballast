"""Step 1: measure the current split. Step 2: compare it to the user's targets."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .categories import category_of
from .models import CASH, Portfolio, Rules, UnclassifiedHoldingError, money


@dataclass(frozen=True)
class Allocation:
    total_value: Decimal
    values: dict[str, Decimal]   # category -> dollars
    weights: dict[str, Decimal]  # category -> fraction of total


def compute_allocation(portfolio: Portfolio, overrides: dict[str, str] | None = None) -> Allocation:
    unknown = [h.symbol for h in portfolio.holdings if category_of(h.symbol, overrides) is None]
    if unknown:
        raise UnclassifiedHoldingError(sorted(set(unknown)))

    values: dict[str, Decimal] = {CASH: money(portfolio.cash)}
    for h in portfolio.holdings:
        cat = category_of(h.symbol, overrides)
        values[cat] = values.get(cat, Decimal("0")) + h.market_value

    total = portfolio.total_value
    weights = {c: (v / total if total else Decimal("0")) for c, v in values.items()}
    return Allocation(total_value=total, values=values, weights=weights)


@dataclass(frozen=True)
class CategoryDrift:
    category: str
    target: Decimal
    current: Decimal
    drift: Decimal          # current - target, in fraction points
    over_limit: bool


@dataclass(frozen=True)
class DriftReport:
    categories: list[CategoryDrift]
    needs_rebalance: bool
    worst: CategoryDrift | None


def check_drift(allocation: Allocation, rules: Rules) -> DriftReport:
    rules.validate()
    cats = sorted(set(rules.targets) | set(allocation.weights))
    rows = []
    for c in cats:
        target = rules.targets.get(c, Decimal("0"))
        current = allocation.weights.get(c, Decimal("0"))
        drift = current - target
        rows.append(CategoryDrift(c, target, current, drift, abs(drift) > rules.drift_limit))

    worst = max(rows, key=lambda r: abs(r.drift)) if rows else None
    return DriftReport(rows, any(r.over_limit for r in rows), worst)
