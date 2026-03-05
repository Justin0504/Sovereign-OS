"""
TaskTreeWidget: Reactive tree of task status (Pending / Running / Passed / Failed).
"""

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_PASSED = "passed"
TASK_STATUS_FAILED = "failed"

# Icons and short labels for status
_STATUS_DISPLAY = {
    TASK_STATUS_PENDING: ("[dim]○[/]", "pending"),
    TASK_STATUS_RUNNING: ("[bold cyan]◐[/]", "running"),
    TASK_STATUS_PASSED: ("[bold green]●[/]", "passed"),
    TASK_STATUS_FAILED: ("[bold red]✕[/]", "failed"),
}


class TaskTreeWidget(Tree):
    """Tree showing mission tasks and their status. Data driven by engine events."""

    def __init__(self, label: str = "Mission", *args, **kwargs) -> None:
        super().__init__(label, *args, **kwargs)
        self._task_nodes: dict[str, TreeNode] = {}
        self._task_status: dict[str, str] = {}
        self._task_skill: dict[str, str] = {}

    def set_plan(self, task_ids: list[str], required_skills: list[str] | None = None) -> None:
        """Set plan: one tree node per task, all pending."""
        self._task_nodes.clear()
        self._task_status.clear()
        self._task_skill.clear()
        root = self.root
        if root is None:
            return
        if hasattr(root, "clear_children"):
            root.clear_children()
        skills = list(required_skills) if required_skills else []
        while len(skills) < len(task_ids):
            skills.append("")
        skills = skills[: len(task_ids)]
        icon, _ = _STATUS_DISPLAY[TASK_STATUS_PENDING]
        for tid, skill in zip(task_ids, skills, strict=False):
            self._task_skill[tid] = skill
            skill_part = f" [dim]· {skill}[/]" if skill else ""
            node = root.add_leaf(f"{icon} [dim]{tid}[/]{skill_part}")
            self._task_nodes[tid] = node
            self._task_status[tid] = TASK_STATUS_PENDING
        self.refresh()

    def _make_label(self, task_id: str, status: str) -> str:
        icon, status_text = _STATUS_DISPLAY.get(status, _STATUS_DISPLAY[TASK_STATUS_PENDING])
        skill = self._task_skill.get(task_id, "")
        skill_part = f" [dim]· {skill}[/]" if skill else ""
        tid_style = "dim" if status == TASK_STATUS_PENDING else "white"
        return f"{icon} [{tid_style}]{task_id}[/]{skill_part} [dim]({status_text})[/]"

    def set_task_status(self, task_id: str, status: str) -> None:
        """Update one task's status (pending|running|passed|failed)."""
        self._task_status[task_id] = status
        node = self._task_nodes.get(task_id)
        if node is None:
            return
        new_label = self._make_label(task_id, status)
        if hasattr(node, "set_label"):
            node.set_label(new_label)
        else:
            setattr(node, "label", new_label)
        self.refresh()
