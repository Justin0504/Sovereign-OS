"""
AgentBackend: pluggable "brains" for complex task delivery.

A worker's default brain is a single LLM (ChatLLM). For complex, tool-heavy work —
multi-file coding, refactors, running the test suite — a purpose-built coding agent
(Claude Code, OpenAI Codex, Gemini CLI, Aider, ...) delivers far better than one chat
call. Rather than hard-wire each of those, Sovereign-OS routes a task to an
`AgentBackend` through ONE stable interface, so:

  - the **native** backend keeps today's behavior (zero change by default), and
  - an external agent plugs in as a **command template** (config, not code) — a new
    agent is a new entry in KNOWN_AGENTS or a `SOVEREIGN_BACKEND_CMD` override.

This is also how the OS adapts to platform churn: the interface stays fixed while
CLIs evolve; and because Claude Agent SDK and Codex both speak MCP, MCP-discovered
tools flow in without touching this layer.

Safety: spawning an external agent runs code/tools on the host, so it is DRY-RUN
unless `SOVEREIGN_AGENT_BACKEND_ENABLED=true`. In dry-run `execute_task` returns a
note and success=False (nothing runs). Selection is opt-in per skill/global; unset =
native (workers use their own LLM). The subprocess runner is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# Known headless coding agents: how to invoke each non-interactively. `prompt_via`
# is "stdin" (prompt piped in) or "arg" (prompt appended as the final argument).
# Override the command for any of these with SOVEREIGN_BACKEND_CMD (a JSON list).
KNOWN_AGENTS: dict[str, dict[str, Any]] = {
    "claude-code": {"cmd": ["claude", "-p"], "prompt_via": "stdin"},
    "codex":       {"cmd": ["codex", "exec"], "prompt_via": "arg"},
    "gemini-cli":  {"cmd": ["gemini", "-p"], "prompt_via": "arg"},
    "aider":       {"cmd": ["aider", "--yes", "--message"], "prompt_via": "arg"},
}

# A runner executes a command and returns {"rc", "stdout", "stderr"}.
Runner = Callable[[list[str], str, str, float], Awaitable[dict[str, Any]]]


async def _default_runner(cmd: list[str], cwd: str, stdin: str, timeout: float) -> dict[str, Any]:
    """Spawn `cmd` in `cwd`, feed `stdin`, capture output. Never raises — returns rc."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd or None,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return {"rc": 127, "stdout": "", "stderr": f"agent binary not found: {e}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate((stdin or "").encode("utf-8")), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"rc": 124, "stdout": "", "stderr": f"timed out after {timeout}s"}
    return {"rc": proc.returncode, "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace")}


def _env_bool(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CLIAgentBackend:
    """
    Delegate a whole task to a headless external coding agent (Claude Code, Codex, ...).

    The agent runs in the task's workspace and does its own tool use (edit files, run
    tests) internally, then returns a final summary. Gated: actually spawns only when
    `enabled`; otherwise returns a dry-run note.
    """

    backend_id: str
    cmd: list[str]
    prompt_via: str = "stdin"          # "stdin" | "arg"
    enabled: bool = False
    timeout_s: float = 900.0
    runner: Runner = field(default=_default_runner, repr=False)

    async def execute_task(
        self,
        *,
        description: str,
        skill: str = "",
        system_prompt: str = "",
        cwd: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(description, system_prompt, context or {})
        if not self.enabled:
            logger.info("AGENT BACKEND: %s DRY-RUN (set SOVEREIGN_AGENT_BACKEND_ENABLED=true to run).", self.backend_id)
            return {
                "output": f"[{self.backend_id} dry-run] would run: {' '.join(self.cmd)} in {cwd or '.'}",
                "success": False,
                "metadata": {"backend": self.backend_id, "dry_run": True, "model_id": self.backend_id},
            }
        if self.prompt_via == "arg":
            cmd, stdin = [*self.cmd, prompt], ""
        else:
            cmd, stdin = list(self.cmd), prompt
        r = await self.runner(cmd, cwd, stdin, self.timeout_s)
        rc = int(r.get("rc", 1))
        out = (r.get("stdout") or "").strip()
        text = _extract_text(out) or out or (r.get("stderr") or "").strip()
        return {
            "output": text,
            "success": rc == 0 and bool(text),
            "metadata": {
                "backend": self.backend_id, "model_id": self.backend_id,
                "rc": rc, "dry_run": False,
            },
        }

    def _build_prompt(self, description: str, system_prompt: str, context: dict[str, Any]) -> str:
        parts = []
        if system_prompt.strip():
            parts.append(system_prompt.strip())
        parts.append((description or "").strip())
        code = str(context.get("code", "")).strip()
        if code:
            lang = str(context.get("language", "")).strip()
            parts.append(f"Relevant code ({lang}):\n```\n{code}\n```")
        parts.append(
            "Complete the task fully in this workspace. Make the changes, run the tests, "
            "and ensure they pass. End with a short summary of what you did."
        )
        return "\n\n".join(p for p in parts if p)


def _extract_text(stdout: str) -> str:
    """
    Pull the human text out of an agent's output. Many CLIs support --output-format
    json; if the last line parses as JSON with a result/text/output field, use it.
    Otherwise return "" so the caller falls back to raw stdout.
    """
    s = (stdout or "").strip()
    if not s:
        return ""
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for key in ("result", "text", "output", "content", "response"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def resolve_backend_name(skill: str) -> str:
    """
    Which backend to use for `skill`: per-skill `SOVEREIGN_BACKEND_<CATEGORY>` wins,
    else global `SOVEREIGN_AGENT_BACKEND`, else "native". `skill` may be a skill or a
    category key; both spellings of the env var are accepted.
    """
    from sovereign_os.agents.categories import category_for_skill

    category = category_for_skill(skill).key if skill else ""
    for key in (category, skill):
        if key:
            v = (os.getenv(f"SOVEREIGN_BACKEND_{key.upper()}") or "").strip()
            if v:
                return v.lower()
    return (os.getenv("SOVEREIGN_AGENT_BACKEND") or "native").strip().lower()


def build_backend(name: str, *, runner: Runner | None = None) -> CLIAgentBackend | None:
    """
    Build a CLI agent backend by name, or return None for native/unknown so the caller
    uses the worker's own LLM. Command/prompt-delivery are overridable via env:
      SOVEREIGN_BACKEND_CMD        JSON list or shell string (overrides KNOWN_AGENTS[name])
      SOVEREIGN_BACKEND_PROMPT_VIA "stdin" | "arg"
      SOVEREIGN_AGENT_BACKEND_TIMEOUT  seconds (default 900)
    """
    name = (name or "").strip().lower()
    if name in ("", "native"):
        return None
    spec = KNOWN_AGENTS.get(name)
    cmd_override = (os.getenv("SOVEREIGN_BACKEND_CMD") or "").strip()
    if cmd_override:
        try:
            cmd = json.loads(cmd_override) if cmd_override.startswith("[") else shlex.split(cmd_override)
        except ValueError:
            cmd = shlex.split(cmd_override)
    elif spec:
        cmd = list(spec["cmd"])
    else:
        logger.warning("AGENT BACKEND: unknown backend '%s' and no SOVEREIGN_BACKEND_CMD; using native.", name)
        return None
    prompt_via = (os.getenv("SOVEREIGN_BACKEND_PROMPT_VIA") or (spec or {}).get("prompt_via") or "stdin").strip().lower()
    try:
        timeout = float((os.getenv("SOVEREIGN_AGENT_BACKEND_TIMEOUT") or "").strip() or 900.0)
    except ValueError:
        timeout = 900.0
    return CLIAgentBackend(
        backend_id=name,
        cmd=cmd if isinstance(cmd, list) else [str(cmd)],
        prompt_via="arg" if prompt_via == "arg" else "stdin",
        enabled=_env_bool("SOVEREIGN_AGENT_BACKEND_ENABLED"),
        timeout_s=timeout,
        runner=runner or _default_runner,
    )


def resolve_backend(skill: str, *, runner: Runner | None = None) -> CLIAgentBackend | None:
    """Convenience: name -> backend for a skill (None = use the worker's own LLM)."""
    return build_backend(resolve_backend_name(skill), runner=runner)
