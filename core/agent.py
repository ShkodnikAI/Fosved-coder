import litellm
from core.memory import CONFIG, save_message, get_history
from core.keys_manager import keys_manager

litellm.suppress_debug_info = True

SYSTEM_PROMPT_TEMPLATE = """Ты Fosved Coder — AI-ассистент для разработки.
Ты помогаешь писать код, анализировать проекты и решать задачи.

{repo_map}

{ideas_context}

Правила:
- Отвечай на том языке, на котором задан вопрос
- Для кода используй Markdown code blocks с указанием языка
- Если задача требует выполнения команд — укажи какие команды выполнить
- Будь кратким и по делу"""


async def stream_llm_response(prompt: str, history: list, websocket, model: str = None, system_prompt: str = None):
    """Stream AI response chunk by chunk to WebSocket. Uses keys_manager for API config."""
    if model is None:
        # Try first selected model from project or fallback
        model = CONFIG["llm"].get("default_model")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="")

    # Resolve model config from keys_manager if model_id is provided
    api_key = CONFIG["llm"].get("api_key", "")
    api_base = CONFIG["llm"].get("api_base", "")

    model_config = keys_manager.get_model_config(model)
    if model_config:
        model = model_config["model"]  # full litellm model name
        api_key = model_config["api_key"]
        api_base = model_config["api_base"]

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


async def handle_chat_message(prompt: str, project_id, repo_map: str | None, websocket):
    """Main entry point: get history, add repo_map context, stream response, save to memory."""
    history = await get_history(project_id)

    repo_map_text = ""
    if repo_map:
        repo_map_text = f"СТРУКТУРА ПРОЕКТА (Repo Map):\n{repo_map}"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_map=repo_map_text,
        ideas_context=""
    )

    await save_message(project_id, "user", prompt)

    # Determine which model to use
    model = None
    if project_id:
        from core.memory import get_project
        project = await get_project(project_id)
        if project and project.get("selected_models"):
            import json
            try:
                models = json.loads(project["selected_models"])
                if models:
                    model = models[0]  # Use first selected model
            except (json.JSONDecodeError, TypeError):
                pass

    # Cyclic agent: up to 3 retries on errors
    max_retries = CONFIG["system"].get("max_iterations", 3)
    ai_response = None
    for attempt in range(max_retries):
        ai_response = await stream_llm_response(prompt, history, websocket, model=model, system_prompt=system_prompt)
        if ai_response is not None:
            break
        if attempt < max_retries - 1:
            await websocket.send_json({
                "type": "info",
                "content": f"Попытка {attempt + 2}/{max_retries}..."
            })

    if ai_response:
        await save_message(project_id, "ai", ai_response)
