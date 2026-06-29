"""Anthropic system-prompt caching shaping (no SDK call)."""

from sovereign_os.llm.providers import _anthropic_system, _CACHE_MIN_CHARS


def test_small_system_stays_plain_string():
    assert _anthropic_system("short prompt") == "short prompt"
    assert _anthropic_system("") is None
    assert _anthropic_system(None) is None


def test_large_system_is_cache_eligible():
    big = "x" * (_CACHE_MIN_CHARS + 10)
    out = _anthropic_system(big)
    assert isinstance(out, list) and len(out) == 1
    block = out[0]
    assert block["type"] == "text" and block["text"] == big
    assert block["cache_control"] == {"type": "ephemeral"}
