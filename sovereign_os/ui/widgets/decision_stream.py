"""
DecisionStream: Rich-text log with CEO (blue), CFO (gold), Auditor (purple/red).
"""

from rich.text import Text
from textual.widgets import RichLog


def _style_for_source(source: str) -> str:
    if source == "ceo":
        return "bold blue"
    if source == "cfo":
        return "bold gold1"
    if source == "auditor_pass":
        return "bold medium_purple1"
    if source == "auditor_fail":
        return "bold red1"
    return "dim"


class DecisionStream(RichLog):
    """Scrollable rich log: CEO=Blue, CFO=Gold, Auditor Pass=Purple, Auditor Fail=Red."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_lines = 600

    def push_ceo(self, message: str) -> None:
        self.write(Text.from_markup(f"[bold blue]▸ CEO[/] [blue]{message}[/]"))
        self._trim()

    def push_cfo(self, message: str) -> None:
        self.write(Text.from_markup(f"[bold gold1]▸ CFO[/] [gold1]{message}[/]"))
        self._trim()

    def push_auditor(self, message: str, passed: bool = True) -> None:
        if passed:
            self.write(Text.from_markup(f"[bold medium_purple1]▸ AUDIT[/] [medium_purple1]{message}[/]"))
        else:
            self.write(Text.from_markup(f"[bold red1]▸ AUDIT FAIL[/] [red1]{message}[/]"))
        self._trim()

    def push_log(self, source: str, message: str) -> None:
        style = _style_for_source(source)
        self.write(Text.from_markup(f"[{style}]{message}[/]"))
        self._trim()

    def push_generic(self, message: str) -> None:
        self.write(Text.from_markup(f"[dim]{message}[/]"))
        self._trim()

    def _trim(self) -> None:
        try:
            lines = getattr(self, "_lines", None)
            if lines is not None and len(lines) > self._max_lines:
                while len(lines) > self._max_lines:
                    lines.pop(0)
        except Exception:
            pass
