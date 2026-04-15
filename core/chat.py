"""AI Chat — multi-provider routing via litellm with streaming."""
import json

PROVIDER_PREFIX = {
    "openai":     "openai/",
    "claude":     "anthropic/",
    "openrouter": "openrouter/",
    "grok":       "xai/",
    "google":     "gemini/",
    "deepseek":   "deepseek/",
    "minimax":    "",
    "custom":     "",
}

def _get_all_keys():
    from core.keys_manager import keys_manager
    return keys_manager.list_keys()

def _get_key(provider_id):
    from core.keys_manager import keys_manager
    return keys_manager.get_key(provider_id)

def find_key_for_model(model_id):
    all_keys = _get_all_keys()
    for k in all_keys:
        if not k.get("is_active", True):
            continue
        if model_id in (k.get("models") or []):
            prefix = PROVIDER_PREFIX.get(k["provider_id"], "")
            return k["provider_id"], k["api_key"], prefix + model_id
    # Guess by model name
    if "gpt" in model_id or "o1" in model_id or "o3" in model_id:
        key = _get_key("openai")
        if key: return "openai", key, "openai/" + model_id
    if "claude" in model_id:
        key = _get_key("claude")
        if key: return "claude", key, "anthropic/" + model_id
    if "gemini" in model_id:
        key = _get_key("google")
        if key: return "google", key, "gemini/" + model_id
    if "deepseek" in model_id:
        key = _get_key("deepseek")
        if key: return "deepseek", key, "deepseek/" + model_id
    if "/" in model_id:
        key = _get_key("openrouter")
        if key: return "openrouter", key, "openrouter/" + model_id
    # Fallback: use any active key
    for k in all_keys:
        if k.get("is_active", True) and k.get("api_key"):
            prefix = PROVIDER_PREFIX.get(k["provider_id"], "")
            return k["provider_id"], k["api_key"], prefix + model_id
    return None, None, model_id

async def stream_chat(model_id, messages, on_token, on_done, on_error, stop_event=None):
    import asyncio
    provider_id, api_key, litellm_model = find_key_for_model(model_id)
    if not api_key:
        await on_error("No API key for: " + model_id + ". Add key in Manage API Keys.")
        return
    try:
        import litellm
        litellm.suppress_debug_info = True
        response = await litellm.acompletion(
            model=litellm_model,
            messages=messages,
            api_key=api_key,
            stream=True,
            timeout=120,
        )
        async for chunk in response:
            if stop_event and stop_event.is_set():
                break
            content = ""
            try:
                content = chunk.choices[0].delta.content or ""
            except Exception:
                pass
            if content:
                await on_token(content)
        await on_done()
    except ImportError:
        await on_error("litellm not installed. Run: pip install litellm")
    except Exception as e:
        err = str(e)
        if "auth" in err.lower() or "key" in err.lower() or "401" in err:
            await on_error("Invalid API key for " + (provider_id or model_id) + ". Check key in Manage API Keys.")
        elif "rate" in err.lower() or "429" in err:
            await on_error("Rate limit exceeded for " + (provider_id or model_id) + ". Wait and try again.")
        elif "model" in err.lower() or "404" in err:
            await on_error("Model not found: " + model_id + ". Check model name.")
        else:
            await on_error("AI Error: " + err)