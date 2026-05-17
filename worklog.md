---
Task ID: 1
Agent: main
Task: Упрощение моделиной системы fosved-coder V3

Work Log:
- Клонировал репозиторий fosved-coder из GitHub
- Изучил полную архитектуру: agent.py (1984 строки), run.py (1065 строк), index.html (~4200 строк), intelligent_router.py (371 строка), keys_manager.py (1371 строка)
- Удалил из agent.py: `_no_credits_providers` кэш (64-109), `_mark_no_credits()` (80-86), `_is_no_credits_provider()` (89-109), `_last_model_error` дедуп (70-72), `_MODEL_ERROR_COOLDOWN`
- Заменил `_build_models_to_try()` (90 строк fallback-логики) на `_pick_model()` (простая выборка одной модели по приоритету)
- Упростил `handle_chat_message()`: убрал fallback-цикл (60+ строк), pre-validation loop, no_credits проверки, `stream_with_prompt_injection` fallback. Теперь = одна модель, один вызов
- Упростил `handle_hub_message()`: аналогично, убран fallback-цикл (50+ строк)
- Упростил `stream_llm_response()`: убран `_error_info` параметр, убрана дедупликация ошибок (30с cooldown), убран traceback в логах, убран signal no_credits для caller. Ошибка = лог-панель + error message + done
- Обновил комментарий в run.py (318)
- Проверил фронтенд: routing уже правильный (chunk/done → чат, error/auto_log/tool_call → лог-панель)
- Проверил синтаксис: agent.py OK, run.py OK
- Проверил отсутствие ссылок на удалённые функции во всех .py файлах

Stage Summary:
- agent.py: удалено ~200 строк сложной логики, заменено на простую выборку одной модели
- run.py: минимальное изменение (комментарий)
- index.html: не трогалось — routing уже правильный
- Ключевой принцип: одна модель = одна попытка = никаких fallback-циклов, retry-цепочек, кэшей no-credits
