"""End-to-end loop tests with the fake broker: READ -> DRIFT -> PLAN -> APPROVE -> EXECUTE -> VERIFY."""
from decimal import Decimal as D

from adapters.fake import FakeBroker
from engine import Confirmation, Constraints, Rules, Side, Trade
from service.ballast import Ballast

RULES = Rules(targets={"us_stocks": D("0.60"), "bonds": D("0.20"), "cash": D("0.20")})


def setup(market_open=True):
    broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000", market_open=market_open)
    app = Ballast(broker, fill_timeout=0, sleep=lambda s: None)
    app.store.save_rules("neel", RULES)
    return broker, app


def test_full_loop():
    broker, app = setup()
    status = app.portfolio_status("neel")
    assert status["needs_rebalance"] and status["allocation"]["us_stocks"] == "67.7%"

    plan = app.plan_rebalance("neel")
    assert plan["status"] == "awaiting_approval"
    assert plan["trades"] == [
        {"side": "sell", "symbol": "VTI", "amount": "960.00", "category": "us_stocks"},
        {"side": "buy", "symbol": "BND", "amount": "480.00", "category": "bonds"},
    ]

    pid = plan["proposal_id"]
    assert app.approve("neel", pid)["status"] == "approved"
    result = app.execute("neel", pid)
    assert result["status"] == "executed"
    assert result["allocation_now"] == {"bonds": "20.0%", "cash": "20.0%", "us_stocks": "60.0%"}
    assert result["still_needs_rebalance"] is False
    events = [e["event"] for e in app.history("neel")]
    assert events == ["proposal_created", "proposal_approved", "orders_filled"]


def test_cannot_execute_without_approval():
    _, app = setup()
    pid = app.plan_rebalance("neel")["proposal_id"]
    assert app.execute("neel", pid)["status"] == "refused"


def test_stale_proposal_refused_after_holdings_change():
    broker, app = setup()
    pid = app.plan_rebalance("neel")["proposal_id"]
    app.approve("neel", pid)
    broker.qty["VTI"] += D("1")                     # user bought a share elsewhere
    result = app.execute("neel", pid)
    assert result["status"] == "refused"
    assert any("holdings changed" in e for e in result["errors"])
    assert broker.orders == {}                       # nothing was traded


def test_stale_proposal_refused_after_rules_change():
    _, app = setup()
    pid = app.plan_rebalance("neel")["proposal_id"]
    app.approve("neel", pid)
    app.store.save_rules("neel", Rules(targets={"us_stocks": D("0.5"), "bonds": D("0.3"), "cash": D("0.2")}))
    assert app.execute("neel", pid)["status"] == "refused"


def test_market_closed_places_nothing():
    broker, app = setup(market_open=False)
    plan = app.plan_rebalance("neel")
    assert plan["warnings"]
    app.approve("neel", plan["proposal_id"])
    assert app.execute("neel", plan["proposal_id"])["status"] == "market_closed"
    assert broker.orders == {}


def test_failed_sell_means_no_buys():
    broker, app = setup()
    broker.reject_symbols.add("VTI")
    pid = app.plan_rebalance("neel")["proposal_id"]
    app.approve("neel", pid)
    result = app.execute("neel", pid)
    assert result["status"] == "incomplete" and result["stage"] == "sells"
    assert all(o.side == "sell" for o in broker.orders.values())


def test_on_track_portfolio_gets_no_proposal():
    broker = FakeBroker({"VTI": ("20", "300"), "BND": ("20", "100")}, cash="2000")
    app = Ballast(broker)
    app.store.save_rules("neel", RULES)
    assert app.plan_rebalance("neel")["status"] == "on_track"


def test_blocked_plan_creates_no_proposal():
    _, app = setup()
    app.store.save_rules("neel", Rules(targets=RULES.targets, constraints=Constraints(max_rebalance_usd=D("100"))))
    res = app.plan_rebalance("neel")
    assert res["status"] == "blocked" and app.store.proposals == {}


def test_target_change_by_voice():
    _, app = setup()
    new = Rules(targets={"us_stocks": D("0.5"), "bonds": D("0.3"), "cash": D("0.2")})
    assert app.set_rules("neel", new)["status"] == "needs_confirmation"
    assert app.set_rules("neel", new, confirmed=True)["status"] == "saved"


def test_safety_limits_cannot_be_loosened_by_voice():
    _, app = setup()
    loosened = Rules(targets=RULES.targets, constraints=Constraints(max_trade_pct=D("1")))
    assert app.set_rules("neel", loosened, confirmed=True)["status"] == "needs_web_confirmation"
    assert app.set_rules("neel", loosened, confirmed=True, channel=Confirmation.WEB)["status"] == "saved"


def test_100_percent_nvidia_rule_rejected():
    _, app = setup()
    app.store.save_rules("neel", Rules(targets=RULES.targets,
                                       constraints=Constraints(max_single_stock_pct=D("0.30"))))
    bad = Rules(targets={"us_stocks": D("1")}, buy_symbols={"us_stocks": "NVDA"},
                constraints=Constraints(max_single_stock_pct=D("0.30")))
    res = app.set_rules("neel", bad)
    assert res["status"] == "invalid" and "single stock" in res["errors"][0]


def test_retrying_an_order_does_not_duplicate_it():
    broker, _ = setup()
    t = Trade("BND", Side.BUY, D("100"), "bonds")
    assert broker.submit_order(t, "BL-1-0") == broker.submit_order(t, "BL-1-0")
    assert len(broker.orders) == 1
