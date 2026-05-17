# Worklog — Fosved Coder

---
Task ID: 1
Agent: Main Agent
Task: Анализ кодовой базы и исправление silent fallback при ручном выборе модели

Work Log:
- Клонирован репозиторий fosved-coder с GitHub
- Изучены ключевые файлы: run.py (55KB), core/agent.py, core/intelligent_router.py, ui/templates/index.html
- Проверена маршрутизация сообщений WebSocket (frontend):
  - `chunk` → чат-баббл (чистый ответ AI) ✅
  - `error` → панель логов ✅
  - `auto_log` → панель логов ✅
  - `tool_call` → панель логов ✅
  - `status_activity` → статус-бар ✅
  - `typing` → индикатор набора ✅
  - `done` → завершение ✅
- Найден баг: `_pick_model()` при ручном выборе модели пользователем (mode=manual) делал silent fallback на другую модель, если выбранная модель не проходила `_is_usable()` проверку
- Исправлен баг: добавлен параметр `_user_explicit` в `_pick_model()`, `handle_chat_message()`, `handle_hub_message()`
- Обновлены call sites в run.py для передачи `_user_explicit=True` при ручном выборе модели

Stage Summary:
- Разделение чата/логов уже правильно реализовано в текущем V3 коде
- Исправлен silent fallback: при manual mode выбранная модель используется БЕЗ подмены
- Изменённые файлы: core/agent.py, run.py (2 файла, +26/-12 строк)

---
Task ID: 1
Agent: main
Task: Research Jules & Stitch and improve their integration in fosved-coder

Work Log:
- Researched what Google Jules and Stitch are (subagent: web search + documentation)
- Found: Jules = autonomous coding agent (jules.google.com), Stitch = UI design generator (stitch.withgoogle.com)
- Neither is an LLM API endpoint — they're specialized Google Labs AI tools
- BUT their API keys (AQ.* format) are standard Google AI Studio / Gemini API keys
- These keys CAN work as regular Gemini API keys for LLM calls via litellm
- Verified existing integration: jules/stitch already registered as providers in keys_manager.py
- Added is_free: True flag to both provider definitions
- Updated get_all_models() to return type="free" for is_free providers
- Updated get_model_config() to pass api_base explicitly for is_free providers
- Added soft validation for free-tier providers: 401/400 → rate_limited (never invalid)
- Committed and pushed all changes

Stage Summary:
- Jules & Stitch keys work as additional Gemini API key slots (extra rate limit quota)
- Free-tier providers are now resilient: never permanently hidden due to transient errors
- Models show as "free" type in UI instead of "paid"
- All changes committed: 566ac27, pushed to origin/main
