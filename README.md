# Fosved Coder v2.0

AI-ассистент для разработки с персистентной памятью. Объединяет лучшее из **Aider** (Repo Map), **Claude Code** (автономные агенты), **agentmemory** (4-уровневая память) и **Cursor** (UI), с уникальными фичами, которых нет ни у одного инструмента.

![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Ключевые возможности

### Умная система памяти (Smart Memory v2)

Система памяти, вдохновлённая [agentmemory](https://github.com/rohitg00/agentmemory) (18.7K ★) и кривой забывания Эббингауза:

- **Семантический поиск** — находит observations по смыслу, а не только по ключевым словам. При запросе «где чинили авторизацию» находит observation «fixed JWT token validation in auth middleware»
  - Движок: `sentence-transformers` (all-MiniLM-L6-v2, 384 dim, локально без API)
  - Хранение: numpy BLOB в SQLite / PostgreSQL (без pgvector dependency)
  - Комбинирование: FTS5 + vector search через **Reciprocal Rank Fusion** (RRF, k=60)

- **Smart Context Assembly** — при каждом запросе инжектит в system prompt не просто последние N observations, а семантически релевантные факты из прошлых сессий
  - Session summaries (последние 3) — всегда
  - Последние observations (24h) — всегда
  - Семантически релевантные факты — если query передан и модель загружена

- **Memory Decay (кривая Эббингауза)** — плавное затухание памяти вместо жёсткого порога
  - Score = recency_factor × access_boost
  - Часто используемые факты живут дольше, редкие затухают
  - Фоновый eviction loop (раз в час)
  - Hard floor: 180 дней — защита от бесконечного роста

- **Observations** — сжатые записи о действиях агента (tool use, ошибки, решения, инсайты). LLM-компрессия в фоне, privacy tag stripping, 3-layer progressive search

- **Session Summaries** — AI-сгенерированные резюме сессий при закрытии WebSocket

### Ядро агента

- **Умный Роутер (Money Saver)** — единая точка выбора модели. Учитывает: явный выбор пользователя, приоритетные модели проекта, `fallback_chain`, рекомендацию `intelligent_router` (классификация задачи), список проверенных моделей. Фоновое revalidation каждые 5 минут.
- **Циклический Агент** — tool-calling цикл до 10 итераций. Антизалипание: 3 одинаковых tool_call подряд → прерывание цикла.
- **Двойной режим** — Tool Calling (Claude, GPT, Gemini) + Prompt Injection (fallback для Qwen, Llama, Ollama)
- **Автономный режим** — AI итеративно выполняет задачи без участия пользователя

### Инструменты и контекст

- **Repo Map** — сканирует структуру проекта, извлекает сигнатуры функций/классов. MD5-кеш инвалидации.
- **Идеи-Инъектор** — анализирует GitHub-репозитории через API: скачивает ключевые файлы, создаёт ИИ-выжимку архитектуры
- **Context Compressor** — LLM-сжатие истории + regex fallback. Не-деструктивная архивация (`archived=True`, данные сохраняются для поиска)
- **69 скиллов** — готовые пресеты для ppt, pdf, docx, xlsx, market-research, UI/UX, charts и др. Авто-выбор моделью по контексту

### Безопасность и инфраструктура

- **Киборг-режим** — блокировка критических команд (rm -rf, DROP TABLE) с подтверждением и git checkpoint
- **Постоянная БД** — PostgreSQL (Supabase/Neon/Render) + SQLite fallback. 15 таблиц, async SQLAlchemy.
- **REST API** — 120+ эндпоинтов под `/api/v1` для внешней интеграции
- **WebSocket** — RFC 6455 PING frames keepalive, восстановление PAT-токенов из БД

---

## Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Веб-сервер | FastAPI + Uvicorn (async) |
| ИИ-оболочка | LiteLLM (Anthropic, OpenAI, Grok, Cerebras, DeepSeek, Gemini, Qwen, Ollama) |
| Память | sentence-transformers + numpy + FTS5 |
| База данных | PostgreSQL (asyncpg) / SQLite (aiosqlite) + SQLAlchemy |
| HTTP-клиент | aiohttp + httpx |
| UI | HTML + CSS + Vanilla JS, marked.js, highlight.js |

---

## Установка

### Требования

- Python 3.10+
- Windows 10 / macOS / Linux
- API ключи: [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), [Groq](https://console.groq.com/) (бесплатно), [Cerebras](https://cloud.cerebras.ai/) (бесплатно), или другие

### Шаги

```bash
git clone https://github.com/ShkodnikAI/Fosved-coder.git
cd Fosved-coder
python -m venv venv
source venv/bin/activate  # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Настройте API ключи в config.yaml или через UI
python run.py
```

Откройте [http://localhost:8000](http://localhost:8000) в браузере.

### Переменные окружения (опционально)

| Переменная | Описание | Default |
|-----------|----------|---------|
| `DATABASE_URL` | PostgreSQL/SQLite URL | SQLite |
| `EMBEDDING_MODEL` | Модель для эмбеддингов | `all-MiniLM-L6-v2` |
| `EMBEDDING_CACHE_DIR` | Кеш модели | `data/embeddings` |
| `WS_PING_INTERVAL` | WebSocket PING интервал (сек) | `15` |
| `WS_PING_TIMEOUT` | WebSocket PING таймаут (сек) | `10` |

---

## Структура проекта

```
Fosved-coder/
├── run.py                     # Точка входа (Uvicorn) + WebSocket handler
├── config.yaml                # Конфигурация (не в git)
├── requirements.txt           # Python зависимости
│
├── core/                      # Бизнес-логика (17 модулей)
│   ├── agent.py               # LLM-цикл, tool-calling, fallback
│   ├── intelligent_router.py  # Советник по выбору модели
│   ├── keys_manager.py        # API-ключи, валидация, revalidation
│   ├── memory.py              # SQLAlchemy модели + CRUD (15 таблиц)
│   ├── memory_embeddings.py   # Векторные эмбеддинги + RRF fusion
│   ├── memory_decay.py        # Затухание памяти (Эббингауз) + eviction
│   ├── observation_manager.py # Observations, search, context assembly
│   ├── context_compressor.py  # LLM + regex сжатие истории
│   ├── context_manager.py     # Repo Map (сканирование, кеш)
│   ├── executor.py            # Async shell-команды, киборг-режим
│   ├── response_parser.py     # Парсинг ответа модели
│   ├── prompt_injector.py     # Инъекция скиллов и контекста
│   ├── ideas_injector.py      # GitHub API анализ
│   ├── auto_agent.py          # Автономный режим
│   ├── action_logger.py       # Структурированный лог
│   ├── code_tester.py         # Запуск тестов
│   └── apk_builder.py         # Сборка APK
│
├── api/                       # REST API (120+ эндпоинтов)
│   └── endpoints.py
│
├── ui/                        # Веб-интерфейс
│   ├── static/style.css       # Тёмная тема VS Code
│   └── templates/index.html   # SPA + WebSocket + JS
│
├── skills/                    # 69 скиллов (ppt, pdf, docx, xlsx, ui-ux)
├── projects/                  # Рабочие папки пользователей
└── data/                      # SQLite БД, логи, кеш эмбеддингов
```

---

## Конфигурация

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
  db_url: "sqlite+aiosqlite:///data/fosved_coder.db"
  projects_dir: "./projects"
```

### Поддерживаемые провайдеры

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
| **Groq** | **Бесплатно** | `llama-3.3-70b-versatile` |
| **Cerebras** | **Бесплатно** | `llama-4-scout-17b-16e-instruct` |
| Ollama | Локальный | `llama3` и другие |

---

## Архитектура памяти

```
User Query
    │
    ▼
┌──────────────────────────────────────────────┐
│         Smart Context Assembly                │
│                                               │
│  1. Session Summaries (последние 3)           │
│  2. Recent Observations (24h)                 │
│  3. Semantic Search (query → embedding →      │
│     cosine similarity → top-5 relevant facts)  │
│                                               │
│  Output → injected into system prompt         │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│         Hybrid Search (search_observations)    │
│                                               │
│  FTS5 (keywords) ──┐                          │
│                     ├─ RRF (k=60) → ranked list │
│  Vector (semantic) ─┘                          │
│                                               │
│  decay_score = e^(-λ·days) × (1+ln(acc+1))   │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────┐
│         Background Tasks                      │
│                                               │
│  - Embedding computation (per observation)     │
│  - LLM compression (per observation)           │
│  - Session summary (on WS disconnect)          │
│  - Decay eviction loop (ежечасно)              │
└──────────────────────────────────────────────┘
```

---

## REST API

Базовый URL: `http://localhost:8000/api/v1/`

| Домен | Префикс | Примеры |
|-------|---------|--------|
| Проекты | `/projects` | CRUD, переименование, регенерация ключа |
| Ключи | `/keys` | добавление, удаление, валидация |
| Модели | `/models` | список, probe, local |
| Скиллы | `/skills` | список, активация |
| Идеи | `/ideas` | анализ GitHub-репо |
| Память | `/memory` | observations, поиск, статистика |
| Драфты | `/drafts` | CRUD, генерация промпта |
| Чат | `/chat` | история, очистка |
| Статистика | `/stats`, `/health` | системная информация |

Полная документация: `http://localhost:8000/docs`

---

## Slash-команды

| Команда | Описание |
|---------|---------|
| `/terminal <cmd>` | Выполнить shell-команду |
| `/git_pull` | git pull |
| `/git_push` | git push |
| `/quick_push` | git add -A + commit + push |
| `/git_clone <url>` | Клонировать GitHub-репозиторий |
| `/repo_map` | Показать структуру проекта |
| `/ideas <url>` | Проанализировать GitHub-репозиторий |
| `/clear` | Очистить историю чата |
| `/help` | Справка |

---

## Лицензия

MIT
