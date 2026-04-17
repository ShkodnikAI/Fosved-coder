import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from core.memory import init_db, save_message, clear_history, get_project, get_repo_map
from core.keys_manager import keys_manager
from core.agent import handle_chat_message
from core.executor import CommandExecutor
from core.ideas_injector import IdeasInjector
from core.context_manager import ContextManager
from core.router import HybridRouter
from api.endpoints import router as api_router

# Global instances
executor = CommandExecutor()
ideas_injector = IdeasInjector()
context_manager = ContextManager()
hybrid_router = HybridRouter()

# Track pending approvals: {request_id: {"cmd": str, "websocket": WebSocket}}
pending_approvals: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB
    await init_db()
    # Validate all API keys on startup
    print("  ⏳ Проверка API-ключей...")
    results = await keys_manager.startup_validation()
    for pid, info in results.items():
        if pid == "local" and isinstance(info, dict):
            # info — dict of {model_id: {status, name}}
            count = len(info)
            print(f"    ● local: {count} локальных моделей")
            continue
        status_icon = {"valid": "✓", "rate_limited": "⚠", "invalid": "✗", "available": "●"}.get(info.get("status", "?"), "?")
        model_count = len(info.get("models", []))
        print(f"    {status_icon} {pid}: {info.get('status', '?')} ({model_count} моделей)")
    gh = keys_manager.get_github_status()
    if gh["has_token"]:
        icon = "✓" if gh["enabled"] else "○"
        print(f"    {icon} GitHub: {'активен (' + gh['user'] + ')' if gh['enabled'] else 'отключён'}")
    print("  ✓ Ключи проверены\n")
    yield


app = FastAPI(title="Fosved Coder", version="2.0", lifespan=lifespan)

# Include REST API router
app.include_router(api_router)

@app.get("/")
async def get_index():
    return FileResponse("ui/templates/index.html")

app.mount("/static", StaticFiles(directory="ui/static"), name="static")

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and return its contents as text."""
    content_bytes = await file.read()
    try:
        text_content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text_content = f"[Binary file: {file.filename}, {len(content_bytes)} bytes]"
    return {"filename": file.filename, "content": text_content, "size": len(content_bytes)}


@app.websocket("/ws")
async def websocket_chat(websocket: WebSocket):
    """Main chat WebSocket — streaming AI responses + command execution"""
    await websocket.accept()
    current_project_id = None
    repo_map = None

    try:
        while True:
            data = await websocket.receive_text()

            # Handle slash commands
            if data.startswith("/"):
                await handle_command(data, current_project_id, websocket)
                continue

            # Parse JSON payload (chat message with model/priority info)
            import json
            try:
                payload = json.loads(data)
                prompt = payload.get("prompt", data)
                model_id = payload.get("model")
                priority = payload.get("priority_models", [])
            except (json.JSONDecodeError, TypeError):
                prompt = data
                model_id = None
                priority = []
                payload = {}

            # Handle heartbeat ping
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle refactor requests
            if payload.get("type") == "refactor":
                refactor_code = payload.get("code", "")
                refactor_type = payload.get("refactor_type", "optimize")
                instructions = payload.get("instructions", "")
                type_prompts = {
                    "optimize": "Оптимизируй этот код для лучшей производительности",
                    "clean": "Очисти и отформатируй этот код",
                    "modernize": "Модернизируй этот код, используя современные возможности Python 3.10+",
                    "simplify": "Упрости логику этого кода",
                    "document": "Добавь полные docstrings и комментарии к этому коду",
                    "type_hints": "Добавь аннотации типов ко всем функциям и переменным",
                    "error_handling": "Улучши обработку ошибок в этом коде",
                }
                refactor_prompt = f"""{type_prompts.get(refactor_type, 'Рефактори этот код')}.
{'Дополнительные инструкции: ' + instructions if instructions else ''}
Верни ТОЛЬКО улучшенный код без пояснений, в code block.

Код для рефакторинга:
```
{refactor_code}
```"""
                await handle_chat_message(refactor_prompt, current_project_id, repo_map, websocket)
                continue

            # Build project context (Repo Map)
            if current_project_id:
                project = await get_project(current_project_id)
                if project:
                    # Override priority models from UI if provided
                    if priority:
                        from core.memory import update_project_models
                        await update_project_models(current_project_id, priority)

                    cached_map = await get_repo_map(current_project_id)
                    if cached_map:
                        repo_map = cached_map["content"]
                    else:
                        repo_map = await context_manager.build_repo_map(
                            project["path"], current_project_id
                        )

            # Route and execute AI response (pass model_id from UI)
            await handle_chat_message(prompt, current_project_id, repo_map, websocket, model_id=model_id)

    except WebSocketDisconnect:
        pass


async def handle_command(cmd: str, project_id, websocket):
    """Handle slash commands from the UI"""
    parts = cmd.strip().split(" ", 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    await websocket.send_json({"type": "system", "content": f"Выполняю: {cmd}"})

    if command == "/terminal":
        result = await executor.execute(args, cwd=None)
        if result.get("approval_required"):
            request_id = result["request_id"]
            pending_approvals[request_id] = {"cmd": args, "websocket": websocket}
            await websocket.send_json({
                "type": "approval_required",
                "content": result["message"],
                "request_id": request_id,
                "cmd": args
            })
        else:
            output = f"Exit code: {result['exit_code']}\n\n{result['stdout']}"
            if result.get("stderr"):
                output += f"\n\nSTDERR:\n{result['stderr']}"
            await websocket.send_json({"type": "command_result", "content": output})
        await websocket.send_json({"type": "done"})

    elif command == "/approve":
        request_id = args.strip()
        if request_id in pending_approvals:
            pending = pending_approvals.pop(request_id)
            await websocket.send_json({"type": "system", "content": f"Подтверждаю: {pending['cmd']}"})
            result = await executor.execute_approved(pending["cmd"], request_id)
            output = f"Exit code: {result['exit_code']}\n\n{result['stdout']}"
            if result.get("stderr"):
                output += f"\n\nSTDERR:\n{result['stderr']}"
            await websocket.send_json({"type": "command_result", "content": output})
            await websocket.send_json({"type": "done"})
        else:
            await websocket.send_json({"type": "system", "content": "Нет ожидающих подтверждения команд."})

    elif command == "/reject":
        request_id = args.strip()
        if request_id in pending_approvals:
            pending_approvals.pop(request_id)
            await websocket.send_json({"type": "system", "content": "Команда отклонена."})
        else:
            await websocket.send_json({"type": "system", "content": "Нет ожидающих команд."})

    elif command == "/git_pull":
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        result = await executor.execute("git pull", cwd=project_path)
        await websocket.send_json({"type": "command_result", "content": result.get("stdout") or result.get("stderr", "Готово")})
        await websocket.send_json({"type": "done"})

    elif command == "/git_push":
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        result = await executor.execute("git push", cwd=project_path)
        await websocket.send_json({"type": "command_result", "content": result.get("stdout") or result.get("stderr", "Готово")})
        await websocket.send_json({"type": "done"})

    elif command == "/clear":
        await clear_history(project_id)
        await websocket.send_json({"type": "system", "content": "История чата очищена."})

    elif command == "/ideas":
        if not args.strip():
            await websocket.send_json({"type": "system", "content": "Использование: /ideas <github_url>"})
            return
        result = await ideas_injector.process_idea(args.strip())
        await websocket.send_json({"type": "idea_result", "content": result})
        await websocket.send_json({"type": "done"})

    elif command == "/repo_map":
        if project_id:
            project = await get_project(project_id)
            if project:
                repo_map = await context_manager.build_repo_map(project["path"], project_id)
                await websocket.send_json({"type": "command_result", "content": repo_map})
                await websocket.send_json({"type": "done"})
            else:
                await websocket.send_json({"type": "system", "content": "Проект не найден."})
        else:
            await websocket.send_json({"type": "system", "content": "Выберите проект для построения Repo Map."})

    elif command == "/help":
        help_text = (
            "Доступные команды:\n"
            "/terminal <cmd> — выполнить shell-команду\n"
            "/approve <id> — подтвердить критическую команду\n"
            "/reject <id> — отклонить критическую команду\n"
            "/git_pull — git pull в текущем проекте\n"
            "/git_push — git push в текущем проекте\n"
            "/ideas <github_url> — проанализировать репозиторий\n"
            "/repo_map — показать структуру проекта\n"
            "/clear — очистить историю чата\n"
            "/help — эта справка"
        )
        await websocket.send_json({"type": "system", "content": help_text})

    else:
        await websocket.send_json({"type": "system", "content": f"Неизвестная команда: {command}. Введите /help"})


@app.websocket("/ws/executor")
async def websocket_executor(websocket: WebSocket):
    """Dedicated WebSocket for real-time command output streaming"""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            cmd = data.get("command", "")
            request_id = data.get("request_id", "")

            async for chunk in executor.execute_stream(cmd):
                await websocket.send_json({
                    "type": "stream",
                    "request_id": request_id,
                    "data": chunk
                })
            await websocket.send_json({"type": "stream_done", "request_id": request_id})
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Fosved Coder v2.0 — Starting...    ║")
    print(f"  ║   http://0.0.0.0:{port}               ║")
    print("  ╚══════════════════════════════════════╝")
    uvicorn.run("run:app", host="0.0.0.0", port=port, reload=False)
