"""
WorkerRegistry: Maps required_skill (from TaskPlan) to Worker implementations.

Workers are instantiated with a system prompt derived from the Charter.
When MemoryManager is provided, top-3 similar past lessons are injected into the prompt.
"""

import logging
from typing import TYPE_CHECKING, Type

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sovereign_os.memory.manager import MemoryManager


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


def _inject_lessons(system_prompt: str, lessons: list[str]) -> str:
    """Append Corporate Memory lessons to the system prompt."""
    if not lessons:
        return system_prompt
    parts = [system_prompt, "", "Relevant lessons from past tasks (apply where applicable):"]
    for i, le in enumerate(lessons, 1):
        parts.append(f"  {i}. {le}")
    return "\n".join(parts)


class WorkerRegistry:
    """
    HR layer: maps required_skill to Worker class. Instantiates workers
    with a Charter-derived system prompt. Supports multiple bidders per skill for auction.
    """

    def __init__(self, charter: Charter) -> None:
        self._charter = charter
        self._skill_to_worker_class: dict[str, type[BaseWorker]] = {}
        self._skill_to_bidders: dict[str, list[tuple[str, type[BaseWorker]]]] = {}  # skill -> [(agent_id, class), ...]
        self._default_worker_class: type[BaseWorker] | None = None

    def register(self, skill_name: str, worker_class: type[BaseWorker], *, agent_id: str | None = None) -> None:
        """Register a Worker implementation for a skill (e.g. 'research', 'code'). Optionally set agent_id for bidding."""
        key = skill_name.strip().lower()
        self._skill_to_worker_class[key] = worker_class
        aid = agent_id or f"{key}-{worker_class.__name__}"
        if key not in self._skill_to_bidders:
            self._skill_to_bidders[key] = []
        self._skill_to_bidders[key].append((aid, worker_class))
        logger.debug("AGENTS REGISTRY: Registered %s -> %s (agent_id=%s)", skill_name, worker_class.__name__, aid)

    def get_bidders(self, required_skill: str) -> list[tuple[str, type[BaseWorker]]]:
        """Return all (agent_id, worker_class) that can bid for this skill (for RFP auction)."""
        key = required_skill.strip().lower()
        if key in self._skill_to_bidders and self._skill_to_bidders[key]:
            return list(self._skill_to_bidders[key])
        # Fallback: single worker from main registry
        worker_class = self._skill_to_worker_class.get(key) or self._default_worker_class
        if worker_class is None:
            return []
        return [(f"{key}-{worker_class.__name__}", worker_class)]

    def set_default(self, worker_class: type[BaseWorker]) -> None:
        """Set fallback Worker when skill is not registered."""
        self._default_worker_class = worker_class

    def get_worker(
        self,
        required_skill: str,
        agent_id: str,
        task_description: str = "",
        memory_manager: "MemoryManager | None" = None,
    ) -> BaseWorker:
        """
        Return an instantiated Worker for the given skill and agent_id.
        When multiple bidders exist, resolves worker class by agent_id; otherwise uses single registered class.
        Uses Charter to build system prompt; if memory_manager and task_description
        are provided, injects top-3 similar past lessons (Corporate Memory).
        """
        key = required_skill.strip().lower()
        worker_class: type[BaseWorker] | None = None
        if key in self._skill_to_bidders:
            for bidder_agent_id, clazz in self._skill_to_bidders[key]:
                if bidder_agent_id == agent_id:
                    worker_class = clazz
                    break
        if worker_class is None:
            worker_class = self._skill_to_worker_class.get(key) or self._default_worker_class
        if worker_class is None:
            raise KeyError(f"No worker registered for skill '{required_skill}' and no default set")
        system_prompt = _system_prompt_from_charter(self._charter, required_skill)
        if memory_manager and task_description:
            lessons = memory_manager.get_similar_lessons(task_description, k=3)
            system_prompt = _inject_lessons(system_prompt, lessons)

        # Optional: attach an LLM client to the worker, resolved per skill.
        llm_client = None
        try:  # pragma: no cover - optional LLM path
            from sovereign_os.llm.providers import create_llm_client

            llm_client = create_llm_client(f"worker_{key}")
        except Exception:
            # It is fine if no LLM is configured for this worker; many workers are tool-only.
            llm_client = None

        return worker_class(agent_id=agent_id, system_prompt=system_prompt, llm=llm_client)
