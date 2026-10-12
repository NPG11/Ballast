"""The same storage behavior, checked against memory and a mock DynamoDB (moto)."""
from decimal import Decimal as D

import pytest

from adapters.fake import FakeBroker
from engine import Constraints, Rules, Status
from service.ballast import Ballast
from store.memory import MemoryStore

moto = pytest.importorskip("moto")

RULES = Rules(targets={"us_stocks": D("0.60"), "bonds": D("0.20"), "cash": D("0.20")},
              buy_symbols={"us_stocks": "VTI"},
              constraints=Constraints(max_trade_pct=D("0.25"), max_rebalance_usd=D("25000"),
                                      max_single_stock_pct=D("0.30")))


def make_table(name="ballast-test"):
    import boto3
    ddb = boto3.resource("dynamodb", region_name="us-east-2")
    table = ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


@pytest.fixture(params=["memory", "dynamo"])
def store(request, monkeypatch):
    if request.param == "memory":
        yield MemoryStore()
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    from moto import mock_aws
    from store.dynamo import DynamoStore
    with mock_aws():
        yield DynamoStore("ballast-test", ttl_days=14, table=make_table())


def test_rules_round_trip_exactly(store):
    assert store.get_rules("neel") is None
    store.save_rules("neel", RULES)
    back = store.get_rules("neel")
    assert back == RULES and back.version == RULES.version


def test_full_rebalance_loop_on_the_store(store):
    broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")
    app = Ballast(broker, store=store, fill_timeout=0, sleep=lambda s: None)
    store.save_rules("neel", RULES)
    plan = app.plan_rebalance("neel")
    assert plan["status"] == "awaiting_approval"
    assert [p.id for p in store.open_proposals("neel")] == [plan["proposal_id"]]
    assert app.approve("neel", plan["proposal_id"])["status"] == "approved"
    assert app.execute("neel", plan["proposal_id"])["status"] == "executed"
    assert store.get_proposal(plan["proposal_id"]).status == Status.EXECUTED
    assert store.open_proposals("neel") == []
    events = [e["event"] for e in app.history("neel")]
    assert events == ["proposal_created", "proposal_approved", "orders_filled"]


def test_superseding_survives_storage(store):
    broker = FakeBroker({"VTI": ("30", "280"), "BND": ("25", "80")}, cash="2000")
    app = Ballast(broker, store=store, fill_timeout=0, sleep=lambda s: None)
    store.save_rules("neel", RULES)
    first = app.plan_rebalance("neel")["proposal_id"]
    second = app.plan_rebalance("neel")["proposal_id"]
    assert store.get_proposal(first).status == Status.SUPERSEDED
    assert [p.id for p in store.open_proposals("neel")] == [second]


def test_audit_log_is_per_user_and_in_order(store):
    store.audit.record("neel", "a", "BL-1", x=1)
    store.audit.record("maya", "b")
    store.audit.record("neel", "c", "BL-2")
    assert [e.event for e in store.audit.events("neel")] == ["a", "c"]
    assert [e.event for e in store.audit.events("neel", "BL-1")] == ["a"]
    assert store.audit.events("neel")[0].details == {"x": 1}


def test_state_documents(store):
    assert store.get_state("history:s1") is None
    store.put_state("history:s1", {"messages": [{"role": "user", "content": [{"text": "hi"}]}]})
    assert store.get_state("history:s1")["messages"][0]["content"][0]["text"] == "hi"


def test_confirm_cards_answer_once_and_only_by_owner(store):
    store.create_confirm("c1", "sessA", "SELL $1 VTI")
    assert store.get_confirm("c1")["answer"] == "pending"
    assert store.answer_confirm("c1", "sessB", True) is False        # someone else's card
    assert store.answer_confirm("c1", "sessA", True) is True
    assert store.answer_confirm("c1", "sessA", False) is False       # already answered
    assert store.get_confirm("c1")["answer"] == "yes"
    assert store.answer_confirm("missing", "sessA", True) is False


def test_counter(store):
    assert [store.incr("turns:2026-10-11") for _ in range(3)] == [1, 2, 3]


def test_demo_portfolio_survives_between_requests(store):
    """Plan on one 'machine', execute on another: the stored demo portfolio stays consistent."""
    from adapters.stored import StoredDemoBroker
    store.save_rules("web-a", RULES)
    plan = Ballast(StoredDemoBroker(store, "web-a"), store=store,
                   fill_timeout=0, sleep=lambda s: None).plan_rebalance("web-a")
    other = Ballast(StoredDemoBroker(store, "web-a"), store=store, fill_timeout=0, sleep=lambda s: None)
    other.approve("web-a", plan["proposal_id"])
    assert other.execute("web-a", plan["proposal_id"])["status"] == "executed"
    fresh = Ballast(StoredDemoBroker(store, "web-a"), store=store)
    assert fresh.portfolio_status("web-a")["needs_rebalance"] is False
    # another visitor still has the untouched starting portfolio
    store.save_rules("web-b", RULES)
    assert Ballast(StoredDemoBroker(store, "web-b"), store=store).portfolio_status("web-b")["needs_rebalance"]
