"""
WorkerRegistry: Maps required_skill (from TaskPlan) to Worker implementations.

Workers are instantiated with a system prompt derived from the Charter.
"""

import logging
from typing import Type

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)


def _system_prompt_from_charter(charter: Charter, skill_name: str) -> str:
    """Build a system prompt so workers align with the company mission and competency."""
    parts = [
        "You operate as an agent of a charter-driven entity.",
        "",
        "Mission:",
        charter.mission.strip(),
        "",
    ]
    for c in charter.core_competencies:
        if c.name.lower() == skill_name.lower():
            parts.append(f"Your competency: {c.name}. {c.description or 'Execute tasks in this domain.'}")
            break
    else:
        parts.append(f"Your role: execute tasks requiring skill '{skill_name}' in line with the mission above.")
    return "\n".join(parts)


class WorkerRegistry:
    """
    HR layer: maps required_skill to Worker class. Instantiates workers
    with a Charter-derived system prompt.
    """

    def __init__(self, charter: Charter) -> None:
        self._charter = charter
        self._skill_to_worker_class: dict[str, type[BaseWorker]] = {}
        self._default_worker_class: type[BaseWorker] | None = None

    def register(self, skill_name: str, worker_class: type[BaseWorker]) -> None:
        """Register a Worker implementation for a skill (e.g. 'research', 'code')."""
        key = skill_name.strip().lower()
        self._skill_to_worker_class[key] = worker_class
        logger.debug("AGENTS REGISTRY: Registered %s -> %s", skill_name, worker_class.__name__)

    def set_default(self, worker_class: type[BaseWorker]) -> None:
        """Set fallback Worker when skill is not registered."""
        self._default_worker_class = worker_class

    def get_worker(self, required_skill: str, agent_id: str) -> BaseWorker:
        """
        Return an instantiated Worker for the given skill and agent_id.
        Uses Charter to build system prompt for mission alignment.
        """
        key = required_skill.strip().lower()
        worker_class = self._skill_to_worker_class.get(key) or self._default_worker_class
        if worker_class is None:
            raise KeyError(f"No worker registered for skill '{required_skill}' and no default set")
        system_prompt = _system_prompt_from_charter(self._charter, required_skill)
        return worker_class(agent_id=agent_id, system_prompt=system_prompt)
