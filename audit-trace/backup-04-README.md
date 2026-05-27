# Fosved Coder v2.0

Локальный автопилот для разработки, объединяющий лучшее из **Aider** (Repo Map), **Claude Code** (автономное выполнение команд) и **Cursor** (UI), с уникальными фичами, которых нет ни у одного инструмента.

![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Возможности

- **Умный Роутер (Money Saver)** — гибридная система маршрутизации: эвристика по ключевым словам + ИИ-менеджер. Рутину отправляет на дешёвые модели (Gemini Flash), сложные задачи — на дорогие (Claude 3.5 Sonnet, GPT-4o). Статистика маршрутизации сохраняется в БД.
- **Циклический Агент** — ИИ автоматически исправляет ошибки: запрос → генерация кода → выполнение → при ошибке повторяет до 3 раз.
- **Repo Map** — сканирует структуру проекта, извлекает сигнатуры функций/классов и передаёт контекст ИИ. Кешируется с MD5-хешированием.
- **Идеи-Инъектор** — анализирует чужие GitHub-репозитории через API: скачивает ключевые файлы, создаёт ИИ-выжимку архитектуры. Снижает галлюцинации.
- **Мульти-проектность** — переключение между проектами с сохранением отдельной истории чата.
- **Киборг-режим** — блокировка критических команд (rm -rf, DROP TABLE) с звуковым алертом и git checkpoint перед выполнением.
- **REST API** — 12 эндпоинтов для интеграции с внешними ИИ-агентами (ИИ-Офис).
- **Markdown UI** — рендеринг ответов ИИ через marked.js, подсветка кода highlight.js, тёмная тема VS Code.

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
├── run.py                     # Точка входа (Uvicorn)
├── config.yaml                # Конфигурация (НЕ попадает в git!)
├── config.example.yaml        # Шаблон конфигурации
├── requirements.txt           # Зависимости Python
├── PROMPT.md                  # Мастер-промпт проекта
│
├── core/                      # Бизнес-логика
│   ├── agent.py               # Обёртка над LiteLLM (стриминг, циклический агент)
│   ├── router.py              # Гибридный роутер задач
│   ├── executor.py            # Async shell-команды, алерты, git checkpoint
│   ├── context_manager.py     # Repo Map (сканирование, кеш)
│   ├── ideas_injector.py      # GitHub API, скачивание, ИИ-анализ
│   └── memory.py              # SQLAlchemy модели + CRUD
│
├── api/                       # REST API
│   └── endpoints.py           # 12 эндпоинтов + Pydantic схемы
│
├── ui/                        # Веб-интерфейс
│   ├── static/style.css       # Тёмная тема VS Code
│   └── templates/index.html   # Верстка + WebSocket + JS
│
└── projects/                  # Рабочие папки пользователей
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

| Метод | Эндпоинт | Описание |
|-------|---------|---------|
| `GET` | `/projects` | Список проектов |
| `POST` | `/projects` | Создать проект |
| `DELETE` | `/projects/{id}` | Удалить проект |
| `GET` | `/ideas` | Список идей |
| `POST` | `/ideas` | Анализировать репозиторий |
| `DELETE` | `/ideas/{id}` | Удалить идею |
| `POST` | `/task` | Принять задачу от ИИ-агента |
| `POST` | `/approve/{id}` | Подтвердить команду |
| `GET` | `/stats` | Статистика системы |
| `GET` | `/config` | Конфигурация (без ключа) |

Автодокументация Swagger: `http://localhost:8000/docs`

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
