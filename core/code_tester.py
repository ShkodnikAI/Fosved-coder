"""
Fosved Coder v2.0 — Code Tester & Validator
Автоматическая проверка и тестирование кода проекта.

Возможности:
1. Синтаксическая проверка (Python, JavaScript/TypeScript)
2. Линтинг (flake8, eslint, ruff — если доступны)
3. Запуск тестов (pytest, jest, npm test — если есть)
4. LLM-анализ ошибок с предложениями исправления
5. Сохранение тотального лога в файл + git push
"""
import os
import re
from datetime import datetime
from core.executor import CommandExecutor

executor = CommandExecutor()


# ═══════════════════════════════════════════════════
# DETECTORS — определение типа проекта
# ═══════════════════════════════════════════════════

def detect_project_type(project_path: str) -> list[str]:
    """Определяет типы технологий в проекте по файлам."""
    if not project_path or not os.path.isdir(project_path):
        return []
    types = []

    # Python
    py_files = [f for f in os.listdir(project_path) if f.endswith(".py")]
    py_markers = ["main.py", "app.py", "requirements.txt", "setup.py", "pyproject.toml"]
    if py_files or any(f in os.listdir(project_path) for f in py_markers):
        types.append("python")
        # FastAPI / Flask?
        if any(f in os.listdir(project_path) for f in ["main.py", "app.py"]):
            for pf in py_files:
                try:
                    with open(os.path.join(project_path, pf), "r", errors="replace") as fh:
                        content = fh.read()
                        if "fastapi" in content.lower() and "FastAPI" in content:
                            types.append("fastapi")
                            break
                        if "flask" in content.lower() and "Flask" in content:
                            types.append("flask")
                            break
                except Exception:
                    pass

    # Node/React/Next.js
    pkg_path = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg_path):
        types.append("node")
        try:
            with open(pkg_path, "r", errors="replace") as f:
                content = f.read()
                if '"next"' in content or '"next":' in content:
                    types.append("nextjs")
                elif '"react"' in content:
                    types.append("react")
        except Exception:
            pass

    return types


def detect_test_framework(project_path: str) -> str | None:
    """Определяет какой тест-фреймворк есть в проекте."""
    if not project_path or not os.path.isdir(project_path):
        return None

    # Python — ищем конкретные маркеры pytest
    pytest_markers = ["pytest.ini", "setup.cfg", "conftest.py"]
    for marker in pytest_markers:
        if os.path.isfile(os.path.join(project_path, marker)):
            return "pytest"

    # Проверяем pyproject.toml на наличие pytest в зависимостях
    pptoml = os.path.join(project_path, "pyproject.toml")
    if os.path.isfile(pptoml):
        try:
            with open(pptoml, "r", errors="replace") as f:
                content = f.read().lower()
                if "pytest" in content and ("tool.pytest" in content or "dev-dependencies" in content or "test" in content):
                    return "pytest"
        except Exception:
            pass

    # Ищем test_ файлы или папку tests/
    for entry in os.listdir(project_path):
        if entry.startswith("test_") and entry.endswith(".py"):
            return "pytest"
    if os.path.isdir(os.path.join(project_path, "tests")):
        return "pytest"

    # Node — jest / vitest
    pkg_path = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", errors="replace") as f:
                content = f.read()
                if '"jest"' in content or '"vitest"' in content:
                    return "jest"
                if '"test"' in content:
                    return "npm"
        except Exception:
            pass

    return None


# ═══════════════════════════════════════════════════
# CHECKERS — синтаксическая проверка
# ═══════════════════════════════════════════════════

SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", ".next", "dist", "build", ".tox", ".mypy_cache", ".ruff_cache"}


async def check_python_syntax(project_path: str) -> dict:
    """Проверка синтаксиса всех Python файлов."""
    results = {"checked": 0, "errors": []}
    py_files = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py") and not f.startswith("__"):
                py_files.append(os.path.join(root, f))

    if not py_files:
        return results

    for fpath in py_files[:30]:  # лимит 30 файлов
        rel = os.path.relpath(fpath, project_path).replace("\\", "/")
        result = await executor.execute(
            f'python -m py_compile {shlex.quote(fpath)}',
            cwd=project_path,
            timeout=15
        )
        if result["exit_code"] != 0:
            stderr = result.get("stderr", "")
            match = re.search(r'File "(.+)", line (\d+)', stderr)
            line = match.group(2) if match else "?"
            desc = stderr.strip().split("\n")[-1].strip() if stderr else "Syntax error"
            results["errors"].append({"file": rel, "line": line, "message": desc})
        results["checked"] += 1

    return results


async def check_js_syntax(project_path: str) -> dict:
    """Проверка синтаксиса JS/TS через node --check."""
    results = {"checked": 0, "errors": []}

    # Проверяем что node_modules — директория, а не файл (багfix)
    nm_path = os.path.join(project_path, "node_modules")
    if not os.path.isdir(nm_path):
        return results

    # Проверяем что node доступен
    node_check = await executor.execute("node --version", cwd=project_path, timeout=5)
    if node_check["exit_code"] != 0:
        return results

    js_files = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".js", ".jsx", ".ts", ".tsx")) and not f.endswith(".d.ts"):
                js_files.append(os.path.join(root, f))

    for fpath in js_files[:20]:
        rel = os.path.relpath(fpath, project_path).replace("\\", "/")
        result = await executor.execute(
            f'node --check {shlex.quote(fpath)}',
            cwd=project_path,
            timeout=10
        )
        if result["exit_code"] != 0:
            stderr = result.get("stderr", "")
            results["errors"].append({"file": rel, "line": "", "message": stderr.strip()[-200:]})
        results["checked"] += 1

    return results


# ═══════════════════════════════════════════════════
# LINTERS — статический анализ
# ═══════════════════════════════════════════════════

async def run_linter(project_path: str, project_types: list[str]) -> dict:
    """Запускает доступные линтеры."""
    results = {"linter": None, "issues": []}

    if "python" in project_types:
        # Пробуем ruff (быстрый), потом flake8
        for linter_cmd, linter_name in [
            ("ruff check . --output-format=text 2>&1", "ruff"),
            ("flake8 . --max-line-length=120 2>&1", "flake8")
        ]:
            check = await executor.execute(f"which {linter_name}", cwd=project_path, timeout=5)
            if check["exit_code"] == 0:
                result = await executor.execute(linter_cmd, cwd=project_path, timeout=30)
                output = result.get("stdout", "") or result.get("stderr", "")
                if output.strip():
                    results["linter"] = linter_name
                    for line in output.strip().split("\n")[:30]:
                        if line.strip():
                            results["issues"].append(line.strip())
                break

    if "node" in project_types:
        # Пробуем eslint
        check = await executor.execute("npx eslint --version 2>&1", cwd=project_path, timeout=10)
        if check["exit_code"] == 0:
            result = await executor.execute(
                "npx eslint . --format compact 2>&1 | head -30",
                cwd=project_path, timeout=30
            )
            output = result.get("stdout", "") or result.get("stderr", "")
            if output.strip() and "error" in output.lower():
                results["linter"] = "eslint"
                for line in output.strip().split("\n")[:20]:
                    if line.strip():
                        results["issues"].append(line.strip())

    return results


# ═══════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════

async def run_tests(project_path: str, framework: str | None) -> dict:
    """Запускает тесты если фреймворк найден."""
    if not framework:
        return {"framework": None, "status": "no_tests", "output": ""}

    if framework == "pytest":
        result = await executor.execute(
            "python -m pytest --tb=short -q 2>&1 | tail -30",
            cwd=project_path, timeout=60
        )
        output = result.get("stdout", "") or result.get("stderr", "")
        summary = ""
        for line in output.strip().split("\n"):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
        return {
            "framework": "pytest",
            "status": "ok" if result["exit_code"] == 0 else "failed",
            "output": output.strip()[-1500:],
            "summary": summary
        }

    elif framework == "jest":
        result = await executor.execute(
            "npx jest --verbose 2>&1 | tail -30",
            cwd=project_path, timeout=60
        )
        output = result.get("stdout", "") or result.get("stderr", "")
        return {
            "framework": "jest",
            "status": "ok" if result["exit_code"] == 0 else "failed",
            "output": output.strip()[-1500:],
            "summary": ""
        }

    elif framework == "npm":
        result = await executor.execute(
            "npm test 2>&1 | tail -30",
            cwd=project_path, timeout=60
        )
        output = result.get("stdout", "") or result.get("stderr", "")
        return {
            "framework": "npm",
            "status": "ok" if result["exit_code"] == 0 else "failed",
            "output": output.strip()[-1500:],
            "summary": ""
        }

    return {"framework": framework, "status": "unknown", "output": ""}


# ═══════════════════════════════════════════════════
# LLM ANALYSIS — анализ ошибок через ИИ
# ═══════════════════════════════════════════════════

async def llm_analyze_errors(errors_text: str, websocket, model_id: str = None) -> str | None:
    """Отправляет ошибки в LLM для анализа и получения рекомендаций."""
    from core.agent import stream_llm_response
    from core.memory import get_history

    if not errors_text or not errors_text.strip():
        return None

    prompt = f"""Проанализируй ошибки в коде проекта и дай краткие рекомендации по исправлению.

ОШИБКИ:
```
{errors_text[:3000]}
```

Ответь в формате:
1. **Причина** — в чём проблема (1-2 предложения)
2. **Исправление** — конкретный код или команда для исправления
3. **Совет** — как избежать в будущем

Будь кратким, не пиши длинные пояснения."""

    history = []
    response = await stream_llm_response(
        prompt, history, websocket,
        model=model_id
    )
    return response


# ═══════════════════════════════════════════════════
# LOG WRITER — сохранение лога + git push
# ═══════════════════════════════════════════════════

async def save_test_log_and_push(project_path: str, report: str, status: str, error_count: int) -> dict:
    """Сохраняет тотальный лог проверки в файл и пушит в git."""
    from core.action_logger import get_logger
    logger = get_logger()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(project_path, "test_logs")

    # Создаём директорию test_logs
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"  [tester] Не удалось создать test_logs: {e}")
        return {"saved": False, "error": str(e)}

    # Формируем имя файла
    status_tag = "OK" if error_count == 0 else f"ERR_{error_count}"
    log_filename = f"test_{timestamp}_{status_tag}.md"
    log_path = os.path.join(log_dir, log_filename)

    # Полный лог с метаданными
    header = f"""# Code Test Report

- **Дата:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- **Статус:** {'✅ Ошибок нет' if error_count == 0 else f'⚠️ {error_count} проблем(ы)'}
- **Путь:** `{project_path}`

---

"""
    full_log = header + report + "\n"

    # Пишем файл
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(full_log)
        print(f"  [tester] Лог сохранён: {log_filename}")
    except Exception as e:
        print(f"  [tester] Ошибка записи лога: {e}")
        return {"saved": False, "error": str(e)}

    # Добавляем .gitignore если нет
    gitignore_path = os.path.join(log_dir, ".gitignore")
    if not os.path.exists(gitignore_path):
        try:
            with open(gitignore_path, "w") as f:
                f.write("# Старые логи (оставляем последние 20)\n")
            # Удаляем старые логи (оставляем 20 последних)
            log_files = sorted([f for f in os.listdir(log_dir) if f.startswith("test_") and f.endswith(".md")])
            if len(log_files) > 20:
                for old_file in log_files[:-20]:
                    try:
                        os.remove(os.path.join(log_dir, old_file))
                    except Exception:
                        pass
        except Exception:
            pass

    # Git add + commit + push
    try:
        await executor.execute("git add test_logs/", cwd=project_path, timeout=10)
        commit_result = await executor.execute(
            f'git commit -m "test: {status_tag} — {timestamp}" --allow-empty',
            cwd=project_path, timeout=10
        )
        push_result = await executor.execute("git push", cwd=project_path, timeout=30)

        push_ok = push_result.get("exit_code", -1) == 0
        commit_out = (commit_result.get("stdout", "") + commit_result.get("stderr", "")).strip()
        push_out = (push_result.get("stdout", "") + push_result.get("stderr", "")).strip()

        print(f"  [tester] Commit+Push: {'OK' if push_ok else 'FAILED'}")
        logger.log("test_log_pushed", level="success" if push_ok else "warning", source="tester",
                   details={"file": log_filename, "push_ok": push_ok})

        return {
            "saved": True,
            "file": log_filename,
            "pushed": push_ok,
            "commit": commit_out.split("\n")[-1] if commit_out else "",
        }
    except Exception as e:
        print(f"  [tester] Git push ошибка: {e}")
        return {"saved": True, "file": log_filename, "pushed": False, "error": str(e)}


# ═══════════════════════════════════════════════════
# MAIN — полный запуск проверки
# ═══════════════════════════════════════════════════

async def run_full_check(project_path: str, websocket, model_id: str = None, run_tests_flag: bool = True) -> dict:
    """Полная проверка проекта: синтаксис → линтинг → тесты → LLM анализ."""

    await websocket.send_json({"type": "typing", "model": "code-tester"})

    await websocket.send_json({"type": "system", "content": "🔍 Начинаю проверку кода..."})

    # 1. Detect project type
    project_types = detect_project_type(project_path)
    if not project_types:
        await websocket.send_json({"type": "system", "content": "Не удалось определить тип проекта"})
        await websocket.send_json({"type": "done"})
        return {"status": "no_project", "steps": []}

    type_names = ", ".join(t.capitalize() for t in project_types)
    await websocket.send_json({"type": "system", "content": f"📦 Тип: {type_names}"})

    all_errors = []
    report_lines = []

    # 2. Syntax check
    await websocket.send_json({"type": "system", "content": "⚙️ Проверка синтаксиса..."})
    if "python" in project_types:
        py_result = await check_python_syntax(project_path)
        report_lines.append(f"**Python синтаксис:** {py_result['checked']} файлов проверено")
        if py_result["errors"]:
            report_lines.append(f"  ❌ {len(py_result['errors'])} ошибок:")
            for e in py_result["errors"][:10]:
                report_lines.append(f"  - `{e['file']}`:{e['line']} — {e['message']}")
                all_errors.append(f"{e['file']}:{e['line']} — {e['message']}")
        else:
            report_lines.append("  ✅ Ошибок нет")

    if "node" in project_types or "react" in project_types or "nextjs" in project_types:
        js_result = await check_js_syntax(project_path)
        report_lines.append(f"**JS/TS синтаксис:** {js_result['checked']} файлов проверено")
        if js_result["errors"]:
            report_lines.append(f"  ❌ {len(js_result['errors'])} ошибок:")
            for e in js_result["errors"][:10]:
                report_lines.append(f"  - `{e['file']}` — {e['message'][:100]}")
                all_errors.append(f"{e['file']} — {e['message'][:100]}")
        else:
            report_lines.append("  ✅ Ошибок нет")

    # 3. Linting
    await websocket.send_json({"type": "system", "content": "🔍 Линтинг..."})
    lint_result = await run_linter(project_path, project_types)
    if lint_result["linter"]:
        report_lines.append(f"**{lint_result['linter']}:**")
        if lint_result["issues"]:
            report_lines.append(f"  ⚠️ {len(lint_result['issues'])} замечаний:")
            for issue in lint_result["issues"][:10]:
                report_lines.append(f"  - {issue[:120]}")
                all_errors.append(issue[:120])
        else:
            report_lines.append("  ✅ Чисто")
    else:
        report_lines.append("**Линтер:** не найден (ruff/flake8/eslint)")

    # 4. Tests
    if run_tests_flag:
        test_framework = detect_test_framework(project_path)
        if test_framework:
            await websocket.send_json({"type": "system", "content": f"🧪 Запуск тестов ({test_framework})..."})
            test_result = await run_tests(project_path, test_framework)
            report_lines.append(f"**Тесты ({test_result['framework']}):**")
            if test_result["summary"]:
                report_lines.append(f"  {test_result['summary']}")
            elif test_result["output"]:
                lines = [l for l in test_result["output"].strip().split("\n") if l.strip()]
                report_lines.append(f"  {' '.join(lines[-3:])}")
            if test_result["status"] == "ok":
                report_lines.append("  ✅ Тесты пройдены")
            elif test_result["status"] == "failed":
                report_lines.append("  ❌ Тесты провалены")
                if test_result["output"]:
                    all_errors.append(f"TEST OUTPUT:\n{test_result['output'][-1000:]}")
        else:
            report_lines.append("**Тесты:** не найдены (pytest/jest)")

    # 5. Summary
    total_errors = len(all_errors)
    if total_errors == 0:
        report_lines.insert(0, "✅ **Проверка завершена — ошибок не найдено**\n")
    else:
        report_lines.insert(0, f"⚠️ **Проверка завершена — {total_errors} проблем(ы)**\n")

    full_report = "\n".join(report_lines)

    # Send report as AI message (rendered as markdown)
    await websocket.send_json({"type": "chunk", "content": full_report})
    await websocket.send_json({"type": "done"})

    # 5.5 Save log to file + git push
    await websocket.send_json({"type": "system", "content": "💾 Сохраняю лог и пушу в Git..."})
    log_result = await save_test_log_and_push(project_path, full_report, "ok" if total_errors == 0 else "has_errors", total_errors)
    if log_result.get("saved"):
        file_msg = f"📄 Лог: `test_logs/{log_result['file']}`"
        if log_result.get("pushed"):
            await websocket.send_json({"type": "system", "content": f"✅ {file_msg} — запушен в Git"})
        else:
            await websocket.send_json({"type": "system", "content": f"⚠️ {file_msg} — сохранён, пуш не удался"})
    else:
        await websocket.send_json({"type": "system", "content": "⚠️ Не удалось сохранить лог файла"})

    # 6. LLM analysis if errors found and model is available
    if total_errors > 0 and model_id:
        await websocket.send_json({"type": "system", "content": "🤖 Анализирую ошибки через ИИ..."})
        analysis = await llm_analyze_errors("\n".join(all_errors[:20]), websocket, model_id=model_id)

    return {"status": "ok" if total_errors == 0 else "has_errors", "error_count": total_errors, "report": full_report}
