# Fosved Coder v2.0

Локальный автопилот для разработки, объединяющий лучшее из **Aider** (Repo Map), **Claude Code** (автономное выполнение команд) и **Cursor** (UI), с уникальными фичами, которых нет ни у одного инструмента.

![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Возможности

- **Умный Роутер (Money Saver)** — единая точка выбора модели в `_build_models_to_try()`. Учитывает: явный выбор пользователя, приоритетные модели проекта, `fallback_chain` из config.yaml, рекомендацию `intelligent_router` (классификация задачи: рутина → дешёвая модель, сложная → флагман), список проверенных моделей. Статусы провайдеров обновляются фоном (revalidation каждые 5 минут), `rate_limited` имеет TTL 10 минут.
- **Циклический Агент** — tool-calling цикл до 10 итераций (`MAX_TOOL_ITERATIONS`). Защита от залипания: если модель повторяет один и тот же tool_call 3 раза подряд, цикл прерывается и модель просят свернуть работу.
- **Repo Map** — сканирует структуру проекта, извлекает сигнатуры функций/классов и передаёт контекст ИИ. Кешируется с MD5-хешированием.
- **Идеи-Инъектор** — анализирует чужие GitHub-репозитории через API: скачивает ключевые файлы, создаёт ИИ-выжимку архитектуры. Снижает галлюцинации.
- **Мульти-проектность** — переключение между проектами с сохранением отдельной истории чата.
- **Киборг-режим** — блокировка критических команд (rm -rf, DROP TABLE) с звуковым алертом и git checkpoint перед выполнением.
- **REST API** — 120+ эндпоинтов под префиксом `/api/v1` для интеграции с внешними ИИ-агентами (ИИ-Офис, чат, проекты, ключи, модели, скиллы, идеи, статистика).
- **Markdown UI** — рендеринг ответов ИИ через marked.js, подсветка кода highlight.js, тёмная тема VS Code.
- **Скиллы** — 69 готовых пресетов для типовых задач (`skills/`, 18 МБ): работа со скиллом ppt, pdf, docx, xlsx, market-research, UI/UX, charts и др. Авто-выбор скилла моделью по контексту запроса; `SKILL.md` каждого скилла инжектится в системный промпт.

---

## Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Веб-сервер | FastAPI + Uvicorn |
| ИИ-оболочка | LiteLLM (Anthropic, OpenAI, Grok, Cerebras (бесплатно), DeepSeek, Gemini, Qwen, Abacus, Ollama) |
| База данных | SQLite + SQLAlchemy (async) |
| HTTP-клиент | aiohttp |
| Терминал | asyncio.create_subprocess_shell |
| Валидация | Pydantic v2 |
| UI | HTML + CSS + Vanilla JS |
| Markdown | marked.js |
| Подсветка кода | highlight.js |

---

## Установка

### Требования

- Python 3.10+
- Windows 10 / macOS / Linux
- API ключи [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), [Groq](https://console.groq.com/) (бесплатно), [Cerebras](https://cloud.cerebras.ai/) (бесплатно), или другие поддерживаемые провайдеры

### Шаги

```bash
# 1. Клонируйте репозиторий
git clone https://github.com/ShkodnikAI/Fosved-coder.git
cd Fosved-coder

# 2. Создайте виртуальное окружение
python -m venv venv

# 3. Активируйте (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# 3. Активируйте (macOS / Linux)
source venv/bin/activate

# 4. Установите зависимости
pip install -r requirements.txt

# 5. Создайте файл конфигурации
copy config.example.yaml config.yaml

# 6. Отредактируйте config.yaml — настройте API ключи (через UI или переменные окружения)
#    Минимальный набор: хотя бы один API ключ (Groq и Cerebras — бесплатны!)

# 7. Запустите
python run.py
```

Откройте [http://localhost:8000](http://localhost:8000) в браузере.

---

## Структура проекта

```
Fosved-coder/
├── run.py                     # Точка входа (Uvicorn) — 4 WS/HTTP эндпоинта верхнего уровня
├── config.yaml                # Конфигурация (НЕ попадает в git!)
├── config.example.yaml        # Шаблон конфигурации
├── requirements.txt           # Зависимости Python
├── PROMPT.md                  # Мастер-промпт проекта
├── V2_PLAN.md                 # План реализации v2
│
├── core/                      # Бизнес-логика (15 модулей)
│   ├── agent.py               # LLM-цикл, tool-calling, fallback между моделями
│   ├── intelligent_router.py  # Советник по выбору модели (лидер для сложных задач)
│   ├── keys_manager.py        # API-ключи, валидация, статусы, фоновое revalidation
│   ├── memory.py              # SQLAlchemy модели + CRUD, async engine
│   ├── executor.py            # Async shell-команды, киборг-режим
│   ├── context_manager.py     # Repo Map (сканирование, кеш)
│   ├── context_compressor.py  # Сжатие истории сообщений
│   ├── ideas_injector.py      # GitHub API, скачивание, ИИ-анализ
│   ├── prompt_injector.py     # Инъекция скиллов и контекста
│   ├── response_parser.py     # Парсинг ответа модели (file/command/diff блоки)
│   ├── auto_agent.py          # Автономный режим (без участия пользователя)
│   ├── code_tester.py         # Запуск тестов сгенерированного кода
│   ├── apk_builder.py         # Сборка APK для мобильных проектов
│   ├── action_logger.py       # Структурированный лог действий
│   └── observation_manager.py # Наблюдения за процессом
│
├── api/                       # REST API
│   └── endpoints.py           # 120+ эндпоинтов под /api/v1
│
├── ui/                        # Веб-интерфейс
│   ├── static/style.css       # Тёмная тема VS Code
│   └── templates/index.html   # Верстка + WebSocket + JS
│
├── skills/                    # 69 скиллов (18 МБ): ppt, pdf, docx, xlsx, ui-ux и др.
│
├── projects/                  # Рабочие папки пользователей (содержимое в .gitignore)
│
├── data/                      # Runtime: SQLite БД, логи (целиком в .gitignore)
│
└── audit-trace/               # Отчёты и бэкапы аудитов (в .gitignore)
```

---

## Конфигурация

Редактируйте `config.yaml` или используйте UI для добавления API-ключей:

```yaml
llm:
  default_model: "grok/grok-3"
  temperature: 0.2
  max_tokens: 4096
  fallback_chain:
    - "openai/gpt-4o"
    - "anthropic/claude-sonnet-4-6"
    - "gemini/gemini-2.5-flash"
    - "deepseek/deepseek-chat"
    - "openai/llama-4-scout-17b-16e-instruct"   # Cerebras (бесплатно)
    - "groq/llama-3.3-70b-versatile"              # Groq (бесплатно)

system:
  db_url: "sqlite+aiosqlite:///fosved_coder.db"
  projects_dir: "./projects"
  ideas_cache_dir: "./.cache/ideas"
  max_iterations: 3
  max_context_files: 20
```

### Поддерживаемые провайдеры

Через LiteLLM поддерживаются любые провайдеры. Бесплатные: Groq и Cerebras.

| Провайдер | Статус | Пример модели |
|-----------|--------|--------------|
| Anthropic | Платный | `claude-sonnet-4-6` |
| OpenAI | Платный | `gpt-4o`, `gpt-4.1` |
| xAI Grok | Платный | `grok-3`, `grok-3-mini` |
| Google Gemini | Платный | `gemini-2.5-pro`, `gemini-2.5-flash` |
| DeepSeek | Платный | `deepseek-chat`, `deepseek-reasoner` |
| Qwen (Alibaba) | Платный | `qwen3-235b-a22b` |
| Z.AI (GLM) | Платный | `glm-5.1` |
| Kimi (Moonshot) | Платный | `kimi-k2-0711` |
| Abacus.AI (RouteLLM) | Платный | `route-llm` (65+ моделей) |
| **Groq** | **Бесплатно** | `llama-3.3-70b-versatile` |
| **Cerebras** | **Бесплатно** | `llama-4-scout-17b-16e-instruct` |
| Ollama (local) | Локальный | `llama3` и другие |

---

## Использование

### Чат с ИИ

Просто пишите задачи в чат — ИИ отвечает с поддержкой Markdown и подсветкой кода.

### Slash-команды

| Команда | Описание |
|---------|---------|
| `/terminal <cmd>` | Выполнить shell-команду |
| `/approve <id>` | Подтвердить критическую команду |
| `/reject <id>` | Отклонить критическую команду |
| `/git_pull` | git pull в текущем проекте |
| `/git_push` | git push в текущем проекте |
| `/ideas <url>` | Проанализировать GitHub-репозиторий |
| `/repo_map` | Показать структуру проекта |
| `/clear` | Очистить историю чата |
| `/help` | Справка |

### Управление проектами

- Нажмите **+** в левой панели для создания нового проекта
- Клик по проекту — переключение контекста
- Каждый проект имеет отдельную историю чата и Repo Map

### Идеи (База знаний)

- Вставьте ссылку на GitHub-репозиторий в поле ввода
- Нажмите **OK** — ИИ проанализирует структуру и архитектуру
- Результат сохраняется в БД и используется как контекст

---

## REST API

Базовый URL: `http://localhost:8000/api/v1/`

Реализовано **120+ эндпоинтов**, сгруппированных по доменам:

| Домен | Префикс | Примеры |
|-------|---------|--------|
| Проекты | `/projects` | список, создание, удаление, переименование, регенерация ключа |
| Ключи и провайдеры | `/keys` | добавление, удаление, переключение, валидация |
| Модели | `/models` | список, probed, валидация, локальные, custom, abacus refresh |
| Скиллы | `/skills` | список, активация для проекта |
| Идеи (GitHub) | `/ideas` | анализ репо, список, удаление |
| Драфты | `/drafts` | CRUD, генерация промпта, конвертация в проект |
| Анкета | `/questionnaire`, `/questionnaires` | динамические вопросы по проекту |
| Чат | `/chat` | история, очистка |
| Hub (ИИ-Офис) | `/hub` | приём задач от внешних агентов |
| Архивы | `/archives` | архивирование завершённых проектов |
| Шаблоны | `/templates` | шаблоны кода для генерации |
| Память | `/memory` | observation-логи, наблюдения |
| Логи | `/logs` | action-логи, ошибки |
| Статистика | `/stats`, `/status`, `/health`, `/config` | системная информация |
| Probe | `/probe-models`, `/probed-models` | тихое зондирование моделей |
| Webhook | `/webhook` | внешние интеграции |

Точечные действия: `POST /upload` (загрузка файлов), `WS /ws` (основной WebSocket), `WS /ws/executor` (исполнение команд).

Полный список эндпоинтов — в `api/endpoints.py` или через автодокументацию Swagger: `http://localhost:8000/docs`.

---

## Киборг-режим (Безопасность)

При обнаружении опасных команд:
- `rm -rf`, `DROP TABLE`, `FORMAT C:`, `shutdown` и др.
- Звуковой сигнал (winsound на Windows)
- Команда блокируется до подтверждения
- Автоматический git checkpoint перед выполнением

---

## Разработка

Подробное описание архитектуры и логики модулей — в файле [PROMPT.md](PROMPT.md).

---

## Лицензия

MIT
