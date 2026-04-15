import litellm
from core.memory import CONFIG, save_message, get_history

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
    """Stream AI response chunk by chunk to WebSocket"""
    if model is None:
        model = CONFIG["llm"]["default_model"]
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="")

    try:
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": prompt}]

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            api_base=CONFIG["llm"]["api_base"],
            api_key=CONFIG["llm"]["api_key"],
            stream=True,
            temperature=CONFIG["llm"]["temperature"],
            max_tokens=CONFIG["llm"].get("max_tokens", 4096)
        )

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
            error_msg = "Ошибка 401: Неверный API ключ. Проверьте config.yaml!"
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
    # Get chat history from database
    history = await get_history(project_id)

    # Build system prompt with repo map
    repo_map_text = ""
    if repo_map:
        repo_map_text = f"СТРУКТУРА ПРОЕКТА (Repo Map):\n{repo_map}"

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_map=repo_map_text,
        ideas_context=""
    )

    # Save user message to chat history
    await save_message(project_id, "user", prompt)

    # Stream AI response with cyclic auto-retry on errors (up to 3 attempts)
    max_retries = 3
    ai_response = None
    for attempt in range(max_retries):
        ai_response = await stream_llm_response(prompt, history, websocket, system_prompt=system_prompt)
        if ai_response is not None:
            break
        # If we got an error, notify the client and retry
        if attempt < max_retries - 1:
            await websocket.send_json({
                "type": "info",
                "content": f"Попытка {attempt + 2}/{max_retries}..."
            })

    # Save AI response to memory
    if ai_response:
        await save_message(project_id, "ai", ai_response)
