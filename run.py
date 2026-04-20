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
from core.action_logger import get_logger
from api.endpoints import router as api_router

logger = get_logger()

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
    from core.memory import IS_POSTGRES, DB_URL
    # Sanitize URL for banner
    safe_url = DB_URL
    if "://" in safe_url:
        parts = safe_url.split("://", 1)
        auth_part = parts[1].split("@", 1)
        if len(auth_part) == 2 and ":" in auth_part[0]:
            user = auth_part[0].split(":")[0]
            safe_url = f"{parts[0]}://{user}:****@{auth_part[1]}"

    print(f"\n  {'=' * 44}")
    print(f"  Fosved Coder v2.0 — Запуск")
    print(f"  БД: {'PostgreSQL (постоянная)' if IS_POSTGRES else 'SQLite (локальная)'}")
    if IS_POSTGRES:
        print(f"  URL: {safe_url}")
    print(f"  {'=' * 44}\n")
    await init_db()
    # Validate all API keys on startup
    print("  Проверка API-ключей...")
    results = await keys_manager.startup_validation()
    for pid, info in results.items():
        if pid == "local" and isinstance(info, dict):
            # info — dict of {model_id: {status, name}}
            count = len(info)
            print(f"    local: {count} локальных моделей")
            continue
        status_icon = {"valid": "+", "rate_limited": "!", "invalid": "x", "available": "*"}.get(info.get("status", "?"), "?")
        model_count = len(info.get("models", []))
        print(f"    [{status_icon}] {pid}: {info.get('status', '?')} ({model_count} моделей)")
    gh = keys_manager.get_github_status()
    if gh["has_token"]:
        icon = "+" if gh["enabled"] else "o"
        print(f"    [{icon}] GitHub: {'активен (' + gh['user'] + ')' if gh['enabled'] else 'отключён'}")
    print("  Ключи проверены\n")
    print(f"  Готово! Откройте приложение в браузере.\n")
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
    current_mode = "manual"  # "manual" or "auto"
    model_id = None
    logger.log("websocket_connected", level="info", source="ws")

    try:
        while True:
            data = await websocket.receive_text()

            # Handle slash commands
            if data.startswith("/"):
                logger.log(f"command: {data[:100]}", level="info", source="ws", project_id=current_project_id)
                await handle_command(data, current_project_id, websocket, model_id)
                continue

            # Parse JSON payload (chat message with model/priority info)
            import json
            try:
                payload = json.loads(data)
                prompt = payload.get("prompt", data)
                model_id = payload.get("model")
                priority = payload.get("priority_models", [])
                mode = payload.get("mode", current_mode)
                # Sync project_id from client (critical: keeps project context)
                client_project_id = payload.get("project_id")
                if client_project_id is not None:
                    current_project_id = client_project_id
            except (json.JSONDecodeError, TypeError):
                prompt = data
                model_id = None
                priority = []
                payload = {}

            # Log incoming ws message
            logger.ws_message("in", payload, project_id=current_project_id)

            # Handle heartbeat ping
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # Handle mode change
            if payload.get("type") == "mode_change":
                new_mode = payload.get("mode", "manual")
                current_mode = new_mode
                logger.user_action(f"mode_change: {new_mode}", project_id=current_project_id)
                await websocket.send_json({"type": "system", "content": f"Режим: {'Автоматический' if new_mode == 'auto' else 'Ручной'}"})
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
                        try:
                            logger.log("priority_models_updated", level="debug", source="ws", project_id=current_project_id, details={"models": priority[:5]})
                        except Exception:
                            pass

                    cached_map = await get_repo_map(current_project_id)
                    if cached_map:
                        repo_map = cached_map["content"]
                    else:
                        repo_map = await context_manager.build_repo_map(
                            project["path"], current_project_id
                        )

            # Route based on mode
            if mode == "auto":
                logger.user_action("auto_mode_chat", project_id=current_project_id, details={"model": model_id})
                from core.auto_agent import run_auto_mode
                await run_auto_mode(prompt, current_project_id, repo_map, websocket, model_id=model_id)
            else:
                logger.user_action("manual_chat", project_id=current_project_id, details={"model": model_id})
                await handle_chat_message(prompt, current_project_id, repo_map, websocket, model_id=model_id)

    except WebSocketDisconnect:
        logger.log("websocket_disconnected", level="info", source="ws", project_id=current_project_id)
    except Exception as e:
        logger.log("websocket_error", level="error", source="ws", project_id=current_project_id, error=str(e))


async def handle_command(cmd: str, project_id, websocket, model_id: str = None):
    """Handle slash commands from the UI"""
    parts = cmd.strip().split(" ", 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    await websocket.send_json({"type": "system", "content": f"Выполняю: {cmd}"})
    try:
        logger.log(f"slash_command: {command} {args[:80]}", level="info", source="ws", project_id=project_id)
    except Exception:
        pass

    if command == "/terminal":
        try:
            logger.log(f"terminal_exec: {args[:120]}", level="info", source="ws", project_id=project_id, details={"cwd": None})
        except Exception:
            pass
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
        try:
            logger.log("terminal_done", level="success", source="ws", project_id=project_id, details={"exit_code": result.get("exit_code"), "approved": False})
        except Exception:
            pass

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
        try:
            logger.log("git_pull_start", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        result = await executor.execute("git pull", cwd=project_path)
        exit_code = result.get("exit_code", -1)
        if exit_code == 0:
            stdout = result.get("stdout", "").strip()
            # Извлечь полезную инфу: "Already up to date" или "Updating X..Y"
            lines = [l.strip() for l in stdout.split("\n") if l.strip() and not l.startswith("From ")]
            summary = lines[0] if lines else "Already up to date"
            await websocket.send_json({"type": "system", "content": f"📥 Pull OK: {summary}"})
        else:
            await websocket.send_json({"type": "error", "content": f"📥 Pull failed: {result.get('stderr', 'unknown error')[:200]}"})
            try:
                logger.log("git_pull_failed", level="error", source="ws", project_id=project_id, error=result.get('stderr', '')[:200])
            except Exception:
                pass
        await websocket.send_json({"type": "done"})

    elif command == "/git_push":
        try:
            logger.log("git_push_start", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        result = await executor.execute("git push", cwd=project_path)
        exit_code = result.get("exit_code", -1)
        if exit_code == 0:
            stdout = result.get("stdout", "").strip()
            if "Everything up-to-date" in stdout:
                await websocket.send_json({"type": "system", "content": "📤 Push: уже актуально"})
            else:
                # Извлечь что запушилось
                lines = [l for l in stdout.split("\n") if l.strip()]
                summary = lines[-1] if lines else "OK"
                await websocket.send_json({"type": "system", "content": f"📤 Push OK: {summary}"})
        else:
            await websocket.send_json({"type": "error", "content": f"📤 Push failed: {result.get('stderr', 'unknown error')[:200]}"})
            try:
                logger.log("git_push_failed", level="error", source="ws", project_id=project_id, error=result.get('stderr', '')[:200])
            except Exception:
                pass
        await websocket.send_json({"type": "done"})

    elif command == "/quick_push":
        try:
            logger.log(f"quick_push_start: {args[:50]}", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        # Тихий push: auto commit + push без лишнего вывода
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        if not project_path:
            await websocket.send_json({"type": "system", "content": "Выберите проект для Quick Push"})
            await websocket.send_json({"type": "done"})
            return

        msg = args.strip() or "sync"
        # git add -A
        await executor.execute("git add -A", cwd=project_path)
        # git commit (с --allow-empty чтобы не падал)
        commit_result = await executor.execute(f'git commit -m "{msg}" --allow-empty', cwd=project_path)
        commit_out = (commit_result.get("stdout", "") + commit_result.get("stderr", "")).strip()
        if "nothing to commit" in commit_out.lower():
            await websocket.send_json({"type": "system", "content": "📤 Quick Push: нет изменений для коммита"})
        else:
            # push
            push_result = await executor.execute("git push", cwd=project_path)
            push_exit = push_result.get("exit_code", -1)
            if push_exit == 0:
                await websocket.send_json({"type": "system", "content": f"📤 Quick Push OK: {msg}"})
            else:
                await websocket.send_json({"type": "system", "content": f"📤 Committed, push failed: {(push_result.get('stderr', ''))[:100]}"})
        await websocket.send_json({"type": "done"})

    elif command == "/clear":
        await clear_history(project_id)
        await websocket.send_json({"type": "system", "content": "История чата очищена."})

    elif command == "/test":
        try:
            logger.log(f"code_test_start: {args[:50]}", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        # Тестирование и проверка кода проекта
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
        if not project_path:
            await websocket.send_json({"type": "system", "content": "Выберите проект для проверки"})
            await websocket.send_json({"type": "done"})
            return
        from core.code_tester import run_full_check
        # Determine if user wants to skip tests (--no-tests flag)
        skip_tests = "--no-tests" in args or "--skip-tests" in args
        await run_full_check(project_path, websocket, model_id=model_id, run_tests_flag=not skip_tests)

    elif command == "/ideas":
        try:
            logger.log(f"ideas_inject: {args[:80]}", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
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
            "/git_pull — git pull (тихий)\n"
            "/git_push — git push (тихий)\n"
            "/quick_push [msg] — commit + push одним действием\n"
            "/test [--no-tests] — проверить код проекта (синтаксис, линт, тесты)\n"
            "/ideas <github_url> — проанализировать репозиторий\n"
            "/repo_map — показать структуру проекта\n"
            "/clear — очистить историю чата\n"
            "/help — эта справка"
        )
        await websocket.send_json({"type": "system", "content": help_text})

    else:
        try:
            logger.log(f"unknown_command: {command}", level="warning", source="ws", project_id=project_id)
        except Exception:
            pass
        await websocket.send_json({"type": "system", "content": f"Неизвестная команда: {command}. Введите /help"})


@app.websocket("/ws/executor")
async def websocket_executor(websocket: WebSocket):
    """Dedicated WebSocket for real-time command output streaming"""
    await websocket.accept()
    try:
        logger.log("executor_ws_connected", level="info", source="ws")
    except Exception:
        pass
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
        try:
            logger.log("executor_ws_disconnected", level="info", source="ws")
        except Exception:
            pass
    except Exception as e:
        try:
            logger.log("executor_ws_error", level="error", source="ws", error=str(e))
        except Exception:
            pass


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    print("  +========================================+")
    print("  |   Fosved Coder v2.0                  |")
    print(f"  |   http://0.0.0.0:{port:<21}|")
    print("  +========================================+")
    uvicorn.run("run:app", host="0.0.0.0", port=port, reload=False)
