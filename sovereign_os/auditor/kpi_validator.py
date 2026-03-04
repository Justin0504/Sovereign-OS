"""
KPI Validator: Match task to Charter.success_kpis for verification.
"""

from sovereign_os.models.charter import Charter, SuccessKPI


def find_kpi_for_task(charter: Charter, task_description: str, task_required_skill: str) -> SuccessKPI | None:
    """
    Identify the relevant KPI from Charter.success_kpis for the task.

    Strategy: prefer KPI whose name or metric overlaps with task description/skill;
    otherwise return the first KPI. Returns None if charter has no success_kpis.
    """
    kpis = charter.success_kpis
    if not kpis:
        return None

    task_lower = f"{task_description} {task_required_skill}".lower()
    for kpi in kpis:
        if kpi.name.lower() in task_lower or kpi.metric.lower() in task_lower:
            return kpi
    for kpi in kpis:
        if task_required_skill.lower() in kpi.name.lower():
            return kpi
    return kpis[0]


class KPIValidator:
    """Helper to resolve KPI and build verification context for the Judge."""

    def __init__(self, charter: Charter) -> None:
        self._charter = charter

    def get_verification_prompt(self, task_description: str, task_required_skill: str) -> tuple[str, str]:
        """
        Return (kpi_name, verification_prompt) for the task.
        If no KPI matches, returns ("default", "Did the output satisfy the task success criteria?").
        """
        kpi = find_kpi_for_task(self._charter, task_description, task_required_skill)
        if kpi is None:
            return "default", "Did the output satisfy the task success criteria?"
        prompt = kpi.verification_prompt or "Did the output satisfy the task success criteria?"
        return kpi.name, prompt
