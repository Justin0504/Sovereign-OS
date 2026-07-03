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


def _score_color(v: float) -> str:
    if v >= 0.85:
        return "green3"
    if v >= 0.70:
        return "gold1"
    if v >= 0.50:
        return "dark_orange"
    return "red1"


def _mini_bar(v: float, width: int = 8) -> str:
    v = max(0.0, min(1.0, float(v)))
    filled = round(v * width)
    return "█" * filled + "░" * (width - filled)


def format_rubric(sub_scores: dict, category: str = "") -> str:
    """
    Compact per-criterion rubric breakdown for the stream (Rich markup).

    Each criterion gets a mini bar + 0–100 score, colored by value. Returns ""
    when there are no sub-scores (e.g. a lenient low-value audit) so callers can
    skip emitting an empty line.
    """
    if not sub_scores:
        return ""
    head = f"   [dim]{category} rubric:[/]" if category else "   [dim]rubric:[/]"
    lines = [head]
    for k, v in sub_scores.items():
        color = _score_color(v)
        lines.append(f"     [dim]{str(k)[:12]:<12}[/] [{color}]{_mini_bar(v)} {int(round(float(v) * 100)):>3}[/]")
    return "\n".join(lines)


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

    def push_rubric(self, sub_scores: dict, category: str = "") -> None:
        """Write a compact per-criterion rubric breakdown under an audit line."""
        text = format_rubric(sub_scores, category)
        if not text:
            return
        self.write(Text.from_markup(text))
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
