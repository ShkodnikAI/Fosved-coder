import litellm
from core.memory import CONFIG

# Отключаем лишние логи LiteLLM для чистоты терминала
litellm.suppress_debug_info = True

async def stream_llm_response(prompt: str, history: list, websocket):
    """
    Отправляет промпт в ИИ и транслирует ответ по частям (chunks) в WebSocket
    """
    try:
        # Формируем массив сообщений для ИИ (история + новый запрос)
        messages = history + [{"role": "user", "content": prompt}]
        
        # Вызов к ИИ через LiteLLM (асинхронно, со стримингом)
        response = await litellm.acompletion(
            model=CONFIG["llm"]["default_model"],
            messages=messages,
            api_base=CONFIG["llm"]["api_base"],
            api_key=CONFIG["llm"]["api_key"],
            stream=True,
            temperature=CONFIG["llm"]["temperature"]
        )
        
        # Читаем ответ по кускам и сразу шлем в браузер
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta is not None:
                await websocket.send_json({"type": "chunk", "content": delta})
                
        # Сигнализируем концовку
        await websocket.send_json({"type": "done"})
        
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg: error_msg = "Ошибка 401: Неверный API ключ. Проверьте config.yaml!"
        elif "429" in error_msg: error_msg = "Ошибка 429: Лимит запросов исчерпан или нет средств."
        await websocket.send_json({"type": "error", "content": error_msg})