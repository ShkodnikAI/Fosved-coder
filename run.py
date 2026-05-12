import os
import re
import uvicorn
import shlex
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from core.memory import init_db, save_message, clear_history, get_project, get_repo_map, git_push_with_token, save_probed_models, get_probed_models, save_questionnaire
from core.keys_manager import keys_manager
from core.agent import handle_chat_message, handle_hub_message
from core.executor import CommandExecutor
from core.ideas_injector import IdeasInjector
from core.context_manager import ContextManager
from core.intelligent_router import intelligent_router
from core.action_logger import get_logger
from core.observation_manager import store_observation, assemble_context, generate_session_summary_async, ensure_observation_tables
from api.endpoints import router as api_router

logger = get_logger()


async def safe_ws_send(websocket, data: dict):
    """Send JSON to websocket, silently ignoring any errors (closed conn, etc.)."""
    try:
        await websocket.send_json(data)
    except Exception:
        pass


# Global instances
executor = CommandExecutor()
ideas_injector = IdeasInjector()
context_manager = ContextManager()
# hybrid_router removed — see core/intelligent_router.py

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

    # CRITICAL: Восстановить ключи из БД если keys.yaml пустой (Render ephemeral FS)
    if keys_manager._db_restore_pending:
        print("  keys.yaml пуст — восстанавливаем ключи из базы данных...")
        restored = await keys_manager.restore_from_db()
        if restored:
            print(f"  Ключи восстановлены из БД\n")

    # Validate all API keys in BACKGROUND (не блокирует старт — WS сразу доступен)
    async def _bg_validate_keys():
        try:
            print("  [bg] Проверка API-ключей...")
            results = await keys_manager.startup_validation()
            await keys_manager.sync_to_db()
            for pid, info in results.items():
                if pid == "local" and isinstance(info, dict):
                    count = len(info)
                    print(f"    [bg] local: {count} локальных моделей")
                    continue
                status_icon = {"valid": "+", "rate_limited": "!", "invalid": "x", "available": "*"}.get(info.get("status", "?"), "?")
                model_count = len(info.get("models", []))
                print(f"    [bg] [{status_icon}] {pid}: {info.get('status', '?')} ({model_count} моделей)")
            gh = keys_manager.get_github_status()
            if gh["has_token"]:
                icon = "+" if gh["enabled"] else "o"
                print(f"    [bg] [{icon}] GitHub: {'активен (' + gh['user'] + ')' if gh['enabled'] else 'отключён'}")
            print("  [bg] Ключи проверены")
        except Exception as e:
            print(f"  [bg] Key validation error: {e}")
    asyncio.create_task(_bg_validate_keys())

    # Abacus.AI: загрузка моделей в фоне (не блокирует старт)
    async def _bg_load_abacus():
        try:
            abacus_cfg = keys_manager.providers.get("abacus", {})
            if abacus_cfg.get("api_key"):
                result = await keys_manager.fetch_abacus_models()
                if result["success"]:
                    print(f"  [abacus] Загружено {result['count']} моделей в фоне")
                else:
                    print(f"  [abacus] Фоновая загрузка не удалась: {result.get('error', '?')[:80]}")
        except Exception as e:
            print(f"  [abacus] Фоновая загрузка: {e}")
    asyncio.create_task(_bg_load_abacus())

    # Тихое зондирование моделей при старте (фоновая задача)
    async def _background_probe():
        try:
            # Сначала восстановить кэш probing из БД (если есть)
            try:
                cached = await get_probed_models()
                if cached:
                    keys_manager.update_probed_model_ids(cached)
                    print(f"  [startup] Restored probe cache: {len(cached)} models")
            except Exception:
                pass

            from core.agent import probe_models
            results = await probe_models()
            if results:
                await save_probed_models(results)
                print(f"  [startup] Probed {len(results)} models successfully")
            else:
                print(f"  [startup] No models responded to probing")
        except Exception as e:
            print(f"  [startup] Probe failed: {e}")
    asyncio.create_task(_background_probe())

    # Init observation/memory tables (claude-mem inspired)
    try:
        await ensure_observation_tables()
        print(f"  [memory] Observation tables ready")
    except Exception as e:
        print(f"  [memory] Warning: {e}")

    print(f"  Готово! Откройте приложение в браузере.\n")
    yield


app = FastAPI(title="Fosved Coder", version="2.0", lifespan=lifespan)

# Include REST API router
app.include_router(api_router)

@app.get("/")
async def get_index():
    return FileResponse("ui/templates/index.html")

app.mount("/static", StaticFiles(directory="ui/static"), name="static")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
WS_RECEIVE_TIMEOUT = 30  # seconds — drop idle WS connections (proxy keepalive is separate)
WS_MAX_MESSAGE_BYTES = 2 * 1024 * 1024  # 2 MB per message
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ]")


def _sanitize_filename(name: str | None) -> str:
    """Strip path components and dangerous chars from a user-supplied filename."""
    base = os.path.basename(name or "upload")
    base = _SAFE_FILENAME_RE.sub("_", base).strip().strip(".") or "upload"
    return base[:200]


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file and return its contents as text. Limited to MAX_UPLOAD_BYTES."""
    # Stream-read with size cap to avoid OOM on huge uploads.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        chunks.append(chunk)
    content_bytes = b"".join(chunks)
    safe_name = _sanitize_filename(file.filename)
    try:
        text_content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text_content = f"[Binary file: {safe_name}, {len(content_bytes)} bytes]"
    return {"filename": safe_name, "content": text_content, "size": len(content_bytes)}


@app.websocket("/ws")
async def websocket_chat(websocket: WebSocket):
    """Main chat WebSocket — streaming AI responses + command execution"""
    await websocket.accept()
    current_project_id = None
    repo_map = None
    current_mode = "manual"  # "manual" or "auto"
    model_id = None
    ws_session_id = str(__import__('uuid').uuid4())  # Unique session ID for memory
    logger.log("websocket_connected", level="info", source="ws")

    # ── Cancellation flag: set by client to abort current generation ──
    ws_cancelled = False

    # ── Server-side keepalive: ping every 4 sec to prevent proxy idle kill ──
    async def _ws_keepalive():
        try:
            while True:
                await asyncio.sleep(4)
                try:
                    await safe_ws_send(websocket, {"type": "ping"})
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
    keepalive_task = asyncio.create_task(_ws_keepalive())

    # Задача 2: Отправить клиенту кэшированные результаты probing
    try:
        probed = await get_probed_models()
        if probed:
            await safe_ws_send(websocket, {"type": "probed_models", "models": probed})
    except Exception:
        pass

    # Inject memory context from previous sessions (claude-mem inspired)
    try:
        memory_ctx = await assemble_context(project_id=None, max_tokens=300)
        if memory_ctx:
            await safe_ws_send(websocket, {
                "type": "auto_log",
                "content": f"🧠 Память загружена",
                "level": "info",
            })
    except Exception:
        pass

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=WS_RECEIVE_TIMEOUT)
            except asyncio.TimeoutError:
                # Idle: send a ping; if peer is gone, the next iteration will raise
                try:
                    await safe_ws_send(websocket, {"type": "ping"})
                except Exception:
                    raise WebSocketDisconnect()
                continue

            if len(data) > WS_MAX_MESSAGE_BYTES:
                await safe_ws_send(websocket, {"type": "error", "content": f"Сообщение превышает лимит {WS_MAX_MESSAGE_BYTES // (1024 * 1024)} MB"})
                continue

            # Handle slash commands
            if data.startswith("/"):
                logger.log(f"command: {data[:100]}", level="info", source="ws", project_id=current_project_id)
                try:
                    await handle_command(data, current_project_id, websocket, model_id)
                except Exception as cmd_err:
                    err_msg = str(cmd_err)[:300]
                    logger.log("command_error", level="error", source="ws", project_id=current_project_id, error=err_msg)
                    try:
                        await safe_ws_send(websocket, {"type": "command_result", "content": f"Ошибка: {err_msg}"})
                    except Exception:
                        pass
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
                await safe_ws_send(websocket, {"type": "pong"})
                continue

            # Handle mode change
            if payload.get("type") == "mode_change":
                new_mode = payload.get("mode", "manual")
                current_mode = new_mode
                logger.user_action(f"mode_change: {new_mode}", project_id=current_project_id)
                await safe_ws_send(websocket, {"type": "auto_log", "content": f"Режим: {'Автоматический' if new_mode == 'auto' else 'Ручной'}", "level": "info"})
                continue

            # Handle stop_generation — abort current LLM call
            if payload.get("type") == "stop_generation":
                ws_cancelled = True
                logger.user_action("stop_generation", project_id=current_project_id)
                await safe_ws_send(websocket, {"type": "generation_stopped", "content": "⏹ Генерация остановлена"})
                continue

            # Handle hub chat (главный экран — без контекста проекта)
            if payload.get("type") == "hub_chat":
                hub_prompt = payload.get("prompt", "")
                hub_model = payload.get("model_id")
                if hub_prompt:
                    logger.user_action("hub_chat", details={"model": hub_model})
                    try:
                        await handle_hub_message(hub_prompt, websocket, model_id=hub_model, _cancel_check=lambda: ws_cancelled)
                    except Exception as chat_err:
                        import traceback
                        err_msg = str(chat_err)[:300]
                        logger.log("hub_chat_error", level="error", source="ws", error=err_msg)
                        try:
                            await safe_ws_send(websocket, {"type": "error", "content": f"Ошибка модели: {err_msg}"})
                        except Exception:
                            pass
                continue

            # Handle start_questionnaire (создание анкеты из UI)
            if payload.get("type") == "start_questionnaire":
                q_title = payload.get("title", "Новый проект")
                q_id = await save_questionnaire({"title": q_title, "project_id": project_id})
                await safe_ws_send(websocket, {"type": "questionnaire_created", "id": q_id})
                from core.agent import QUESTIONNAIRE_SYSTEM_PROMPT
                questionnaire_prompt = f"{QUESTIONNAIRE_SYSTEM_PROMPT}\n\nНачни опрос для проекта: {q_title}"
                try:
                    await handle_chat_message(questionnaire_prompt, project_id, repo_map, websocket, model_id=model_id, _cancel_check=lambda: ws_cancelled)
                except Exception as q_err:
                    err_msg = str(q_err)[:300]
                    logger.log("questionnaire_error", level="error", source="ws", error=err_msg)
                    try:
                        await safe_ws_send(websocket, {"type": "error", "content": f"Ошибка анкетирования: {err_msg}"})
                    except Exception:
                        pass
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
                try:
                    await handle_chat_message(refactor_prompt, current_project_id, repo_map, websocket, _cancel_check=lambda: ws_cancelled)
                except Exception as ref_err:
                    err_msg = str(ref_err)[:300]
                    logger.log("refactor_error", level="error", source="ws", project_id=current_project_id, error=err_msg)
                    try:
                        await safe_ws_send(websocket, {"type": "error", "content": f"Ошибка рефакторинга: {err_msg}"})
                    except Exception:
                        pass
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

            # Задача 5: Intelligent Router — выбрать модель если не указана
            if not model_id:
                try:
                    from core.keys_manager import keys_manager
                    all_models = keys_manager.get_all_models()
                    route_result = intelligent_router.select_model(prompt, all_models)
                    if route_result.get("model_id") and route_result.get("overridden"):
                        model_id = route_result["model_id"]
                        logger.log("intelligent_router_selected", level="info", source="ws",
                                   details={"model": model_id, "complexity": route_result.get("complexity"),
                                            "reason": route_result.get("reason", "")[:200]})
                        await safe_ws_send(websocket, {
                            "type": "auto_log",
                            "content": f"🔀 Маршрутизатор: {route_result.get('reason', '')}",
                            "level": "info",
                        })
                except Exception as route_err:
                    try:
                        logger.log("intelligent_router_error", level="warning", source="ws", error=str(route_err)[:200])
                    except Exception:
                        pass

            # Route based on mode
            if mode == "auto":
                logger.user_action("auto_mode_chat", project_id=current_project_id, details={"model": model_id})
                from core.auto_agent import run_auto_mode
                try:
                    await run_auto_mode(prompt, current_project_id, repo_map, websocket, model_id=model_id)
                except Exception as auto_err:
                    import traceback
                    err_msg = str(auto_err)[:300]
                    logger.log("auto_mode_error", level="error", source="ws", project_id=current_project_id,
                               error=err_msg, model=model_id)
                    try:
                        await safe_ws_send(websocket, {"type": "error", "content": f"Ошибка авто-режима: {err_msg}"})
                    except Exception:
                        pass
            else:
                logger.user_action("manual_chat", project_id=current_project_id, details={"model": model_id})
                try:
                    await handle_chat_message(prompt, current_project_id, repo_map, websocket, model_id=model_id, _cancel_check=lambda: ws_cancelled)
                except Exception as chat_err:
                    import traceback
                    err_msg = str(chat_err)[:300]
                    logger.log("manual_chat_error", level="error", source="ws", project_id=current_project_id,
                               error=err_msg, model=model_id)
                    try:
                        await safe_ws_send(websocket, {"type": "error", "content": f"Ошибка модели ({model_id}): {err_msg}"})
                    except Exception:
                        pass

    except WebSocketDisconnect:
        keepalive_task.cancel()
        logger.log("websocket_disconnected", level="info", source="ws", project_id=current_project_id)
        # Generate session summary (background, non-blocking)
        try:
            asyncio.create_task(generate_session_summary_async(
                ws_session_id, project_id=current_project_id
            ))
        except Exception:
            pass
    except Exception as e:
        keepalive_task.cancel()
        import traceback
        logger.log("websocket_error", level="error", source="ws", project_id=current_project_id,
                   error=str(e)[:500], stack_trace=traceback.format_exc()[-2000:])


async def handle_command(cmd: str, project_id, websocket, model_id: str = None):
    """Handle slash commands from the UI"""
    parts = cmd.strip().split(" ", 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    await safe_ws_send(websocket, {"type": "auto_log", "content": f"Выполняю: {cmd}", "level": "info"})
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
            await safe_ws_send(websocket, {
                "type": "approval_required",
                "content": result["message"],
                "request_id": request_id,
                "cmd": args
            })
        else:
            output = f"Exit code: {result['exit_code']}\n\n{result['stdout']}"
            if result.get("stderr"):
                output += f"\n\nSTDERR:\n{result['stderr']}"
            await safe_ws_send(websocket, {"type": "command_result", "content": output})
        await safe_ws_send(websocket, {"type": "done"})
        try:
            logger.log("terminal_done", level="success", source="ws", project_id=project_id, details={"exit_code": result.get("exit_code"), "approved": False})
        except Exception:
            pass

    elif command == "/approve":
        request_id = args.strip()
        if request_id in pending_approvals:
            pending = pending_approvals.pop(request_id)
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Подтверждаю: {pending['cmd']}", "level": "info"})
            result = await executor.execute_approved(pending["cmd"], request_id)
            output = f"Exit code: {result['exit_code']}\n\n{result['stdout']}"
            if result.get("stderr"):
                output += f"\n\nSTDERR:\n{result['stderr']}"
            await safe_ws_send(websocket, {"type": "command_result", "content": output})
            await safe_ws_send(websocket, {"type": "done"})
        else:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Нет ожидающих подтверждения команд.", "level": "info"})

    elif command == "/reject":
        request_id = args.strip()
        if request_id in pending_approvals:
            pending_approvals.pop(request_id)
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Команда отклонена.", "level": "info"})
        else:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Нет ожидающих команд.", "level": "info"})

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
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"📥 Pull OK: {summary}", "level": "info"})
        else:
            await safe_ws_send(websocket, {"type": "error", "content": f"📥 Pull failed: {result.get('stderr', 'unknown error')[:200]}"})
            try:
                logger.log("git_pull_failed", level="error", source="ws", project_id=project_id, error=result.get('stderr', '')[:200])
            except Exception:
                pass
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/git_push":
        try:
            logger.log("git_push_start", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        project_path = None
        project_token = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None
        push_out = await git_push_with_token(executor, project_path, project_token)
        push_out_stripped = push_out.strip()
        if "error" in push_out_stripped.lower() or "fatal" in push_out_stripped.lower() or "denied" in push_out_stripped.lower():
            await safe_ws_send(websocket, {"type": "error", "content": f"📤 Push failed: {push_out_stripped[:200]}"})
            try:
                logger.log("git_push_failed", level="error", source="ws", project_id=project_id, error=push_out_stripped[:200])
            except Exception:
                pass
        elif "Everything up-to-date" in push_out_stripped:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "📤 Push: уже актуально", "level": "info"})
        else:
            lines = [l for l in push_out_stripped.split("\n") if l.strip()]
            summary = lines[-1] if lines else "OK"
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"📤 Push OK: {summary}", "level": "info"})
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/quick_push":
        try:
            logger.log(f"quick_push_start: {args[:50]}", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        # Тихий push: auto commit + push без лишнего вывода
        project_path = None
        project_token = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None
        if not project_path:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект для Quick Push", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        msg = args.strip() or "sync"
        # git add -A
        await executor.execute("git add -A", cwd=project_path)
        # git commit (shlex.quote prevents shell injection from message)
        commit_result = await executor.execute(f"git commit -m {shlex.quote(msg)} --allow-empty", cwd=project_path)
        commit_out = (commit_result.get("stdout", "") + commit_result.get("stderr", "")).strip()
        if "nothing to commit" in commit_out.lower():
            await safe_ws_send(websocket, {"type": "auto_log", "content": "📤 Quick Push: нет изменений для коммита", "level": "info"})
        else:
            # push with project PAT token
            push_out = await git_push_with_token(executor, project_path, project_token)
            push_out_stripped = push_out.strip()
            if "error" in push_out_stripped.lower() or "fatal" in push_out_stripped.lower() or "denied" in push_out_stripped.lower():
                await safe_ws_send(websocket, {"type": "auto_log", "content": f"📤 Committed, push failed: {push_out_stripped[:100]}", "level": "info"})
            else:
                await safe_ws_send(websocket, {"type": "auto_log", "content": f"📤 Quick Push OK: {msg}", "level": "info"})
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/clear":
        await clear_history(project_id)
        await safe_ws_send(websocket, {"type": "auto_log", "content": "История чата очищена.", "level": "info"})

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
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект для проверки", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
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
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Использование: /ideas <github_url>", "level": "info"})
            return
        result = await ideas_injector.process_idea(args.strip())
        await safe_ws_send(websocket, {"type": "idea_result", "content": result})
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/repo_map":
        if project_id:
            project = await get_project(project_id)
            if project:
                repo_map = await context_manager.build_repo_map(project["path"], project_id)
                await safe_ws_send(websocket, {"type": "command_result", "content": repo_map})
                await safe_ws_send(websocket, {"type": "done"})
            else:
                await safe_ws_send(websocket, {"type": "auto_log", "content": "Проект не найден.", "level": "info"})
        else:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект для построения Repo Map.", "level": "info"})

    elif command == "/probe":
        probed = await get_probed_models()
        await safe_ws_send(websocket, {"type": "probed_models", "models": probed})
        if probed:
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Зондировано моделей: {len(probed)}", "level": "info"})
        else:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Нет результатов зондирования. Попробуйте позже.", "level": "info"})

    elif command == "/questionnaire":
        title = args.strip() or "Новый проект"
        q_id = await save_questionnaire({"title": title, "project_id": project_id})
        await safe_ws_send(websocket, {"type": "questionnaire_created", "id": q_id})
        from core.agent import QUESTIONNAIRE_SYSTEM_PROMPT
        questionnaire_prompt = f"{QUESTIONNAIRE_SYSTEM_PROMPT}\n\nНачни опрос для проекта: {title}"
        await handle_chat_message(questionnaire_prompt, project_id, repo_map, websocket, model_id=model_id, _cancel_check=lambda: ws_cancelled)

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
            "/probe — показать результаты зондирования моделей\n"
            "/questionnaire [title] — начать опрос для создания проекта\n"
            "/clear — очистить историю чата\n"
            "/help — эта справка"
        )
        await safe_ws_send(websocket, {"type": "auto_log", "content": help_text, "level": "info"})

    else:
        try:
            logger.log(f"unknown_command: {command}", level="warning", source="ws", project_id=project_id)
        except Exception:
            pass
        await safe_ws_send(websocket, {"type": "auto_log", "content": f"Неизвестная команда: {command}. Введите /help", "level": "info"})


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
                await safe_ws_send(websocket, {
                    "type": "stream",
                    "request_id": request_id,
                    "data": chunk
                })
            await safe_ws_send(websocket, {"type": "stream_done", "request_id": request_id})
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
    port = int(os.environ.get("PORT", 8000))
    print("  +========================================+")
    print("  |   Fosved Coder v2.0                  |")
    print(f"  |   http://0.0.0.0:{port:<21}|")
    print("  +========================================+")
    uvicorn.run("run:app", host="0.0.0.0", port=port, reload=False)
