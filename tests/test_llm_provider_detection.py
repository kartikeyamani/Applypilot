"""Tests for llm.py::_detect_provider's env-var precedence.

Anthropic is checked first deliberately: unlike Gemini's free tier, an
Anthropic key is always a paid, deliberate opt-in, so if one is present it
should win even if a GEMINI_API_KEY is still sitting in .env from initial
setup.
"""

from applypilot.llm import _detect_provider


def _clear_llm_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_anthropic_key_takes_precedence_over_gemini(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

    base_url, model, api_key = _detect_provider()

    assert "anthropic.com" in base_url
    assert api_key == "anthropic-key"


def test_gemini_used_when_no_anthropic_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    base_url, model, api_key = _detect_provider()

    assert "generativelanguage.googleapis.com" in base_url
    assert model == "gemini-3.5-flash-lite"  # the fixed default, not the retired gemini-2.0-flash


def test_llm_model_env_var_overrides_default(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.6-flash")

    _, model, _ = _detect_provider()

    assert model == "gemini-3.6-flash"


def test_raises_when_no_provider_configured(monkeypatch):
    _clear_llm_env(monkeypatch)

    try:
        _detect_provider()
        assert False, "expected RuntimeError when no provider is configured"
    except RuntimeError:
        pass
