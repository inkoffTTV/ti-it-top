from config import Config
from services.ai_assistant import _ai_providers


def test_provider_order_contains_openrouter_and_optional_groq():
    cfg = Config(
        ai_api_key="primary",
        ai_base_url="https://openrouter.ai/api/v1/chat/completions",
        ai_model="openrouter/free",
        ai_fallback_model="qwen/qwen3.8-27b:free",
        ai_backup_api_key="backup",
        ai_backup_base_url="https://api.groq.com/openai/v1/chat/completions",
        ai_backup_model="openai/gpt-oss-120b",
    )
    providers = _ai_providers(cfg)
    assert [p["name"] for p in providers] == ["primary", "openrouter-fallback", "groq-backup"]
    assert providers[1]["model"] == "qwen/qwen3.8-27b:free"
    assert providers[2]["model"] == "openai/gpt-oss-120b"


def test_groq_is_optional():
    cfg = Config(
        ai_api_key="primary",
        ai_model="openrouter/free",
        ai_fallback_model="qwen/qwen3.8-27b:free",
        ai_backup_api_key="",
    )
    providers = _ai_providers(cfg)
    assert [p["name"] for p in providers] == ["primary", "openrouter-fallback"]
