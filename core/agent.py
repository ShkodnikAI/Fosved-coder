import os
import time
import glob as glob_mod
import litellm
import json
from core.memory import CONFIG, save_message, get_history, get_project
from core.keys_manager import keys_manager
from core.context_compressor import ContextCompressor
from core.action_logger import get_logger

litellm.suppress_debug_info = True
logger = get_logger()

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
            full_path = os.path.join(project_path, path)
            if not os.path.isfile(full_path):
                return f"Ошибка: файл не найден: {path}"
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # Trim if too large
            if len(content) > 30000:
                content = content[:30000] + "\n\n... (файл обрезан, всего " + str(len(content)) + " символов)"
            logger.log(f"tool: read_file {path}", level="info", source="agent")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"path": path}, "status": "done"})
            return content

        elif name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = os.path.join(project_path, path)
            # Create dirs
            dir_path = os.path.dirname(full_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.log(f"tool: write_file {path} ({len(content)} chars)", level="info", source="agent")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"path": path, "size": len(content)}, "status": "done"})
            return f"Файл {path} сохранён ({len(content)} символов)"

        elif name == "list_files":
            rel_path = arguments.get("path", ".")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            full_path = os.path.join(project_path, rel_path)
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
            return result

        elif name == "execute_command":
            command = arguments.get("command", "")
            if not command.strip():
                return "Ошибка: пустая команда"
            logger.log(f"tool: execute_command '{command[:100]}'", level="info", source="agent")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"command": command}, "status": "running"})
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
            return f"[{status}]\n{output}"

        elif name == "git_commit_push":
            message = arguments.get("message", "update")
            if not project_path:
                return "Ошибка: нет пути к проекту"
            logger.log(f"tool: git_commit_push '{message}'", level="info", source="agent")
            await websocket.send_json({"type": "tool_call", "tool": name, "args": {"message": message}, "status": "running"})
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
            return f"Commit: {commit_out.strip()}\nPush: {push_out.strip()}"

        else:
            return f"Неизвестный инструмент: {name}"

    except Exception as e:
        logger.log(f"tool_error: {name} -> {str(e)[:200]}", level="error", source="agent")
        await websocket.send_json({"type": "tool_call", "tool": name, "status": "error", "error": str(e)[:200]})
        return f"Ошибка выполнения {name}: {str(e)[:500]}"


# ═══════════════════════════════════════════════════════
# LLM STREAMING WITH TOOL CALLING
# ═══════════════════════════════════════════════════════

def _resolve_model(model_id: str) -> tuple[str, str, str, bool]:
    """Resolve model_id to (litellm_model, api_key, api_base, is_thinking)."""
    api_key = CONFIG["llm"].get("api_key", "")
    api_base = CONFIG["llm"].get("api_base", "")
    is_thinking = False

    if "YOUR_" in api_key.upper() or api_key == "YOUR_OPENROUTER_API_KEY_HERE":
        api_key = ""

    model_config = keys_manager.get_model_config(model_id)
    if model_config:
        model = model_config["model"]
        api_key = model_config["api_key"]
        api_base = model_config.get("api_base", "")
        is_thinking = model_config.get("thinking", False)
    else:
        model = model_id
        # Fallback: free models
        for fm in keys_manager.FREE_MODELS:
            if fm["id"] == model:
                or_config = keys_manager.providers.get("openrouter", {})
                api_key = or_config.get("api_key", "")
                model = f"openrouter/{fm['model']}"
                api_base = or_config.get("api_base", "https://openrouter.ai/api/v1")
                break
        else:
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
                              project_path: str | None = None, use_tools: bool = True):
    """Stream AI response with tool calling support. Loops until no more tool calls."""
    if model is None:
        model = CONFIG["llm"].get("default_model")
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(repo_map="", ideas_context="", project_context="", compressed_context="")

    model, api_key, api_base, is_thinking = _resolve_model(model)

    if not api_key:
        logger.log(f"no_api_key: {model}", level="error", source="agent")
        await websocket.send_json({"type": "error", "content": f"Нет API ключа для модели '{model}'. Добавьте ключ в настройках."})
        return None

    print(f"  [agent] stream_llm_response: model={model}, has_key=True, tools={'ON' if use_tools else 'OFF'}, thinking={is_thinking}, project={project_path}")

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

    start_time = time.time()
    full_response = ""
    max_tool_iterations = 10  # Prevent infinite loops

    for iteration in range(max_tool_iterations):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": CONFIG["llm"].get("temperature", 0.2),
                "max_tokens": CONFIG["llm"].get("max_tokens", 16384) if is_thinking else CONFIG["llm"].get("max_tokens", 4096),
            }
            if api_key:
                kwargs["api_key"] = api_key
            if api_base and not api_base.startswith("https://api.anthropic.com") and not api_base.startswith("https://api.openai.com/v1") and not api_base.startswith("https://api.x.ai/v1"):
                kwargs["api_base"] = api_base

            # Add tools if supported by the model
            if use_tools:
                kwargs["tools"] = TOOLS

            # Check if model supports tool calling
            # OpenRouter models and some others may not support tools
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as tool_err:
                err_str = str(tool_err)
                # If tools not supported, retry without tools
                if "tools" in err_str.lower() or "function" in err_str.lower() or "parameter" in err_str.lower():
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
            return full_response

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            error_msg = str(e)
            if "401" in error_msg:
                error_msg = "Ошибка 401: Неверный API ключ."
            elif "429" in error_msg:
                error_msg = "Ошибка 429: Лимит запросов исчерпан."
            elif "500" in error_msg:
                error_msg = "Ошибка 500: Сервер ИИ недоступен."
            elif "timeout" in error_msg.lower():
                error_msg = "Таймаут модели. Попробуйте другую."
            else:
                error_msg = f"Ошибка ИИ: {error_msg}"
            logger.ai_response(model=model, success=False, error=error_msg, duration_ms=duration)
            await websocket.send_json({"type": "error", "content": error_msg})
            return None

    # Max iterations reached
    await websocket.send_json({"type": "done"})
    return full_response


# ═══════════════════════════════════════════════════════
# PRIORITY ROUTING
# ═══════════════════════════════════════════════════════

def _get_priority_models(project: dict) -> list[str]:
    """Extract up to 3 priority model IDs from project's selected_models."""
    if not project or not project.get("selected_models"):
        return []
    try:
        models = json.loads(project["selected_models"])
        if isinstance(models, list):
            return models[:3]
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
        return

    # Try each model
    ai_response = None
    tried_count = 0

    for i, model_to_try in enumerate(models_to_try):
        model_config = keys_manager.get_model_config(model_to_try)
        if not model_config:
            continue
        has_key = bool(model_config.get("api_key"))
        is_local = model_to_try in [m["id"] for m in all_models if m["type"] == "local"]
        if not has_key and not is_local:
            continue

        tried_count += 1
        print(f"  [agent] trying model #{i}: {model_to_try}")

        await websocket.send_json({"type": "typing", "model": model_to_try})

        if i > 0:
            m_info = next((m for m in all_models if m["id"] == model_to_try), None)
            model_name = m_info["name"] if m_info else model_to_try
            await websocket.send_json({"type": "info", "content": f"Переключаюсь на {model_name} (попытка {i+1})..."})

        ai_response = await stream_llm_response(
            prompt, history, websocket,
            model=model_to_try, system_prompt=system_prompt,
            project_path=project_path, use_tools=True
        )
        if ai_response is not None:
            break

    if ai_response:
        await save_message(project_id, "ai", ai_response)
    elif tried_count == 0:
        await websocket.send_json({"type": "error", "content": "Нет модели с API ключом."})
    else:
        await websocket.send_json({"type": "error", "content": f"Все {tried_count} моделей не ответили."})
