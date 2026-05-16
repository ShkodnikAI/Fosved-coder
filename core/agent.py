import os
import platform
import time
import shlex
import re
import asyncio
import fnmatch
import litellm
import json
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime, timezone
from core.memory import CONFIG, save_message, get_history, get_project, get_project_token_by_path, git_push_with_token, git_clone_with_token, save_probed_models, save_tool_usage, save_model_usage
from core.keys_manager import keys_manager
from core.context_compressor import ContextCompressor
from core.action_logger import get_logger

litellm.suppress_debug_info = True


def _safe_join(base: str, rel: str) -> str | None:
    """Resolve `rel` under `base` and reject anything escaping the project dir.

    Returns absolute path on success, None on traversal/symlink-escape attempts.
    """
    if not base or rel is None:
        return None
    try:
        base_real = Path(base).resolve()
        # `..` and absolute paths are normalized away by resolve()
        target = (base_real / rel).resolve()
        target.relative_to(base_real)
        return str(target)
    except (ValueError, OSError):
        return None


def get_platform_info() -> str:
    """Определить платформу и доступный shell для инструкций AI."""
    system = platform.system().lower()
    if system == "windows":
        return ("СЕРВЕР: Windows. Shell: PowerShell (или cmd).\n"
                "- Используй PowerShell-синтаксис для файловых операций\n"
                "- Используй пути с обратными слешами или прямыми: C:\\Projects\\...\n"
                "- Пакетные менеджеры: npm, pip, python, git")
    elif system == "linux":
        return ("СЕРВЕР: Linux. Shell: bash.\n"
                "- НЕ используй PowerShell команды (powershell, Get-Content, etc.) — они не работают!\n"
                "- Используй bash: cat, ls, grep, find, sed, awk\n"
                "- Пути: /home/user/project/...\n"
                "- Пакетные менеджеры: npm, pip3, python3, git")
    elif system == "darwin":
        return ("СЕРВЕР: macOS. Shell: zsh.\n"
                "- Используй bash/zsh синтаксис\n"
                "- Пути: /Users/user/project/...\n"
                "- Пакетные менеджеры: npm, pip3, python3, brew, git")
    return "СЕРВЕР: неизвестная ОС. Используй стандартные bash-команды."
logger = get_logger()

# Context variable for task ID — propagates through all async calls
# Set by run.py when creating a parallel task, auto-injected by safe_ws_send
_current_task_id: ContextVar[str] = ContextVar('current_task_id', default='')

# Глобальный кэш провайдеров с нулевым балансом (402 / insufficient credits)
# Избегает бесконечных повторных попыток к мёртвым провайдерам
_no_credits_providers: set[str] = set()
_NO_CREDITS_COOLDOWN = 300  # секунд до сброса (5 минут)
_no_credits_ts: float = 0.0  # timestamp последнего добавления


def _now():
    """Current UTC time as HH:MM:SS string."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _mark_no_credits(provider_id: str):
    """Пометить провайдера как без кредитов (глобальный кэш с TTL 5 мин)."""
    import time as _time
    global _no_credits_ts
    _no_credits_providers.add(provider_id)
    _no_credits_ts = _time.time()
    print(f"  [agent] no-credits provider: {provider_id} (cached {_NO_CREDITS_COOLDOWN}s)")


def _is_no_credits_provider(model_id: str) -> bool:
    """Проверить, относится ли модель к провайдеру без кредитов."""
    import time as _time
    global _no_credits_ts
    # Сброс кэша по TTL
    if _no_credits_providers and _time.time() - _no_credits_ts > _NO_CREDITS_COOLDOWN:
        print(f"  [agent] no-credits cache expired, clearing")
        _no_credits_providers.clear()
        return False
    mc = keys_manager.get_model_config(model_id)
    if not mc:
        return False
    provider = mc.get("provider", "")
    if provider in _no_credits_providers:
        return True
    # Также проверяем по model_id напрямую (provider__model формат)
    if "__" in model_id:
        pid = model_id.split("__")[0]
        if pid in _no_credits_providers:
            return True
    return False


async def safe_ws_send(websocket, data: dict, _skip_task_id: bool = False):
    """Send JSON to websocket, silently ignoring any errors (closed conn, etc.)."""
    try:
        if not _skip_task_id:
            tid = _current_task_id.get('')
            if tid:
                data = {**data, "task_id": tid}
        await websocket.send_json(data)
    except Exception:
        pass


async def _send_log(websocket, content: str, level: str = "info"):
    """Send auto_log message to frontend log panel."""
    try:
        await websocket.send_json({
            "type": "auto_log",
            "content": content,
            "level": level,
            "time": _now(),
        })
    except Exception:
        pass

# ═══════════════════════════════════════════════════════
# SKILL CONTEXT LOADER
# ═══════════════════════════════════════════════════════

def _load_skill_context(active_skills: list[str] | None) -> str:
    """
    Загружает SKILL.md контент для активных скиллов.
    Ищет файлы в skills/{skill_id}/SKILL.md относительно корня проекта.
    Возвращает строку для инжекции в system prompt.
    """
    if not active_skills:
        return ""

    parts = []
    skills_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")

    for skill_id in active_skills:
        skill_id = skill_id.strip()
        if not skill_id:
            continue
        skill_path = os.path.join(skills_dir, skill_id, "SKILL.md")
        if os.path.isfile(skill_path):
            try:
                with open(skill_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"\n## Активный навык: {skill_id}\n{content}\n")
                    print(f"  [agent] skill loaded: {skill_id} ({len(content)} chars)")
            except Exception as e:
                print(f"  [agent] skill read error {skill_id}: {e}")
        else:
            print(f"  [agent] skill NOT found: {skill_id} (looked: {skill_path})")

    if not parts:
        return ""
    return "\n# АКТИВНЫЕ НАВЫКИ (применяй эти знания при генерации кода):\n" + "\n".join(parts)


# ═══════════════════════════════════════════════════════
# TOOL DEFINITIONS для litellm function calling
# ═══════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать содержимое файла из проекта. Возвращает полный текст файла.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Относительный путь к файлу от корня проекта (напр. 'src/app.py')"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Создать или перезаписать файл в проекте. Автоматически создаёт поддиректории.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Относительный путь к файлу (напр. 'src/utils.py')"
                    },
                    "content": {
                        "type": "string",
                        "description": "Полное содержимое файла"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Получить список файлов и директорий в проекте. Возвращает древовидную структуру.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Относительный путь к директории (по умолчанию — корень проекта)",
                        "default": "."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Поиск текста во всех файлах проекта. Возвращает список совпадений с путями и строками.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Текст или regex для поиска"
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Шаблон имени файла (напр. '*.py', '*.ts'), опционально",
                        "default": "*"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Выполнить shell-команду в директории проекта. Используй для: git, npm, pip, python и т.д.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell-команда для выполнения (напр. 'git status', 'pip install requests')"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_push",
            "description": "Закоммитить все изменения и запушить на GitHub. Используй после создания/изменения файлов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Сообщение коммита (напр. 'feat: add login page')"
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_clone",
            "description": "Склонировать GitHub репозиторий в текущий проект. Использует PAT-токен автоматически для приватных репо. НЕ используй execute_command('git clone ...') — всегда используй этот инструмент.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "URL GitHub репозитория (напр. 'https://github.com/user/repo')"
                    }
                },
                "required": ["repo_url"]
            }
        }
    },
]


SYSTEM_PROMPT_TEMPLATE = """Ты Fosved Coder — AI-ассистент для разработки проекта.
Ты работаешь внутри IDE пользователя и имеешь ДОСТУП К ИНСТРУМЕНТАМ для работы с файлами и командами.

{project_context}

{repo_map}

{ideas_context}

{compressed_context}

Твои инструменты (function calling):
- read_file(path) — прочитать файл проекта
- write_file(path, content) — создать или перезаписать файл
- list_files(path) — список файлов в директории
- search_files(pattern) — поиск текста в файлах
- execute_command(command) — выполнить shell-команду (git, npm, pip, python и т.д.)
- git_commit_push(message) — закоммитить и запушить на GitHub
- git_clone(repo_url) — склонировать GitHub репозиторий (использует PAT-токен автоматически)

{platform_info}

Правила:
- ИСПОЛЬЗУЙ ИНСТРУМЕНТЫ для работы с файлами — читай, создавай, редактируй файлы через tools
- НЕ проси пользователя скопировать код — пиши прямо в файлы через write_file
- После изменения файлов — предлагай git_commit_push
- Для выполнения команд — используй execute_command, НЕ пиши пользователю команды для ручного выполнения
- Отвечай на том языке, на котором задан вопрос
- Будь проактивным — если нужно создать файл, создавай его
- Если задача требует нескольких шагов — делай их последовательно
- Для кода в тексте ответа используй Markdown code blocks только для объяснений, реальные файлы пиши через write_file"""


SYSTEM_PROMPT_INJECTION_TEMPLATE = """Ты Fosved Coder — AI-ассистент для разработки проекта.
Ты работаешь внутри IDE пользователя и можешь управлять файлами проекта через специальный формат ответа.
Модель НЕ знает про инструменты — она просто видит контекст задачи и использует формат ответа.

{project_context}

{platform_info}

ФОРМАТ ОТВЕТА — как создавать и изменять файлы:

1. Создать или перезаписать файл:
<file path="относительный/путь/файла.py">
полное содержимое файла...
</file>

2. Создать файл через code block (альтернативный формат):
```python:относительный/путь/файла.py
содержимое файла...
```

3. Показать изменения (diff):
```diff
--- a/файл.py
+++ b/файл.py
@@ -10,5 +10,5 @@
-старая строка
+новая строка
```

4. Выполнить команду:
<command>
shell-команда
</command>

5. Git операции:
<git operation="commit_push" message="описание">
</git>
(Автоматически: git add -A → commit → push. БЕЗ force-push.)

ПРАВИЛА:
- Отвечай на том языке, на котором задан вопрос
- Сначала объясни что делаешь, потом показывай файлы
- НЕ проси пользователя копировать код — пиши прямо в файлы через формат выше
- НЕ используй force-push — никогда
- После изменения файлов система автоматически сделает git commit + push
- Будь проактивным — если нужно создать файл, создавай его
- Для кода в тексте ответа используй Markdown code blocks только для объяснений, реальные файлы пиши через <file> или ```lang:path"""


# ═══════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════

async def execute_tool(name: str, arguments: dict, project_path: str | None, websocket) -> str:
    """Выполнить tool call и вернуть результат как строку для LLM."""
    from core.executor import CommandExecutor
    executor = CommandExecutor()
    _tool_start = time.time()

    try:
        if name == "read_file":
            path = arguments.get("path", "")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = _safe_join(project_path, path)
            if not full_path:
                return f"Ошибка: путь вне проекта: {path}"
            if not os.path.isfile(full_path):
                return f"Ошибка: файл не найден: {path}"
            try:
                if os.path.getsize(full_path) > 5 * 1024 * 1024:
                    return f"Ошибка: файл слишком большой (>5MB): {path}"
            except OSError:
                pass
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # Trim if too large
            if len(content) > 30000:
                content = content[:30000] + "\n\n... (файл обрезан, всего " + str(len(content)) + " символов)"
            logger.log(f"tool: read_file {path}", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"path": path}, "status": "done"})
            await _send_log(websocket, f"📖 Читаю: {path} ({len(content)} симв.)", "info")
            try: asyncio.create_task(save_tool_usage(None, "", "", "read_file", json.dumps({"path": path}), "done", duration_ms=int((time.time()-_tool_start)*1000), result_length=len(content)))
            except Exception: pass
            return content

        elif name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = _safe_join(project_path, path)
            if not full_path:
                return f"Ошибка: путь вне проекта: {path}"
            dir_path = os.path.dirname(full_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.log(f"tool: write_file {path} ({len(content)} chars)", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"path": path, "size": len(content)}, "status": "done"})
            await _send_log(websocket, f"💾 Записываю: {path} ({len(content)} симв.)", "file")
            try: asyncio.create_task(save_tool_usage(None, "", "", "write_file", json.dumps({"path": path}), "done", duration_ms=int((time.time()-_tool_start)*1000), result_length=len(content)))
            except Exception: pass
            return f"Файл {path} сохранён ({len(content)} символов)"

        elif name == "list_files":
            rel_path = arguments.get("path") or "."  # None, "", False → "." (корень проекта)
            rel_path = rel_path.strip() or "."  # whitespace-only → "."
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = _safe_join(project_path, rel_path)
            if not full_path:
                return f"Ошибка: путь вне проекта: {rel_path}"
            if not os.path.isdir(full_path):
                return f"Ошибка: директория не найдена: {rel_path}"
            entries = []
            skip_dirs = {"venv", "__pycache__", "node_modules", ".cache", ".venv", "env", ".idea", ".vscode", "dist", "build", "__pypackages__", ".next", ".nuxt", ".gradle", "target"}
            try:
                for item in sorted(os.listdir(full_path)):
                    # Пропускаем скрытые файлы/папки, КРОМЕ известных конфигурационных
                    if item.startswith(".") and item not in {".env", ".gitignore", ".gitattributes", ".editorconfig", ".eslintrc", ".prettierrc", ".dockerignore"}:
                        # Но .git показываем как индикатор репозитория
                        if item == ".git" and os.path.isdir(os.path.join(full_path, ".git")):
                            entries.append(f"📂 .git/")
                        continue
                    item_path = os.path.join(full_path, item)
                    if os.path.isdir(item_path) and item not in skip_dirs:
                        entries.append(f"📁 {item}/")
                    elif os.path.isfile(item_path):
                        size = os.path.getsize(item_path)
                        if size > 1024:
                            entries.append(f"📄 {item} ({size // 1024}KB)")
                        else:
                            entries.append(f"📄 {item} ({size}B)")
            except PermissionError:
                return "Ошибка: нет доступа к директории"
            # Если только .git — указываем что это git-репозиторий
            if not entries and os.path.isdir(os.path.join(full_path, ".git")):
                entries.append("📂 .git/ (пустой рабочий каталог — возможно нужна checkout)")
            result = "\n".join(entries) if entries else "(пустая директория)"
            logger.log(f"tool: list_files {rel_path} ({len(entries)} entries)", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"path": rel_path}, "status": "done"})
            await _send_log(websocket, f"📁 Список файлов: {rel_path} ({len(entries)} элементов)", "info")
            try: asyncio.create_task(save_tool_usage(None, "", "", "list_files", json.dumps({"path": rel_path}), "done", duration_ms=int((time.time()-_tool_start)*1000)))
            except Exception: pass
            return result

        elif name == "search_files":
            pattern = arguments.get("pattern", "")
            file_pattern = arguments.get("file_pattern", "*")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            results = []
            try:
                for root, dirs, files in os.walk(project_path):
                    dirs[:] = [d for d in dirs if d not in {"venv", "__pycache__", "node_modules", ".git", ".cache", ".venv", "dist", "build", ".next"}]
                    for f in files:
                        if file_pattern != "*" and not fnmatch.fnmatch(f, file_pattern):
                            continue
                        fpath = os.path.join(root, f)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                                for i, line in enumerate(fh, 1):
                                    try:
                                        if re.search(pattern, line, re.IGNORECASE):
                                            rel = os.path.relpath(fpath, project_path)
                                            results.append(f"{rel}:{i}: {line.strip()[:120]}")
                                            if len(results) >= 30:
                                                break
                                    except re.error:
                                        # Invalid regex — fall back to substring match
                                        if pattern.lower() in line.lower():
                                            rel = os.path.relpath(fpath, project_path)
                                            results.append(f"{rel}:{i}: {line.strip()[:120]}")
                                            if len(results) >= 30:
                                                break
                        except Exception:
                            continue
                    if len(results) >= 30:
                        break
            except Exception as e:
                return f"Ошибка поиска: {e}"
            if not results:
                result = f"Совпадений для '{pattern}' не найдено"
            else:
                result = f"Найдено {len(results)} совпадений:\n" + "\n".join(results[:30])
            logger.log(f"tool: search_files '{pattern}' -> {len(results)} results", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"pattern": pattern}, "status": "done"})
            await _send_log(websocket, f"🔍 Поиск '{pattern}': {len(results)} совпадений", "info")
            try: asyncio.create_task(save_tool_usage(None, "", "", "search_files", json.dumps({"pattern": pattern}), "done", duration_ms=int((time.time()-_tool_start)*1000)))
            except Exception: pass
            return result

        elif name == "execute_command":
            command = arguments.get("command", "")
            if not command.strip():
                return "Ошибка: пустая команда"
            # Проверяем cwd для git и других команд, требующих директорию
            exec_cwd = project_path
            if not exec_cwd and any(cmd in command.lower() for cmd in ["git ", "git\n", "npm ", "pip ", "python "]):
                return f"Ошибка: нет пути к проекту (project_path is None). Команда '{command[:50]}' требует рабочую директорию. Откройте проект перед выполнением."
            logger.log(f"tool: execute_command '{command[:100]}'", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"command": command}, "status": "running"})
            await _send_log(websocket, f"⚡ Выполняю: $ {command[:120]}", "command")
            try: asyncio.create_task(save_tool_usage(None, "", "", "execute_command", json.dumps({"command": command[:200]}), "done", duration_ms=int((time.time()-_tool_start)*1000)))
            except Exception: pass
            result = await executor.execute(command, cwd=project_path, need_approval=False, timeout=60)
            output = ""
            if result.get("stdout"):
                output += result["stdout"][:5000]
            if result.get("stderr"):
                output += ("\n" if output else "") + result["stderr"][:2000]
            if not output:
                output = "(команда выполнена без вывода)"
            exit_code = result.get("exit_code", -1)
            status = "OK" if exit_code == 0 else f"exit code {exit_code}"
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"command": command}, "status": "done", "exit_code": exit_code})
            log_level = "success" if exit_code == 0 else "error"
            log_text = f"Команда завершена ({status})" if exit_code == 0 else f"Команда: {status}"
            if output:
                preview = output.strip().split('\n')[0][:100]
                log_text += f" → {preview}"
            await _send_log(websocket, log_text, log_level)
            return f"[{status}]\n{output}"

        elif name == "git_commit_push":
            message = arguments.get("message", "update")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            logger.log(f"tool: git_commit_push '{message}'", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"message": message}, "status": "running"})
            await _send_log(websocket, f"🚀 Git commit: {message}", "command")
            try: asyncio.create_task(save_tool_usage(None, "", "", "git_commit_push", json.dumps({"message": message}), "done", duration_ms=int((time.time()-_tool_start)*1000)))
            except Exception: pass
            # Stage all
            r1 = await executor.execute("git add -A", cwd=project_path, need_approval=False)
            # Commit
            safe_msg = shlex.quote(message)
            r2 = await executor.execute(f'git commit -m "{safe_msg}" --allow-empty', cwd=project_path, need_approval=False)
            commit_out = r2.get("stdout", "") + r2.get("stderr", "")
            # Push with project PAT token if available
            project_token = await get_project_token_by_path(project_path)
            push_out = await git_push_with_token(executor, project_path, project_token)
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"message": message}, "status": "done"})
            await _send_log(websocket, f"🚀 Push: {push_out.strip()[:100]}", "success")
            return f"Commit: {commit_out.strip()}\nPush: {push_out.strip()}"

        elif name == "git_clone":
            repo_url = arguments.get("repo_url", "")
            if not repo_url:
                return "Ошибка: не указан URL репозитория"
            if not project_path:
                return "Ошибка: нет пути к проекту"
            logger.log(f"tool: git_clone '{repo_url}'", level="info", source="agent")
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"repo_url": repo_url}, "status": "running"})
            await _send_log(websocket, f"📦 Git clone: {repo_url}", "command")
            try: asyncio.create_task(save_tool_usage(None, "", "", "git_clone", json.dumps({"repo_url": repo_url}), "done", duration_ms=int((time.time()-_tool_start)*1000)))
            except Exception: pass
            project_token = await get_project_token_by_path(project_path)
            result = await git_clone_with_token(executor, project_path, repo_url, token=project_token)
            await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "args": {"repo_url": repo_url}, "status": "done"})
            if result.get("success"):
                await _send_log(websocket, f"✅ Clone OK: {result.get('output', '')[:100]}", "success")
                return f"Репозиторий склонирован: {result.get('output', '')}"
            else:
                err = result.get("error", "Unknown error")
                await _send_log(websocket, f"❌ Clone fail: {err[:150]}", "error")
                return f"Ошибка клонирования: {err}"

        else:
            return f"Неизвестный инструмент: {name}"

    except Exception as e:
        logger.log(f"tool_error: {name} -> {str(e)[:200]}", level="error", source="agent")
        await safe_ws_send(websocket, {"type": "tool_call", "tool": name, "status": "error", "error": str(e)[:200]})
        await _send_log(websocket, f"❌ Ошибка {name}: {str(e)[:150]}", "error")
        try: asyncio.create_task(save_tool_usage(None, "", "", name, "", "error", duration_ms=int((time.time()-_tool_start)*1000)))
        except Exception: pass
        return f"Ошибка выполнения {name}: {str(e)[:500]}"


# ═══════════════════════════════════════════════════════
# LLM STREAMING WITH TOOL CALLING
# ═══════════════════════════════════════════════════════

def _resolve_model(model_id: str) -> tuple[str, str, str, bool]:
    """Resolve model_id to (litellm_model, api_key, api_base, is_thinking)."""
    api_key = CONFIG["llm"].get("api_key", "")
    api_base = CONFIG["llm"].get("api_base", "")
    is_thinking = False

    if "YOUR_" in api_key.upper():
        api_key = ""

    model_config = keys_manager.get_model_config(model_id)
    if model_config:
        model = model_config["model"]
        api_key = model_config["api_key"]
        api_base = model_config.get("api_base", "")
        is_thinking = model_config.get("thinking", False)
    else:
        model = model_id
        # Fallback: ищем модель по списку провайдеров
        bare_name = model.split("/")[-1]
        for provider_id, config in keys_manager.providers.items():
            if config.get("status") in ("valid", "rate_limited") and config.get("api_key"):
                for model_name in config.get("models", []):
                    if model_name == bare_name:
                        prefix = config.get("litellm_prefix", provider_id)
                        model = f"{prefix}/{model_name}"
                        api_key = config["api_key"]
                        api_base = config.get("api_base", "")
                        break
                if api_key:
                    break

    return model, api_key, api_base, is_thinking


async def stream_llm_response(prompt: str, history: list, websocket,
                              model: str = None, system_prompt: str = None,
                              project_path: str | None = None, use_tools: bool = True,
                              _error_info: dict = None, _cancel_check=None):
    """Stream AI response with tool calling support. Loops until no more tool calls.
    _cancel_check: optional callable() -> bool, if True → abort immediately."""
    if model is None:
        model = CONFIG["llm"].get("default_model")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="", project_context="", compressed_context="", platform_info=get_platform_info())

    model, api_key, api_base, is_thinking = _resolve_model(model)

    if not api_key:
        logger.log(f"no_api_key: {model}", level="error", source="agent")
        print(f"  [agent] ERROR: no_api_key for model={model}")
        await _send_log(websocket, f"❌ Нет API ключа для {model}", "error")
        return None

    print(f"  [agent] stream_llm_response: model={model}, has_key=True, tools={'ON' if use_tools else 'OFF'}, thinking={is_thinking}, project={project_path}, api_base={'yes' if api_base else 'no'}")
    await _send_log(websocket, f"🧠 Модель: {model}", "info")

    # Extended Thinking: уведомить клиента и настроить параметры
    if is_thinking:
        await safe_ws_send(websocket, {"type": "auto_log", "content": "Extended Thinking включён — модель будет рассуждать глубже", "level": "info"})

    # Build messages
    api_messages = []
    role_map = {"ai": "assistant", "system": "system", "user": "user"}
    for msg in history:
        mapped_role = role_map.get(msg.get("role", ""), msg.get("role", "user"))
        api_messages.append({"role": mapped_role, "content": msg.get("content", "")})
    messages = [{"role": "system", "content": system_prompt}] + api_messages + [{"role": "user", "content": prompt}]

    print(f"  [agent] messages_count={len(messages)}, system_prompt_len={len(system_prompt)}")

    # Anthropic Claude 4+ не поддерживает temperature — убираем для этих моделей
    anthropic_no_temp_models = ("claude-opus-4-", "claude-sonnet-4-")
    skip_temperature = any(m in model.lower() for m in anthropic_no_temp_models)

    start_time = time.time()
    full_response = ""
    max_tool_iterations = 10  # Prevent infinite loops

    for iteration in range(max_tool_iterations):
        # Check cancellation before each iteration
        if _cancel_check and _cancel_check():
            await safe_ws_send(websocket, {"type": "generation_stopped", "content": "⏹ Генерация остановлена"})
            await _send_log(websocket, "⏹ Генерация отменена пользователем", "warning")
            return None

        current_tool_calls = {}  # Accumulate tool call chunks: {index: {id, name, arguments}}
        current_content = ""

        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": CONFIG["llm"].get("max_tokens", 16384) if is_thinking else CONFIG["llm"].get("max_tokens", 4096),
                "stream": True,
            }
            if not skip_temperature:
                kwargs["temperature"] = CONFIG["llm"].get("temperature", 0.2)
            if api_key:
                kwargs["api_key"] = api_key
            if api_base and not api_base.startswith("https://api.anthropic.com") and not api_base.startswith("https://api.openai.com/v1") and not api_base.startswith("https://api.x.ai/v1"):
                kwargs["api_base"] = api_base

            # Add tools if supported by the model
            if use_tools:
                kwargs["tools"] = TOOLS

            # Check if model supports tool calling
            # Some models may not support tools
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as tool_err:
                err_str = str(tool_err).lower()
                # Only retry without tools if the error is SPECIFICALLY about tool calling
                # Don't match generic words like "function" or "parameter" that appear in auth/401 errors
                _tool_only_patterns = [
                    "does not support tool", "tools are not supported",
                    "tool use is not enabled", "tool_choice", "tool calling",
                    "does not support function calling", "invalid tool",
                    "unknown tool", "unsupported tool",
                ]
                if any(p in err_str for p in _tool_only_patterns):
                    print(f"  [agent] Tools not supported by {model}, retrying without tools")
                    await _send_log(websocket, f"⚠️ {model} не поддерживает tools — продолжаю без инструментов", "warning")
                    use_tools = False
                    kwargs.pop("tools", None)
                    # Signal caller that tools are not supported (for prompt injection fallback)
                    if _error_info is not None:
                        _error_info["no_tools"] = True
                    response = await litellm.acompletion(**kwargs)
                else:
                    raise

            # Process streamed chunks
            finish_reason = None
            async for chunk in response:
                # Check cancellation during streaming
                if _cancel_check and _cancel_check():
                    await safe_ws_send(websocket, {"type": "generation_stopped", "content": "⏹ Генерация остановлена"})
                    await _send_log(websocket, "⏹ Генерация отменена пользователем", "warning")
                    return full_response or None

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Stream content chunks to client in real-time
                if delta.content:
                    await safe_ws_send(websocket, {"type": "chunk", "content": delta.content})
                    full_response += delta.content
                    current_content += delta.content

                # Accumulate tool calls from streaming chunks
                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": ""
                            }
                        if tc.id:
                            current_tool_calls[idx]["id"] = tc.id
                        if tc.function:
                            if hasattr(tc.function, 'name') and tc.function.name:
                                current_tool_calls[idx]["name"] = tc.function.name
                            if hasattr(tc.function, 'arguments') and tc.function.arguments:
                                current_tool_calls[idx]["arguments"] += tc.function.arguments

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            # After stream ends, check if we need to handle tool calls
            if finish_reason == "tool_calls" and current_tool_calls:
                # Reconstruct the assistant message with tool calls
                tool_calls_list = []
                for idx in sorted(current_tool_calls.keys()):
                    tc = current_tool_calls[idx]
                    tool_calls_list.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"]
                        }
                    })

                assistant_msg = {"role": "assistant", "content": current_content or None, "tool_calls": tool_calls_list}
                messages.append(assistant_msg)

                # Execute each tool call
                for tc_data in tool_calls_list:
                    fn_name = tc_data["function"]["name"]
                    try:
                        fn_args = json.loads(tc_data["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    await safe_ws_send(websocket, {
                        "type": "tool_call",
                        "tool": fn_name,
                        "args": fn_args,
                        "status": "running"
                    })

                    result = await execute_tool(fn_name, fn_args, project_path, websocket)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "content": result
                    })

                # Continue loop — LLM will process tool results and respond
                continue

            # Normal text response completed (or finish_reason is stop/length/end_turn)
            duration = (time.time() - start_time) * 1000
            tokens = len(full_response) // 4
            await safe_ws_send(websocket, {"type": "done", "tools_used": iteration, "duration_ms": int(duration), "tokens": tokens})
            logger.ai_response(model=model, tokens=tokens, success=True, duration_ms=duration)
            try: asyncio.create_task(save_model_usage(None, "", model, "", "", total_tokens=tokens, duration_ms=int(duration), success=True))
            except Exception: pass
            await _send_log(websocket, f"✅ Ответ от {model}: {len(full_response)} симв., {(duration/1000):.1f}с", "success")
            return full_response

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            import traceback
            error_msg = str(e)
            print(f"  [agent] stream_llm_response ERROR: model={model}, error={error_msg[:300]}")
            print(f"  [agent] traceback: {traceback.format_exc()[-1500:]}")
            if "401" in error_msg:
                error_msg = "Ошибка 401: Неверный API ключ."
            elif "429" in error_msg:
                error_msg = "Ошибка 429: Лимит запросов исчерпан."
            elif "402" in error_msg or "insufficient credits" in error_msg.lower():
                error_msg = "Ошибка 402: Недостаточно кредитов."
            elif "500" in error_msg:
                error_msg = "Ошибка 500: Сервер ИИ недоступен."
            elif "timeout" in error_msg.lower():
                error_msg = "Таймаут модели. Попробуйте другую."
            else:
                error_msg = f"Ошибка ИИ: {error_msg}"
            logger.ai_response(model=model, success=False, error=error_msg, duration_ms=duration)
            try: asyncio.create_task(save_model_usage(None, "", model, "", "", duration_ms=int(duration), success=False))
            except Exception: pass
            # Ошибки API (401/429/402/500) — ТОЛЬКО в панель логов, НЕ на главный экран
            await _send_log(websocket, f"❌ {model}: {error_msg}", "error")
            # Signal 402/credit errors to caller for provider skipping in fallback
            _no_credits_kw = ("402" in error_msg or "insufficient credits" in error_msg.lower()
                              or "credit balance" in error_msg.lower())
            if _error_info is not None and _no_credits_kw:
                _error_info["no_credits"] = True
            return None

    # Max iterations reached — return accumulated response even if empty
    await safe_ws_send(websocket, {"type": "done", "tools_used": max_tool_iterations, "duration_ms": int((time.time()-start_time)*1000), "tokens": len(full_response)//4})
    await _send_log(websocket, f"⚠️ Достигнут лимит {max_tool_iterations} итераций tool calling", "warning")
    try: asyncio.create_task(save_model_usage(None, "", model, "", "", total_tokens=len(full_response)//4, duration_ms=int((time.time()-start_time)*1000), tool_calls_count=max_tool_iterations, success=True))
    except Exception: pass
    return full_response or "⚠️ Лимит итераций tool calling достигнут. Задача слишком сложная для одного запроса — попробуйте разбить на шаги."


def _get_priority_models(project: dict) -> list[str]:
    """Extract up to 10 priority model IDs from project's selected_models."""
    if not project or not project.get("selected_models"):
        return []
    try:
        models = json.loads(project["selected_models"])
        if isinstance(models, list):
            return models[:10]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _build_models_to_try(
    model_id: str | None,
    all_models: list[dict],
    project: dict | None = None,
) -> list[str]:
    """Единая функция построения списка моделей для попытки (DRY).

    Логика:
    1. Если model_id задан — эта модель ПЕРВАЯ, затем проверенные как fallback
    2. Если не задан — приоритетные модели проекта + проверенные (probed)
    3. Если НЕТ проверенных — fallback на любые валидные модели с API-ключом
    4. Фильтры: status valid/available/rate_limited, skip free+no_key/local без base_url,
       skip failed_probe_ids

    Returns: list[model_id, ...]
    """
    models_to_try: list[str] = []
    seen: set[str] = set()

    def _is_usable(m: dict) -> bool:
        """Проверяет, что модель пригодна для использования."""
        mid = m["id"]
        if mid in seen:
            return False
        status = m.get("status", "")
        if status not in ("valid", "available", "rate_limited"):
            return False
        if mid in keys_manager._failed_probe_ids:
            return False
        if _is_no_credits_provider(mid):
            return False
        if m.get("type") == "free" and not m.get("api_key"):
            return False
        if m.get("type") == "local" and not m.get("base_url"):
            return False
        # Модель должна иметь API-ключ (кроме local с base_url)
        mc = keys_manager.get_model_config(mid)
        if not mc or not mc.get("api_key"):
            if m.get("type") != "local":
                return False
        return True

    def _add_probed_models(existing: list[str]) -> list[str]:
        """Добавить проверенные модели к списку (без дублей)."""
        result = list(existing)
        for m in all_models:
            mid = m["id"]
            if mid in result:
                continue
            if mid not in keys_manager._probed_model_ids:
                continue
            if _is_usable(m):
                result.append(mid)
                seen.add(mid)
        return result

    def _add_any_valid_models(existing: list[str]) -> list[str]:
        """Fallback: добавить ЛЮБЫЕ валидные модели (когда нет probed)."""
        result = list(existing)
        for m in all_models:
            mid = m["id"]
            if mid in result or mid in seen:
                continue
            if _is_usable(m):
                result.append(mid)
                seen.add(mid)
        return result

    if model_id:
        # Указанная модель — первая, но с fallback на проверенные
        # Если модель от провайдера без кредитов — пропускаем её, только fallback
        if _is_no_credits_provider(model_id):
            print(f"  [agent] _build_models_to_try: skip {model_id} (no credits provider), fallback only")
            return _add_probed_models([])
        return _add_probed_models([model_id])

    # Приоритетные модели проекта
    for pm in _get_priority_models(project):
        models_to_try.append(pm)
        seen.add(pm)

    # Проверенные (probed) модели
    models_to_try = _add_probed_models(models_to_try)

    # FALLBACK: если нет проверенных моделей — пробуем любые валидные с API-ключом
    if not models_to_try:
        models_to_try = _add_any_valid_models(models_to_try)

    return models_to_try


# ═══════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════

# Глобальный список чекпоинтов (используется из run.py)
_checkpoints: list[dict] = []


async def handle_chat_message(prompt: str, project_id, repo_map: str | None, websocket, model_id: str = None, _cancel_check=None, _silent=False, active_skills: list[str] | None = None):
    """Main entry point: get history, build context, stream response with tool calling and fallback."""
    print(f"  [agent] handle_chat_message: prompt='{prompt[:80]}', project_id={project_id}, model_id={model_id}, skills={active_skills}")
    history = await get_history(project_id)

    # Project context
    project_context_text = ""
    project_path = None
    if project_id:
        project = await get_project(project_id)
        if project:
            project_context_text += f"ПРОЕКТ: {project.get('name', 'Без названия')}\n"
            if project.get("description"):
                project_context_text += f"ОПИСАНИЕ: {project['description']}\n"
            if project.get("template"):
                project_context_text += f"ШАБЛОН/ТЕХНОЛОГИЯ: {project['template']}\n"
            if project.get("base_prompt"):
                project_context_text += f"ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ: {project['base_prompt']}\n"
            # GitHub-инфо для git операций (клонирование с токеном)
            if project.get("github_repo"):
                project_context_text += f"GITHUB_REPO: {project['github_repo']}\n"
                gh_token = project.get("github_token", "")
                if gh_token:
                    # Показываем токен модели для git clone/push — токен уже из БД
                    project_context_text += f"GITHUB_TOKEN: {gh_token}\n"
                    project_context_text += "ПРИМЕЧАНИЕ: Для git clone используй URL с токеном: https://{GITHUB_TOKEN}@github.com/{owner}/{repo}.git\n"
                    project_context_text += "НЕ пытайся клонировать без токена — сначала попробуй с токеном.\n"
            if project.get("path"):
                project_path = project["path"]
                # Validate path exists — try to self-heal if broken
                if not os.path.isdir(project_path):
                    # Try to create the directory
                    try:
                        os.makedirs(project_path, exist_ok=True)
                        await _send_log(websocket, f"✅ Создан каталог проекта: {project_path}", "success")
                    except OSError:
                        # Try fallback path
                        from core.memory import CONFIG
                        fallback = os.path.join(
                            CONFIG["system"].get("projects_dir", "./projects"),
                            os.path.basename(project_path.rstrip("/")) or project.get("name", "project").lower()
                        )
                        fallback = os.path.normpath(fallback)
                        try:
                            os.makedirs(fallback, exist_ok=True)
                            project_path = fallback
                            # Update DB with new path
                            from core.memory import async_session
                            from sqlalchemy import text
                            async with async_session() as session:
                                async with session.begin():
                                    await session.execute(
                                        text("UPDATE projects SET path = :p WHERE id = :id"),
                                        {"p": fallback, "id": project_id}
                                    )
                            await _send_log(websocket, f"🔄 Путь проекта исправлен: {project['path']} → {fallback}", "warning")
                        except Exception:
                            warn_msg = f"⚠️ Путь проекта не существует: {project_path}. Файловые операции будут недоступны."
                            print(f"  [agent] {warn_msg}")
                            await _send_log(websocket, warn_msg, "error")
                project_context_text += f"ПУТЬ К ПРОЕКТУ: {project_path}\n"
                if os.path.isdir(project_path):
                    try:
                        file_list = []
                        skip_dirs = {"venv", "__pycache__", "node_modules", ".git", ".cache", "__pypackages__", ".venv", "env", ".idea", ".vscode", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", "target", "bin", "obj", ".next", ".nuxt", ".gradle"}
                        for root, dirs, files in os.walk(project_path):
                            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
                            level = root.replace(project_path, "").count(os.sep)
                            indent = "  " * level
                            dir_name = os.path.basename(root) or project.get("name", "project")
                            file_list.append(f"{indent}{dir_name}/")
                            sub_indent = "  " * (level + 1)
                            for f in sorted(files)[:10]:
                                if f.startswith(".") or f.endswith((".pyc", ".class")):
                                    continue
                                file_list.append(f"{sub_indent}{f}")
                        if file_list:
                            if len(file_list) > 80:
                                file_list = file_list[:80]
                                file_list.append("  ... (и другие файлы)")
                            project_context_text += f"\nФАЙЛЫ ПРОЕКТА:\n" + "\n".join(file_list) + "\n"
                    except Exception:
                        pass

    # Auto-compression
    compressed_context_text = ""
    compressor = ContextCompressor()
    if project_id and compressor.should_compress(history):
        try:
            comp_model = ContextCompressor.get_compression_model_config()
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Автосжатие контекста...", "level": "info"})
            compressed_summary, remaining, was_llm = await compressor.compress_and_cleanup(history, project_id, model_config=comp_model)
            if compressed_summary:
                compressed_context_text = compressed_summary
                method = "LLM" if was_llm else "regex"
                removed = len(history) - len(remaining)
                await safe_ws_send(websocket, {"type": "auto_log", "content": f"Автосжатие ({method}): {removed} сообщений сжато", "level": "info"})
                history = remaining
        except Exception as e:
            print(f"  [agent] compression error: {e}")

    repo_map_text = f"СТРУКТУРА ПРОЕКТА (Repo Map):\n{repo_map}" if repo_map else ""

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        repo_map=repo_map_text,
        ideas_context="",
        project_context=project_context_text,
        compressed_context="",
        platform_info=get_platform_info(),
    )
    if compressed_context_text:
        system_prompt = compressor.build_compressed_system_prompt(system_prompt, compressed_context_text)

    # Инжектируем контекст активных навыков
    skill_context = _load_skill_context(active_skills)
    if skill_context:
        system_prompt += skill_context

    await save_message(project_id, "user", prompt)

    # Build model list — единая логика через _build_models_to_try()
    all_models = keys_manager.get_all_models()
    project = await get_project(project_id) if project_id else None
    models_to_try = _build_models_to_try(model_id, all_models, project)

    if not models_to_try:
        await _send_log(websocket, "❌ Нет доступных моделей. Добавьте API-ключ или опросите модели.", "error")
        await safe_ws_send(websocket, {"type": "done", "tools_used": 0, "duration_ms": 0, "tokens": 0})
        return

    # Автоперевалидация rate_limited провайдеров перед первой попыткой
    validated_providers = set()
    for m_id in models_to_try:
        if "__" in m_id:
            pid = m_id.split("__")[0]
        elif m_id.startswith("local_") or m_id.startswith("custom_"):
            continue
        else:
            # bare model name — skip
            continue
        if pid not in validated_providers:
            config = keys_manager.providers.get(pid, {})
            if config.get("status") == "rate_limited":
                new_status = await keys_manager.ensure_provider_active(pid)
                if new_status == "valid":
                    await _send_log(websocket, f"✓ {pid} перевалидирован и активен", "success")
            validated_providers.add(pid)

    # Try each model
    ai_response = None
    tried_count = 0
    # no_credits фильтрация через глобальный кэш _no_credits_providers (в _is_usable)

    for i, model_to_try in enumerate(models_to_try):
        model_config = keys_manager.get_model_config(model_to_try)
        if not model_config:
            continue
        has_key = bool(model_config.get("api_key"))
        is_local = any(m["id"] == model_to_try and m.get("type") == "local" for m in all_models)
        if not has_key and not is_local:
            continue

        # Двойная проверка no_credits (уже в _is_usable, но на всякий случай)
        model_provider = model_config.get("provider", "")
        if _is_no_credits_provider(model_to_try):
            print(f"  [agent] skipping #{i} {model_to_try} — provider has no credits (global cache)")
            continue

        tried_count += 1
        print(f"  [agent] trying model #{i}: {model_to_try}")

        # Send display name (not raw ID) to frontend
        m_info = next((m for m in all_models if m["id"] == model_to_try), None)
        display_model = m_info["name"] if m_info else model_to_try
        await safe_ws_send(websocket, {"type": "typing", "model": display_model})

        if i > 0:
            m_info = next((m for m in all_models if m["id"] == model_to_try), None)
            model_name = m_info["name"] if m_info else model_to_try
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Переключаюсь на {model_name} (попытка {tried_count})...", "level": "info"})
            await _send_log(websocket, f"🔄 Переключаюсь на {model_name} (попытка {tried_count})", "warning")

        error_info = {}
        ai_response = await stream_llm_response(
            prompt, history, websocket,
            model=model_to_try, system_prompt=system_prompt,
            project_path=project_path, use_tools=True,
            _error_info=error_info, _cancel_check=_cancel_check
        )
        print(f"  [agent] chat: model {model_to_try} result={'OK' if ai_response else 'FAILED'}, no_tools={error_info.get('no_tools')}, no_credits={error_info.get('no_credits')}")
        if error_info.get("no_credits") and model_provider:
            _mark_no_credits(model_provider)
            await _send_log(websocket, f"⏭️ Пропускаю {model_provider} (нет кредитов, кэш 5 мин)", "warning")
        # Если модель упала и есть ещё модели в списке — продолжаем с fallback
        if ai_response is None and i == 0 and len(models_to_try) > 1:
            await _send_log(websocket, f"⚠️ {display_model} не ответила, пробую следующую модель...", "warning")
        if ai_response is not None:
            # Prompt injection fallback: model doesn't support tools but we have a project
            if error_info.get("no_tools") and project_path:
                await _send_log(websocket, f"🔄 {display_model}: переключаюсь на prompt injection", "warning")
                injection_result = await stream_with_prompt_injection(
                    prompt, history, websocket,
                    model=model_to_try,
                    project_path=project_path,
                    project_id=project_id,
                    repo_map=repo_map,
                )
                if injection_result is not None:
                    ai_response = injection_result
                # If injection failed, keep the raw response as fallback
            break

    if ai_response is not None:
        # ai_response может быть пустой строкой "" (при лимите tool calls) — это не ошибка
        await save_message(project_id, "ai", ai_response)
    elif tried_count == 0:
        await safe_ws_send(websocket, {"type": "error", "content": "Нет модели с API ключом. Проверьте настройки API."})
        await _send_log(websocket, "❌ Нет модели с API ключом", "error")
    else:
        total_available = len(models_to_try)
        if tried_count < total_available:
            skipped = total_available - tried_count
            await safe_ws_send(websocket, {"type": "error", "content": f"{tried_count} из {total_available} моделей не ответили ({skipped} пропущено). Попробуйте позже или выберите другую."})
        else:
            await safe_ws_send(websocket, {"type": "error", "content": f"Все {tried_count} моделей не ответили. Проверьте API ключи."})
        await _send_log(websocket, f"❌ Модели не ответили: {tried_count}/{total_available}", "error")


# ═══════════════════════════════════════════════════════
# HUB MODE — Главный экран (без проекта, со скиллами)
# ═══════════════════════════════════════════════════════

HUB_SYSTEM_PROMPT = """Ты Fosved Coder — AI-ассистент для подготовки проектов.
Ты находишься на ГЛАВНОМ ЭКРАНЕ — это место для обсуждения идей, подготовки промптов и создания проектов.

Твои задачи:
- Обсуждать идеи пользователя и помогать их развивать
- Задавать уточняющие вопросы для формирования полного ТЗ
- Помогать создавать профессиональный промпт для проекта (оценка 10/10)
- При необходимости подключать скиллы для enriching контекста

Доступные скиллы (модель сама решает когда подключить):
- charts — генерация графиков, диаграмм, ER-схем
- web-search — поиск информации в интернете
- image-generation — генерация изображений (логотипы, мокапы)
- pdf — генерация PDF документов
- docx — генерация Word документов
- xlsx — генерация таблиц Excel
- ASR — распознавание речи
- VLM — анализ изображений

Для активации скилла используй: <skill name="название_скилла">описание задачи</skill>

Когда пользователь готов создать проект, помоги сформировать:
1. Название проекта
2. Описание
3. Шаблон (FastAPI, React, Next.js, Expo, Flask, Python CLI)
4. Детальное описание дизайна (цвета, стиль, тема, шрифты)
5. GitHub репозиторий (если есть)
6. Базовый промпт (инструкции для ИИ при работе в проекте)

{platform_info}

Правила:
- Отвечай на том языке, на котором задан вопрос
- Будь проактивным — предлагай идеи и уточнения
- Если идея неполная — задай вопросы чтобы дополнить
- Стремись создать промпт на 10/10 — максимально подробный и конкретный
"""


# ═══════════════════════════════════════════════════════
# DYNAMIC QUESTIONNAIRE — Опросный лист для создания проекта
# ═══════════════════════════════════════════════════════

QUESTIONNAIRE_SYSTEM_PROMPT = """Ты Fosved Coder — аналитик требований проекта.
Твоя задача — провести краткий опрос пользователя (3-5 вопросов) для формирования карточки проекта.

ПРАВИЛА:
1. Задавай по ОДНОМУ вопросу за раз — жди ответа перед следующим
2. Вопросы должны быть адаптивными — уточняй на основе предыдущих ответов
3. Не спрашивай то, что уже понятно из описания пользователя
4. Вопросы должны быть сфокусированными и конкретными (не абстрактными)

ТИПЫ ВОПРОСОВ (выбирай адаптивно):
- Если неясна цель → «Какую основную проблему решает проект?»
- Если неясна аудитория → «Кто будет пользоваться проектом?»
- Если неясен стек → «Есть ли предпочтения по технологиям?»
- Если неясен дизайн → «Есть ли примеры дизайна или референсы?»
- Если неясен масштаб → «Какой масштаб проекта (MVP / полноценный продукт)?»
- Если неясна интеграция → «Нужна ли интеграция с внешними сервисами (GitHub, БД, API)?»
- Если неясен дизайн → «Какой стиль предпочитаете (минимализм, корпоративный, яркий)?»

КОГДА ВСЕ ВОПРОСЫ ОТВЕЧЕНЫ:
Сгенерируй карточку проекта в формате JSON (без markdown обёрток):
```json
{
  "name": "Короткое название проекта",
  "description": "Подробное описание (2-3 предложения)",
  "template": "Технологический стек (напр. FastAPI + React, Next.js, Python CLI, Flask)",
  "design": "Описание дизайна (цвета, стиль, тема, шрифты)",
  "base_prompt": "Детальные инструкции для ИИ-ассистента при работе в проекте",
  "github_repo": "URL GitHub репозитория (если есть, иначе пустая строка)"
}
```

ПРИМЕЧАНИЯ:
- Отвечай на том языке, на котором общается пользователь
- base_prompt должен быть максимально подробным — это основная инструкция для ИИ
- Если пользователь сам предоставил достаточно информации — пропусти лишние вопросы и сразу выдай JSON
- Если пользователь отвечает «не знаю» / «без разницы» — выбери оптимальный вариант и объясни почему
"""


async def handle_hub_message(prompt: str, websocket, model_id: str = None, _cancel_check=None, active_skills: list[str] | None = None):
    """
    Обработчик сообщений ГЛАВНОГО ЭКРАНА.
    Нет контекста проекта, нет инструментов (read/write/execute).
    Только чат с ИИ + опросный лист + скиллы.
    """
    print(f"  [agent] handle_hub_message: prompt='{prompt[:80]}', model_id={model_id}, skills={active_skills}")
    from core.memory import get_history as get_hub_history

    # История с project_id=None — чат главного экрана
    history = await get_hub_history(None, limit=50)

    system_prompt = HUB_SYSTEM_PROMPT.format(platform_info=get_platform_info())

    # Инжектируем контекст активных навыков
    skill_context = _load_skill_context(active_skills)
    if skill_context:
        system_prompt += skill_context

    await save_message(None, "user", prompt)

    # Build model list — единая логика через _build_models_to_try()
    all_models = keys_manager.get_all_models()
    models_to_try = _build_models_to_try(model_id, all_models)

    if not models_to_try:
        print(f"  [agent] hub: NO models available! all_models={len(all_models)}")
        await safe_ws_send(websocket, {"type": "error", "content": "Нет доступных моделей. Добавьте API ключ."})
        return

    print(f"  [agent] hub: models_to_try={len(models_to_try)}, first={models_to_try[0] if models_to_try else 'none'}")
    ai_response = None
    tried_count = 0
    # no_credits фильтрация через глобальный кэш _no_credits_providers

    for i, model_to_try in enumerate(models_to_try):
        model_config = keys_manager.get_model_config(model_to_try)
        if not model_config:
            continue
        has_key = bool(model_config.get("api_key"))
        is_local = any(m["id"] == model_to_try and m.get("type") == "local" for m in all_models)
        if not has_key and not is_local:
            continue

        # Двойная проверка no_credits
        model_provider = model_config.get("provider", "")
        if _is_no_credits_provider(model_to_try):
            print(f"  [agent] hub: skip {model_to_try} (no credits provider)")
            continue

        tried_count += 1
        print(f"  [agent] hub: trying #{i}: {model_to_try} (key={has_key}, local={is_local})")
        m_info = next((m for m in all_models if m["id"] == model_to_try), None)
        display_model = m_info["name"] if m_info else model_to_try
        await safe_ws_send(websocket, {"type": "typing", "model": display_model})

        if i > 0:
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Переключаюсь на {display_model} (попытка {tried_count})...", "level": "info"})

        error_info = {}
        ai_response = await stream_llm_response(
            prompt, history, websocket,
            model=model_to_try,
            system_prompt=system_prompt,
            project_path=None,
            use_tools=False,  # На главном экране инструментов НЕТ
            _error_info=error_info,
            _cancel_check=_cancel_check,
        )
        print(f"  [agent] hub: model {model_to_try} result={'OK' if ai_response else 'FAILED'}")
        if error_info.get("no_credits") and model_provider:
            _mark_no_credits(model_provider)
        if ai_response is not None:
            break

    if ai_response:
        await save_message(None, "ai", ai_response)
        # Проверяем есть ли <skill> теги в ответе — парсим и выполняем
        await _process_skill_tags(ai_response, websocket)
    elif tried_count == 0:
        await safe_ws_send(websocket, {"type": "error", "content": "Нет доступных моделей. Добавьте API ключ."})
        await _send_log(websocket, "❌ Нет модели с API ключом", "error")
    elif tried_count == 1:
        await safe_ws_send(websocket, {"type": "error", "content": "Выбранная модель не ответила. Попробуйте другую."})
        await _send_log(websocket, "❌ Модель не ответила", "error")
    else:
        await safe_ws_send(websocket, {"type": "error", "content": f"Все {tried_count} моделей не ответили."})
        await _send_log(websocket, f"❌ Все {tried_count} моделей не ответили", "error")


async def _process_skill_tags(response_text: str, websocket):
    """
    Парсит ответ модели на наличие <skill name="...">...</skill> тегов
    и отправляет информацию о запрошенных скиллах на клиент.
    Клиент сам решает как выполнить скилл.
    """
    skill_pattern = re.compile(r'<skill\s+name\s*=\s*["\']([^"\']+)["\']\s*>(.*?)</skill>', re.DOTALL)
    matches = skill_pattern.findall(response_text)

    for skill_name, task_desc in matches:
        skill_name = skill_name.strip()
        task_desc = task_desc.strip()
        logger.log(f"hub: skill requested: {skill_name} — {task_desc[:80]}", level="info", source="agent")
        await safe_ws_send(websocket, {
            "type": "skill_request",
            "skill": skill_name,
            "task": task_desc,
        })
        await _send_log(websocket, f"🧩 Скилл запрошен: {skill_name}", "info")


# ═══════════════════════════════════════════════════════
# PROMPT INJECTION STREAMING — Dual mode for no-tool models
# ═══════════════════════════════════════════════════════

async def stream_with_prompt_injection(
    prompt: str, history: list, websocket,
    model: str = None, project_path: str = None,
    project_id=None, repo_map: str = None,
) -> str | None:
    """
    Prompt injection mode: send enriched prompt without tools, parse response,
    execute actions (file writes, commands, git), return clean text.

    This is the dual mode alongside tool calling — used when a model does NOT
    support function/tool calling but we still need to manipulate project files.

    Flow:
    1. PromptInjector builds rich context (project files, git status, repo map)
    2. Model receives system prompt WITHOUT tools parameter
    3. ResponseParser.parse() extracts actions from XML tags / code blocks
    4. ActionExecutor.execute() runs each action
    5. Auto git commit+push after successful file writes (NEVER force-push)
    6. If actions found, sends results back to model for a clean summary
    7. Streams clean text to websocket and returns it
    """
    from core.prompt_injector import PromptInjector
    from core.response_parser import ResponseParser, ActionExecutor
    from core.executor import CommandExecutor

    if model is None:
        model = CONFIG["llm"].get("default_model")

    model, api_key, api_base, is_thinking = _resolve_model(model)

    if not api_key:
        logger.log(f"injection: no_api_key for {model}", level="error", source="agent")
        return None

    # ── Build injection context via PromptInjector ──
    project = await get_project(project_id) if project_id else None
    project_name = project.get("name", "") if project else ""
    project_desc = project.get("description", "") if project else ""
    base_prompt = project.get("base_prompt", "") if project else ""
    template = project.get("template", "") if project else ""

    injector = PromptInjector(project_path)
    context = await injector.build_context(
        project_name=project_name,
        project_description=project_desc,
        base_prompt=base_prompt,
        template=template,
        repo_map=repo_map or "",
        include_git_status=True,
    )

    injection_system = SYSTEM_PROMPT_INJECTION_TEMPLATE.format(
        project_context=context,
        platform_info=get_platform_info(),
    )

    # ── Build messages (no tools parameter!) ──
    api_messages = []
    role_map = {"ai": "assistant", "system": "system", "user": "user"}
    for msg in history:
        mapped_role = role_map.get(msg.get("role", ""), msg.get("role", "user"))
        api_messages.append({"role": mapped_role, "content": msg.get("content", "")})
    messages = [{"role": "system", "content": injection_system}] + api_messages + [{"role": "user", "content": prompt}]

    # Anthropic Claude 4+ temperature skip
    anthropic_no_temp_models = ("claude-opus-4-", "claude-sonnet-4-")
    skip_temperature = any(m in model.lower() for m in anthropic_no_temp_models)

    start_time = time.time()

    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": CONFIG["llm"].get("max_tokens", 4096),
        }
        if not skip_temperature:
            kwargs["temperature"] = CONFIG["llm"].get("temperature", 0.2)
        if api_key:
            kwargs["api_key"] = api_key
        if api_base and not api_base.startswith("https://api.anthropic.com") and not api_base.startswith("https://api.openai.com/v1") and not api_base.startswith("https://api.x.ai/v1"):
            kwargs["api_base"] = api_base
        # NO tools parameter — this is the whole point of prompt injection

        print(f"  [agent] stream_with_prompt_injection: model={model}, project={project_path}")
        await _send_log(websocket, f"🔄 Prompt injection mode: {model}", "info")

        response = await litellm.acompletion(stream=True, **kwargs)

        # Collect streamed response text
        response_text = ""
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                response_text += delta.content

        if not response_text.strip():
            return None

        # ── Parse response for actions ──
        parser = ResponseParser(project_path)
        parse_result = parser.parse(response_text)

        # ── Execute each action ──
        had_file_writes = False
        action_results = []

        if parse_result.actions:
            await _send_log(websocket, f"📝 Найдено {len(parse_result.actions)} действий в ответе", "info")
            executor = ActionExecutor(project_path)

            for action in parse_result.actions:
                result = await executor.execute(action, websocket)
                level = "success" if result["success"] else "error"
                await _send_log(websocket, f"{'✅' if result['success'] else '❌'} {result['message']}", level)
                action_results.append(result)

                if action.action_type == "write_file" and result["success"]:
                    had_file_writes = True
                    injector.invalidate_file(action.path)

        # ── Auto git commit+push after successful file writes ──
        if had_file_writes and project_path:
            try:
                git_exec = CommandExecutor()
                await git_exec.execute("git add -A", cwd=project_path, need_approval=False, timeout=10)
                # Generate commit message from written file paths
                file_paths = [r.get("path", "files") for r in action_results if r.get("path")]
                commit_msg = f"update: {', '.join(file_paths[:3])}" if file_paths else "update files"
                # Security: use shlex.quote for git commit message
                safe_msg = shlex.quote(commit_msg[:200])
                r_commit = await git_exec.execute(
                    f"git commit -m {safe_msg} --allow-empty",
                    cwd=project_path, need_approval=False, timeout=10,
                )
                commit_out = (r_commit.get("stdout", "") + r_commit.get("stderr", "")).strip()
                if "nothing to commit" not in commit_out.lower() and r_commit.get("exit_code") == 0:
                    # NEVER force-push — only git push (with project PAT token if available)
                    project_token = await get_project_token_by_path(project_path)
                    push_out = await git_push_with_token(git_exec, project_path, project_token)
                    await _send_log(websocket, f"🚀 Auto git push: {push_out.strip()[:100]}", "success")
            except Exception as e:
                logger.log(f"injection: auto git error: {e}", level="warning", source="agent")

        # ── Build clean text for user ──
        clean_text = parse_result.clean_text.strip()

        # ── If actions were found, ask model for a clean summary ──
        if parse_result.actions and action_results:
            results_text = "\n".join(f"- {r['message']}" for r in action_results)
            summary_prompt = (
                f"Предыдущий ответ был обработан. Вот результаты выполнения:\n"
                f"{results_text}\n\n"
                f"Кратко подведи итог — что было сделано. Без упоминания тегов и инструкций."
            )
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": summary_prompt})

            try:
                summary_kwargs = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": CONFIG["llm"].get("max_tokens", 2048),
                }
                if not skip_temperature:
                    summary_kwargs["temperature"] = CONFIG["llm"].get("temperature", 0.2)
                if api_key:
                    summary_kwargs["api_key"] = api_key
                if api_base and not api_base.startswith("https://api.anthropic.com") and not api_base.startswith("https://api.openai.com/v1") and not api_base.startswith("https://api.x.ai/v1"):
                    summary_kwargs["api_base"] = api_base

                summary_response = await litellm.acompletion(stream=True, **summary_kwargs)
                summary_text = ""
                async for schunk in summary_response:
                    if not schunk.choices:
                        continue
                    sdelta = schunk.choices[0].delta
                    if sdelta.content:
                        summary_text += sdelta.content
                if summary_text.strip():
                    clean_text = summary_text.strip()
            except Exception:
                pass  # Keep clean_text from parse_result as-is

        # ── Stream clean text to websocket in real-time ──
        if clean_text:
            await safe_ws_send(websocket, {"type": "chunk", "content": clean_text})

        await safe_ws_send(websocket, {"type": "done"})

        duration = (time.time() - start_time) * 1000
        tokens = len(clean_text) // 4
        logger.ai_response(model=model, tokens=tokens, success=True, duration_ms=duration)
        await _send_log(websocket, f"✅ Injection: {len(clean_text)} симв., {(duration / 1000):.1f}с", "success")

        return clean_text or response_text

    except Exception as e:
        duration = (time.time() - start_time) * 1000
        error_msg = str(e)
        logger.ai_response(model=model, success=False, error=error_msg, duration_ms=duration)
        await _send_log(websocket, f"❌ Injection error: {error_msg[:200]}", "error")
        return None


# ═══════════════════════════════════════════════════════
# SILENT PROBING — Тихое зондирование моделей
# Алгоритм: опрашиваем ВСЕ модели, каждая успешная сразу
# отправляется на клиент (прогрессивное заполнение панели).
# ВСЕ логи — ТОЛЬКО в панель логов. На главном экране — НИЧЕГО.
# ═══════════════════════════════════════════════════════

async def probe_models(websocket=None) -> list[dict]:
    """
    Зондирование без прогрессивной отправки (для фонового использования).
    Возвращает список проверенных моделей.
    """
    return await _do_probe(websocket, live=False)


async def probe_models_live(websocket) -> list[dict]:
    """
    Зондирование с прогрессивной отправкой: каждая успешная модель
    сразу отправляется клиенту через {"type": "probed_models", "models": [one]}.
    """
    return await _do_probe(websocket, live=True)


async def probe_selected_models(websocket, selected_model_ids: list[str]) -> list[dict]:
    """
    Тихий опрос ТОЛЬКО выбранных моделей (из UI панели).
    - Отправляет результаты ТОЛЬКО через probed_models type (в модельную панель)
    - Прогресс — ТОЛЬКО через _send_log (в панель логов)
    - НЕ отправляет chunk/type сообщения на главный экран
    """
    _sem = asyncio.Semaphore(5)
    _probed: list[dict] = []
    _lock = asyncio.Lock()

    all_models = keys_manager.get_all_models()
    # Фильтруем candidates только по выбранным ID
    selected_set = set(selected_model_ids)
    candidates = [
        m for m in all_models
        if m.get("id") in selected_set
    ]
    # Дополнительно фильтруем: модель должна иметь API-ключ или быть local
    usable_candidates = []
    for m in candidates:
        mid = m["id"]
        mc = keys_manager.get_model_config(mid)
        has_key = mc and mc.get("api_key")
        is_local = m.get("type") == "local" and m.get("base_url")
        if has_key or is_local:
            usable_candidates.append(m)

    if not usable_candidates:
        await _send_log(websocket, "⚠️ Нет подходящих моделей для опроса (нет API-ключей)", "warning")
        # Покажем сколько было отфильтровано
        await _send_log(websocket, f"   Выбрано: {len(candidates)}, с ключом: 0", "info")
        return []

    candidates = usable_candidates

    total = len(candidates)
    await _send_log(websocket, f"🔍 Опрос {total} выбранных моделей...", "info")

    # Отправляем клиенту состояние начала опроса (для UI прогресса)
    await safe_ws_send(websocket, {
        "type": "probe_progress",
        "total": total,
        "done": 0,
        "ok": 0,
        "fail": 0,
    })

    async def _probe_one(model_info: dict) -> dict | None:
        model_id = model_info["id"]
        model_name = model_info.get("name", model_id)
        litellm_model, api_key, api_base, _ = _resolve_model(model_id)
        if not api_key:
            print(f"  [probe] SKIP {model_id}: no API key")
            await _send_log(websocket, f"⏭️ {model_name}: нет API-ключа — пропуск", "warning")
            return None

        probe_messages = [{"role": "user", "content": "Hi"}]

        async with _sem:
            try:
                resp = await asyncio.wait_for(
                    litellm.acompletion(
                        model=litellm_model,
                        messages=probe_messages,
                        max_tokens=3,
                        stream=False,
                        api_key=api_key,
                        **({"api_base": api_base} if api_base else {}),
                    ),
                    timeout=30,
                )
                print(f"  [probe] OK {model_id} ({litellm_model})")
                return {"id": model_id, "name": model_name, "status": "valid"}
            except asyncio.TimeoutError:
                print(f"  [probe] TIMEOUT {model_id} ({litellm_model})")
                await _send_log(websocket, f"⏱️ {model_name}: таймаут (30с)", "warning")
                return None
            except Exception as e:
                print(f"  [probe] FAIL {model_id} ({litellm_model}): {str(e)[:200]}")
                await _send_log(websocket, f"❌ {model_name}: {str(e)[:80]}", "error")
                return None

    # Запускаем параллельно, с прогрессивной отправкой
    tasks = {asyncio.ensure_future(_probe_one(m)): m for m in candidates}
    _failed_ids = []
    _done_count = 0
    _ok_count = 0

    for future in asyncio.as_completed(tasks):
        try:
            result = await future
            _done_count += 1
            if isinstance(result, dict) and result:
                async with _lock:
                    _probed.append(result)
                # Сразу отправляем клиенту — модель появится в панели
                await safe_ws_send(websocket, {"type": "probed_models", "models": [result]})
                keys_manager._probed_model_ids.add(result["id"])
                keys_manager._failed_probe_ids.discard(result["id"])
                _ok_count += 1
            else:
                model_info = tasks[future]
                _failed_ids.append(model_info["id"])
                keys_manager._failed_probe_ids.add(model_info["id"])

            # Прогресс
            await safe_ws_send(websocket, {
                "type": "probe_progress",
                "total": total,
                "done": _done_count,
                "ok": _ok_count,
                "fail": _done_count - _ok_count,
            })
        except Exception:
            _done_count += 1

    # Сохраняем результаты
    if _failed_ids:
        keys_manager.update_failed_probe_ids(_failed_ids)
    await save_probed_models(_probed)

    await _send_log(websocket, f"✅ Опрос завершён: {_ok_count}/{total} моделей работают", "success")

    # Финальное сообщение
    await safe_ws_send(websocket, {
        "type": "probe_progress",
        "total": total,
        "done": total,
        "ok": _ok_count,
        "fail": total - _ok_count,
        "finished": True,
    })

    return _probed


async def _do_probe(websocket=None, live: bool = False) -> list[dict]:
    """
    Общее зондирование. Если live=True — отправляет каждую модель по мере проверки.
    """
    _sem = asyncio.Semaphore(5)
    _probed: list[dict] = []
    _lock = asyncio.Lock()  # для потокобезопасного добавления

    all_models = keys_manager.get_all_models()
    candidates = [
        m for m in all_models
        if m.get("status") in ("valid", "rate_limited", "available")
    ]

    if not candidates:
        if websocket:
            await _send_log(websocket, "Нет моделей для опроса", "warning")
        return []

    if websocket:
        await _send_log(websocket, f"🔍 Опрос: {len(candidates)} моделей...", "info")

    async def _probe_one(model_info: dict) -> dict | None:
        model_id = model_info["id"]
        model_name = model_info.get("name", model_id)

        litellm_model, api_key, api_base, _ = _resolve_model(model_id)
        if not api_key:
            return None

        probe_messages = [{"role": "user", "content": "Hi"}]

        async with _sem:
            try:
                resp = await asyncio.wait_for(
                    litellm.acompletion(
                        model=litellm_model,
                        messages=probe_messages,
                        max_tokens=3,
                        stream=False,
                        api_key=api_key,
                        **({"api_base": api_base} if api_base else {}),
                    ),
                    timeout=12,
                )
                result = {"id": model_id, "name": model_name, "status": "valid"}
                return result

            except asyncio.TimeoutError:
                return None
            except Exception:
                return None

    # Запускаем ВСЕ параллельно, но при live — каждую успешную сразу отправляем
    if live and websocket:
        # Live-режим: запускаем по одной через as_completed
        tasks = {asyncio.ensure_future(_probe_one(m)): m for m in candidates}
        _failed_ids = []
        for future in asyncio.as_completed(tasks):
            try:
                result = await future
                if isinstance(result, dict) and result:
                    async with _lock:
                        _probed.append(result)
                    # Сразу отправляем клиенту — модель появится в панели
                    await safe_ws_send(websocket, {"type": "probed_models", "models": [result]})
                    keys_manager._probed_model_ids.add(result["id"])
                    # Убираем из failed если там была
                    keys_manager._failed_probe_ids.discard(result["id"])
                else:
                    model_info = tasks[future]
                    _failed_ids.append(model_info["id"])
                    keys_manager._failed_probe_ids.add(model_info["id"])
            except Exception:
                pass

        # Сохраняем результаты
        if _failed_ids:
            keys_manager.update_failed_probe_ids(_failed_ids)
        await save_probed_models(_probed)

        if websocket:
            await _send_log(websocket, f"✅ Опрос завершён: {len(_probed)}/{len(candidates)} моделей работают", "success")
        return _probed
    else:
        # Обычный режим: все параллельно, результат в конце
        tasks = [_probe_one(m) for m in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _failed_ids = []
        for i, r in enumerate(results):
            if isinstance(r, dict) and r is not None:
                _probed.append(r)
            elif i < len(candidates):
                _failed_ids.append(candidates[i]["id"])
        if _failed_ids:
            keys_manager.update_failed_probe_ids(_failed_ids)
        _probed.sort(key=lambda x: x.get("response_time_ms", 999999))
        if websocket:
            await _send_log(websocket, f"✅ Опрос: {len(_probed)}/{len(candidates)} моделей", "success")
        return _probed
