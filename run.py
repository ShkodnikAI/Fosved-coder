import json, os, re, subprocess, asyncio, shlex, sys, platform
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from api.endpoints import router, _collect_project_files
from core.chat_history import ChatHistory
from core.chat import stream_chat

app = FastAPI(title="Fosved Coder")
app.include_router(router)

chat_hist = ChatHistory()

@app.get("/")
def index():
    return FileResponse("ui/templates/index.html")

app.mount("/static", StaticFiles(directory="ui/static"), name="static")


def _build_system_prompt(project, messages_count=0):
    """Build system prompt with project context."""
    parts = [
        "Ты — Fosved Coder AI, опытный программист-ассистент. Помогай пользователю с кодом, архитектурой, "
        "отладкой и разработкой. Отвечай на том языке, на котором задан вопрос.",
        "Форматируй код в блоках ```язык ... ``` с указанием языка."
    ]
    if project:
        if project.get("instructions"):
            parts.append(f"\nИНСТРУКЦИИ ПРОЕКТА:\n{project['instructions']}")
        if project.get("prompt"):
            parts.append(f"\nОПИСАНИЕ ПРОЕКТА:\n{project['prompt']}")
        if project.get("ideas"):
            parts.append(f"\nИДЕИ И ЗАМЕТКИ:\n{project['ideas']}")
        if project.get("github_repo"):
            parts.append(f"\nGitHub репозиторий: {project['github_repo']}")
        if project.get("name"):
            parts.append(f"\nНазвание проекта: {project['name']}")
        if project.get("folder"):
            parts.append(f"\nЛокальная папка: {project['folder']}")
    return "\n".join(parts)


def _replace_read_tags(text, project):
    """Replace [READ:path] tags with file contents."""
    folder = project.get("folder", "") if project else ""
    if not folder:
        return text
    def read_file(match):
        rel_path = match.group(1).strip()
        full_path = os.path.normpath(os.path.join(folder, rel_path))
        if not full_path.startswith(os.path.normpath(folder)):
            return match.group(0)
        if not os.path.isfile(full_path):
            return f"[Файл не найден: {rel_path}]"
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(5000)
            return f"\n--- Содержимое {rel_path} ---\n{content}\n--- Конец файла ---\n"
        except:
            return f"[Ошибка чтения: {rel_path}]"
    return re.sub(r'\[READ:([^\]]+)\]', read_file, text)


def _auto_detect_file_reads(text, project):
    """Auto-detect patterns like 'read main.py' and append file contents."""
    folder = project.get("folder", "") if project else ""
    if not folder or not folder.strip():
        return text, []
    files_read = []
    patterns = [
        r'(?:прочитай|read|посмотри|покажи|открой)\s+([a-zA-Z0-9_./\-]+\.\w{1,5})',
        r'(?:файл|file)\s+([a-zA-Z0-9_./\-]+\.\w{1,5})',
    ]
    additions = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            fname = match.group(1)
            if fname in files_read:
                continue
            files_read.append(fname)
            full_path = os.path.normpath(os.path.join(folder, fname))
            if not full_path.startswith(os.path.normpath(folder)):
                continue
            if not os.path.isfile(full_path):
                additions.append(f"\n[Файл не найден: {fname}]")
                continue
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(5000)
                additions.append(f"\n--- Содержимое {fname} ---\n{content}\n--- Конец файла ---\n")
            except:
                additions.append(f"\n[Ошибка чтения: {fname}]")
    if additions:
        return text + "\n".join(additions), files_read
    return text, []


@app.websocket("/ws/chat/{project_id}")
async def ws_chat(websocket: WebSocket, project_id: str):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message", "")
            model_id = data.get("model", "")
            thread_id = data.get("thread_id", "main")
            project = None

            # Load project context
            projects_file = "data/projects.json"
            if os.path.exists(projects_file):
                with open(projects_file, "r", encoding="utf-8") as f:
                    projects = json.load(f)
                    project = projects.get(project_id)

            # Save user message
            chat_hist.save_message(project_id, "user", message, thread_id)

            # Process file reads
            message = _replace_read_tags(message, project)
            message, _ = _auto_detect_file_reads(message, project)

            # Build message history
            history = chat_hist.load_history(project_id, thread_id)
            system_prompt = _build_system_prompt(project, len(history))
            messages = [{"role": "system", "content": system_prompt}]

            # Add history (last 30 messages for context window)
            for msg in history[-30:]:
                if msg.get("role") in ("user", "assistant"):
                    messages.append({"role": msg["role"], "content": msg["content"]})

            # Use selected model or project default
            if not model_id and project:
                model_id = project.get("selected_model", "")

            # Stream response
            full_response = ""
            async for token, is_complete, error in stream_chat(messages, model_id):
                if error:
                    await websocket.send_json({"type": "error", "content": error})
                    break
                if token:
                    full_response += token
                    await websocket.send_json({"type": "token", "content": token})
                if is_complete and full_response:
                    # Save assistant response
                    chat_hist.save_message(project_id, "assistant", full_response, thread_id)
                    await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except:
            pass


# ===== TERMINAL WEBSOCKET =====
def _get_shell():
    """Detect appropriate shell for the OS."""
    if platform.system() == "Windows":
        return "powershell.exe"
    return "/bin/bash"

# Track running processes for kill support
_running_processes = {}

@app.websocket("/ws/terminal/{project_id}")
async def ws_terminal(websocket: WebSocket, project_id: str):
    await websocket.accept()
    try:
        # Get project folder as working directory
        projects_file = "data/projects.json"
        cwd = None
        if os.path.exists(projects_file):
            with open(projects_file, "r", encoding="utf-8") as f:
                projects = json.load(f)
                project = projects.get(project_id)
                if project:
                    cwd = project.get("folder", "")
        if not cwd or not os.path.isdir(cwd):
            cwd = None

        while True:
            data = await websocket.receive_json()
            action = data.get("action", "run")
            cmd = data.get("command", "")

            if action == "run":
                await websocket.send_json({"type": "status", "content": "running"})
                try:
                    shell = _get_shell()
                    is_windows = platform.system() == "Windows"

                    if is_windows:
                        # PowerShell: combine all args
                        proc = await asyncio.create_subprocess_shell(
                            cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=cwd,
                            shell=True
                        )
                    else:
                        # Bash: use shlex for safety
                        proc = await asyncio.create_subprocess_shell(
                            cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=cwd,
                            shell=True,
                            executable="/bin/bash"
                        )

                    _running_processes[project_id] = proc

                    # Stream stdout
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        await websocket.send_json({"type": "output", "content": text})

                    # Stream stderr
                    while True:
                        line = await proc.stderr.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        await websocket.send_json({"type": "error", "content": text})

                    await proc.wait()
                    exit_code = proc.returncode
                    _running_processes.pop(project_id, None)
                    await websocket.send_json({
                        "type": "done",
                        "exit_code": exit_code,
                        "content": f"\n[Процесс завершён с кодом {exit_code}]"
                    })

                except Exception as e:
                    _running_processes.pop(project_id, None)
                    await websocket.send_json({"type": "error", "content": f"Ошибка: {str(e)}"})

            elif action == "kill":
                proc = _running_processes.get(project_id)
                if proc:
                    try:
                        proc.kill()
                        _running_processes.pop(project_id, None)
                        await websocket.send_json({"type": "done", "exit_code": -1, "content": "[Процесс принудительно остановлен]"})
                    except:
                        pass
                else:
                    await websocket.send_json({"type": "error", "content": "Нет запущенного процесса"})

    except WebSocketDisconnect:
        _running_processes.pop(project_id, None)
    except Exception as e:
        _running_processes.pop(project_id, None)
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except:
            pass


# ===== REFACTOR WEBSOCKET =====
REFACTOR_SYSTEM = """Ты — Fosved Coder AI, эксперт по рефакторингу кода. Твоя задача — проанализировать предоставленные файлы проекта и предложить конкретные улучшения.

Структура ответа ОБЯЗАТЕЛЬНО:

## 📊 Общая оценка
Краткое резюме состояния кода.

## 🔴 Критические проблемы
Перечисли критические проблемы (безопасность, баги, утечки).

## 🟡 Улучшения
Предложения по улучшению кода. Для каждого:
- Файл: путь к файлу
- Проблема: описание
- Решение: конкретный исправленный код в блоке ```язык

## 🟢 Хорошее
Что уже сделано хорошо.

## 📋 Чеклист
- [ ] Пункт 1
- [ ] Пункт 2

ВАЖНО: Всегда указывай полный путь к файлу в формате [FILE:путь/к/файлу.расширение] перед блоками кода с исправлениями."""


@app.websocket("/ws/refactor/{project_id}")
async def ws_refactor(websocket: WebSocket, project_id: str):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        model_id = data.get("model", "")
        focus = data.get("focus", "full")  # full, security, performance, style

        # Load project
        projects_file = "data/projects.json"
        if not os.path.exists(projects_file):
            await websocket.send_json({"type": "error", "content": "Проект не найден"})
            return
        with open(projects_file, "r", encoding="utf-8") as f:
            projects = json.load(f)
        project = projects.get(project_id)
        if not project:
            await websocket.send_json({"type": "error", "content": "Проект не найден"})
            return

        folder = project.get("folder", "")
        if not folder or not os.path.isdir(folder):
            await websocket.send_json({"type": "error", "content": "Папка проекта не задана"})
            return

        # Collect files
        await websocket.send_json({"type": "status", "content": "collecting"})
        files = _collect_project_files(folder, max_files=30)
        if not files:
            await websocket.send_json({"type": "error", "content": "Нет исходных файлов для анализа"})
            return

        await websocket.send_json({"type": "status", "content": f"Анализирую {len(files)} файлов..."})

        # Build code dump
        code_dump = f"=== ПРОЕКТ: {project.get('name', '')} ===\n\n"
        for f in files:
            code_dump += f"--- {f['path']} ({f['lang']}, {f['size']} байт) ---\n{f['content']}\n\n"

        # Build focus instruction
        focus_map = {
            "full": "Полный анализ: безопасность, производительность, читаемость, архитектура",
            "security": "Фокус на БЕЗОПАСНОСТИ: уязвимости, инъекции, утечки данных",
            "performance": "Фокус на ПРОИЗВОДИТЕЛЬНОСТИ: оптимизация, алгоритмы, память",
            "style": "Фокус на СТИЛЕ: читаемость, именование, структура, DRY",
        }
        focus_text = focus_map.get(focus, focus_map["full"])

        messages = [
            {"role": "system", "content": REFACTOR_SYSTEM + "\n\nФокус анализа: " + focus_text},
            {"role": "user", "content": f"Проанализируй и предложи рефакторинг:\n\n{code_dump}"}
        ]

        # Use selected model or project default
        if not model_id:
            model_id = project.get("selected_model", "")

        # Stream response
        full_response = ""
        async for token, is_complete, error in stream_chat(messages, model_id):
            if error:
                await websocket.send_json({"type": "error", "content": error})
                return
            if token:
                full_response += token
                await websocket.send_json({"type": "token", "content": token})
            if is_complete:
                await websocket.send_json({"type": "done", "content": full_response})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
