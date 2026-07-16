"""Tests for dynamic bid pricing (thin-margin volume)."""

from sovereign_os.governance.bidding import price_bid, recommended_bid_cents


def test_bid_floor_no_ceiling():
    q = price_bid(100, min_margin_ratio=0.15)
    assert q.bid_cents == 115 and q.floor_cents == 115  # cost + 15% margin


def test_bid_always_above_cost():
    q = price_bid(0, min_margin_ratio=0.0)
    assert q.bid_cents == 1  # strictly above cost even at 0% margin


def test_bid_floor_under_ceiling_is_taken():
    q = price_bid(100, min_margin_ratio=0.15, reward_ceiling_cents=500)
    assert q.bid_cents == 115  # volume mode: bid the floor to maximize win probability


def test_skip_when_floor_exceeds_ceiling():
    q = price_bid(1000, min_margin_ratio=0.15, reward_ceiling_cents=800)
    assert q.bid_cents is None and "skip" in q.reason


def test_undercut_captures_margin_on_generous_ceiling():
    # ceiling 1000, undercut 10% -> bid 900 (above the 115 floor)
    q = price_bid(100, min_margin_ratio=0.15, reward_ceiling_cents=1000, undercut_ratio=0.10)
    assert q.bid_cents == 900


def test_undercut_never_below_floor():
    # thin ceiling: undercut target would be below the floor -> clamp up to floor
    q = price_bid(100, min_margin_ratio=0.15, reward_ceiling_cents=120, undercut_ratio=0.5)
    assert q.bid_cents == 115  # floor wins over the 60 undercut target


def test_recommended_bid_reads_env(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_BID_MIN_MARGIN", "0.25")
    assert recommended_bid_cents(100) == 125
    monkeypatch.setenv("SOVEREIGN_BID_UNDERCUT", "0.1")
    assert recommended_bid_cents(100, 1000) == 900


# -------------------------------------------------- wired into stackstasker bid
def test_stackstasker_includes_dynamic_bid(monkeypatch):
    from sovereign_os.delivery.stackstasker import deliver_result_to_stackstasker

    monkeypatch.setenv("STACKSTASKER_LIVE", "true")
    monkeypatch.setenv("STACKSTASKER_AGENT_ID", "agentX")
    calls = []

    def _post(url, body, headers, timeout):
        calls.append((url, body))
        return {"ok": True}

    ok = deliver_result_to_stackstasker(
        {"platform": "stackstasker", "bounty_id": "st-9", "reward_cents": 1000, "est_cost_cents": 200},
        "solution", "job-2", post_json=_post,
    )
    assert ok is True
    assert calls[0][1]["bidAmount"] == 230  # 200 cost + 15% floor, under the 1000 ceiling


def test_stackstasker_skips_unprofitable_bid(monkeypatch):
    from sovereign_os.delivery.stackstasker import deliver_result_to_stackstasker

    monkeypatch.setenv("STACKSTASKER_LIVE", "true")
    monkeypatch.setenv("STACKSTASKER_AGENT_ID", "agentX")

    def _post(url, body, headers, timeout):
        return {"ok": True}

    # cost 1000, ceiling 800 -> floor 1150 > ceiling -> skip (no post, returns False)
    ok = deliver_result_to_stackstasker(
        {"platform": "stackstasker", "bounty_id": "st-1", "reward_cents": 800, "est_cost_cents": 1000},
        "x", "job-9", post_json=_post,
    )
    assert ok is False


def test_stackstasker_unpriced_when_no_reward_data(monkeypatch):
    from sovereign_os.delivery.stackstasker import deliver_result_to_stackstasker

    monkeypatch.setenv("STACKSTASKER_LIVE", "true")
    monkeypatch.setenv("STACKSTASKER_AGENT_ID", "agentX")
    calls = []

    def _post(url, body, headers, timeout):
        calls.append(body)
        return {"ok": True}

    deliver_result_to_stackstasker({"platform": "stackstasker", "bounty_id": "st-3"},
                                   "sol", "job-3", post_json=_post)
    assert "bidAmount" not in calls[0]  # unchanged behavior without reward data
