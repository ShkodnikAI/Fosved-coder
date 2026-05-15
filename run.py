import os
import re
import json
import uuid
import uvicorn
import shlex
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from core.memory import init_db, clear_history, get_project, get_project_internal, get_repo_map, git_push_with_token, git_pull_with_token, git_clone_with_token, get_git_sync_status, save_questionnaire
from core.keys_manager import keys_manager
from core.agent import handle_chat_message, handle_hub_message
from core.executor import CommandExecutor
from core.ideas_injector import IdeasInjector
from core.context_manager import ContextManager
from core.intelligent_router import intelligent_router
from core.action_logger import get_logger
from core.observation_manager import assemble_context, generate_session_summary_async, ensure_observation_tables
from api.endpoints import router as api_router

logger = get_logger()


async def safe_ws_send(websocket, data: dict, _skip_task_id: bool = False):
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

    # При старте — НЕ восстанавливаем кэш probe.
    # Модели доступны ТОЛЬКО после ручного опроса через кнопку в UI.
    # (DB-кэш используется только для восстановления списка на клиенте,
    #  но роутер на сервере НЕ использует эти данные до явного probe_selected)
    # keys_manager._probed_model_ids остаётся пустым до probe_selected
    keys_manager._probed_model_ids = set()
    keys_manager._failed_probe_ids = set()
    print(f"  [startup] Probed models: empty (manual probe required)")

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


async def _auto_clone_if_needed(project_id: int, websocket):
    """Background task: clone the project's GitHub repo if github_repo is set but no .git exists.

    Runs silently — only sends a brief notification to the client if clone succeeds.
    No-op if the project directory already has a .git folder or no github_repo is configured.
    """
    try:
        project = await get_project_internal(project_id)
        if not project:
            return
        github_repo = project.get("github_repo", "").strip()
        project_path = project.get("path", "").strip()
        if not github_repo or "github.com" not in github_repo:
            return
        if not project_path or not os.path.isdir(project_path):
            return
        if os.path.isdir(os.path.join(project_path, ".git")):
            return  # Already cloned

        token = project.get("github_token") or None
        await safe_ws_send(websocket, {
            "type": "auto_log",
            "content": f"Авто-клон: {github_repo.split('github.com/')[-1].rstrip('/')}",
            "level": "info",
        })
        result = await git_clone_with_token(executor, project_path, github_repo, token)
        if result["success"]:
            await safe_ws_send(websocket, {
                "type": "auto_log",
                "content": f"Авто-клон завершён",
                "level": "success",
            })
            try:
                logger.log("auto_clone_success", level="success", source="ws",
                           project_id=project_id, details={"repo": github_repo[:80]})
            except Exception:
                pass
        else:
            err = (result.get("error") or "unknown")[:150]
            await safe_ws_send(websocket, {
                "type": "auto_log",
                "content": f"Авто-клон не удался: {err}",
                "level": "warning",
            })
            try:
                logger.log("auto_clone_failed", level="warning", source="ws",
                           project_id=project_id, error=err)
            except Exception:
                pass
    except Exception as e:
        try:
            logger.log("auto_clone_error", level="warning", source="ws",
                       project_id=project_id, error=str(e)[:150])
        except Exception:
            pass


@app.websocket("/ws")
async def websocket_chat(websocket: WebSocket):
    """Main chat WebSocket — streaming AI responses + command execution"""
    await websocket.accept()
    current_project_id = None
    repo_map = None
    current_mode = "manual"  # "manual" or "auto"
    model_id = None
    ws_session_id = str(__import__('uuid').uuid4())  # Unique session ID for memory
    _no_models_warned = False  # Спам-защита: предупреждение "нет моделей" только 1 раз
    logger.log("websocket_connected", level="info", source="ws")

    # ── Force client to clear any stale message queue from localStorage ──
    await safe_ws_send(websocket, {"type": "clear_queue"})

    # ── Cancellation flag: set by client to abort current generation ──
    ws_cancelled = False
    active_tasks: dict[str, dict] = {}  # task_id -> {"cancel": bool, "project_id": int|None, "task": Task}
    # ── Drop counter: after stop, drop N queued messages (no timers!) ──
    _messages_to_drop = 0
    # ── Auto-clone dedup: only attempt clone once per project per session ──
    last_auto_clone_pid = None

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

    async def _run_chat_task(task_id: str, prompt: str, project_id, repo_map_val, mode_val, model_id_val, priority_models_val, probed_ids_val):
        """Run a chat task in parallel — each task has its own cancel flag and ContextVar."""
        from core.agent import _current_task_id, handle_chat_message, handle_hub_message
        from core.intelligent_router import intelligent_router
        
        # Set context var — propagates to all child async calls (safe_ws_send, etc.)
        _current_task_id.set(task_id)
        
        # Per-task cancel flag stored in active_tasks (mutable by outer scope)
        active_tasks[task_id] = {"cancel": False, "project_id": project_id, "task": None}
        _cancelled = lambda: active_tasks.get(task_id, {}).get("cancel", False)
        
        try:
            if mode_val == "auto" and project_id:
                from core.auto_agent import run_auto_mode
                await run_auto_mode(prompt, project_id, repo_map_val, websocket, model_id=model_id_val, _cancel_check=_cancelled)
            elif project_id:
                # Intelligent router (if no explicit model)
                resolved_model = model_id_val
                if not resolved_model:
                    try:
                        from core.keys_manager import keys_manager
                        from core.memory import get_project
                        all_m = keys_manager.get_all_models()
                        pm = priority_models_val
                        if not pm and project_id:
                            proj = await get_project(project_id)
                            if proj and proj.get("selected_models"):
                                pm = json.loads(proj["selected_models"])
                        # Всегда пытаемся маршрутизировать — даже без probe,
                        # передаём пустой probed set и has_been_probed=False
                        route_result = intelligent_router.select_model(
                            prompt, all_m, user_preferred_model=None,
                            probed_model_ids=probed_ids_val or set(), failed_probe_ids=set(),
                            has_been_probed=bool(probed_ids_val), priority_models=pm,
                            in_project_context=bool(project_id),
                        )
                        resolved_model = route_result.get("model_id")
                    except Exception as route_err:
                        print(f"  [ws] task {task_id[:8]} router error: {route_err}")
                
                # Всегда вызываем handle_chat_message — если resolved_model=None,
                # внутри используется _build_models_to_try() с фоллбэком на проверенные модели
                if project_id:
                    await handle_chat_message(
                        prompt, project_id, repo_map_val, websocket,
                        model_id=resolved_model, _cancel_check=_cancelled
                    )
                else:
                    await safe_ws_send(websocket, {"type": "auto_log", "content": "⚠️ Нет выбранного проекта", "level": "warning"})
                    await safe_ws_send(websocket, {"type": "done", "tools_used": 0, "duration_ms": 0, "tokens": 0})
            # Hub messages don't go through here (handled separately below)
        except Exception as task_err:
            print(f"  [ws] task {task_id[:8]} error: {task_err}")
            try:
                await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Ошибка задачи: {str(task_err)[:150]}", "level": "error"})
            except Exception:
                pass
        finally:
            active_tasks.pop(task_id, None)
            _current_task_id.set('')

    # НЕ отправляем кэшированные результаты probe.
    # Клиент сам восстанавливает список из localStorage.
    # Модели активны только после явного ручного опроса (probe_selected).

    # Inject memory context from previous sessions (claude-mem inspired) — тихо, без UI логов
    try:
        memory_ctx = await assemble_context(project_id=None, max_tokens=300)
        # Тихо — не отправляем на frontend
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

            try:
                # ── All message processing is inside this try/except ──
                # so NO exception can kill the WS handler loop

                if len(data) > WS_MAX_MESSAGE_BYTES:
                    await safe_ws_send(websocket, {"type": "auto_log", "content": f"⚠️ Сообщение превышает лимит {WS_MAX_MESSAGE_BYTES // (1024 * 1024)} MB", "level": "warning"})
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
                try:
                    payload = json.loads(data)
                    prompt = payload.get("prompt", data)
                    model_id = payload.get("model")
                    priority = payload.get("priority_models", [])
                    mode = payload.get("mode", current_mode)
                    # Список явно проверенных моделей с клиента (из localStorage)
                    explicitly_probed_ids = set(payload.get("explicitly_probed_ids", []))
                    # Sync project_id from client (critical: keeps project context)
                    client_project_id = payload.get("project_id")
                    if client_project_id is not None:
                        current_project_id = client_project_id
                        # ── Auto-clone: if project has github_repo but no .git, clone it ──
                        if current_project_id and current_project_id != last_auto_clone_pid:
                            last_auto_clone_pid = current_project_id
                            asyncio.create_task(
                                _auto_clone_if_needed(current_project_id, websocket)
                            )
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

                # Handle stop_generation — abort current LLM call + drop queued messages
                if payload.get("type") == "stop_generation":
                    stop_task_id = payload.get("task_id")
                    if stop_task_id and stop_task_id in active_tasks:
                        # Cancel specific task
                        active_tasks[stop_task_id]["cancel"] = True
                        await safe_ws_send(websocket, {"type": "generation_stopped", "content": "Генерация остановлена", "task_id": stop_task_id}, _skip_task_id=True)
                    else:
                        # Cancel ALL active tasks (legacy / no task_id)
                        ws_cancelled = True
                        for tid, tinfo in active_tasks.items():
                            tinfo["cancel"] = True
                        await safe_ws_send(websocket, {"type": "generation_stopped", "content": "Все задачи остановлены"})
                    _messages_to_drop = 5
                    logger.user_action("stop_generation", project_id=current_project_id)
                    continue

                # ── Drop queued messages after stop (no timers!) ──
                _msg_type = payload.get("type", "")
                _is_gen_msg = _msg_type in ("chat", "hub_chat", "refactor", "start_questionnaire")
                if _is_gen_msg and _messages_to_drop > 0:
                    _messages_to_drop -= 1
                    print(f"  [ws] DROP queued message ({_messages_to_drop} remaining)")
                    continue
                if _is_gen_msg:
                    pass  # cancel is now per-task, not global

                # Handle probe_selected — тихий опрос только выбранных моделей
                if payload.get("type") == "probe_selected":
                    selected_ids = payload.get("models", [])
                    if selected_ids:
                        logger.user_action("probe_selected", details={"count": len(selected_ids)})
                        try:
                            from core.agent import probe_selected_models
                            await probe_selected_models(websocket, selected_ids)
                        except Exception as probe_err:
                            err_msg = str(probe_err)[:200]
                            logger.log("probe_selected_error", level="error", source="ws", error=err_msg)
                            await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Ошибка опроса: {err_msg}", "level": "error"})
                    else:
                        await safe_ws_send(websocket, {"type": "auto_log", "content": "⚠️ Не выбрано ни одной модели", "level": "warning"})
                    continue

                # Handle hub chat (главный экран — без контекста проекта)
                # ws_cancelled already reset above for all chat types
                if payload.get("type") == "hub_chat":
                    hub_prompt = payload.get("prompt", "")
                    hub_model = payload.get("model_id")
                    task_id = payload.get("task_id") or str(uuid.uuid4())
                    
                    async def _run_hub_task(tid, hp, hm):
                        from core.agent import _current_task_id, handle_hub_message
                        _current_task_id.set(tid)
                        active_tasks[tid] = {"cancel": False, "project_id": None, "task": None}
                        try:
                            await handle_hub_message(hp, websocket, model_id=hm, _cancel_check=lambda: active_tasks.get(tid, {}).get("cancel", False))
                        except Exception as hub_err:
                            print(f"  [ws] hub task {tid[:8]} error: {hub_err}")
                            try:
                            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Ошибка hub-чата: {str(hub_err)[:150]}", "level": "error"})
                            except Exception:
                                pass
                        finally:
                            active_tasks.pop(tid, None)
                            _current_task_id.set('')
                    
                    if hub_prompt:
                        logger.user_action("hub_chat", details={"model": hub_model})
                        asyncio.create_task(_run_hub_task(task_id, hub_prompt, hub_model))
                    continue

                # Handle start_questionnaire (создание анкеты из UI)
                # Тихий режим: ответы идут ТОЛЬКО в панель логов, не на главный экран
                if payload.get("type") == "start_questionnaire":
                    q_title = payload.get("title", "Новый проект")
                    q_id = await save_questionnaire({"title": q_title, "project_id": current_project_id})
                    await safe_ws_send(websocket, {"type": "questionnaire_created", "id": q_id})
                    await safe_ws_send(websocket, {"type": "auto_log", "content": f"📝 Тихий опрос: {q_title}", "level": "info"})
                    from core.agent import QUESTIONNAIRE_SYSTEM_PROMPT
                    questionnaire_prompt = f"{QUESTIONNAIRE_SYSTEM_PROMPT}\n\nНачни опрос для проекта: {q_title}"
                    try:
                        await handle_chat_message(questionnaire_prompt, current_project_id, repo_map, websocket, model_id=model_id, _cancel_check=lambda: ws_cancelled, _silent=True)
                    except Exception as q_err:
                        err_msg = str(q_err)[:300]
                        logger.log("questionnaire_error", level="error", source="ws", error=err_msg)
                        try:
                            await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Ошибка анкетирования: {err_msg[:100]}", "level": "error"})
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
                            await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Ошибка рефакторинга: {err_msg[:100]}", "level": "error"})
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

                # Model routing handled inside _run_chat_task (parallel)

                # Route based on mode — launch as parallel task
                task_id = payload.get("task_id") or str(uuid.uuid4())
                logger.user_action(f"{'auto' if mode == 'auto' else 'manual'}_chat", project_id=current_project_id, details={"model": model_id, "task_id": task_id[:8]})
                asyncio.create_task(_run_chat_task(
                    task_id=task_id,
                    prompt=prompt,
                    project_id=current_project_id,
                    repo_map_val=repo_map,
                    mode_val=mode,
                    model_id_val=model_id,
                    priority_models_val=priority,
                    probed_ids_val=explicitly_probed_ids,
                ))
                print(f"  [ws] task started: {task_id[:8]} project={current_project_id} mode={mode} model={model_id}")

            except Exception as iter_err:
                import traceback
                logger.log("ws_loop_error", level="warning", source="ws",
                           error=str(iter_err)[:500],
                           stack_trace=traceback.format_exc()[-1000:])
                try:
                    await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Внутренняя ошибка: {str(iter_err)[:150]}", "level": "error"})
                except Exception:
                    pass
                ws_cancelled = False  # Reset cancel flag after error
                continue

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
        project_token = None
        if project_id:
            project = await get_project_internal(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None
        pull_out = await git_pull_with_token(executor, project_path, project_token)
        pull_stripped = pull_out.strip()
        if "error" in pull_stripped.lower() or "fatal" in pull_stripped.lower() or "denied" in pull_stripped.lower():
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"📥 Pull не удался: {pull_stripped[:150]}", "level": "error"})
            try:
                logger.log("git_pull_failed", level="error", source="ws", project_id=project_id, error=pull_stripped[:200])
            except Exception:
                pass
        else:
            lines = [l.strip() for l in pull_stripped.split("\n") if l.strip() and not l.startswith("From ")]
            summary = lines[0] if lines else "Already up to date"
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"📥 Pull OK: {summary}", "level": "info"})
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/git_push":
        try:
            logger.log("git_push_start", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        project_path = None
        project_token = None
        if project_id:
            project = await get_project_internal(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None
        push_out = await git_push_with_token(executor, project_path, project_token)
        push_out_stripped = push_out.strip()
        if "error" in push_out_stripped.lower() or "fatal" in push_out_stripped.lower() or "denied" in push_out_stripped.lower():
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"📤 Push не удался: {push_out_stripped[:150]}", "level": "error"})
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
            project = await get_project_internal(project_id)
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

    elif command == "/git_clone":
        # Usage: /git_clone <repo_url> [token]
        # Clone a GitHub repo into current project directory
        try:
            logger.log("git_clone_start", level="info", source="ws", project_id=project_id)
        except Exception:
            pass
        parts = args.strip().split(maxsplit=1)
        repo_url = parts[0] if parts else ""
        clone_token = parts[1] if len(parts) > 1 else None

        if not repo_url:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Использование: /git_clone <repo_url> [token]", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        if "github.com" not in repo_url:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Поддерживаются только GitHub репозитории", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        project_path = None
        project_token = None
        if project_id:
            project = await get_project_internal(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None

        if not project_path:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект для clone", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        token = clone_token or project_token or None
        await safe_ws_send(websocket, {"type": "auto_log", "content": f"Клонирую {repo_url}...", "level": "info"})

        result = await git_clone_with_token(executor, project_path, repo_url, token)

        if result["success"]:
            # Update github_repo in DB
            try:
                from core.memory import async_session, Project, select
                async with async_session() as session:
                    async with session.begin():
                        db_proj = await session.execute(select(Project).where(Project.id == project_id))
                        p = db_proj.scalar_one_or_none()
                        if p:
                            p.github_repo = repo_url.rstrip("/").replace(".git", "")
                            if clone_token and not p.github_token:
                                p.github_token = clone_token
            except Exception:
                pass
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Клонирование выполнено: {repo_url}", "level": "success"})
        else:
            err = result.get("error", "Unknown error")[:200]
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"Clone failed: {err}", "level": "error"})
            try:
                logger.log("git_clone_failed", level="error", source="ws", project_id=project_id, error=err)
            except Exception:
                pass
        await safe_ws_send(websocket, {"type": "done"})

    elif command == "/git_sync":
        # Show git sync status (ahead/behind/changes)
        project_path = None
        project_token = None
        if project_id:
            project = await get_project_internal(project_id)
            if project:
                project_path = project["path"]
                project_token = project.get("github_token") or None

        if not project_path:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект", "level": "info"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        status = await get_git_sync_status(executor, project_path, project_token)

        if not status["is_git_repo"]:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Проект не является Git репозиторием", "level": "warning"})
            await safe_ws_send(websocket, {"type": "done"})
            return

        # Build status summary
        parts = []
        parts.append(f"Branch: {status['branch'] or 'detached'}")
        if status["remote_url"]:
            parts.append(f"Remote: {status['remote_url'].split('github.com')[-1] if 'github.com' in status['remote_url'] else status['remote_url']}")
        parts.append(f"Connected: {'Yes' if status['remote_connected'] else 'No'}")

        if status["ahead"] or status["behind"]:
            indicators = []
            if status["ahead"]:
                indicators.append(f"ahead {status['ahead']}")
            if status["behind"]:
                indicators.append(f"behind {status['behind']}")
            parts.append(f"Sync: {', '.join(indicators)}")
        else:
            parts.append("Sync: up-to-date")

        if status["has_changes"]:
            parts.append(f"Changes: {len(status['changed_files'])} files")
        else:
            parts.append("Working tree: clean")

        if status["last_commit"]:
            parts.append(f"Last: {status['last_commit'][:60]}")
            if status["last_commit_date"]:
                parts.append(f"Date: {status['last_commit_date']}")

        level = "success" if status["is_clean"] and status["ahead"] == 0 and status["behind"] == 0 else "warning"
        for line in parts:
            await safe_ws_send(websocket, {"type": "auto_log", "content": line, "level": level})
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
        # Принудительный опрос ВСЕХ моделей — тихий, с прогрессивной отправкой результатов
        await safe_ws_send(websocket, {"type": "auto_log", "content": "🔍 Начинаю опрос моделей...", "level": "info"})
        try:
            from core.agent import probe_models_live
            await probe_models_live(websocket)
        except Exception as probe_err:
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"❌ Ошибка опроса: {str(probe_err)[:100]}", "level": "error"})

    elif command == "/checkpoints":
        from core.agent import _checkpoints
        if not _checkpoints:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Нет сохранённых чекпоинтов.", "level": "info"})
        else:
            lines = [f"Чекпоинты ({len(_checkpoints)}):"]
            for cp in _checkpoints[-10:]:  # Last 10
                file_count = len(cp.get("files", {}))
                desc = cp.get("description", "")
                ts = cp.get("timestamp", "")[:19]
                lines.append(f"  [{ts}] {desc} — {file_count} файл(ов)")
            await safe_ws_send(websocket, {"type": "auto_log", "content": "\n".join(lines), "level": "info"})

    elif command == "/rewind":
        from core.agent import _checkpoints, _safe_join
        args_part = args.strip()
        project_path = None
        if project_id:
            project = await get_project(project_id)
            if project:
                project_path = project["path"]

        if not _checkpoints:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Нет чекпоинтов для отката.", "level": "info"})
        elif not project_path:
            await safe_ws_send(websocket, {"type": "auto_log", "content": "Выберите проект для отката.", "level": "info"})
        else:
            # Rewind last checkpoint
            cp = _checkpoints.pop()
            restored = 0
            for rel_path, original_content in cp.get("files", {}).items():
                full_path = _safe_join(project_path, rel_path)
                if full_path:
                    try:
                        with open(full_path, "w", encoding="utf-8") as f:
                            f.write(original_content)
                        restored += 1
                    except Exception:
                        pass
            desc = cp.get("description", "checkpoint")
            await safe_ws_send(websocket, {"type": "auto_log", "content": f"⏪ Откат: {desc} — {restored} файл(ов) восстановлено", "level": "success"})
            logger.log(f"rewind: {desc} {restored} files restored", level="info", source="ws", project_id=project_id)

    elif command == "/questionnaire":
        title = args.strip() or "Новый проект"
        q_id = await save_questionnaire({"title": title, "project_id": project_id})
        await safe_ws_send(websocket, {"type": "questionnaire_created", "id": q_id})
        from core.agent import QUESTIONNAIRE_SYSTEM_PROMPT
        questionnaire_prompt = f"{QUESTIONNAIRE_SYSTEM_PROMPT}\n\nНачни опрос для проекта: {title}"
        await handle_chat_message(questionnaire_prompt, project_id, repo_map, websocket, model_id=model_id)

    elif command == "/help":
        help_text = (
            "Доступные команды:\n"
            "/terminal <cmd> — выполнить shell-команду\n"
            "/approve <id> — подтвердить критическую команду\n"
            "/reject <id> — отклонить критическую команду\n"
            "/git_pull — git pull (тихий)\n"
            "/git_push — git push (тихий)\n"
            "/quick_push [msg] — commit + push одним действием\n"
            "/git_clone <url> [token] — клонировать GitHub репозиторий в проект\n"
            "/git_sync — статус синхронизации с GitHub (ahead/behind)\n"
            "/test [--no-tests] — проверить код проекта (синтаксис, линт, тесты)\n"
            "/ideas <github_url> — проанализировать репозиторий\n"
            "/repo_map — показать структуру проекта\n"
            "/probe — показать результаты зондирования моделей\n"
            "/checkpoints — показать сохранённые чекпоинты\n"
            "/rewind — откатить последний чекпоинт\n"
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
