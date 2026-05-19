---
Task ID: 1
Agent: main
Task: Глубокий анализ корневых причин бесконечного цикла fosved-coder

Work Log:
- Прочитал полностью core/agent.py (2421 строк), core/keys_manager.py, run.py
- Трассировал полный поток: WS message → _run_chat_task → handle_chat_message → stream_llm_response → tool calls
- Проанализировал 3 вложенных уровня защиты от бесконечного цикла в stream_llm_response
- Нашёл что stream_llm_response внутренне корректен (max_tool_iterations=2, 7 анти-зацикливание проверок)
- Обнаружил ГЛАВНУЮ причину: asyncio.create_task в run.py создаёт НЕОГРАНИЧЕННОЕ количество параллельных тасок
- Параллельные таски на один проект одновременно вызывают _pick_model → litellm → rate limit
- Каждая rate-limited таска отправляет "Нет доступных моделей" — создавая эффект бесконечного спама
- Обнаружил что пустой ответ модели (0 симв.) сохраняется как AI-сообщение, засоряя контекст
- Обнаружил что rate_limited cooldown 15 минут слишком долгий для Gemini (который восстанавливается за 1-2 мин)

Stage Summary:
- 3 КОРНЕВЫЕ ПРИЧИНЫ найдены и исправлены
- Fix 1: Per-project asyncio.Lock в run.py — mutex для параллельных тасок
- Fix 2: Empty response guard в agent.py — return None вместо пустой строки
- Fix 3: Rate limit cooldown 15мин → 2мин в agent.py
- Commit: 85a8 (fosved-coder submodule)
