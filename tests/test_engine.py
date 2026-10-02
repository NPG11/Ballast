from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from engine import (Confirmation, Constraints, Holding, Portfolio, Rules, RulesError, Side, Status,
                    Trade, UnclassifiedHoldingError, approve, check_drift, check_trades,
                    compute_allocation, create_proposal, plan_rebalance, review_rules_change,
                    validate_for_execution)

RULES = Rules(targets={"us_stocks": D("0.60"), "bonds": D("0.20"), "cash": D("0.20")})


def drifted():
    """Worked example: $10k at 60/20/20, then stocks rose 40%."""
    return Portfolio((Holding("VTI", D("30"), D("280")),    # $8,400
                      Holding("BND", D("25"), D("80"))),     # $2,000
                     D("2000"))


# ---------- allocation & drift ----------

def test_allocation_matches_worked_example():
    a = compute_allocation(drifted())
    assert a.total_value == D("12400.00")
    assert round(a.weights["us_stocks"] * 100, 1) == D("67.7")


def test_drift_over_limit():
    r = check_drift(compute_allocation(drifted()), RULES)
    assert r.needs_rebalance and r.worst.category == "us_stocks"


def test_small_drift_ignored():
    p = Portfolio((Holding("VTI", D("30"), D("240")), Holding("BND", D("25"), D("76"))), D("2000"))
    assert not check_drift(compute_allocation(p), RULES).needs_rebalance


# ---------- rebalancing ----------

def test_rebalance_matches_worked_example():
    plan = plan_rebalance(drifted(), RULES)
    assert [(t.symbol, t.side, t.notional) for t in plan.trades] == [
        ("VTI", Side.SELL, D("960.00")), ("BND", Side.BUY, D("480.00"))]
    assert plan.after_values == {"us_stocks": D("7440.00"), "bonds": D("2480.00"), "cash": D("2480.00")}


def test_cash_first_no_sells_when_cash_overweight():
    p = Portfolio((Holding("VTI", D("10"), D("300")), Holding("BND", D("10"), D("100"))), D("6000"))
    plan = plan_rebalance(p, RULES)
    assert all(t.side == Side.BUY for t in plan.trades) and plan.total_buys <= D("6000")


def test_many_categories_with_default_funds():
    rules = Rules(targets={"us_stocks": D("0.35"), "intl_stocks": D("0.20"), "emerging_markets": D("0.05"),
                           "bonds": D("0.25"), "tips": D("0.05"), "real_estate": D("0.05"), "cash": D("0.05")})
    p = Portfolio((Holding("VTI", D("100"), D("300")), Holding("VXUS", D("100"), D("60")),
                   Holding("BND", D("100"), D("75"))), D("2500"))
    plan = plan_rebalance(p, rules)
    assert plan.trades[0] .symbol == "VTI" and plan.trades[0].side == Side.SELL
    assert {"VWO", "SCHP", "VNQ"} <= {t.symbol for t in plan.trades}
    assert plan.total_buys <= p.cash + plan.total_sells


def test_tiny_trades_skipped():
    plan = plan_rebalance(drifted(), Rules(targets=RULES.targets, min_trade_usd=D("1000")))
    assert all(t.notional >= D("1000") for t in plan.trades) and "bonds" in plan.skipped


# ---------- guardrails ----------

def test_plan_passes_guardrails():
    p = drifted()
    assert check_trades(p, plan_rebalance(p, RULES).trades, RULES).ok


def test_per_order_limit():
    rules = Rules(targets=RULES.targets, constraints=Constraints(max_trade_pct=D("0.05")))
    res = check_trades(drifted(), plan_rebalance(drifted(), rules).trades, rules)
    assert any("per-order limit" in v for v in res.violations)


def test_max_rebalance_amount():
    rules = Rules(targets=RULES.targets, constraints=Constraints(max_rebalance_usd=D("500")))
    res = check_trades(drifted(), plan_rebalance(drifted(), rules).trades, rules)
    assert any("limit per rebalance is $500" in v for v in res.violations)


def test_cannot_sell_more_than_held():
    res = check_trades(drifted(), [Trade("BND", Side.SELL, D("2500"), "bonds")],
                       Rules(targets=RULES.targets, constraints=Constraints(max_trade_pct=D("1"))))
    assert any("only hold" in v for v in res.violations)


def test_buying_power_respected():
    p = drifted()
    res = check_trades(p, [Trade("BND", Side.BUY, D("480"), "bonds")], RULES, buying_power=D("100"))
    assert any("available" in v for v in res.violations)


def test_market_closed_flagged():
    p = drifted()
    res = check_trades(p, plan_rebalance(p, RULES).trades, RULES, market_open=False)
    assert res.ok and res.queue_for_open


# ---------- rules & constraints ----------

def test_targets_must_add_to_100():
    with pytest.raises(RulesError, match="110.0%"):
        Rules(targets={"us_stocks": D("0.70"), "bonds": D("0.40")}).validate()


def test_100_percent_nvidia_blocked_by_single_stock_cap():
    rules = Rules(targets={"us_stocks": D("1")}, buy_symbols={"us_stocks": "NVDA"},
                  constraints=Constraints(max_single_stock_pct=D("0.30")))
    with pytest.raises(RulesError, match="cap on any single stock"):
        rules.validate()


def test_broad_fund_not_limited_by_single_stock_cap():
    Rules(targets={"us_stocks": D("0.6"), "bonds": D("0.4")}, buy_symbols={"us_stocks": "VTI"},
          constraints=Constraints(max_single_stock_pct=D("0.30"))).validate()


def test_category_cap():
    with pytest.raises(RulesError, match="cap on any one category"):
        Rules(targets={"us_stocks": D("0.9"), "bonds": D("0.1")},
              constraints=Constraints(max_category_pct=D("0.8"))).validate()


def test_changing_targets_needs_voice_confirmation_only():
    new = Rules(targets={"us_stocks": D("0.50"), "bonds": D("0.30"), "cash": D("0.20")})
    review = review_rules_change(RULES, new)
    assert review.ok and review.required == Confirmation.VOICE


def test_changing_constraints_needs_web_confirmation():
    loosened = Rules(targets=RULES.targets, constraints=Constraints(max_trade_pct=D("1")))
    assert review_rules_change(RULES, loosened).required == Confirmation.WEB


def test_rules_version_changes_with_rules():
    assert RULES.version != Rules(targets=RULES.targets, drift_limit=D("0.03")).version
    assert RULES.version == Rules(targets=dict(RULES.targets)).version


def test_unknown_symbol_stops_engine():
    p = Portfolio((Holding("ZZZZ", D("1"), D("100")),), D("0"))
    with pytest.raises(UnclassifiedHoldingError):
        compute_allocation(p)
    assert compute_allocation(p, overrides={"ZZZZ": "us_stocks"}).values["us_stocks"] == D("100.00")


# ---------- proposals ----------

def _approved():
    p = drifted()
    prop = create_proposal("neel", plan_rebalance(p, RULES).trades, p, RULES)
    approve(prop, "neel")
    return p, prop


def test_proposal_happy_path():
    p, prop = _approved()
    assert prop.id.startswith("BL-") and validate_for_execution(prop, "neel", p, RULES) == []


def test_unapproved_cannot_execute():
    p = drifted()
    prop = create_proposal("neel", [], p, RULES)
    assert any("approved" in e for e in validate_for_execution(prop, "neel", p, RULES))


def test_expired_proposal():
    p = drifted()
    t0 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
    prop = create_proposal("neel", [], p, RULES, now=t0)
    assert any("expired" in e for e in approve(prop, "neel", now=t0 + timedelta(minutes=6)))
    assert prop.status == Status.EXPIRED


def test_wrong_user():
    p = drifted()
    assert any("different user" in e for e in approve(create_proposal("neel", [], p, RULES), "eve"))


def test_stale_when_holdings_change():
    _, prop = _approved()
    changed = Portfolio((Holding("VTI", D("31"), D("280")), Holding("BND", D("25"), D("80"))), D("1720"))
    assert any("holdings changed" in e for e in validate_for_execution(prop, "neel", changed, RULES))


def test_stale_when_rules_change():
    p, prop = _approved()
    new_rules = Rules(targets={"us_stocks": D("0.5"), "bonds": D("0.3"), "cash": D("0.2")})
    assert any("rules changed" in e for e in validate_for_execution(prop, "neel", p, new_rules))


def test_price_moves_alone_keep_proposal_valid():
    _, prop = _approved()
    moved = Portfolio((Holding("VTI", D("30"), D("281.50")), Holding("BND", D("25"), D("79.90"))), D("2000"))
    assert validate_for_execution(prop, "neel", moved, RULES) == []
