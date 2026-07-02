"""x402 live facilitator path (injected poster) + go-live preflight. No real funds."""

import pytest
from sovereign_os.payments.x402 import X402PaymentService
from sovereign_os.payments.x402_preflight import run_preflight


@pytest.mark.asyncio
async def test_live_settle_calls_facilitator_and_returns_tx():
    calls = []
    def poster(url, payload, headers, timeout):
        calls.append((url, payload, headers))
        return {"txHash": "0xabc123"}
    svc = X402PaymentService(pay_to="0xPay", network="base-sepolia", sandbox=False,
                             facilitator_url="https://facil.test", api_key="k", post_settle=poster)
    tx = await svc.charge(2500, "usd", metadata={"job_id": "j1"})
    assert tx == "0xabc123" and len(calls) == 1
    url, payload, headers = calls[0]
    assert url.endswith("/settle")
    assert payload["payTo"] == "0xPay" and payload["amount"] == str(2500 * 10_000)  # atomic USDC
    assert headers["Authorization"] == "Bearer k" and headers["Idempotency-Key"] == "job-j1"


@pytest.mark.asyncio
async def test_live_settle_raises_without_tx():
    svc = X402PaymentService(pay_to="0xP", sandbox=False, facilitator_url="https://f.test",
                             post_settle=lambda *a: {"noTx": True})
    with pytest.raises(Exception):
        await svc.charge(100, "usd")


def test_preflight_sandbox_is_go():
    svc = X402PaymentService(sandbox=True, network="base-sepolia")
    r = run_preflight(svc=svc)
    assert r["go"] is True and r["live"] is False
    assert any(c["name"] == "sandbox_settle" and c["status"] == "ok" for c in r["checks"])


def test_preflight_live_missing_config_is_no_go():
    svc = X402PaymentService(sandbox=False, facilitator_url="", pay_to="")
    r = run_preflight(svc=svc)
    assert r["go"] is False


def test_preflight_live_probe_ok():
    svc = X402PaymentService(sandbox=False, pay_to="0xP", facilitator_url="https://f.test", network="base-sepolia")
    r = run_preflight(svc=svc, probe_settle=lambda *a: {"txHash": "0xdead"})
    assert any(c["name"] == "facilitator_probe" and c["status"] == "ok" for c in r["checks"])
    assert r["go"] is True
