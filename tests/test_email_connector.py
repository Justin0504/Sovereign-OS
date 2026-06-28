"""Tests for the send_email connector (dry-run gating; injected SMTP for live)."""

from sovereign_os.connectors import dispatch, send_email


class FakeSMTP:
    def __init__(self):
        self.sent = []
    def sendmail(self, sender, to, msg):
        self.sent.append((sender, tuple(to), msg))


def test_dry_run_does_not_send(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_EMAIL_LIVE", raising=False)
    r = send_email("a@b.com", "Hi", "Body")
    assert r["sent"] is False and r["dry_run"] is True and r["to"] == "a@b.com"


def test_no_recipient():
    assert send_email("", "s", "b")["sent"] is False


def test_live_sends_via_injected_smtp(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SOVEREIGN_SMTP_USER", "u@test")
    fake = FakeSMTP()
    r = send_email("x@y.com", "Subject", "Hello", live=True, smtp=fake)
    assert r["sent"] is True and r["to"] == "x@y.com"
    assert len(fake.sent) == 1 and fake.sent[0][1] == ("x@y.com",)


def test_dispatch_routes_send_email(monkeypatch):
    monkeypatch.delenv("SOVEREIGN_EMAIL_LIVE", raising=False)
    r = dispatch("send_email", to="a@b.com", subject="s", body="b")
    assert r["dry_run"] is True
    assert "error" in dispatch("nonexistent_connector")
