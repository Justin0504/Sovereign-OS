"""Tests for the APB (Agent Payment Bounty) x402 ingest source."""

from sovereign_os.ingest_bridge.sources.apb import (
    APBOrderSource,
    apb_amount_to_cents,
    parse_apb_document,
)


# --------------------------------------------------------------- amount parsing
def test_amount_decimal_and_atomic():
    assert apb_amount_to_cents("5.00") == 500
    assert apb_amount_to_cents(5) == 500
    assert apb_amount_to_cents(5_000_000, 6) == 500      # USDC atomic (6 decimals)
    assert apb_amount_to_cents(25_000_000, 6) == 2500    # $25
    assert apb_amount_to_cents(1_000_000_000, 9) == 100  # 9-decimal chain, $1


def test_amount_unparseable_is_zero():
    assert apb_amount_to_cents("abc") == 0
    assert apb_amount_to_cents(None) == 0


# ------------------------------------------------------------------ doc parsing
def test_parse_wrapped_and_nested_reward():
    doc = {"bounties": [
        {"id": "b1", "action": "Summarize this PDF",
         "reward": {"amount": 25_000_000, "decimals": 6, "asset": "USDC",
                    "network": "base", "payTo": "0xabc"},
         "claim": "https://pub.example/claim/b1"},
    ]}
    orders = parse_apb_document(doc, source_url="https://pub.example/.well-known/bounties.json")
    assert len(orders) == 1
    o = orders[0]
    assert o.source_id == "apb:b1"
    assert o.amount_cents == 2500 and o.currency == "USDC"
    assert o.contact["platform"] == "apb"
    assert o.contact["network"] == "base" and o.contact["pay_to"] == "0xabc"
    assert o.contact["claim"] == "https://pub.example/claim/b1"
    assert o.meta["source_url"].endswith("/.well-known/bounties.json")


def test_parse_flat_fields_and_field_spellings():
    doc = [
        {"bountyId": "b2", "title": "Fix bug", "description": "Fix null deref",
         "amount": "12.50", "currency": "USDC"},
        {"slug": "b3", "task": "Translate", "payout": "3", "asset": "USDC", "chain": "solana"},
    ]
    orders = parse_apb_document(doc)
    ids = {o.source_id for o in orders}
    assert ids == {"apb:b2", "apb:b3"}
    b2 = next(o for o in orders if o.source_id == "apb:b2")
    assert b2.amount_cents == 1250 and "Fix bug" in b2.goal and "null deref" in b2.goal
    b3 = next(o for o in orders if o.source_id == "apb:b3")
    assert b3.amount_cents == 300 and b3.contact["network"] == "solana"


def test_parse_skips_entries_without_id_or_action():
    doc = {"bounties": [
        {"action": "no id here"},          # no id -> skip
        {"id": "b9"},                       # no action/title -> skip
        {"id": "ok", "action": "do it"},   # kept
    ]}
    orders = parse_apb_document(doc)
    assert [o.source_id for o in orders] == ["apb:ok"]


def test_parse_handles_garbage():
    assert parse_apb_document(None) == []
    assert parse_apb_document({"nope": 1}) == []
    assert parse_apb_document("not json") == []


# ----------------------------------------------------------------------- source
def test_source_builds_well_known_url_and_filters():
    doc = {"bounties": [
        {"id": "cheap", "action": "tiny", "amount": "1.00"},
        {"id": "rich", "action": "big", "amount": "50.00"},
    ]}
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen["url"] = url
        seen["accept"] = headers.get("Accept")
        return doc

    src = APBOrderSource(publishers=["https://pub.example/"], min_amount_usd=10.0, get_json=fake_get)
    orders = list(src.fetch())
    assert seen["url"] == "https://pub.example/.well-known/bounties.json"
    assert seen["accept"] == "application/json"
    assert [o.source_id for o in orders] == ["apb:rich"]  # cheap filtered out


def test_source_fetch_failure_is_swallowed():
    def boom(*a, **k):
        raise RuntimeError("network down")

    src = APBOrderSource(publishers=["https://x/"], get_json=boom)
    assert list(src.fetch()) == []  # no raise


def test_source_respects_limit_across_publishers():
    doc = {"bounties": [{"id": f"b{i}", "action": "x", "amount": "5"} for i in range(10)]}
    src = APBOrderSource(publishers=["https://a/", "https://b/"], limit=3,
                         get_json=lambda *a, **k: doc)
    assert len(list(src.fetch())) == 3


# ------------------------------------------------------------------------ config
def test_config_from_env(monkeypatch):
    from sovereign_os.ingest_bridge.config import BridgeConfig

    monkeypatch.setenv("BRIDGE_APB_ENABLED", "true")
    monkeypatch.setenv("APB_PUBLISHERS", "https://a.example, https://b.example")
    monkeypatch.setenv("APB_MIN_AMOUNT_USD", "2.5")
    cfg = BridgeConfig.from_env()
    assert cfg.apb.enabled is True
    assert cfg.apb.publishers == ["https://a.example", "https://b.example"]
    assert cfg.apb.min_amount_usd == 2.5
