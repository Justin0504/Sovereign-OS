"""
TaskTreeWidget: Reactive tree of task status (Pending / Running / Passed / Failed).
"""

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_PASSED = "passed"
TASK_STATUS_FAILED = "failed"


class TaskTreeWidget(Tree):
    """Tree showing mission tasks and their status. Data driven by engine events."""

    def __init__(self, label: str = "Tasks", *args, **kwargs) -> None:
        super().__init__(label, *args, **kwargs)
        self._task_nodes: dict[str, TreeNode] = {}
        self._task_status: dict[str, str] = {}

    def set_plan(self, task_ids: list[str], required_skills: list[str] | None = None) -> None:
        """Set plan: one tree node per task, all pending."""
        self._task_nodes.clear()
        self._task_status.clear()
        root = self.root
        if root is None:
            return
        if hasattr(root, "clear_children"):
            root.clear_children()
        skills = list(required_skills) if required_skills else []
        while len(skills) < len(task_ids):
            skills.append("")
        skills = skills[: len(task_ids)]
        for tid, skill in zip(task_ids, skills, strict=False):
            node = root.add_leaf(f"[dim]⏳ {tid}[/] ({skill})", expand=False)
            self._task_nodes[tid] = node
            self._task_status[tid] = TASK_STATUS_PENDING
        self.refresh()

    def set_task_status(self, task_id: str, status: str) -> None:
        """Update one task's status (pending|running|passed|failed)."""
        self._task_status[task_id] = status
        node = self._task_nodes.get(task_id)
        if node is None:
            return
        if status == TASK_STATUS_RUNNING:
            new_label = f"[bold cyan]▶ {task_id}[/] (running)"
        elif status == TASK_STATUS_PASSED:
            new_label = f"[bold green]✓ {task_id}[/] (passed)"
        elif status == TASK_STATUS_FAILED:
            new_label = f"[bold red]✗ {task_id}[/] (failed)"
        else:
            new_label = f"[dim]⏳ {task_id}[/] (pending)"
        if hasattr(node, "set_label"):
            node.set_label(new_label)
        else:
            setattr(node, "label", new_label)
        self.refresh()
