---
Task ID: 1
Agent: main
Task: Полная переработка логики работы с моделями и исправление WS стабильности

Work Log:
- Прочитал текущее состояние core/agent.py (2264 строки), run.py (1144 строки), index.html
- Проанализировал корень проблемы WS code 1006: сервер отправлял JSON text ping вместо WebSocket PING control frames
- Прокси (Render/nginx) распознаёт только WebSocket PING frames (RFC 6455) как keepalive
- Исправил WS keepalive в run.py: заменил `safe_ws_send({"type":"ping"})` на `websocket.send_ping()`
- Переработал _AgentLoopState: добавил трекинг прогресса, классификацию tool calls, детекцию зацикливания
- Увеличил лимит итераций с 3 до 8 (было слишком мало для реальных задач)
- Добавил детекцию отсутствия прогресса (3 итерации без записи/команды → стоп)
- Добавил защиту "все дубликаты" (если все tool calls были дубликатами → немедленный стоп)
- Увеличил лимит tool calls с 15 до 25, контекст с 80K до 120K
- Удалил мёртвый код: SYSTEM_PROMPT_INJECTION_TEMPLATE (~50 строк), stream_with_prompt_injection (~200 строк)
- Итого удалено ~300 строк мёртвого кода, добавлено ~50 строк новых защит

Stage Summary:
- core/agent.py: 2264 → 1969 строк (чистый синтаксис)
- run.py: 1144 → 1065 строк
- WS keepalive: JSON text → WebSocket PING frame (критический фикс для code 1006)
- Agent loop: увеличены лимиты, добавлена детекция прогресса
