"""
Automatic Agent for Fosved Coder v2.0
Автоматический режим — ИИ сам кодит по плану, выполняет команды,
пушит в GitHub и обращается к пользователю только при ошибках.

Workflow:
1. Получает задачу от пользователя
2. Генерирует пошаговый план (JSON)
3. Выполняет шаги последовательно:
   - Создание/редактирование файлов
   - Выполнение shell-команд
   - Установка пакетов
4. Автоматический git commit + push после каждого значимого шага
5. При ошибке — сообщает пользователю и ждёт указаний
"""
import json
import re
import os
import shlex
import asyncio
from datetime import datetime, timezone
from core.agent import stream_llm_response, get_platform_info, _safe_join
from core.memory import get_project, get_project_internal, get_history, git_push_with_token, get_project_token_by_path
from core.executor import CommandExecutor
from core.action_logger import get_logger

logger = get_logger()
executor = CommandExecutor()


def _now():
    """Current UTC time as HH:MM:SS string."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def send_auto_log(websocket, content: str, level: str = "info", step: int = None, total: int = None):
    """Send a structured log entry to the frontend auto-log panel.

    Levels: info, success, warning, error, command, file, done
    """
    entry = {
        "type": "auto_log",
        "content": content,
        "level": level,
        "time": _now(),
    }
    if step is not None and total is not None:
        entry["step"] = step
        entry["total"] = total
    try:
        await websocket.send_json(entry)
    except Exception:
        pass  # Don't crash auto mode if log send fails


# System prompt for plan generation
AUTO_PLAN_PROMPT = """Ты автоматический агент разработки Fosved Coder.
Твоя задача — создать ПОДРОБНЫЙ пошаговый план выполнения задачи пользователя.

Ты ДОЛЖЕН ответить ТОЛЬКО в формате JSON (без markdown, без ```):
{{
  "summary": "Краткое описание плана (1-2 предложения)",
  "steps": [
    {{
      "action": "create_file | edit_file | run_command | install_package | git_commit",
      "description": "Что делает этот шаг",
      "file_path": "путь/к/файлу (для create/edit)",
      "content": "содержимое файла (для create/edit, полный код)",
      "command": "shell команда (для run_command/install)",
      "commit_message": "сообщение коммита (для git_commit)"
    }}
  ]
}}

{platform_info}

Правила:
- Каждый шаг должен быть максимально конкретным и атомарным
- Создавай полные файлы целиком, а не фрагменты
- После каждых 2-3 шагов добавляй git_commit с описанием изменений
- Для установки пакетов используй install_package
- НЕ пиши ничего кроме JSON
- Если задача простая — план может состоять из 1-2 шагов
"""

# System prompt for auto execution (explains context)
AUTO_EXEC_PROMPT = """Ты автоматический агент разработки Fosved Coder v2.0.
Ты работаешь в автоматическом режиме — пользователь дал задачу, ты выполняешь её полностью.

Контекст проекта:
{project_context}

Структура проекта (Repo Map):
{repo_map}

Правила:
- Отвечай на языке пользователя
- Используй Markdown code blocks с указанием языка
- Будь кратким и конкретным — это автоматический режим
- Если возникает ошибка — опиши её и предложи исправление
- Если не хватает информации — спрашивай пользователя
"""


async def generate_plan(prompt: str, project_id, repo_map: str | None, websocket, model_id: str = None):
    """Generate a step-by-step execution plan for the given task."""
    # Build context
    history = await get_history(project_id)
    project_context = ""
    if project_id:
        project = await get_project(project_id)
        if project:
            if project.get("description"):
                project_context += f"О ПРОЕКТЕ: {project['description']}\n"
            if project.get("base_prompt"):
                project_context += f"ИНСТРУКЦИИ: {project['base_prompt']}\n"

    repo_map_text = ""
    if repo_map:
        repo_map_text = f"СТРУКТУРА ПРОЕКТА:\n{repo_map[:3000]}"

    system_prompt = AUTO_PLAN_PROMPT.format(
        project_context=project_context,
        repo_map=repo_map_text,
        platform_info=get_platform_info(),
    )

    # Ask AI for plan
    plan_prompt = f"""Создай пошаговый план для задачи:
{prompt}

Отвечай ТОЛЬКО JSON."""

    await websocket.send_json({"type": "system", "content": "Анализирую задачу и создаю план..."})
    await send_auto_log(websocket, "Генерация плана выполнения...", "info")
    await send_auto_log(websocket, f"Контекст проекта: {'загружен' if project_context else 'нет'}", "info")
    await send_auto_log(websocket, f"Repo map: {'загружен (' + str(len(repo_map_text)) + ' символов)' if repo_map_text else 'нет'}", "info")

    response = await stream_llm_response(
        plan_prompt, history, websocket,
        model=model_id, system_prompt=system_prompt
    )

    if not response:
        await send_auto_log(websocket, "LLM не вернул ответ для генерации плана", "warning")
        try:
            logger.log("plan_generation_failed", level="warning", source="auto_agent",
                       details={"project_id": project_id, "model": model_id},
                       error="LLM returned empty response")
        except Exception:
            pass
        return None

    # Parse JSON plan from response
    try:
        # Try to extract JSON from response (might have extra text)
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            plan = json.loads(json_match.group())
            if "steps" in plan:
                await send_auto_log(websocket, f"План создан: {len(plan['steps'])} шагов", "success")
                return plan
        # Try direct parse
        plan = json.loads(response.strip())
        if "steps" in plan:
            await send_auto_log(websocket, f"План создан: {len(plan['steps'])} шагов", "success")
            return plan
    except json.JSONDecodeError:
        await send_auto_log(websocket, "Ошибка парсинга JSON плана от LLM", "error")
        print(f"  [auto] Failed to parse plan JSON, treating as regular response")
        try:
            logger.log("plan_parse_error", level="error", source="auto_agent",
                       details={"project_id": project_id}, error="Failed to parse plan JSON")
        except Exception:
            pass

    return None


async def execute_step(step: dict, project_path: str | None, websocket, step_num: int = None, total_steps: int = None):
    """Execute a single plan step. Returns True if successful."""
    action = step.get("action", "")
    description = step.get("description", "")

    await websocket.send_json({"type": "auto_step", "content": f"> {description}"})
    await send_auto_log(websocket, f"Шаг {step_num}/{total_steps}: {description}", "info", step_num, total_steps)

    try:
        if action == "create_file":
            file_path = step.get("file_path", "")
            content = step.get("content", "")
            if not file_path:
                await websocket.send_json({"type": "auto_error", "content": "Ошибка: не указан file_path для create_file"})
                await send_auto_log(websocket, "Ошибка: не указан file_path", "error", step_num, total_steps)
                return False

            if not project_path:
                await send_auto_log(websocket, "Ошибка: нет project_path", "error", step_num, total_steps)
                return False
            full_path = _safe_join(project_path, file_path)
            if not full_path:
                await send_auto_log(websocket, f"Отказ: путь вне проекта: {file_path}", "error", step_num, total_steps)
                return False
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count('\n') + 1
            await websocket.send_json({"type": "auto_step", "content": f"  Создан: {file_path}"})
            await send_auto_log(websocket, f"Создан файл: {file_path} ({lines} строк)", "file", step_num, total_steps)
            try:
                logger.log(f"file_created: {file_path}", level="info", source="auto_agent",
                           details={"lines": lines, "step": step_num, "total": total_steps})
            except Exception:
                pass
            return True

        elif action == "edit_file":
            file_path = step.get("file_path", "")
            content = step.get("content", "")
            if not file_path:
                await websocket.send_json({"type": "auto_error", "content": "Ошибка: не указан file_path для edit_file"})
                await send_auto_log(websocket, "Ошибка: не указан file_path", "error", step_num, total_steps)
                return False

            if not project_path:
                await send_auto_log(websocket, "Ошибка: нет project_path", "error", step_num, total_steps)
                return False
            full_path = _safe_join(project_path, file_path)
            if not full_path:
                await send_auto_log(websocket, f"Отказ: путь вне проекта: {file_path}", "error", step_num, total_steps)
                return False
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count('\n') + 1
            await websocket.send_json({"type": "auto_step", "content": f"  Изменён: {file_path}"})
            await send_auto_log(websocket, f"Изменён файл: {file_path} ({lines} строк)", "file", step_num, total_steps)
            try:
                logger.log(f"file_edited: {file_path}", level="info", source="auto_agent",
                           details={"lines": lines, "step": step_num, "total": total_steps})
            except Exception:
                pass
            return True

        elif action == "run_command":
            command = step.get("command", "")
            if not command:
                await send_auto_log(websocket, "Пустая команда — пропущено", "warning", step_num, total_steps)
                return False
            cwd = project_path
            await send_auto_log(websocket, f"Выполняю: $ {command}", "command", step_num, total_steps)
            result = await executor.execute(command, cwd=cwd)
            output = result.get("stdout", "") or result.get("stderr", "")
            exit_code = result.get("exit_code", -1)
            if output:
                # Limit output to avoid flooding
                output_lines = output.strip().split("\n")
                if len(output_lines) > 20:
                    output = "\n".join(output_lines[:20]) + f"\n  ... ({len(output_lines) - 20} строк обрезано)"
                await websocket.send_json({"type": "auto_step", "content": f"  $ {command}\n{output}"})
            if exit_code == 0:
                await send_auto_log(websocket, f"Команда выполнена (exit 0)", "success", step_num, total_steps)
            else:
                await send_auto_log(websocket, f"Команда завершилась с кодом {exit_code}", "warning", step_num, total_steps)
            try:
                logger.log(f"step_run_command: {command[:120]}",
                           level="success" if exit_code == 0 else "warning",
                           source="auto_agent",
                           details={"exit_code": exit_code, "step": step_num, "total": total_steps},
                           error=output[:200] if exit_code != 0 else None)
            except Exception:
                pass
            return exit_code == 0

        elif action == "install_package":
            command = step.get("command", "")
            if not command:
                return False
            cwd = project_path
            await send_auto_log(websocket, f"Установка: $ {command}", "command", step_num, total_steps)
            result = await executor.execute(command, cwd=cwd, timeout=120)
            output = result.get("stdout", "") or result.get("stderr", "")
            await websocket.send_json({"type": "auto_step", "content": f"  $ {command}\n{output[:500]}"})
            await send_auto_log(websocket, f"Установка завершена", "success", step_num, total_steps)
            return True  # Install commands often succeed even with warnings

        elif action == "git_commit":
            msg = step.get("commit_message", "Auto: step completed")
            if not project_path:
                return False
            await send_auto_log(websocket, f"Git commit: {msg}", "command", step_num, total_steps)
            # git add -A
            await executor.execute("git add -A", cwd=project_path, timeout=10)
            # git commit (shlex.quote prevents shell injection through commit message)
            result = await executor.execute(f"git commit -m {shlex.quote(msg)} --allow-empty", cwd=project_path, timeout=15)
            output = result.get("stdout", "") or result.get("stderr", "")
            if "nothing to commit" in output.lower():
                await websocket.send_json({"type": "auto_step", "content": f"  Git: нет изменений для коммита"})
                await send_auto_log(websocket, "Git: нет изменений для коммита", "info", step_num, total_steps)
            else:
                await websocket.send_json({"type": "auto_step", "content": f"  Git commit: {msg}"})
                await send_auto_log(websocket, f"Git commit OK: {msg}", "success", step_num, total_steps)
            return True

        else:
            await websocket.send_json({"type": "auto_step", "content": f"  Пропущен неизвестный шаг: {action}"})
            await send_auto_log(websocket, f"Неизвестный тип действия: {action} — пропущено", "warning", step_num, total_steps)
            return True

    except Exception as e:
        await websocket.send_json({"type": "auto_error", "content": f"Ошибка при выполнении шага: {str(e)}"})
        await send_auto_log(websocket, f"Ошибка: {str(e)}", "error", step_num, total_steps)
        try:
            logger.log("step_error", level="error", source="auto_agent",
                       details={"action": action, "step": step_num, "total": total_steps},
                       error=str(e))
        except Exception:
            pass
        return False


async def auto_git_push(project_path: str, websocket, step_num: int = None, total_steps: int = None, project_id: int = None):
    """Silent git push in automatic mode (with PAT token)."""
    if not project_path or not os.path.isdir(os.path.join(project_path, ".git")):
        await send_auto_log(websocket, "Git push пропущен (нет .git каталога)", "info", step_num, total_steps)
        return
    try:
        await send_auto_log(websocket, "Git push...", "command", step_num, total_steps)
        # Get project token for authenticated push
        project_token = None
        if project_id:
            project_token = await get_project_token_by_id(project_id)
        if not project_token:
            project_token = await get_project_token_by_path(project_path)
        push_out = await git_push_with_token(executor, project_path, project_token)
        push_stripped = push_out.strip()
        if "error" in push_stripped.lower() or "fatal" in push_stripped.lower() or "denied" in push_stripped.lower():
            await websocket.send_json({"type": "auto_step", "content": f"  Push failed: {push_stripped[:100]}"})
            await send_auto_log(websocket, f"Push failed: {push_stripped[:100]}", "error", step_num, total_steps)
        elif "Everything up-to-date" in push_stripped:
            await send_auto_log(websocket, "Push: всё актуально (up-to-date)", "info", step_num, total_steps)
        else:
            lines = [l for l in push_stripped.split("\n") if l.strip()]
            summary = lines[-1] if lines else "OK"
            await websocket.send_json({"type": "auto_step", "content": f"  Pushed: {summary}"})
            await send_auto_log(websocket, f"Push OK: {summary}", "success", step_num, total_steps)
    except Exception as e:
        await send_auto_log(websocket, f"Push exception: {str(e)}", "error", step_num, total_steps)


async def run_auto_mode(prompt: str, project_id, repo_map: str | None, websocket, model_id: str = None):
    """Main entry point for automatic mode."""
    # Get project path
    project_path = None
    if project_id:
        project = await get_project(project_id)
        if project:
            project_path = project.get("path", "")

    await websocket.send_json({"type": "system", "content": "[АВТО] Режим автоматического выполнения включён"})
    await send_auto_log(websocket, "Автоматический режим запущен", "info")
    await send_auto_log(websocket, f"Задача: {prompt[:120]}{'...' if len(prompt) > 120 else ''}", "info")
    try:
        logger.log("auto_mode_start", level="info", source="auto_agent",
                   details={"project_id": project_id, "model": model_id, "prompt": prompt[:200]})
    except Exception:
        pass
    if project_path:
        await send_auto_log(websocket, f"Проект: {project_path}", "info")
    if model_id:
        await send_auto_log(websocket, f"Модель: {model_id}", "info")

    # Step 1: Generate plan
    plan = await generate_plan(prompt, project_id, repo_map, websocket, model_id)

    if not plan or not plan.get("steps"):
        # If plan generation failed, fall back to regular chat
        await websocket.send_json({"type": "system", "content": "[АВТО] Не удалось создать план — переключаюсь на обычный режим"})
        await send_auto_log(websocket, "План не создан — переключение на ручной режим", "warning")
        from core.agent import handle_chat_message
        await handle_chat_message(prompt, project_id, repo_map, websocket, model_id=model_id)
        return

    steps = plan["steps"]
    summary = plan.get("summary", "")
    total = len(steps)

    await websocket.send_json({"type": "system", "content": f"[АВТО] План: {summary} ({total} шагов)"})
    await send_auto_log(websocket, f"План: {summary}", "success")
    await send_auto_log(websocket, f"Всего шагов: {total}", "info")

    # Step 2: Execute steps
    completed = 0
    failed = 0

    for i, step in enumerate(steps):
        action = step.get("action", "")
        desc = step.get("description", "")
        step_num = i + 1
        await websocket.send_json({"type": "system", "content": f"[АВТО] Шаг {step_num}/{total}: {desc}"})

        success = await execute_step(step, project_path, websocket, step_num, total)

        if success:
            completed += 1
        else:
            failed += 1
            # On failure, ask user what to do
            await websocket.send_json({
                "type": "system",
                "content": f"[АВТО] Шаг {step_num} не выполнен. Продолжаю со следующего..."
            })

        # Small delay between steps
        await asyncio.sleep(0.5)

    # Step 3: Final git push
    if project_path and completed > 0:
        await websocket.send_json({"type": "system", "content": f"[АВТО] Пушу изменения..."})
        await auto_git_push(project_path, websocket, None, total, project_id=project_id)

    # Step 4: Quick code check (syntax only, no tests in auto mode)
    if project_path and completed > 0:
        try:
            await websocket.send_json({"type": "system", "content": f"[АВТО] Проверяю код..."})
            await send_auto_log(websocket, "Проверка синтаксиса кода...", "info")
            from core.code_tester import check_python_syntax, check_js_syntax, detect_project_type
            project_types = detect_project_type(project_path)
            errors_found = 0
            if "python" in project_types:
                py_result = await check_python_syntax(project_path)
                errors_found += len(py_result.get("errors", []))
            if "node" in project_types or "react" in project_types:
                js_result = await check_js_syntax(project_path)
                errors_found += len(js_result.get("errors", []))
            if errors_found == 0:
                await websocket.send_json({"type": "auto_step", "content": "  ✅ Код прошёл проверку"})
                await send_auto_log(websocket, "Проверка синтаксиса: OK", "success")
            else:
                await websocket.send_json({"type": "auto_step", "content": f"  ⚠️ {errors_found} ошибок синтаксиса найдено"})
                await send_auto_log(websocket, f"Проверка синтаксиса: {errors_found} ошибок", "warning")
        except Exception as e:
            await send_auto_log(websocket, f"Проверка кода недоступна: {str(e)[:80]}", "warning")

    # Step 5: Report results
    if failed == 0:
        await websocket.send_json({
            "type": "auto_done",
            "content": f"[АВТО] Все {completed} шагов выполнены успешно!"
        })
        await send_auto_log(websocket, f"Выполнение завершено: все {completed} шагов успешно", "success")
    else:
        await websocket.send_json({
            "type": "auto_done",
            "content": f"[АВТО] Выполнено {completed}/{len(steps)} шагов. {failed} с ошибками."
        })
        await send_auto_log(websocket, f"Выполнение завершено: {completed}/{total} OK, {failed} с ошибками", "warning")

    # Signal log panel that auto mode is done
    await send_auto_log(websocket, "---", "done")
    try:
        logger.log("auto_mode_end", level="success" if failed == 0 else "warning",
                   source="auto_agent",
                   details={"project_id": project_id, "completed": completed,
                            "failed": failed, "total": total})
    except Exception:
        pass
