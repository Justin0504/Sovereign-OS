"""
LLM provider abstraction for Sovereign-OS.

This module lets you plug different model vendors (OpenAI, Anthropic, etc.)
behind a single async chat interface so that Strategist, Auditor, and Workers
do not depend on any specific SDK.

Only the OpenAI provider is guaranteed to work out-of-the-box. Other providers
require the corresponding optional dependencies and environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ChatLLM(Protocol):
    """Minimal async chat interface used by Strategist / Auditor / Workers."""

    @property
    def model_name(self) -> str: ...

    async def chat(self, messages: list[dict[str, Any]]) -> str: ...


@dataclass
class LLMConfig:
    """Resolved configuration for a logical role (strategist, judge, worker_x)."""

    provider: str
    model: str


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    return value if value is not None and value.strip() else default


def _default_provider() -> str:
    """If SOVEREIGN_LLM_PROVIDER not set: use anthropic when only ANTHROPIC_API_KEY is set."""
    if _env("SOVEREIGN_LLM_PROVIDER"):
        return _env("SOVEREIGN_LLM_PROVIDER", "openai") or "openai"
    if _env("ANTHROPIC_API_KEY") and not _env("OPENAI_API_KEY"):
        return "anthropic"
    return "openai"


def _default_model(provider: str) -> str:
    """Default model per provider when SOVEREIGN_LLM_MODEL not set."""
    if provider in ("anthropic", "claude"):
        return "claude-sonnet-4-20250514"
    return "gpt-4o"


def _get_llm_config(role: str) -> LLMConfig | None:
    """
    Resolve provider/model for a logical role.

    Environment variables (checked in this order):
    - SOVEREIGN_LLM_PROVIDER_<ROLE>, SOVEREIGN_LLM_MODEL_<ROLE>
    - SOVEREIGN_LLM_PROVIDER, SOVEREIGN_LLM_MODEL

    If SOVEREIGN_LLM_PROVIDER is unset and only ANTHROPIC_API_KEY is set, provider defaults to anthropic.
    If nothing is set, returns None (callers should fall back to stub logic).
    """
    role_key = role.upper().replace(":", "_")
    provider = _env(f"SOVEREIGN_LLM_PROVIDER_{role_key}") or _env("SOVEREIGN_LLM_PROVIDER") or _default_provider()
    model = _env(f"SOVEREIGN_LLM_MODEL_{role_key}") or _env("SOVEREIGN_LLM_MODEL") or _default_model(provider)
    if not provider or not model:
        return None
    return LLMConfig(provider=provider.lower(), model=model)


class OpenAIChatLLM:
    """ChatLLM implementation backed by openai.AsyncOpenAI."""

    def __init__(self, *, model: str, api_key: str | None = None) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - optional dependency
            raise ImportError(
                "openai package is required for OpenAI provider; "
                "install with: pip install 'sovereign-os[llm]'"
            ) from e

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set; export it or configure a different provider."
            )
        self._client = AsyncOpenAI(api_key=key)
        self._model = model
        self._last_usage: dict[str, int] | None = None

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        self._last_usage = None
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        content = getattr(response.choices[0].message, "content", "") or ""
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._last_usage = {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }
        return str(content)


class AnthropicChatLLM:
    """ChatLLM implementation backed by anthropic.AsyncAnthropic."""

    def __init__(self, *, model: str, api_key: str | None = None) -> None:
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - optional dependency
            raise ImportError(
                "anthropic package is required for Anthropic provider; "
                "install with: pip install 'sovereign-os[llm]'"
            ) from e

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; export it or configure a different provider."
            )
        self._client = AsyncAnthropic(api_key=key)
        self._model = model
        self._last_usage: dict[str, int] | None = None

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        self._last_usage = None
        # Split system vs user/assistant messages for Anthropic API.
        system_parts: list[str] = []
        convo: list[dict[str, Any]] = []
        for m in messages:
            role = str(m.get("role", "user"))
            content = str(m.get("content", ""))
            if role == "system":
                system_parts.append(content)
            else:
                convo.append(
                    {
                        "role": role,
                        "content": [{"type": "text", "text": content}],
                    }
                )
        system_text = "\n\n".join(system_parts) if system_parts else None
        if not convo:
            convo = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": ""}],
                }
            ]
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system_text,
            messages=convo,
        )
        usage = getattr(message, "usage", None)
        if usage is None and hasattr(message, "model_dump"):
            try:
                raw = message.model_dump()
                usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
                if usage is not None:
                    usage = type("Usage", (), usage)()  # simple namespace for getattr below
            except Exception:
                pass
        inp, out_tok = None, None
        if usage is not None:
            inp = getattr(usage, "input_tokens", None)
            out_tok = getattr(usage, "output_tokens", None)
            if inp is None or out_tok is None:
                try:
                    d = usage.model_dump() if hasattr(usage, "model_dump") else (vars(usage) if hasattr(usage, "__dict__") else {})
                    if not isinstance(d, dict):
                        d = {}
                    inp = inp if inp is not None else d.get("input_tokens")
                    out_tok = out_tok if out_tok is not None else d.get("output_tokens")
                except Exception:
                    pass
            if inp is None:
                inp = getattr(usage, "prompt_tokens", 0) or 0
            if out_tok is None:
                out_tok = getattr(usage, "completion_tokens", 0) or 0
            self._last_usage = {
                "input_tokens": int(inp) if inp is not None else 0,
                "output_tokens": int(out_tok) if out_tok is not None else 0,
            }
        else:
            self._last_usage = None
            logger.info(
                "Anthropic response had no message.usage; token table will show estimated 2000/2000. "
                "Check anthropic SDK version (pip show anthropic) and API response."
            )
        parts = []
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", "") == "text":
                parts.append(getattr(block, "text", ""))
        return "".join(parts)


def create_llm_client(role: str) -> ChatLLM:
    """
    Factory used by Strategist / Auditor / Workers.

    - role: logical role name, e.g. "strategist", "judge", "worker_research"
    - returns: ChatLLM implementation

    If configuration or provider is missing, raises a descriptive error so that
    callers can decide whether to fall back to stub behavior.
    """
    cfg = _get_llm_config(role)
    if cfg is None:
        raise RuntimeError(
            f"No LLM configuration found for role '{role}'. "
            "Set SOVEREIGN_LLM_PROVIDER[_ROLE] and SOVEREIGN_LLM_MODEL[_ROLE], "
            "or leave unset to rely on stub logic."
        )

    provider = cfg.provider
    model = cfg.model

    if provider == "openai":
        return OpenAIChatLLM(model=model)
    if provider in {"anthropic", "claude"}:
        return AnthropicChatLLM(model=model)

    # Placeholders for future providers; they raise clear errors instead of failing silently.
    if provider in {"deepseek", "deepseek-ai"}:
        raise RuntimeError(
            "DeepSeek provider is not wired yet in code. "
            "Add a DeepSeekChatLLM implementation in sovereign_os.llm.providers "
            "or use provider 'openai' for now."
        )
    if provider in {"ollama"}:
        raise RuntimeError(
            "Ollama provider is not wired yet. "
            "You can add an OllamaChatLLM implementation that calls the local HTTP API, "
            "or change SOVEREIGN_LLM_PROVIDER to 'openai'."
        )

    raise ValueError(f"Unknown LLM provider '{provider}'. Supported today: 'openai'.")

