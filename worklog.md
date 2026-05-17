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
