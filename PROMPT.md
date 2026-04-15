FOSVED CODER v2.0 — МАСТЕР-ПРОМПТ ДЛЯ РАЗРАБОТКИ
(Enterprise AI-Coding Microservice)

════════════════════════════════════════════════════════════════════════════
КРИТИЧЕСКИЕ ПРАВИЛА — ПРОЧИТАЙ ПЕРЕД ГЕНЕРАЦИЕЙ
════════════════════════════════════════════════════════════════════════════

ПРАВИЛО 1 — АРХИТЕКТУРА: Микросервис (Headless Core + Web UI + REST API).
  Ядро (core/) НЕ знает про UI и API. API (api/) — шлюз для внешних систем.
  UI (ui/) — только отображение. Вся бизнес-логика в core/.

ПРАВИЛО 2 — НИКАКИХ ЗАГЛУШОК: Каждый файл — полный рабочий код.
  Запрещено: "// TODO", "// реализация позже", пустые функции pass, "raise NotImplementedError".
  Если что-то пока невозможно — добавь явный комментарий # V2.0 и минимальную заглушку
  с осмысленным поведением (return None, return []), а не падением.

ПРАВИЛО 3 — БЕЗ ВЕНДОР ЛОКА: Все запросы идут через библиотеку litellm.
  Поддерживаем: OpenRouter, Anthropic, OpenAI, локальные Ollama.
  Переключение — одной строкой в config.yaml.

ПРАВИЛО 4 — АСИНХРОННОСТЬ: Всё что можно — async/await.
  FastAPI, SQLAlchemy (aiosqlite), litellm.acompletion, aiohttp для скачивания.
  Блокирующие операции (subprocess) — через asyncio.create_subprocess_shell.

ПРАВИЛО 5 — ТИПИЗАЦИЯ: Все функции с type hints, Pydantic модели для JSON.

ПРАВИЛО 6 — ОБРАБОТКА ОШИБОК: Каждая внешняя операция (API, файлы, shell)
  оборачена в try/except с информативным сообщением для пользователя.

ПРАВИЛО 7 — БЕЗОПАСНОСТЬ: config.yaml и .env файлы НИКОГДА не попадают в git.

════════════════════════════════════════════════════════════════════════════
СРЕДА РАЗРАБОТКИ
════════════════════════════════════════════════════════════════════════════

ОС:           Windows 10, PowerShell 5.1+
Путь проекта: D:\AI_projectS\FosvedCoder
Python:       3.10+ (f-strings, type hints, match/case, modern syntax)
Запуск:       python run.py -> открывается http://localhost:8000
БД:           SQLite (файл fosved_coder.db в корне, автоматически создаётся)

════════════════════════════════════════════════════════════════════════════
КОНЦЕПЦИЯ И УТП (Unique Selling Proposition)
════════════════════════════════════════════════════════════════════════════

Fosved Coder — локальный автопилот для разработки, который объединяет лучшее
из Aider (Repo Map), Claude Code (Автономное выполнение команд) и Cursor (UI),
но добавляет то, чего нет ни у кого:

1. УМНЫЙ РОУТЕР (Money Saver) — Двухуровневая система с гибридной логикой:
   Level 0 — Менеджер (дешёвая модель: Gemini Flash / DeepSeek):
     - Сначала эвристика по ключевым словам (fix bug -> исполнитель, 
       архитектура -> дорогая модель, объяснение -> дешёвая)
     - Если эвристика не уверена — ИИ-менеджер анализирует промпт и 
       разбивает на подзадачи
     - Возвращает JSON: {tasks: [{model, reason, prompt}]}
     - Fallback: если JSON не парсится — отправка в DEFAULT_MODEL напрямую
   Level 1 — Исполнитель: распределяет задачи по моделям.
     Рутина (XML, простые классы, тесты) -> дешёвые модели.
     Сложная архитектура, рефакторинг -> дорогие модели (Claude 3.5 Sonnet / GPT-4o).
     Статистика маршрутизации сохраняется в БД для будущего обучения.

2. ЦИКЛИЧЕСКИЙ АГЕНТ (Auto-Iterate) — ИИ работает циклом:
   Пользователь -> Роутер -> ИИ генерирует код -> Исполнитель запускает ->
   Если ошибка -> ИИ видит stderr -> Исправляет -> Повторяет (макс 3 итерации) ->
   Если успех -> Отчёт пользователю.
   Это позволяет ИИ САМ исправлять ошибки, а не ждать человека.

3. ИДЕИ-ИНЪЕКТОР (Repo Digger) — Снижение галлюцинаций:
   - Получает ссылку на GitHub репозиторий
   - Через GitHub API получает дерево файлов (быстрее чем ZIP)
   - Скачивает ключевые файлы (README, .py/.ts исходники)
   - Дешёвый ИИ создаёт выжимку: что делает репо, архитектура, ключевые файлы
   - Сохраняет в БД (таблица Ideas) для последующего использования
   - При генерации кода — контекст идей подмешивается в промпт

4. REPO MAP (Context Manager) — ИИ видит структуру проекта:
   - Сканирует дерево файлов проекта (игнорируя venv, __pycache__, node_modules)
   - Для каждого файла извлекает: import'ы, сигнатуры классов и функций
   - Формирует компактную строку (tree map) и prepend'ит к промпту
   - Кеширует Repo Map в SQLite, обновляет при изменении файлов

5. ИНТЕГРАЦИЯ С "ИИ-ОФИСОМ" — REST API:
   - POST /api/v1/task — принимает JSON задачу от внешнего ИИ-агента
   - GET /api/v1/status — статус текущей задачи
   - GET /api/v1/projects — список проектов
   - POST /api/v1/ideas — добавить идею (ссылку на репо)
   - Webhook-уведомления о результате
   - API token аутентификация

6. МУЛЬТИ-ПРОЕКТНОСТЬ — Левая панель проектов:
   - Список папок из ./projects/
   - Клик = переключение контекста (загружается история чата из БД)
   - Создание нового проекта прямо из UI
   - Repo Map пересчитывается при переключении проекта

7. КИБОРГ-РЕЖИМ — Безопасность выполнения:
   - Проверка команд на критические слова (DROP, DELETE, rm -rf, FORMAT, FORMAT)
   - При критической команде: winsound.Beep + красная кнопка в UI
   - Блокировка до подтверждения человека
   - Белый список разрешённых директорий
   - Git checkpoint (автокоммит) перед выполнением опасной команды

════════════════════════════════════════════════════════════════════════════
СТЕК ТЕХНОЛОГИЙ (СТРОГО)
════════════════════════════════════════════════════════════════════════════

- Ядро/Веб: FastAPI + Uvicorn (async, автодоки API, WebSockets)
- ИИ Оболочка: litellm (ЕДИНСТВЕННЫЙ способ общения с ИИ)
- Валидация: Pydantic v2 (строгая проверка JSON)
- БД: SQLite через SQLAlchemy (async, aiosqlite)
- HTTP клиент: aiohttp (для скачивания файлов, GitHub API)
- Терминал: asyncio.create_subprocess_shell (async выполнение команд)
- Звук: winsound (встроенная Windows)
- Парсинг YAML: pyyaml
- UI: HTML + CSS + Vanilla JS (тёмная тема VS Code стиль)
- Markdown: marked.js (рендеринг ответов ИИ)
- Подсветка кода: highlight.js

════════════════════════════════════════════════════════════════════════════
ПОЛНАЯ СТРУКТУРА ФАЙЛОВ
════════════════════════════════════════════════════════════════════════════

D:\AI_projectS\FosvedCoder\
├── run.py                     # Точка входа (запуск Uvicorn на 0.0.0.0:8000)
├── config.yaml                # Настройки моделей, API ключи (В .gitignore!)
├── config.example.yaml        # Пример конфигурации (для GitHub)
├── requirements.txt           # Зависимости Python
├── PROMPT.md                  # Этот файл — мастер-промпт проекта
│
├── core/                      # МОЗГ — бизнес-логика (не знает про UI/API)
│   ├── __init__.py
│   ├── router.py              # Гибридный умный распределитель задач
│   ├── agent.py               # Обёртка над litellm (стриминг, история)
│   ├── executor.py            # Async shell-команды, winsound, sandbox
│   ├── context_manager.py     # Repo Map: сканирование, кеш, обновление
│   ├── ideas_injector.py      # GitHub API, скачивание, ИИ-анализ репо
│   └── memory.py              # SQLAlchemy модели + полный CRUD
│
├── api/                       # ШЛЮЗ — REST API для внешних ИИ-агентов
│   ├── __init__.py
│   └── endpoints.py           # Все REST эндпоинты + Pydantic схемы
│
├── ui/                        # ГЛАЗА — Веб-интерфейс для человека
│   ├── static/
│   │   ├── style.css          # Тёмная тема VS Code, flexbox
│   │   └── alert.mp3          # Звук критического алерта
│   └── templates/
│       └── index.html         # 3-колоночная верстка + WebSocket + JS
│
└── projects/                  # Рабочие столы пользователей
    └── .gitkeep

════════════════════════════════════════════════════════════════════════════
WEB UI — ЛОГИКА И ВЕРСТКА (index.html)
════════════════════════════════════════════════════════════════════════════

Три колонки:
1. ЛЕВАЯ ПАНЕЛЬ (260px, #252526):
   - Блок "ПРОЕКТЫ": Список проектов из ./projects/. Клик = переключение.
     Кнопка "+ Новый проект" — ввод имени, создаёт папку + запись в БД.
     Кнопка "Удалить" — с подтверждением.
   - Блок "ИДЕИ (База знаний)":
     Поле ввода "Вставить ссылку GitHub..." + кнопка "Анализировать".
     Список скачанных идей (имя репо + краткая суть, кликабельно).
     Кнопка "Удалить" для каждой идеи.
   - Кнопка "Настройки" — открывает модальное окно с config.

2. ЦЕНТР (auto, #1e1e1e):
   - Хедер: имя текущего проекта + кнопка "Очистить чат".
   - Окно чата: сообщения пользователя, ИИ (Markdown рендеринг),
     системные уведомления, логи выполнения команд.
     Код-блоки с подсветкой синтаксиса (highlight.js).
     Кнопка "Копировать" на каждом код-блоке.
   - Input: текстовое поле + кнопка "Отправить" + кнопка "Прикрепить файл".
   - Markdown в ответах ИИ: через marked.js.
   - Поддержка Shift+Enter для новой строки в input.

3. НИЖНЯЯ ПАНЕЛЬ КНОПОК (#007acc, высота 40px):
   - [Terminal] [DB] [Server] [Git Pull] [Git Push] [Settings]
   - Terminal: переключает input в режим shell-команды (префикс /terminal)
   - DB: показывает текущую статистику БД (проекты, чаты, идеи)
   - Server: статус сервера (uptime, модель, память)
   - Git Pull/Push: выполняет git pull/push в папке проекта
   - Settings: открывает модалку с config.yaml

════════════════════════════════════════════════════════════════════════════
ЛОГИКА КЛЮЧЕВЫХ МОДУЛЕЙ
════════════════════════════════════════════════════════════════════════════

── core/router.py ───────────────────────────────────────────────

HybridRouter:
  async def route_task(user_prompt: str, project_context: str) -> RouterResult:
    # Шаг 1: Эвристика по ключевым словам
    keyword_model = self._match_keywords(user_prompt)
    if keyword_model:
      return RouterResult(model=keyword_model, reason="keyword_match", 
                          subtasks=[{prompt: user_prompt, model: keyword_model}])

    # Шаг 2: Если эвристика не уверена — ИИ-менеджер
    system_prompt = """Ты ИИ-менеджер. Проанализируй задачу и верни JSON:
    {
      "subtasks": [
        {"prompt": "подзадача 1", "model": "claude-3.5-sonnet", "reason": "сложная логика"},
        {"prompt": "подзадача 2", "model": "gemini/gemini-2.0-flash", "reason": "рутинная задача"}
      ]
    }
    Доступные модели: {models}"""

    # Шаг 3: Парсинг JSON через Pydantic
    # Шаг 4: Если парсинг падает — fallback на DEFAULT_MODEL
    # Шаг 5: Сохраняем статистику маршрутизации в БД

  def _match_keywords(self, prompt: str) -> str | None:
    KEYWORDS = {
      "complex": ["архитектура", "refactor", "splicing", "redesign", 
                  "система", "framework", "engine"],
      "simple":  ["fix typo", "формат", "xml", "json", "test", "docstring",
                  "комментарий", "простой", "trivial"],
    }
    prompt_lower = prompt.lower()
    for kw in KEYWORDS["simple"]:
      if kw in prompt_lower:
        return CONFIG["llm"]["router_model"]  # дешёвая
    for kw in KEYWORDS["complex"]:
      if kw in prompt_lower:
        return CONFIG["llm"]["default_model"]  # дорогая
    return None  # unsure -> ИИ-менеджер

── core/context_manager.py ──────────────────────────────────────

ContextManager:
  IGNORED_DIRS = {"venv", "__pycache__", "node_modules", ".git", ".cache", 
                  "__pypackages__", ".venv", "env", ".idea", ".vscode"}

  async def build_repo_map(project_path: str) -> str:
    # 1. Сканируем дерево файлов (os.walk, фильтруем IGNORED_DIRS)
    # 2. Для каждого .py/.ts/.js/.yaml/.json файла:
    #    - Читаем первые 50 строк
    #    - Извлекаем import'ы, class, def, async def сигнатуры
    # 3. Формируем компактную строку:
    #    project/
    #    ├── main.py
    #    │   ├── import os, sys
    #    │   ├── class App: __init__, run()
    #    │   └── def main() -> None
    #    └── utils.py
    #        └── def parse_config(path: str) -> dict
    # 4. Кешируем в БД (таблица RepoMap: project_id, content, hash)
    # 5. При следующем вызове — проверяем hash файлов, обновляем только при изменении

  async def read_file_content(project_path: str, relative_path: str) -> str:
    # Безопасное чтение файла (проверка на выход за пределы проекта)
    # Возвращает содержимое файла или ошибку

── core/executor.py ────────────────────────────────────────────

CommandExecutor:
  CRITICAL_PATTERNS = [
    r"rm\s+-rf", r"DROP\s+TABLE", r"DROP\s+DATABASE", r"DELETE\s+FROM",
    r"FORMAT\s+[A-Z]:", r"del\s+/[fqs]", r"rmdir\s+/[s]", r"shutdown"
  ]

  async def execute(cmd: str, project_path: str | None = None) -> ExecutorResult:
    # 1. Проверка на критические паттерны (regex)
    # 2. Если критическое:
    #    - winsound.Beep(2500, 1000)
    #    - Отправить WebSocket сигнал: {"type": "approval_required", "cmd": cmd}
    #    - Ждать подтверждения через API endpoint /approve/{request_id}
    #    - Если таймаут 60 сек — автоматически отменить
    # 3. Git checkpoint: git add -A && git commit -m "auto-checkpoint before: {cmd}"
    # 4. Выполнить: asyncio.create_subprocess_shell(cmd, cwd=project_path)
    # 5. Захват stdout + stderr (стриминг в реальном времени через WebSocket)
    # 6. Вернуть ExecutorResult(exit_code, stdout, stderr, success)

── core/ideas_injector.py ──────────────────────────────────────

IdeasInjector:
  async def process_idea(repo_url: str) -> IdeaResult:
    # 1. Парсинг URL: извлечь owner/repo из github.com/owner/repo
    # 2. GitHub API: GET /repos/{owner}/{repo}
    #    - Получаем описание, язык, звёзды,topics
    # 3. GitHub API: GET /repos/{owner}/{repo}/git/trees/main?recursive=1
    #    - Получаем дерево файлов (быстрее чем ZIP для анализа)
    # 4. Фильтруем интересные файлы (.py, .ts, .js, README*, *.md)
    # 5. Скачиваем содержимое ключевых файлов (через GitHub API contents)
    #    - Максимум 10 файлов, ограничение по размеру (не больше 50KB каждый)
    # 6. Формируем промпт для дешёвого ИИ:
    #    "Проанализируй этот репозиторий. Напиши выжимку:
    #     - Что делает проект (1-2 предложения)
    #     - Ключевые файлы и их роль
    #     - Архитектурные паттерны
    #     - Что можно заимствовать"
    # 7. Сохраняем в БД (таблица Ideas)
    # 8. Возвращаем результат пользователю

── core/memory.py (SQLAlchemy Models) ──────────────────────────

Модели:
  Project:   id, name, path, created_at
  Idea:      id, repo_url, name, summary (Text), raw_data (Text), created_at
  ChatHistory: id, project_id (FK), role, content (Text), timestamp
  RepoMap:   id, project_id (FK), content (Text), file_hash (str), updated_at
  RoutingStats: id, prompt_snippet, chosen_model, reason, success, created_at

CRUD функции (async):
  # Projects
  create_project(name, path) -> Project
  get_all_projects() -> list[Project]
  get_project(id) -> Project | None
  delete_project(id) -> bool

  # Ideas
  save_idea(repo_url, name, summary, raw_data) -> Idea
  get_all_ideas() -> list[Idea]
  get_idea(id) -> Idea | None
  delete_idea(id) -> bool

  # ChatHistory
  save_message(project_id, role, content) -> None
  get_history(project_id, limit=50) -> list[dict]
  clear_history(project_id) -> None

  # RepoMap
  save_repo_map(project_id, content, file_hash) -> None
  get_repo_map(project_id) -> str | None

  # RoutingStats
  save_routing_stat(prompt, model, reason, success) -> None

── api/endpoints.py ────────────────────────────────────────────

Pydantic схемы (в том же файле):
  TaskInput:       prompt, project_id (optional), priority (optional)
  TaskResult:      status, output, files_changed, build_result, error
  ProjectCreate:   name, path (optional)
  IdeaInput:       repo_url
  ApprovalRequest: request_id, approved (bool)

REST эндпоинты:
  POST   /api/v1/task          — Принять задачу от ИИ-Офиса
  GET    /api/v1/task/{id}/status — Статус выполнения задачи
  GET    /api/v1/projects      — Список проектов
  POST   /api/v1/projects      — Создать проект
  DELETE /api/v1/projects/{id} — Удалить проект
  GET    /api/v1/ideas         — Список идей
  POST   /api/v1/ideas         — Добавить идею (ссылка на репо)
  DELETE /api/v1/ideas/{id}    — Удалить идею
  POST   /api/v1/approve/{id}  — Подтвердить критическую команду
  GET    /api/v1/stats         — Статистика системы
  GET    /api/v1/config        — Текущая конфигурация (без API ключа!)

WebSocket эндпоинты (в run.py):
  /ws — основной чат (streaming)
  /ws/executor — стриминг вывода команд в реальном времени

════════════════════════════════════════════════════════════════
ПОРЯДОК ГЕНЕРАЦИИ ФАЙЛОВ
════════════════════════════════════════════════════════════════

БАТЧ 1: Фундамент (существующие файлы — доработка)
 1. requirements.txt        — добавить aiohttp, marked.js CDN
 2. config.yaml             — актуальная структура
 3. config.example.yaml     — пример для GitHub (без ключей)
 4. run.py                  — полноценный запуск с Uvicorn + все WebSocket
 5. core/memory.py          — все модели + полный CRUD
 6. ui/static/style.css     — доработанная тёмная тема

БАТЧ 2: Ядро ИИ
 7. core/__init__.py
 8. api/__init__.py
 9. core/agent.py           — стриминг + циклический агент
10. core/router.py          — гибридный роутер
11. core/context_manager.py — Repo Map с кешированием

БАТЧ 3: Исполнитель и Инъекции
12. core/executor.py        — async shell + winsound + sandbox
13. core/ideas_injector.py  — GitHub API + анализ

БАТЧ 4: API и UI
14. api/endpoints.py        — все REST эндпоинты
15. ui/templates/index.html — полный UI с Markdown + логика панелей

════════════════════════════════════════════════════════════════
УСТАНОВКА И ПЕРВЫЙ ЗАПУСК
════════════════════════════════════════════════════════════════

# PowerShell:
cd D:\AI_projectS\FosvedCoder
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Отредактировать config.yaml — вставить свой API ключ OpenRouter
python run.py
# Открыть в браузере: http://localhost:8000
