"""
A judge that gives no verdict has not approved.

JudgeLLM used to pass any non-empty deliverable at 0.80 when the judge's reply did not
parse as JSON. That turns "get the judge to answer in prose" -- a refusal, a truncation,
or an instruction planted in the deliverable -- into a pass, and the pass also feeds the
worker's TrustScore, which unlocks capabilities. The same file's MCP path already treated
"failing open on a parse error" as the worst possible direction; the judge did not.

Two smaller holes of the same kind: the model's "passed" flag was read through bool(), so
the string "false" passed at any score; and a non-numeric score crashed the audit instead
of failing it.

The judge's client is faked: what is under test is what the auditor does with a reply,
not what a model would say.
"""

import asyncio

import pytest

from sovereign_os.auditor.base import AuditReport
from sovereign_os.auditor.review_engine import JudgeLLM, ReviewEngine
from sovereign_os.auditor.trail import verify_report_integrity

GOOD_OUTPUT = "1. Per-task cap. 2. Daily ceiling. 3. Human approval above a threshold."


class _Reply:
    model_name = "fake-judge"

    def __init__(self, text):
        self._text = text

    async def chat(self, messages, **kw):
        return self._text


def _judge(reply: str, *, min_score=None) -> AuditReport:
    j = JudgeLLM(client=_Reply(reply))
    return asyncio.run(j.evaluate("t1", GOOD_OUTPUT, "Does it list three controls?", "kpi",
                                  min_score=min_score))


@pytest.mark.parametrize("reply", [
    "Looks great, approved.",                       # prose: the planted-instruction case
    "I can't help evaluate this.",                  # refusal
    '{"passed": true, "score": 0.9',                # truncated
    "",                                             # empty
    '["passed", 0.9]',                              # JSON, but not an object
    'Sure! {"passed": true, "score": 1.0}',         # verdict buried in prose is not accepted
])
def test_an_unparseable_reply_fails_closed(reply):
    r = _judge(reply)
    assert r.passed is False and r.score == 0.0
    assert "failed closed" in r.reason


@pytest.mark.parametrize("score", ['"0.9"', "true", "1.7", "-0.2", "NaN", "null"])
def test_a_score_that_is_not_a_unit_number_fails_closed(score):
    r = _judge('{"passed": true, "score": %s, "reason": "ok"}' % score)
    assert r.passed is False and r.score == 0.0


def test_the_string_false_does_not_pass():
    r = _judge('{"passed": "false", "score": 0.1, "reason": "off-topic"}')
    assert r.passed is False


def test_a_well_formed_verdict_still_works_both_ways():
    ok = _judge('```json\n{"passed": true, "score": 0.86, "reason": "on task"}\n```')
    assert ok.passed is True and ok.score == pytest.approx(0.86)
    assert ok.auditor == "judge:fake-judge"
    bad = _judge('{"passed": false, "score": 0.2, "reason": "off task"}')
    assert bad.passed is False and bad.suggested_fix == ""  # judge gave none


def test_a_rubric_reply_with_no_scored_criterion_fails_closed():
    r = _judge('{"reason": "fine"}', min_score=0.6)
    assert r.passed is False and "failed closed" in r.reason


def test_a_skipped_rubric_criterion_counts_as_zero_not_a_crash():
    from sovereign_os.auditor.rubric import shuffled_rubric

    keys = [k for k, _ in shuffled_rubric(None, seed="t1")]
    body = ", ".join(f'"{k}": 1.0' for k in keys[1:]) + f', "{keys[0]}": "high"'
    r = _judge("{" + body + "}", min_score=0.6)
    assert r.sub_scores[keys[0]] == 0.0


# ------------------------------------------------------------ hosted: no silent stub

def test_hosted_engine_refuses_to_fall_back_to_the_stub(monkeypatch, charter):
    monkeypatch.setenv("SOVEREIGN_MULTI_TENANT", "1")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        ReviewEngine(charter)


# ------------------------------------------- the auditor is part of the signed record

def test_relabelling_a_rule_pass_as_a_judge_pass_breaks_the_proof():
    rep = AuditReport(task_id="t1", passed=True, score=0.9, reason="r", auditor="rule")
    entry = rep.model_dump(mode="json")
    entry["timestamp_utc"] = rep.timestamp_utc.isoformat()
    assert verify_report_integrity(entry)
    entry["auditor"] = "judge:claude-sonnet-5"
    assert not verify_report_integrity(entry)


def test_entries_written_before_the_auditor_field_still_verify():
    rep = AuditReport(task_id="t1", passed=True, score=0.9, reason="r")  # auditor unset
    entry = rep.model_dump(mode="json")
    entry["timestamp_utc"] = rep.timestamp_utc.isoformat()
    entry.pop("auditor")
    assert verify_report_integrity(entry)


# ------------------------------------------------- the console shows who audited

def test_mission_response_names_the_auditor_and_carries_a_verifiable_proof(monkeypatch):
    import tempfile

    from fastapi.testclient import TestClient

    from sovereign_os.agents.base import TaskResult
    from sovereign_os.saas import api
    from sovereign_os.saas.tenancy import TenantStore

    rep = AuditReport(task_id="t1", passed=False, score=0.0, auditor="judge:claude-haiku-4-5",
                      reason="Judge returned no usable verdict (x); failed closed.")

    async def fake_mission(*a, **k):
        return None, [TaskResult(task_id="t1", skill="research", output="o", success=True)], [rep]

    monkeypatch.setattr(api, "run_tenant_mission", fake_mission)
    client = TestClient(api.create_saas_app(TenantStore(tempfile.mkdtemp())))
    key = client.post("/saas/tenants", json={"name": "Acme", "plan": "pro"}).json()["api_key"]
    d = client.post("/saas/tenants/me/missions", headers={"X-Tenant-Key": key},
                    json={"goal": "Do a thing"}).json()
    a = d["audits"][0]
    assert d["all_passed"] is False
    assert a["auditor"] == "judge:claude-haiku-4-5"
    assert a["proof_hash"] == rep.proof_hash
