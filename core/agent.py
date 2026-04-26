import os
import sys
import platform
import time
import glob as glob_mod
import litellm
import json
from pathlib import Path
from datetime import datetime, timezone
from core.memory import CONFIG, save_message, get_history, get_project
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


def _now():
    """Current UTC time as HH:MM:SS string."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


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


# ═══════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════

async def execute_tool(name: str, arguments: dict, project_path: str | None, websocket) -> str:
    """Выполнить tool call и вернуть результат как строку для LLM."""
    from core.executor import CommandExecutor
    executor = CommandExecutor()

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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"path": path}, "status": "done"})
            await _send_log(websocket, f"📖 Читаю: {path} ({len(content)} симв.)", "info")
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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"path": path, "size": len(content)}, "status": "done"})
            await _send_log(websocket, f"💾 Записываю: {path} ({len(content)} симв.)", "file")
            return f"Файл {path} сохранён ({len(content)} символов)"

        elif name == "list_files":
            rel_path = arguments.get("path", ".")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = _safe_join(project_path, rel_path)
            if not full_path:
                return f"Ошибка: путь вне проекта: {rel_path}"
            if not os.path.isdir(full_path):
                return f"Ошибка: директория не найдена: {rel_path}"
            entries = []
            skip_dirs = {"venv", "__pycache__", "node_modules", ".git", ".cache", ".venv", "env", ".idea", ".vscode", "dist", "build", "__pypackages__", ".next", ".nuxt", ".gradle", "target"}
            try:
                for item in sorted(os.listdir(full_path)):
                    if item.startswith(".") and item not in {".env", ".gitignore"}:
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
            result = "\n".join(entries) if entries else "(пустая директория)"
            logger.log(f"tool: list_files {rel_path} ({len(entries)} entries)", level="info", source="agent")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"path": rel_path}, "status": "done"})
            await _send_log(websocket, f"📁 Список файлов: {rel_path} ({len(entries)} элементов)", "info")
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
                        if file_pattern != "*" and not f.endswith(file_pattern.replace("*", "")):
                            continue
                        fpath = os.path.join(root, f)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                                for i, line in enumerate(fh, 1):
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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"pattern": pattern}, "status": "done"})
            await _send_log(websocket, f"🔍 Поиск '{pattern}': {len(results)} совпадений", "info")
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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"command": command}, "status": "running"})
            await _send_log(websocket, f"⚡ Выполняю: $ {command[:120]}", "command")
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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"command": command}, "status": "done", "exit_code": exit_code})
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
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"message": message}, "status": "running"})
            await _send_log(websocket, f"🚀 Git commit: {message}", "command")
            # Stage all
            r1 = await executor.execute("git add -A", cwd=project_path, need_approval=False)
            # Commit
            safe_msg = message.replace('"', "'")
            r2 = await executor.execute(f'git commit -m "{safe_msg}" --allow-empty', cwd=project_path, need_approval=False)
            commit_out = r2.get("stdout", "") + r2.get("stderr", "")
            # Push
            r3 = await executor.execute("git push", cwd=project_path, need_approval=False, timeout=30)
            push_out = r3.get("stdout", "") + r3.get("stderr", "")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"message": message}, "status": "done"})
            await _send_log(websocket, f"🚀 Push: {push_out.strip()[:100]}", "success")
            return f"Commit: {commit_out.strip()}\nPush: {push_out.strip()}"

        else:
            return f"Неизвестный инструмент: {name}"

    except Exception as e:
        logger.log(f"tool_error: {name} -> {str(e)[:200]}", level="error", source="agent")
        await websocket.send_json({"type": "tool_call", "tool": name, "status": "error", "error": str(e)[:200]})
        await _send_log(websocket, f"❌ Ошибка {name}: {str(e)[:150]}", "error")
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
                              _error_info: dict = None):
    """Stream AI response with tool calling support. Loops until no more tool calls."""
    if model is None:
        model = CONFIG["llm"].get("default_model")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="", project_context="", compressed_context="", platform_info=get_platform_info())

    model, api_key, api_base, is_thinking = _resolve_model(model)

    if not api_key:
        logger.log(f"no_api_key: {model}", level="error", source="agent")
        await websocket.send_json({"type": "error", "content": f"Нет API ключа для модели '{model}'. Добавьте ключ в настройках."})
        await _send_log(websocket, f"❌ Нет API ключа для {model}", "error")
        return None

    print(f"  [agent] stream_llm_response: model={model}, has_key=True, tools={'ON' if use_tools else 'OFF'}, thinking={is_thinking}, project={project_path}")
    await _send_log(websocket, f"🧠 Модель: {model}", "info")

    # Extended Thinking: уведомить клиента и настроить параметры
    if is_thinking:
        await websocket.send_json({"type": "info", "content": "Extended Thinking включён — модель будет рассуждать глубже"})

    # Build messages
    api_messages = []
    role_map = {"ai": "assistant", "system": "system", "user": "user"}
    for msg in history:
        mapped_role = role_map.get(msg.get("role", ""), msg.get("role", "user"))
        api_messages.append({"role": mapped_role, "content": msg.get("content", "")})
    messages = [{"role": "system", "content": system_prompt}] + api_messages + [{"role": "user", "content": prompt}]

    # Anthropic Claude 4+ не поддерживает temperature — убираем для этих моделей
    anthropic_no_temp_models = ("claude-opus-4-", "claude-sonnet-4-")
    skip_temperature = any(m in model.lower() for m in anthropic_no_temp_models)

    start_time = time.time()
    full_response = ""
    max_tool_iterations = 10  # Prevent infinite loops

    for iteration in range(max_tool_iterations):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": CONFIG["llm"].get("max_tokens", 16384) if is_thinking else CONFIG["llm"].get("max_tokens", 4096),
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
                err_str = str(tool_err)
                # If tools not supported, retry without tools
                if "tools" in err_str.lower() or "tool use" in err_str.lower() or "function" in err_str.lower() or "parameter" in err_str.lower():
                    print(f"  [agent] Tools not supported by {model}, retrying without tools")
                    use_tools = False
                    kwargs.pop("tools", None)
                    response = await litellm.acompletion(**kwargs)
                else:
                    raise

            choice = response.choices[0]
            msg_obj = choice.message

            # Check for tool calls
            tool_calls = getattr(msg_obj, 'tool_calls', None)

            if tool_calls:
                # Add assistant message with tool calls to history
                messages.append(msg_obj.model_dump())

                # Execute each tool call
                for tc in tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}

                    await websocket.send_json({
                        "type": "tool_call",
                        "tool": fn_name,
                        "args": fn_args,
                        "status": "running"
                    })

                    result = await execute_tool(fn_name, fn_args, project_path, websocket)

                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })

                # Continue loop — LLM will process tool results and respond
                continue

            # No tool calls — stream the text response
            response_text = getattr(msg_obj, 'content', None) or ""

            if response_text:
                # Stream text to client
                # Split into small chunks for real-time feel
                chunk_size = 20
                for i in range(0, len(response_text), chunk_size):
                    chunk = response_text[i:i+chunk_size]
                    await websocket.send_json({"type": "chunk", "content": chunk})
                full_response += response_text

            await websocket.send_json({"type": "done"})
            duration = (time.time() - start_time) * 1000
            tokens = len(full_response) // 4
            logger.ai_response(model=model, tokens=tokens, success=True, duration_ms=duration)
            await _send_log(websocket, f"✅ Ответ от {model}: {len(full_response)} симв., {(duration/1000):.1f}с", "success")
            return full_response

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            error_msg = str(e)
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
            await websocket.send_json({"type": "error", "content": error_msg})
            await _send_log(websocket, f"❌ {model}: {error_msg}", "error")
            # Signal 402 to caller for provider skipping in fallback
            if _error_info is not None and ("402" in error_msg or "insufficient credits" in error_msg.lower()):
                _error_info["no_credits"] = True
            return None

    # Max iterations reached
    await websocket.send_json({"type": "done"})
    await _send_log(websocket, f"⚠️ Достигнут лимит {max_tool_iterations} итераций tool calling", "warning")
    return full_response


# ═══════════════════════════════════════════════════════
# PRIORITY ROUTING
# ═══════════════════════════════════════════════════════

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


async def _route_with_priority(prompt: str, priority_models: list[str]) -> str | None:
    """Route to cheapest or best model based on prompt complexity."""
    prompt_lower = prompt.lower()

    simple_keywords = ["fix typo", "формат", "xml", "json", "тест", "docstring", "комментарий", "простой", "trivial", "rename", "lint"]
    for kw in simple_keywords:
        if kw in prompt_lower:
            return priority_models[-1] if len(priority_models) > 1 else priority_models[0]

    complex_keywords = ["архитектур", "refactor", "redesign", "систем", "framework", "engine", "параллельн", "микросервис", "database schema", "security", "интеграц"]
    for kw in complex_keywords:
        if kw in prompt_lower:
            return priority_models[0]

    return priority_models[0]


# ═══════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════

async def handle_chat_message(prompt: str, project_id, repo_map: str | None, websocket, model_id: str = None):
    """Main entry point: get history, build context, stream response with tool calling and fallback."""
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
            if project.get("path"):
                project_path = project["path"]
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
            await websocket.send_json({"type": "info", "content": "Автосжатие контекста..."})
            compressed_summary, remaining, was_llm = await compressor.compress_and_cleanup(history, project_id, model_config=comp_model)
            if compressed_summary:
                compressed_context_text = compressed_summary
                method = "LLM" if was_llm else "regex"
                removed = len(history) - len(remaining)
                await websocket.send_json({"type": "info", "content": f"Автосжатие ({method}): {removed} сообщений сжато"})
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

    await save_message(project_id, "user", prompt)

    # Build model list with fallback
    models_to_try = []
    if model_id:
        models_to_try.append(model_id)

    project = await get_project(project_id) if project_id else None
    for pm in _get_priority_models(project):
        if pm not in models_to_try:
            models_to_try.append(pm)

    all_models = keys_manager.get_all_models()
    for m in all_models:
        if m["id"] not in models_to_try and m.get("status") in ("valid", "rate_limited", "available"):
            if m.get("type") == "free" and m.get("status") == "no_key":
                continue
            if m.get("type") == "local" and not m.get("base_url"):
                continue
            models_to_try.append(m["id"])

    if not models_to_try:
        await websocket.send_json({"type": "error", "content": "Нет доступных моделей. Добавьте API ключ."})
        await _send_log(websocket, "❌ Нет доступных моделей", "error")
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
    no_credits_providers = set()  # providers that returned 402 — skip remaining models from them

    for i, model_to_try in enumerate(models_to_try):
        model_config = keys_manager.get_model_config(model_to_try)
        if not model_config:
            continue
        has_key = bool(model_config.get("api_key"))
        is_local = model_to_try in [m["id"] for m in all_models if m["type"] == "local"]
        if not has_key and not is_local:
            continue

        # Skip models from providers with no credits (402)
        model_provider = model_config.get("provider", "")
        if model_provider in no_credits_providers:
            print(f"  [agent] skipping #{i} {model_to_try} — provider {model_provider} has no credits")
            continue

        tried_count += 1
        print(f"  [agent] trying model #{i}: {model_to_try}")

        await websocket.send_json({"type": "typing", "model": model_to_try})

        if i > 0:
            m_info = next((m for m in all_models if m["id"] == model_to_try), None)
            model_name = m_info["name"] if m_info else model_to_try
            await websocket.send_json({"type": "info", "content": f"Переключаюсь на {model_name} (попытка {tried_count})..."})
            await _send_log(websocket, f"🔄 Переключаюсь на {model_name} (попытка {tried_count})", "warning")

        error_info = {}
        ai_response = await stream_llm_response(
            prompt, history, websocket,
            model=model_to_try, system_prompt=system_prompt,
            project_path=project_path, use_tools=True,
            _error_info=error_info
        )
        if error_info.get("no_credits") and model_provider:
            no_credits_providers.add(model_provider)
            await _send_log(websocket, f"⏭️ Пропускаю {model_provider} (нет кредитов)", "warning")
        if ai_response is not None:
            break

    if ai_response:
        await save_message(project_id, "ai", ai_response)
    elif tried_count == 0:
        await websocket.send_json({"type": "error", "content": "Нет модели с API ключом."})
        await _send_log(websocket, "❌ Нет модели с API ключом", "error")
    else:
        await websocket.send_json({"type": "error", "content": f"Все {tried_count} моделей не ответили."})
        await _send_log(websocket, f"❌ Все {tried_count} моделей не ответили", "error")
