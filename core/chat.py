"""
[DEPRECATED/LEGACY] — This module is no longer actively used.
It references `keys_mgr.get_key(provider)` which doesn't exist in the current KeysManager class.
See core/agent.py for the current chat/streaming implementation.
"""
import json
import os
import asyncio
import litellm
from datetime import datetime
from core.keys_manager import KeysManager

# Silence litellm info logs
litellm.suppress_debug_info = True

keys_mgr = KeysManager()

# FREE_OPENROUTER_MODELS — удалены (OpenRouter удалён из проекта)
FREE_OPENROUTER_MODELS = []

FREE_MODELS = [
    {"id": "gpt-3.5-turbo", "name": "GPT-3.5 Turbo", "provider": "openai"},
    {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat", "provider": "deepseek"},
]

async def find_key_for_model(model_id, explicit_provider=None):
    """Find API key for a given model. Returns (key, provider)."""
    if explicit_provider and explicit_provider != "free":
        key = keys_mgr.get_key(explicit_provider)
        if key:
            return key, explicit_provider

    # Check by model name prefix
    model_lower = model_id.lower()
    provider_map = {
        "claude": "claude",
        "gpt": "openai",
        "openai": "openai",
        "grok": "grok",
        "google": "google",
        "gemini": "google",
        "deepseek": "deepseek",
        "minimax": "minimax",
        "abacus": "abacus",
        "zai": "zai",
    }
    for prefix, provider in provider_map.items():
        if prefix in model_lower:
            key = keys_mgr.get_key(provider)
            if key:
                return key, provider

    # Try any available key
    for provider in ["claude", "openai", "google", "deepseek", "grok", "minimax", "abacus", "zai"]:
        key = keys_mgr.get_key(provider)
        if key:
            return key, provider

    return None, None


async def stream_chat(messages, model_id=None, project_keys=None):
    """Stream chat via litellm. Yields (token, is_complete, error)."""
    if not model_id:
        model_id = "deepseek/deepseek-chat"

    api_key, provider = await find_key_for_model(model_id)

    if not api_key:
        yield "", True, "Нет API ключа для выбранной модели. Добавьте ключ в настройках."
        return

    # Set environment variable for the provider
    env_map = {
        "claude": ("ANTHROPIC_API_KEY", api_key),
        "openai": ("OPENAI_API_KEY", api_key),
        "grok": ("XAI_API_KEY", api_key),
        "google": ("GOOGLE_API_KEY", api_key),
        "deepseek": ("DEEPSEEK_API_KEY", api_key),
        "minimax": ("MINIMAX_API_KEY", api_key),
        "zai": ("ZAI_API_KEY", api_key),
        "abacus": ("ABACUS_API_KEY", api_key),
    }
    env_var, key_val = env_map.get(provider, (None, None))
    if env_var:
        os.environ[env_var] = key_val

    # Adjust model name for litellm
    litellm_model = model_id
    if not model_id.startswith(("claude-", "gpt-", "google/", "deepseek/", "grok/", "minimax/")):
        if provider == "claude":
            litellm_model = f"claude-{model_id}"
        elif provider == "google":
            litellm_model = f"google/{model_id}"
        elif provider == "deepseek":
            litellm_model = f"deepseek/{model_id}"
        elif provider == "grok":
            litellm_model = f"xai/{model_id}"

    try:
        async for chunk in litellm.acompletion(
            model=litellm_model,
            messages=messages,
            stream=True,
            api_key=key_val if not env_var else None,
            temperature=0.7,
            max_tokens=4096
        ):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content, False, None
        yield "", True, None
    except litellm.exceptions.AuthenticationError:
        yield "", True, "Ошибка авторизации. Проверьте API ключ."
    except litellm.exceptions.RateLimitError:
        yield "", True, "Превышен лимит запросов. Подождите немного."
    except litellm.exceptions.NotFoundError:
        yield "", True, f"Модель {model_id} не найдена. Выберите другую."
    except Exception as e:
        yield "", True, f"Ошибка: {str(e)[:200]}"


def get_available_models():
    """Get all available models: user's + free ones."""
    user_models = keys_mgr.get_all_models()
    result = []

    openrouter_models = []
    other_models = [m for m in user_models if m.get("provider") != "openrouter"]

    # Other user models
    for m in other_models:
        result.append({"id": m['id'], "name": m['name'] or m['id'], "provider": m['provider'], "type": "user"})

    # Free models (no more — OpenRouter removed)
    provider_ids = {m["provider"] for m in user_models}
    if "openai" not in provider_ids and "deepseek" not in provider_ids:
        for m in FREE_MODELS:
            result.append({**m, "type": "free"})

    return result
