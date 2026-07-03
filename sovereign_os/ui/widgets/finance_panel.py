"""
FinancePanel: Balance (USD), Token burn, TrustScore, and governance guardrails
(CFO circuit breaker + active JIT capability leases). Refreshed from
UnifiedLedger + SovereignAuth + SpendCircuitBreaker.
"""

from rich.panel import Panel
from rich.text import Text
from textual.widget import Widget
from textual.reactive import reactive


class FinancePanel(Widget):
    """Real-time finance + governance guardrails."""

    balance_usd = reactive("$0.00")
    total_tokens = reactive("0")
    trust_score = reactive("—")
    agent_id = reactive("—")
    daily_burn = reactive("—")
    # CFO circuit breaker + JIT leases (governance guardrails)
    breaker_state = reactive("off")   # CLOSED | TRIPPED | off
    session_spend = reactive("—")     # "$0.30 / $5.00" or "$0.30"
    fail_streak = reactive("—")
    roi = reactive("—")
    lease_count = reactive(0)
    lease_lines = reactive("")        # compact "agent · capability" list

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ledger = None
        self._auth = None
        self._breaker = None

    def set_ledger(self, ledger: object) -> None:
        self._ledger = ledger

    def set_auth(self, auth: object) -> None:
        self._auth = auth

    def set_breaker(self, breaker: object) -> None:
        self._breaker = breaker

    def refresh_from_backend(self) -> None:
        """Call from timer: read ledger, auth, and breaker; update reactives."""
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
        # CFO circuit breaker
        if self._breaker is not None:
            st = self._breaker.status()
            self.breaker_state = "TRIPPED" if st.get("tripped") else "CLOSED"
            spent = (st.get("spent_cents") or 0) / 100.0
            ceil = st.get("session_ceiling_cents") or 0
            self.session_spend = f"${spent:.2f} / ${ceil/100:.2f}" if ceil > 0 else f"${spent:.2f}"
            self.fail_streak = str(st.get("consecutive_failures") or 0)
            self.roi = f"{st['roi']:.2f}" if st.get("roi") is not None else "—"
        # Active JIT leases
        if self._auth is not None and hasattr(self._auth, "active_leases"):
            leases = self._auth.active_leases()
            self.lease_count = len(leases)
            self.lease_lines = "\n".join(
                f"[#00aaff]{l['capability']}[/] [dim]{str(l['agent_id'])[:14]}[/]"
                for l in leases[:5]
            )

    def render(self) -> Panel:
        # Breaker color: green when closed, red when tripped, dim when off.
        bstate = self.breaker_state
        bcolor = "red1" if bstate == "TRIPPED" else ("#00ff41" if bstate == "CLOSED" else "dim")
        streak = str(self.fail_streak)
        streak_color = "red1" if streak.isdigit() and int(streak) > 0 else "white"
        lease_block = self.lease_lines or "[dim]none[/]"
        body = Text.from_markup(
            f"[bold #00ff41]Balance[/]\n"
            f"[white]{self.balance_usd}[/]\n\n"
            f"[bold #00ff41]Tokens[/] [white]{self.total_tokens}[/]\n"
            f"[bold #00ff41]Daily Burn[/] [white]{self.daily_burn}[/]\n\n"
            f"[bold #00aaff]Agent[/] [dim]{self.agent_id}[/]\n"
            f"[bold #00aaff]TrustScore[/] [white]{self.trust_score}[/]\n\n"
            f"[bold #ff6600]── CFO BREAKER ──[/]\n"
            f"[bold]State[/] [{bcolor}]{bstate}[/]\n"
            f"[bold]Session[/] [white]{self.session_spend}[/]\n"
            f"[bold]Fails[/] [{streak_color}]{streak}[/]  [bold]ROI[/] [white]{self.roi}[/]\n\n"
            f"[bold #ff6600]── JIT LEASES ({self.lease_count}) ──[/]\n"
            f"{lease_block}"
        )
        return Panel(
            body,
            title="[bold #00ff41] Finance & Guardrails [/]",
            border_style="#00ff41",
            padding=(1, 2),
            title_align="left",
        )
