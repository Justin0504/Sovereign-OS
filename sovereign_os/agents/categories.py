"""
TaskCategory registry — the backbone that ties together worker routing, budget
ceilings, permission tiers, and connector needs, starting from the categories the
real marketplaces emit (BotBounty: code/research/creative/data/automation/other;
TaskBounty/StacksTasker: coding/bug-fix; ClawTasks: writing; RentAHuman: physical).

Each delivery category declares:
  - skill        : which top-tier worker handles it
  - risk         : low | medium | high  (drives budget + permission tiers)
  - max_cost_usd : per-task budget ceiling for this category (CFO policy)
  - capability   : permission an agent must hold to take this category of work
  - connectors   : MCP/connector tools the category benefits from (e.g. web_search)
  - aliases      : platform category strings + keywords that map to this category
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sovereign_os.agents.auth import Capability


@dataclass(frozen=True)
class TaskCategory:
    key: str
    skill: str
    risk: str = "low"
    max_cost_usd: float = 0.50
    capability: Capability = Capability.READ_FILES
    connectors: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)


# High-frequency delivery categories. Order matters for keyword matching (specific first).
CATEGORIES: tuple[TaskCategory, ...] = (
    TaskCategory(
        key="coding", skill="code_assistant", risk="medium", max_cost_usd=2.00,
        capability=Capability.WRITE_FILES, connectors=("git", "file_read", "code_search"),
        aliases=("code", "coding", "bug fix", "bug-fix", "bugfix", "pr", "pull request",
                 "feature", "debug", "refactor", "implement", "fix", "develop"),
    ),
    TaskCategory(
        key="data", skill="data_analysis", risk="medium", max_cost_usd=1.50,
        capability=Capability.WRITE_FILES, connectors=("sql", "spreadsheet", "web_fetch"),
        aliases=("data", "analysis", "analytics", "dataset", "csv", "etl", "extract",
                 "scrape", "structured", "transform"),
    ),
    TaskCategory(
        key="design", skill="design_brief", risk="medium", max_cost_usd=1.50,
        capability=Capability.WRITE_FILES, connectors=("figma", "image_gen"),
        aliases=("design", "ui", "ux", "wireframe", "mockup", "logo", "brand", "figma", "layout"),
    ),
    TaskCategory(
        key="email", skill="write_email", risk="medium", max_cost_usd=0.75,
        capability=Capability.CALL_EXTERNAL_API, connectors=("send_email",),
        aliases=("email", "outreach", "cold email", "sequence", "newsletter", "reply"),
    ),
    TaskCategory(
        key="research", skill="research", risk="low", max_cost_usd=1.00,
        capability=Capability.CALL_EXTERNAL_API, connectors=("web_search", "web_fetch"),
        aliases=("research", "investigate", "competitive", "landscape", "market research", "compare"),
    ),
    TaskCategory(
        key="writing", skill="write_article", risk="low", max_cost_usd=1.00,
        capability=Capability.READ_FILES, connectors=(),
        aliases=("writing", "write", "article", "blog", "content", "copy", "post", "creative",
                 "story", "essay", "draft"),
    ),
    TaskCategory(
        key="automation", skill="spec_writer", risk="high", max_cost_usd=2.50,
        capability=Capability.EXECUTE_SHELL, connectors=("workflow", "webhook"),
        aliases=("automation", "automate", "workflow", "pipeline", "integration", "bot", "script"),
    ),
    TaskCategory(
        key="general", skill="assistant_chat", risk="low", max_cost_usd=0.50,
        capability=Capability.READ_FILES, connectors=(),
        aliases=("general", "other", "task", "help", "question", "summarize", "summary"),
    ),
)

_BY_KEY: dict[str, TaskCategory] = {c.key: c for c in CATEGORIES}
GENERAL = _BY_KEY["general"]


def get_category(key: str) -> TaskCategory:
    """Look up a category by key; falls back to 'general'."""
    return _BY_KEY.get((key or "").strip().lower(), GENERAL)


def categorize(platform_category: str = "", text: str = "") -> TaskCategory:
    """
    Classify a task. Prefer the platform's own category label (exact/alias match),
    then keyword-match the task text, else 'general'.
    """
    plat = (platform_category or "").strip().lower()
    if plat:
        for c in CATEGORIES:
            if plat == c.key or plat in c.aliases:
                return c
    blob = f"{platform_category} {text}".lower()
    for c in CATEGORIES:  # specific categories first (general is last)
        if c.key == "general":
            continue
        if any(alias in blob for alias in c.aliases):
            return c
    return GENERAL


def route_skill(platform_category: str = "", text: str = "") -> str:
    """Convenience: category -> top-tier worker skill."""
    return categorize(platform_category, text).skill


_BY_SKILL: dict[str, TaskCategory] = {c.skill: c for c in CATEGORIES}

# Extra skill names that belong to a category but aren't its canonical `skill`.
_SKILL_ALIASES: dict[str, str] = {
    "code_review": "coding", "spec_writer": "automation", "extract_structured": "data",
    "collect_info": "data", "write_post": "writing", "rewrite_polish": "writing",
    "summarize": "general", "research": "research", "reply": "email",
    "meeting_minutes": "writing", "translate": "writing", "solve_problem": "general",
    "help_docs": "writing",
}


def category_for_skill(skill: str) -> TaskCategory:
    """Reverse lookup: which category a worker skill belongs to (falls back to 'general')."""
    s = (skill or "").strip().lower()
    if s in _BY_SKILL:
        return _BY_SKILL[s]
    if s in _SKILL_ALIASES:
        return _BY_KEY[_SKILL_ALIASES[s]]
    return GENERAL
