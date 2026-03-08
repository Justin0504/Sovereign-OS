"""
WorkerRegistry: Maps required_skill (from TaskPlan) to Worker implementations.

Workers are instantiated with a system prompt derived from the Charter.
When MemoryManager is provided, top-3 similar past lessons are injected into the prompt.
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Type

from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult
from sovereign_os.models.charter import Charter

logger = logging.getLogger(__name__)

_env_loaded = False


def _load_env_once() -> None:
    """Load .env from project root and cwd so ANTHROPIC_API_KEY etc. are set (e.g. in job worker thread)."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        return
    except Exception:
        pass
    # Fallback: read .env manually so workers get API keys even without python-dotenv
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        p = base / ".env"
        if not p.is_file():
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line and not line.split("=", 1)[0].strip().startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

if TYPE_CHECKING:
    from sovereign_os.memory.manager import MemoryManager
    from sovereign_os.mcp.tool_graph import MCPToolGraph


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
    Phase 5: when mcp_tool_graph is set, skills with no registered worker can be fulfilled
    by MCPWorker using tools discovered from the graph (self-hiring).
    """

    def __init__(self, charter: Charter, *, mcp_tool_graph: "MCPToolGraph | None" = None) -> None:
        self._charter = charter
        self._mcp_tool_graph = mcp_tool_graph
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
        from sovereign_os.agents.mcp_worker import MCPWorker

        key = required_skill.strip().lower()
        bidders: list[tuple[str, type[BaseWorker]]] = []
        if key in self._skill_to_bidders and self._skill_to_bidders[key]:
            bidders = list(self._skill_to_bidders[key])
        else:
            worker_class = self._skill_to_worker_class.get(key) or self._default_worker_class
            if worker_class is not None:
                bidders = [(f"{key}-{worker_class.__name__}", worker_class)]
        # Phase 5 self-hiring: add MCP bidder when graph has tools for this skill
        if self._mcp_tool_graph and self._mcp_tool_graph.has_tools_for_skill(key):
            bidders = bidders + [(f"mcp-{key}", MCPWorker)]
        return bidders

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
        When agent_id starts with "mcp-" and mcp_tool_graph is set, returns MCPWorker
        (Phase 5 self-hiring). Otherwise uses registered or default worker class.
        """
        from sovereign_os.agents.mcp_worker import MCPWorker

        key = required_skill.strip().lower()
        # Phase 5: MCP-backed worker when agent is mcp-{skill} and graph has tools
        if agent_id.startswith("mcp-") and self._mcp_tool_graph:
            tools = self._mcp_tool_graph.get_tools_for_skill(key)
            if tools:
                system_prompt = _system_prompt_from_charter(self._charter, required_skill)
                if memory_manager and task_description:
                    lessons = memory_manager.get_similar_lessons(task_description, k=3)
                    system_prompt = _inject_lessons(system_prompt, lessons)
                return MCPWorker(
                    agent_id=agent_id,
                    system_prompt=system_prompt,
                    skill=key,
                    tools=tools,
                    get_client=self._mcp_tool_graph.get_client,
                )

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

        llm_client = None
        try:  # pragma: no cover - optional LLM path
            _load_env_once()
            from sovereign_os.llm.providers import create_llm_client

            llm_client = create_llm_client(f"worker_{key}")
        except Exception as e:
            logger.warning(
                "Worker LLM creation failed for skill %r: %s. Ensure .env in project root has ANTHROPIC_API_KEY=sk-ant-... and restart.",
                key,
                e,
            )
            llm_client = None

        return worker_class(agent_id=agent_id, system_prompt=system_prompt, llm=llm_client)
