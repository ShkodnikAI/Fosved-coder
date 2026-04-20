import litellm
import json
from core.memory import CONFIG, save_message, get_history, get_project
from core.keys_manager import keys_manager
from core.context_compressor import ContextCompressor

litellm.suppress_debug_info = True

SYSTEM_PROMPT_TEMPLATE = """Ты Fosved Coder — AI-ассистент для разработки проекта.
Ты помогаешь писать код, анализировать проект и решать задачи.

{project_context}

{repo_map}

{ideas_context}

{compressed_context}

Правила:
- Ты работаешь над конкретным проектом. Если указано название проекта и описание — используй эту информацию в ответах.
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
    else:
        # Fallback: check if this is a free model (model_id without prefix)
        print(f"  [agent] get_model_config вернул None для '{model}' — пробуем fallback")
        for fm in keys_manager.FREE_MODELS:
            if fm["id"] == model:
                or_config = keys_manager.providers.get("openrouter", {})
                api_key = or_config.get("api_key", "")
                model = f"openrouter/{fm['model']}"
                api_base = or_config.get("api_base", "https://openrouter.ai/api/v1")
                print(f"  [agent] fallback: найдена free модель, model={model}, has_key={bool(api_key)}")
                break

    # Debug logging
    has_key = bool(api_key)
    print(f"  [agent] stream_llm_response: model={model}, has_key={has_key}, api_base={api_base}")

    if not has_key:
        await websocket.send_json({"type": "error", "content": f"Нет API ключа для модели '{model}'. Добавьте ключ в настройках (ключ ⚙) или через Environment Variables на сервере."})
        return None

    try:
        # Map internal roles to API-compatible roles
        # Anthropic requires "assistant" (not "ai"), OpenAI accepts both
        api_messages = []
        role_map = {"ai": "assistant", "system": "system", "user": "user"}
        for msg in history:
            mapped_role = role_map.get(msg.get("role", ""), msg.get("role", "user"))
            api_messages.append({"role": mapped_role, "content": msg.get("content", "")})
        messages = [{"role": "system", "content": system_prompt}] + api_messages + [{"role": "user", "content": prompt}]

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
    """Main entry point: get history, add repo_map context, stream response with smart fallback."""
    history = await get_history(project_id)

    # Project context (name, description, template, base_prompt)
    project_context_text = ""
    if project_id:
        project = await get_project(project_id)
        if project:
            project_context_text += f"ПРОЕКТ: {project.get('name', 'Без названия')}\n"
            if project.get("description"):
                project_context_text += f"ОПИСАНИЕ: {project['description']}\n"
            if project.get("template"):
                project_context_text += f"ШАБЛОН: {project['template']}\n"
            if project.get("base_prompt"):
                project_context_text += f"ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ: {project['base_prompt']}\n"
            if project.get("path"):
                project_context_text += f"ПУТЬ К ПРОЕКТУ: {project['path']}\n"

    # Auto-compression: LLM-based with regex fallback + DB cleanup
    compressed_context_text = ""
    compressor = ContextCompressor()
    if project_id and compressor.should_compress(history):
        try:
            # Find a model for LLM compression (prefer free)
            comp_model = ContextCompressor.get_compression_model_config()
            await websocket.send_json({
                "type": "info",
                "content": "Автосжатие контекста..."
            })
            # compress_and_cleanup: LLM summarize + delete old from DB
            compressed_summary, remaining, was_llm = await compressor.compress_and_cleanup(
                history, project_id, model_config=comp_model
            )
            if compressed_summary:
                compressed_context_text = compressed_summary
                method = "LLM" if was_llm else "regex"
                removed = len(history) - len(remaining)
                await websocket.send_json({
                    "type": "info",
                    "content": f"Автосжатие ({method}): {removed} сообщений сжато, {len(remaining)} активны"
                })
                # Use compressed history for LLM context
                history = remaining
        except Exception as e:
            print(f"  [agent] compression error: {e}")

    repo_map_text = ""
    if repo_map:
        repo_map_text = f"СТРУКТУРА ПРОЕКТА (Repo Map):\n{repo_map}"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_map=repo_map_text,
        ideas_context="",
        project_context=project_context_text,
        compressed_context="",
    )

    # Inject compressed context into system prompt
    if compressed_context_text:
        system_prompt = compressor.build_compressed_system_prompt(system_prompt, compressed_context_text)

    await save_message(project_id, "user", prompt)

    # Build list of models to try, in priority order
    models_to_try = []

    # 1. Explicit model from UI (highest priority)
    if model_id:
        models_to_try.append(model_id)

    # 2. Priority models from project settings
    project = await get_project(project_id) if project_id else None
    priority_models = _get_priority_models(project)
    for pm in priority_models:
        if pm not in models_to_try:
            models_to_try.append(pm)

    # 3. Any other valid/available models as fallback
    all_models = keys_manager.get_all_models()
    for m in all_models:
        if m["id"] not in models_to_try and m.get("status") in ("valid", "rate_limited", "available"):
            if m.get("api_key") or m["type"] in ("local", "free", "custom"):
                models_to_try.append(m["id"])

    if not models_to_try:
        await websocket.send_json({"type": "error", "content": "Нет доступных моделей. Добавьте API ключ в настройках (🔑)."})
        return

    # Try each model with fallback
    ai_response = None
    tried_count = 0
    last_error = ""

    for i, model_to_try in enumerate(models_to_try):
        # Verify model has a config with key
        model_config = keys_manager.get_model_config(model_to_try)
        if not model_config:
            print(f"  [agent] fallback #{i}: {model_to_try} — нет конфига, пропускаем")
            continue
        # Skip models without API key (except local models)
        has_key = bool(model_config.get("api_key"))
        is_local = model_to_try in [m["id"] for m in all_models if m["type"] == "local"]
        if not has_key and not is_local:
            print(f"  [agent] fallback #{i}: {model_to_try} — нет API ключа, пропускаем")
            continue

        tried_count += 1
        print(f"  [agent] fallback #{i}: пробую модель {model_to_try}")

        # Send typing indicator
        await websocket.send_json({"type": "typing", "model": model_to_try})

        # Notify user about fallback
        if i > 0:
            m_info = next((m for m in all_models if m["id"] == model_to_try), None)
            model_name = m_info["name"] if m_info else model_to_try
            await websocket.send_json({
                "type": "info",
                "content": f"Переключаюсь на {model_name} (попытка {i+1})..."
            })

        ai_response = await stream_llm_response(
            prompt, history, websocket,
            model=model_to_try, system_prompt=system_prompt
        )
        if ai_response is not None:
            print(f"  [agent] fallback #{i}: {model_to_try} — успешно")
            break
        else:
            print(f"  [agent] fallback #{i}: {model_to_try} — не удалось")

    if ai_response:
        await save_message(project_id, "ai", ai_response)
    elif tried_count == 0:
        await websocket.send_json({"type": "error", "content": "Нет модели с API ключом. Добавьте ключ через кнопку \U0001f511 или через Environment Variables на сервере."})
    else:
        # Все модели не ответили — информируем пользователя
        await websocket.send_json({"type": "error", "content": f"Все {tried_count} моделей не ответили. Проверьте API ключи и подключение к интернету."})
