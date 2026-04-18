import litellm
import json
from core.memory import CONFIG, save_message, get_history, get_project
from core.keys_manager import keys_manager
from core.context_compressor import ContextCompressor

litellm.suppress_debug_info = True

SYSTEM_PROMPT_TEMPLATE = """Ты Fosved Coder — AI-ассистент для разработки.
Ты помогаешь писать код, анализировать проекты и решать задачи.

{repo_map}

{ideas_context}

{project_context}

{compressed_context}

Правила:
- Отвечай на том языке, на котором задан вопрос
- Для кода используй Markdown code blocks с указанием языка
- Если задача требует выполнения команд — укажи какие команды выполнить
- Будь кратким и по делу"""


async def stream_llm_response(prompt: str, history: list, websocket, model: str = None, system_prompt: str = None):
    """Stream AI response chunk by chunk to WebSocket. Uses keys_manager for API config."""
    if model is None:
        model = CONFIG["llm"].get("default_model")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="", project_context="", compressed_context="")

    # Resolve model config from keys_manager if model_id is provided
    api_key = CONFIG["llm"].get("api_key", "")
    api_base = CONFIG["llm"].get("api_base", "")

    # Safety: never use placeholder API keys from config
    if "YOUR_" in api_key.upper() or api_key == "YOUR_OPENROUTER_API_KEY_HERE":
        api_key = ""

    model_config = keys_manager.get_model_config(model)
    if model_config:
        model = model_config["model"]  # full litellm model name
        api_key = model_config["api_key"]
        api_base = model_config.get("api_base", "")

    # Debug logging
    has_key = bool(api_key)
    print(f"  [agent] stream_llm_response: model={model}, has_key={has_key}, api_base={api_base}")

    if not has_key:
        await websocket.send_json({"type": "error", "content": f"Нет API ключа для модели '{model}'. Добавьте ключ в настройках (ключ ⚙) или через Environment Variables на сервере."})
        return None

    try:
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": prompt}]

        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": CONFIG["llm"].get("temperature", 0.2),
            "max_tokens": CONFIG["llm"].get("max_tokens", 4096),
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        response = await litellm.acompletion(**kwargs)

        full_response = ""
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                full_response += delta
                await websocket.send_json({"type": "chunk", "content": delta})

        await websocket.send_json({"type": "done"})
        return full_response

    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg:
            error_msg = "Ошибка 401: Неверный API ключ. Проверьте настройки ключей!"
        elif "429" in error_msg:
            error_msg = "Ошибка 429: Лимит запросов исчерпан или нет средств."
        elif "500" in error_msg:
            error_msg = "Ошибка 500: Сервер ИИ временно недоступен. Попробуйте позже."
        elif "timeout" in error_msg.lower():
            error_msg = "Таймаут: Модель слишком долго отвечает. Попробуйте другую."
        else:
            error_msg = f"Ошибка ИИ: {error_msg}"
        await websocket.send_json({"type": "error", "content": error_msg})
        return None


def _get_priority_models(project: dict) -> list[str]:
    """Extract up to 3 priority model IDs from project's selected_models."""
    if not project or not project.get("selected_models"):
        return []
    try:
        models = json.loads(project["selected_models"])
        if isinstance(models, list):
            return models[:3]  # max 3 priority models
    except (json.JSONDecodeError, TypeError):
        pass
    return []


async def _route_with_priority(prompt: str, priority_models: list[str]) -> str | None:
    """
    Use HybridRouter to decide: should we route to a cheaper model
    from the priority list, or use the first (primary) model?
    Returns the chosen model_id or None.
    """
    from core.router import HybridRouter
    router = HybridRouter()

    prompt_lower = prompt.lower()

    # Simple keywords → use cheapest model from priority list (last one)
    simple_keywords = [
        "fix typo", "формат", "xml", "json", "тест", "docstring",
        "комментарий", "простой", "trivial", "rename", "lint",
        "semicolon", "indent", "whitespace", "небольшой", "опечатк"
    ]
    for kw in simple_keywords:
        if kw in prompt_lower:
            if len(priority_models) > 1:
                # Pick the cheapest — if there's a free model, use it; otherwise use last
                all_models = keys_manager.get_all_models()
                free_in_priority = [m for m in priority_models if any(
                    am["id"] == m and am["type"] == "free" for am in all_models
                )]
                if free_in_priority:
                    return free_in_priority[0]
                return priority_models[-1]  # last = least expensive
            return priority_models[0]

    # Complex keywords → always use primary (first) model
    complex_keywords = [
        "архитектур", "refactor", "redesign", "систем", "framework",
        "engine", "парсером", "compiler", "параллельн", "микросервис",
        "database schema", "модель данных", "api дизайн", "security",
        "аутентификац", "интеграц", "многопоточ", "async"
    ]
    for kw in complex_keywords:
        if kw in prompt_lower:
            return priority_models[0]

    # Default: use primary model
    return priority_models[0]


async def handle_chat_message(prompt: str, project_id, repo_map: str | None, websocket, model_id: str = None):
    """Main entry point: get history, add repo_map context, stream response with fallback."""
    history = await get_history(project_id)

    # Project context (description + base_prompt)
    project_context_text = ""
    if project_id:
        project = await get_project(project_id)
        if project:
            if project.get("description"):
                project_context_text += f"О ПРОЕКТЕ: {project['description']}\n"
            if project.get("base_prompt"):
                project_context_text += f"ИНСТРУКЦИИ: {project['base_prompt']}\n"

    # Auto-compression check
    compressed_context_text = ""
    if project_id:
        try:
            compressor = ContextCompressor()
            if await compressor.should_compress(project_id):
                compress_result = await compressor.compress(project_id)
                if compress_result.get("compressed"):
                    compressed_context_text = f"[Контекст сжат: {compress_result['messages_removed']} сообщений удалено, {compress_result['messages_kept']} оставлено]"
                    await websocket.send_json({
                        "type": "info",
                        "content": f"Автосжатие: {compress_result['messages_removed']} старых сообщений архивировано"
                    })
        except Exception:
            pass

    repo_map_text = ""
    if repo_map:
        repo_map_text = f"СТРУКТУРА ПРОЕКТА (Repo Map):\n{repo_map}"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_map=repo_map_text,
        ideas_context="",
        project_context=project_context_text,
        compressed_context=compressed_context_text,
    )

    await save_message(project_id, "user", prompt)

    # If model_id is explicitly passed from the UI, use it directly (highest priority)
    if model_id:
        # Verify model has a valid config
        model_config = keys_manager.get_model_config(model_id)
        if model_config and model_config.get("api_key"):
            ai_response = await stream_llm_response(
                prompt, history, websocket,
                model=model_id, system_prompt=system_prompt
            )
            if ai_response:
                await save_message(project_id, "ai", ai_response)
                return
        # If model has no key, try priority models as fallback
        elif model_config and not model_config.get("api_key"):
            await websocket.send_json({
                "type": "info",
                "content": f"Нет API ключа для модели. Переключаюсь на приоритетные..."
            })

    # Get priority models for this project
    project = await get_project(project_id) if project_id else None
    priority_models = _get_priority_models(project)

    if priority_models:
        # Smart routing: pick the best model from priority list
        chosen_model = await _route_with_priority(prompt, priority_models)
        if not chosen_model:
            chosen_model = priority_models[0]

        # Stream with fallback: try each priority model in order
        max_retries = len(priority_models)  # one attempt per model
        ai_response = None
        tried_models = []

        for attempt in range(max_retries):
            model_to_try = priority_models[attempt]
            tried_models.append(model_to_try)

            # On first attempt, use the router-chosen model
            if attempt == 0:
                model_to_try = chosen_model
                if model_to_try not in tried_models:
                    tried_models.insert(0, model_to_try)

            if attempt > 0:
                model_name = model_to_try
                all_models = keys_manager.get_all_models()
                m_info = next((m for m in all_models if m["id"] == model_to_try), None)
                if m_info:
                    model_name = m_info["name"]
                await websocket.send_json({
                    "type": "info",
                    "content": f"Переключаюсь на {model_name}..."
                })

            ai_response = await stream_llm_response(
                prompt, history, websocket,
                model=model_to_try, system_prompt=system_prompt
            )
            if ai_response is not None:
                break
        else:
            ai_response = None

    else:
        # No priority models set — use single model from UI or config
        model = model_id or None
        if project and project.get("selected_models"):
            try:
                models = json.loads(project["selected_models"])
                if models:
                    model = models[0]
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback retries
        max_retries = CONFIG["system"].get("max_iterations", 3)
        ai_response = None
        for attempt in range(max_retries):
            ai_response = await stream_llm_response(
                prompt, history, websocket,
                model=model, system_prompt=system_prompt
            )
            if ai_response is not None:
                break
            if attempt < max_retries - 1:
                await websocket.send_json({
                    "type": "info",
                    "content": f"Попытка {attempt + 2}/{max_retries}..."
                })

    if ai_response:
        await save_message(project_id, "ai", ai_response)
