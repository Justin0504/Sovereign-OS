"""
Sovereign-OS Command Center: main Textual App.
Matrix/Cyberpunk theme; Header + TaskTree (30%) + DecisionStream (50%) + FinancePanel (20%).
"""

import asyncio
import logging
import sys
from collections import deque
from threading import Thread
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from sovereign_os.ui.widgets.decision_stream import DecisionStream
from sovereign_os.ui.widgets.finance_panel import FinancePanel
from sovereign_os.ui.widgets.task_tree import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PASSED,
    TASK_STATUS_RUNNING,
    TaskTreeWidget,
)


class DashboardApp(App):
    """Command Center: Header, TaskTree 30%, DecisionStream 50%, FinancePanel 20%. Panic: F12."""

    CSS = """
    Screen {
        background: #0a0a0a;
    }
    Header {
        background: #050505;
        color: #00ff41;
        border-bottom: heavy #00ff41;
        height: 3;
    }
    #header-area {
        height: 4;
        background: #050505;
        color: #00ff41;
        border-bottom: solid #003300;
        padding: 0 2;
        margin: 0 1;
    }
    #title-static {
        width: 1fr;
        content-align: left middle;
    }
    #main-grid {
        height: 1fr;
        width: 1fr;
        layout: horizontal;
        margin: 1 0;
    }
    #left-panel {
        width: 30%;
        height: 1fr;
        min-width: 18;
        border-right: solid #00ff41;
        padding: 0 1;
        background: #080808;
    }
    #panel-title-left {
        color: #00ff41;
        text-style: bold;
        padding: 0 0 1 0;
        height: 1;
    }
    #center-panel {
        width: 50%;
        height: 1fr;
        min-width: 28;
        padding: 0 1;
        background: #0a0a0a;
        border-right: solid #ff6600;
    }
    #panel-title-center {
        color: #ff6600;
        text-style: bold;
        padding: 0 0 1 0;
        height: 1;
    }
    #right-panel {
        width: 20%;
        height: 1fr;
        min-width: 14;
        padding: 0 1;
        background: #080808;
    }
    #panel-title-right {
        color: #00aaff;
        text-style: bold;
        padding: 0 0 1 0;
        height: 1;
    }
    TaskTreeWidget {
        height: 1fr;
        border: solid #004400;
        padding: 1;
        scrollbar-background: #0d0d0d;
        scrollbar-color: #00ff41;
    }
    DecisionStream {
        height: 1fr;
        scrollbar-background: #0d0d0d;
        scrollbar-color: #ff6600;
        padding: 1;
        border: solid #552200;
    }
    FinancePanel {
        height: auto;
        min-height: 10;
    }
    Footer {
        background: #050505;
        color: #00ff41;
        border-top: solid #003300;
        height: 1;
    }
    """

    TITLE = "SOVEREIGN-OS"
    SUB_TITLE = "Command Center"
    BINDINGS = [
        ("f12", "panic", "PANIC"),
        ("r", "run_demo", "Run demo mission"),
    ]

    def __init__(
        self,
        charter_name: str = "Default",
        ledger: Any = None,
        auth: Any = None,
        engine: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._charter_name = charter_name
        self._ledger = ledger
        self._auth = auth
        self._engine = engine
        self._event_queue: deque[tuple[str, dict]] = deque(maxlen=1000)
        self._log_queue: deque[tuple[str, str]] = deque(maxlen=500)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="header-area"):
            yield Static(
                Text.from_markup(
                    f"[bold #00ff41]▸ SOVEREIGN-OS[/] [dim]Command Center[/]\n"
                    f"[dim]Charter:[/] [cyan]{self._charter_name}[/]  [dim]│[/]  [dim]Press [/][bold]R[/][dim] Run demo  [/][bold]F12[/][dim] Panic[/]"
                ),
                id="title-static",
            )
        with Horizontal(id="main-grid"):
            with Vertical(id="left-panel"):
                yield Static("📋 TASKS", id="panel-title-left")
                yield TaskTreeWidget("Mission", id="task-tree")
            with Vertical(id="center-panel"):
                yield Static("📜 DECISION STREAM", id="panel-title-center")
                yield DecisionStream(id="decision-stream")
            with Vertical(id="right-panel"):
                yield Static("💰 FINANCE", id="panel-title-right")
                yield FinancePanel(id="finance-panel")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"Charter: {self._charter_name}"
        fp = self.query_one(FinancePanel)
        if self._ledger:
            fp.set_ledger(self._ledger)
        if self._auth:
            fp.set_auth(self._auth)
        self._refresh_finance()
        self.set_interval(2.0, self._refresh_finance)
        self.set_interval(0.5, self._drain_events)
        ds = self.query_one(DecisionStream)
        ds.push_generic("[dim]Command Center ready.[/]")
        ds.push_generic("[bold green]Press R[/] to run a demo mission (CEO → CFO → Workers → Auditor).")

    def _refresh_finance(self) -> None:
        fp = self.query_one(FinancePanel)
        fp.refresh_from_backend()
        fp.refresh()

    def _drain_events(self) -> None:
        while self._event_queue:
            try:
                etype, data = self._event_queue.popleft()
            except IndexError:
                break
            self._apply_engine_event(etype, data)
        while self._log_queue:
            try:
                source, msg = self._log_queue.popleft()
            except IndexError:
                break
            ds = self.query_one(DecisionStream)
            ds.push_log(source, msg)

    def _apply_engine_event(self, event_type: str, data: dict[str, Any]) -> None:
        tree = self.query_one(TaskTreeWidget)
        ds = self.query_one(DecisionStream)
        if event_type == "plan_created":
            tasks = data.get("tasks", [])
            task_ids = [t.get("task_id", "") for t in tasks if t.get("task_id")]
            skills = [t.get("required_skill", "") for t in tasks if t.get("task_id")]
            if task_ids:
                tree.set_plan(task_ids, skills)
            ds.push_ceo(f"Plan created: {len(task_ids)} tasks. Goal: {(data.get('goal') or '')[:80]}...")
        elif event_type == "task_started":
            task_id = data.get("task_id", "")
            agent_id = data.get("agent_id", "")
            tree.set_task_status(task_id, TASK_STATUS_RUNNING)
            ds.push_cfo(f"Task [cyan]{task_id}[/] started by [gold1]{agent_id}[/]")
        elif event_type == "task_finished":
            task_id = data.get("task_id", "")
            success = data.get("success", False)
            status = TASK_STATUS_PASSED if success else TASK_STATUS_FAILED
            tree.set_task_status(task_id, status)
            ds.push_generic(f"Task {task_id} finished (success={success})")
        elif event_type == "task_audited":
            task_id = data.get("task_id", "")
            passed = data.get("passed", False)
            score = data.get("score", 0)
            reason = data.get("reason", "")
            if passed:
                ds.push_auditor(f"Task [{task_id}] verified. Score: {score:.2f}.", passed=True)
            else:
                ds.push_auditor(f"Task [{task_id}] FAILED. Reason: {reason}", passed=False)

    def enqueue_engine_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Thread-safe: call from engine callback to queue an event for the UI."""
        self._event_queue.append((event_type, data))

    def enqueue_log(self, source: str, message: str) -> None:
        self._log_queue.append((source, message))

    def action_panic(self) -> None:
        """F12: Global kill switch."""
        self.notify("[bold red]PANIC — Shutting down.[/]", severity="error")
        sys.exit(0)

    def action_run_demo(self) -> None:
        """R: Run a demo mission (if engine is configured)."""
        self.run_mission_async("Summarize the market in one paragraph.")

    def run_mission_async(self, goal: str) -> None:
        """Run run_mission_with_audit in a thread and feed events to the UI."""
        engine = self._engine
        if engine is None:
            ds = self.query_one(DecisionStream)
            ds.push_generic("[red]No engine configured. Cannot run mission.[/]")
            return

        def run() -> None:
            async def _run() -> None:
                try:
                    await engine.run_mission_with_audit(goal, abort_on_audit_failure=False)
                except Exception as e:
                    self.call_from_thread(
                        lambda: self.enqueue_log("system", f"Mission error: {e}")
                    )

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run())
            finally:
                loop.close()

        t = Thread(target=run, daemon=True)
        t.start()


def run_dashboard(
    charter_name: str = "Default",
    ledger: Any = None,
    auth: Any = None,
    engine: Any = None,
) -> None:
    """Run the Command Center. If engine is provided, set its on_event to enqueue to app."""
    import os
    port_str = os.environ.get("SOVEREIGN_HEALTH_PORT", "").strip()
    if port_str and port_str.isdigit():
        try:
            from sovereign_os.health.server import run_health_server
            redis_url = os.environ.get("REDIS_URL")
            t = Thread(
                target=lambda: run_health_server(
                    host="0.0.0.0",
                    port=int(port_str),
                    ledger=ledger,
                    redis_url=redis_url,
                ),
                daemon=True,
            )
            t.start()
        except Exception as e:
            logging.getLogger(__name__).warning("Health server not started: %s", e)
    app = DashboardApp(charter_name=charter_name, ledger=ledger, auth=auth, engine=engine)
    if engine is not None:
        engine._on_event = app.enqueue_engine_event
    app.run()


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("SOVEREIGN_PROMETHEUS_PORT", "").strip():
        try:
            from sovereign_os.telemetry.tracer import init_telemetry
            init_telemetry(prometheus_port=int(os.environ["SOVEREIGN_PROMETHEUS_PORT"]))
        except Exception:
            pass
    from sovereign_os import load_charter, UnifiedLedger
    from sovereign_os.agents import SovereignAuth
    from sovereign_os.auditor import ReviewEngine
    from sovereign_os.governance import GovernanceEngine
    from sovereign_os.health.checker import run_health_check

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    charter_path = os.path.join(root, "charter.example.yaml")
    charter = load_charter(charter_path)
    ledger = UnifiedLedger()
    ledger.record_usd(1000)
    run_health_check(ledger=ledger, redis_url=os.environ.get("REDIS_URL"))
    auth = SovereignAuth()
    review = ReviewEngine(charter)
    engine = GovernanceEngine(charter, ledger, auth=auth, review_engine=review, on_event=lambda e, d: None)
    run_dashboard(charter_name="Example Charter", ledger=ledger, auth=auth, engine=engine)
