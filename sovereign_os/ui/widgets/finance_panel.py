"""
FinancePanel: Balance (USD), Token burn, TrustScore. Refreshed from UnifiedLedger + Auth.
"""

from rich.panel import Panel
from rich.text import Text
from textual.widget import Widget
from textual.reactive import reactive


class FinancePanel(Widget):
    """Real-time finance: balance, token burn, active agent trust score."""

    balance_usd = reactive("$0.00")
    total_tokens = reactive("0")
    trust_score = reactive("—")
    agent_id = reactive("—")
    daily_burn = reactive("—")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ledger = None
        self._auth = None

    def set_ledger(self, ledger: object) -> None:
        self._ledger = ledger

    def set_auth(self, auth: object) -> None:
        self._auth = auth

    def refresh_from_backend(self) -> None:
        """Call from timer: read ledger and auth, update reactives."""
        if self._ledger is not None:
            cents = self._ledger.total_usd_cents()
            self.balance_usd = f"${cents / 100:.2f}"
            by_model = getattr(self._ledger, "total_tokens_by_model", lambda: {})()
            total = sum(by_model.values()) if isinstance(by_model, dict) else 0
            self.total_tokens = f"{total:,}"
            if hasattr(self._ledger, "usd_debits_since"):
                from datetime import datetime, timezone
                start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                daily = self._ledger.usd_debits_since(start)
                self.daily_burn = f"${daily / 100:.2f}"
            else:
                self.daily_burn = "—"
        if self._auth is not None and getattr(self._auth, "_scores", None):
            scores = self._auth._scores
            if scores:
                last_agent = list(scores.keys())[-1]
                self.agent_id = last_agent
                self.trust_score = str(scores[last_agent])
            else:
                self.agent_id = "—"
                self.trust_score = str(getattr(self._auth, "_base", 50))

    def render(self) -> Panel:
        body = Text.from_markup(
            f"[bold #00ff41]Balance[/]\n"
            f"[white]{self.balance_usd}[/]\n\n"
            f"[bold #00ff41]Tokens Burned[/]\n"
            f"[white]{self.total_tokens}[/]\n\n"
            f"[bold #00ff41]Daily Burn[/]\n"
            f"[white]{self.daily_burn}[/]\n\n"
            f"[bold #00aaff]Agent[/] [dim]{self.agent_id}[/]\n"
            f"[bold #00aaff]TrustScore[/] [white]{self.trust_score}[/]"
        )
        return Panel(
            body,
            title="[bold #00ff41] Finance [/]",
            border_style="#00ff41",
            padding=(1, 2),
            title_align="left",
        )
