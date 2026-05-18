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

---
Task ID: 2
Agent: main
Task: Implement skill creation module (CRUD API + UI)

Work Log:
- Read worklog and studied project architecture (Python/FastAPI backend + vanilla JS frontend)
- Added 4 CRUD API endpoints to api/endpoints.py after existing skill endpoints (line 1086):
  - POST /skills — create new skill with SKILL.md + _meta.json
  - PUT /skills/{name} — update SKILL.md content (with best-effort frontmatter meta sync)
  - DELETE /skills/{name} — delete skill dir (protected built-in skills)
  - GET /skills/{name}/template — return blank SKILL.md template
- Added skill-modal HTML to index.html (after file-viewer modal, before <script>)
  - Title, slug (auto-generated), description, group dropdown, content textarea
  - Save/Cancel buttons using existing btn-primary/btn-ghost classes
- Modified renderSkills() to add "+ Новый навык" button at top
- Added edit (📝) and delete (🗑) action buttons per skill item (delete hidden for built-in skills)
- Added agents-best-practices entry to SKILL_GROUPS Development section
- Added JavaScript functions: titleToSlug(), onSkillTitleChange(), closeSkillModal(),
  openCreateSkillModal(), openEditSkill(), handleSkillSave(), deleteSkill()
- Added CSS styles: .btn-create-skill, .skill-actions, .skill-action-btn,
  .skill-action-delete, .skill-form (with label/input/textarea/select styles)
- Verified endpoints.py compiles successfully
- All changes use existing patterns: fetch() for API calls, showToast() for notifications,
  showConfirmModal() for delete confirmation, modal-overlay/modal pattern for modal UI

Stage Summary:
- Backend: 4 new CRUD endpoints for skill management (create/update/delete/template)
- Frontend: Skill creation modal + edit/delete buttons on each skill item
- New "agents-best-practices" entry added to Development skill group
- Dark theme consistent with existing VS Code-like UI
- Files modified: api/endpoints.py, ui/templates/index.html, ui/static/style.css
---
Task ID: 1
Agent: Super Z (main)
Task: Интеграция модуля генерации скиллов в раздел скиллов fosved-coder

Work Log:
- Изучен референсный репозиторий agents-best-practices (DenisSergeevitch) — Markdown-only skill specification с YAML frontmatter, progressive disclosure, SKILL.md format
- Изучен текущий раздел скиллов fosved-coder: ~55 skill dirs, CRUD endpoints, SKILL_GROUPS в frontend, _load_skill_context в agent.py, handleSkillRequest для WS
- Спроектирован модуль генерации: endpoint /api/v1/skills/generate (backend), create_skill tool (model function calling), AI generator panel (frontend)
- Реализован бэкенд: POST /api/v1/skills/generate в endpoints.py — AI генерирует skill JSON из описания задачи
- Добавлен tool create_skill в agent.py TOOLS + execute_tool handler — модель может создавать навыки через function calling
- Tool отправляет WS event skill_created с метаданными навыка
- Реализован фронтенд: collapsible "✨ Генерация с AI" панель в модалке создания навыка
- Функции: toggleSkillAIGen(), generateSkillWithAI(), handleSkillCreated()
- WS handler обрабатывает skill_created → добавляет навык в SKILL_GROUPS и показывает toast
- Стили для AI-генератора в style.css
- System prompt обновлён с инструкцией использовать create_skill
- Синтаксическая проверка Python и HTML пройдена
- Коммит cd2c9e9

Stage Summary:
- 4 файла изменено, 397 insertions, 1 deletion
- Новые возможности: (1) Пользователь может генерировать навык через AI прямо в модалке создания, (2) Модель может сама создавать навыки через tool во время работы, (3) Клиент автоматически обновляет список при создании навыка моделью
- Ключевые файлы: core/agent.py (tool), api/endpoints.py (endpoint), ui/templates/index.html (UI), ui/static/style.css (styles)
---
Task ID: 1
Agent: main
Task: Fix infinite loop when all providers are rate_limited (circuit breaker)

Work Log:
- Read core/agent.py: found _is_usable() at line 1081 allows "rate_limited" models
- Read core/context_compressor.py: found get_compression_model_config() has "rate_limited free models" fallback
- Read core/keys_manager.py: found _resolve_model() fallback accepts rate_limited
- Read core/intelligent_router.py: found 6 places allowing rate_limited in model filters
- Fixed _is_usable() to only accept "valid" and "available"
- Fixed _resolve_model() fallback to only check "valid" providers
- Fixed intelligent_router: 6 status checks removing rate_limited
- Fixed probe_candidates list comprehension to exclude rate_limited
- Fixed context_compressor: removed rate_limited last-resort fallback
- Fixed context_compressor._compress_with_llm: added _update_provider_on_error() call on failure
- Fixed error classification: added "quota" and "exceeded your current" to catch OpenAI quota errors
- Fixed _update_provider_on_error: added "速率限制" Chinese rate limit keyword
- Added litellm.num_retries = 0 in both agent.py and keys_manager.py
- Committed as eae4a93

Stage Summary:
- Root cause: all model selection paths (agent, router, compressor) treated rate_limited as usable
- When _update_provider_on_error marked provider as rate_limited, it was STILL picked on next call
- This caused infinite error loops when providers ran out of quota/balance
- Fix: rate_limited models are now excluded from ALL selection paths
- When ALL providers are rate_limited, system shows "Нет доступных моделей" and stops gracefully
