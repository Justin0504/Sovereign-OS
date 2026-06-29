"""
web_fetch connector — fetch a URL and return readable text. Gives research/data
workers real, current information instead of training-data recall.

Size- and time-capped; HTML is reduced to readable text (scripts/styles dropped,
tags stripped, entities unescaped). `opener` is injectable for tests (no real
network in the suite).
"""

from __future__ import annotations

import html
import logging
import re

logger = logging.getLogger(__name__)

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES = re.compile(r"\n\s*\n\s*\n+")


def html_to_text(raw: str) -> str:
    """Reduce HTML to readable text: drop script/style, strip tags, unescape, tidy whitespace."""
    s = _SCRIPT_STYLE.sub(" ", raw)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = _BLANKLINES.sub("\n\n", s)
    return s.strip()


def web_fetch(url: str, *, opener=None, max_bytes: int = 200_000, timeout: float = 15.0) -> dict:
    """
    GET `url` and return {"url", "status", "text", "truncated"}. `opener(url, timeout)`
    may be injected (must return an object with .read() and optional .status/.getcode()).
    Only http/https are allowed.
    """
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"url": url, "status": 0, "text": "", "error": "only http/https URLs are allowed"}

    try:
        if opener is not None:
            resp = opener(url, timeout)
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "sovereign-os/web_fetch"})
            resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - scheme checked above
        raw = resp.read(max_bytes + 1)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        status = getattr(resp, "status", None) or (resp.getcode() if hasattr(resp, "getcode") else 200)
        text = html_to_text(raw) if "<" in raw and ">" in raw else raw.strip()
        return {"url": url, "status": int(status or 200), "text": text, "truncated": truncated}
    except Exception as e:
        logger.warning("CONNECTOR web_fetch failed for %s: %s", url, e)
        return {"url": url, "status": 0, "text": "", "error": str(e)}
