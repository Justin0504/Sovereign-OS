"""
image_gen connector — generate a visual from a text prompt, for the design
category (mockups, logos, illustrations) where a rendered image is the deliverable
(unlike the Figma connector, which only reads existing designs).

Provider-agnostic: a `generator(prompt, size) -> url/b64` callable can be injected
(used by tests and to swap providers). The default real path uses the OpenAI
Images API when IMAGE_GEN_API_KEY (or OPENAI_API_KEY) is set. Without a key /
generator it is DRY-RUN (no spend, returns the intended request).
"""

from __future__ import annotations

import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("IMAGE_GEN_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _openai_generator(prompt: str, size: str) -> str:
    """Real generation via the OpenAI Images API. Returns an image URL."""
    from openai import OpenAI  # type: ignore[import]

    key = os.getenv("IMAGE_GEN_API_KEY") or os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=key)
    model = os.getenv("IMAGE_GEN_MODEL", "gpt-image-1")
    resp = client.images.generate(model=model, prompt=prompt[:4000], size=size, n=1)
    item = resp.data[0]
    return getattr(item, "url", None) or getattr(item, "b64_json", "") or ""


def generate_image(
    prompt: str,
    *,
    size: str = "1024x1024",
    generator: Callable[[str, str], str] | None = None,
) -> dict:
    """
    Generate an image from `prompt`. Returns {"prompt","size","url"|"b64","dry_run"}.
    DRY-RUN (no spend) unless a `generator` is injected or an API key is configured.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "empty prompt"}

    gen = generator
    if gen is None:
        if not is_configured():
            logger.info("CONNECTOR image_gen DRY-RUN: would generate %r (set IMAGE_GEN_API_KEY).", prompt[:60])
            return {"dry_run": True, "prompt": prompt, "size": size}
        gen = _openai_generator

    try:
        out = gen(prompt, size)
        key = "url" if str(out).startswith("http") else "b64"
        return {"prompt": prompt, "size": size, key: out}
    except Exception as e:
        logger.warning("CONNECTOR image_gen failed: %s", e)
        return {"error": str(e), "prompt": prompt}
