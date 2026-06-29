"""Per-category model-tier selection: worker model matches the category risk tier."""

from sovereign_os.llm.providers import model_override_for_skill


def test_model_override_by_category_risk(monkeypatch):
    monkeypatch.setenv("SOVEREIGN_MODEL_HIGH", "claude-opus-4")
    monkeypatch.setenv("SOVEREIGN_MODEL_MEDIUM", "claude-sonnet-4")
    monkeypatch.setenv("SOVEREIGN_MODEL_LOW", "claude-haiku-4")
    # automation=high, coding=medium, writing=low
    assert model_override_for_skill("spec_writer") == "claude-opus-4"
    assert model_override_for_skill("code_assistant") == "claude-sonnet-4"
    assert model_override_for_skill("write_article") == "claude-haiku-4"
    # different categories -> different models
    assert model_override_for_skill("spec_writer") != model_override_for_skill("write_article")


def test_no_env_means_no_override(monkeypatch):
    for k in ("SOVEREIGN_MODEL_HIGH", "SOVEREIGN_MODEL_MEDIUM", "SOVEREIGN_MODEL_LOW"):
        monkeypatch.delenv(k, raising=False)
    assert model_override_for_skill("code_assistant") is None  # backward compatible
