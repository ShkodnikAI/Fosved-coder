"""
Fosved Coder v2.0 — REST API Endpoints
Включает управление ключами, моделями, проектами, локальные модели, кастомные модели.
Поиск файлов, гит, шаблоны, пакеты, архив.
"""
from fastapi import APIRouter, HTTPException, Body, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import json
import fnmatch
import os
import asyncio
import subprocess
import shlex
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
import re

from core.memory import (
    CONFIG, create_project, get_all_projects, get_project,
    delete_project, update_project_progress, update_project_models,
    get_all_ideas, delete_idea, get_message_count,
    save_routing_stat, get_routing_stats, get_history, save_message,
    save_project_archive, get_all_archives, get_archive,
    create_prompt_draft, get_prompt_draft, list_prompt_drafts,
    update_prompt_draft, delete_prompt_draft,
    clear_main_chat_history,
    save_questionnaire, get_questionnaire, get_questionnaires_by_project,
    delete_questionnaire,
    save_probed_models, get_probed_models,
)
from core.keys_manager import keys_manager, PROVIDER_DEFS, LOCAL_PROVIDERS
from core.action_logger import get_logger

router = APIRouter(prefix="/api/v1")
action_logger = get_logger()

# Shared limits — keep them in one place so endpoints can reuse them
MAX_FILE_BYTES = 5 * 1024 * 1024   # 5 MB cap on read-file/save-file
MAX_PACKAGE_TIMEOUT = 120          # seconds
# Shell metachars that must never appear in user-supplied package commands
_PKG_FORBIDDEN_CHARS = frozenset(';&|`$<>(){}[]!*?"\\\n\r')


def _log(action: str, **kwargs) -> None:
    """Best-effort wrapper around action_logger.log — never raises."""
    try:
        action_logger.log(action, **kwargs)
    except Exception:
        pass


def _api(method: str, path: str, **kwargs) -> None:
    """Best-effort wrapper around action_logger.api_call — never raises."""
    try:
        action_logger.api_call(method, path, **kwargs)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# SYSTEM HEALTH & STATUS
# ═══════════════════════════════════════════════════════════════

@router.get("/health")
async def health_check():
    """Проверка состояния системы: БД, API ключи, провайдеры."""
    _api("GET", "/api/v1/health")
    from core.memory import IS_POSTGRES, check_db_connection, DB_URL, engine
    from sqlalchemy import text
    import time

    status = {"status": "ok", "version": "2.0"}

    # Check database
    try:
        start = time.time()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_latency = round((time.time() - start) * 1000)
        status["database"] = {
            "type": "PostgreSQL" if IS_POSTGRES else "SQLite",
            "status": "connected",
            "latency_ms": db_latency,
        }
    except Exception as e:
        status["database"] = {"type": "PostgreSQL" if IS_POSTGRES else "SQLite", "status": "error", "error": str(e)[:200]}
        status["status"] = "degraded"

    # Check API keys
    provider_status = keys_manager.get_provider_status()
    active_providers = sum(1 for p in provider_status.values() if p.get("status") in ("valid", "available"))
    total_providers = len(provider_status)
    status["providers"] = {
        "active": active_providers,
        "total": total_providers,
        "details": {k: v.get("status", "?") for k, v in provider_status.items()},
    }

    # GitHub
    gh = keys_manager.get_github_status()
    status["github"] = gh

    return status


@router.get("/status")
async def system_status():
    """Детальная информация о системе: БД URL (без пароля), модели, память."""
    _api("GET", "/api/v1/status")
    import os
    from core.memory import IS_POSTGRES, DB_URL

    safe_url = DB_URL
    if "://" in safe_url:
        parts = safe_url.split("://", 1)
        auth_part = parts[1].split("@", 1)
        if len(auth_part) == 2 and ":" in auth_part[0]:
            user = auth_part[0].split(":")[0]
            safe_url = f"{parts[0]}://{user}:****@{auth_part[1]}"

    models = keys_manager.get_all_models()
    return {
        "version": "2.0",
        "database": {"type": "PostgreSQL" if IS_POSTGRES else "SQLite", "url": safe_url},
        "models": {"total": len(models), "free": sum(1 for m in models if m.get("is_free")), "paid": sum(1 for m in models if not m.get("is_free"))},
        "providers": list(keys_manager.providers.keys()),
        "environment": {"PORT": os.environ.get("PORT", "8000"), "RENDER": bool(os.environ.get("RENDER"))},
    }


# ═══════════════════════════════════════════════════════════════
# Pydantic Schemas
# ═══════════════════════════════════════════════════════════════

class AddKeyRequest(BaseModel):
    provider: str
    api_key: str
    models: list[str] = []
    api_base: str = ""

class GitHubTokenRequest(BaseModel):
    token: str
    enabled: bool = True

class ExpoTokenRequest(BaseModel):
    token: str
    enabled: bool = True

class ToggleGitHubRequest(BaseModel):
    enabled: bool

class ToggleExpoRequest(BaseModel):
    enabled: bool

class ToggleProviderRequest(BaseModel):
    enabled: bool

class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    base_prompt: str = ""
    ideas: str = ""
    github_repo: str = ""
    github_token: str = ""
    local_path: str = ""
    template: str = ""

class UpdateProgressRequest(BaseModel):
    project_id: int
    progress: int

class UpdateModelsRequest(BaseModel):
    project_id: int
    model_ids: list[str]

class AddIdeaRequest(BaseModel):
    repo_url: str

class SearchFilesRequest(BaseModel):
    project_id: int
    query: str
    file_pattern: str = ""
    max_results: int = 50

class GitOperationRequest(BaseModel):
    project_id: int
    operation: str  # commit, push, pull, log, status, diff
    message: str = ""
    auto_add: bool = False

class RunPackageRequest(BaseModel):
    project_id: int
    command: str  # "pip install flask", "npm install express", "pip list", "npm list"

class CreateFromTemplateRequest(BaseModel):
    name: str
    template: str  # fastapi, react, nextjs, python-cli, flask
    path: str = ""
    description: str = ""
    base_prompt: str = ""
    ideas: str = ""

class UpdateProjectSettingsRequest(BaseModel):
    project_id: int
    description: str = ""
    base_prompt: str = ""
    ideas: str = ""
    github_repo: str = ""
    github_token: str = ""
    local_path: str = ""
    logo: str = ""
    design: str = ""

class ArchiveProjectRequest(BaseModel):
    project_id: int
    description: str

class AddLocalModelRequest(BaseModel):
    provider_key: str  # ollama, lmstudio, vllm, llamacpp, custom_local
    model_name: str
    base_url: str = ""
    display_name: str = ""

class AddCustomModelRequest(BaseModel):
    name: str
    api_base: str
    api_key: str = ""
    model_id: str = ""
    litellm_prefix: str = "openai"

class DiscoverLocalModelsRequest(BaseModel):
    provider_key: str  # ollama, lmstudio, vllm, llamacpp, custom_local
    base_url: str = ""

class CreateDraftRequest(BaseModel):
    title: str = "Новый проект"
    template: str = ""

class UpdateDraftRequest(BaseModel):
    title: str = ""
    template: str = ""
    answers: dict = {}
    generated_prompt: str = ""
    discussion: list = []
    current_step: int = -1  # -1 = don't change
    status: str = ""  # draft | ready | converted


# ═══════════════════════════════════════════════════════════════
# KEYS & MODELS
# ═══════════════════════════════════════════════════════════════

@router.get("/")
async def api_root():
    """Корень API — список доступных endpoints."""
    _api("GET", "/api/v1/")
    return {
        "name": "Fosved Coder API",
        "version": "2.0",
        "docs": "/api/v1/health",
        "endpoints": {
            "system": ["GET /api/v1/health", "GET /api/v1/status"],
            "keys": ["GET /api/v1/keys/providers", "POST /api/v1/keys/add", "DELETE /api/v1/keys/{provider_id}", "GET /api/v1/keys/github", "POST /api/v1/keys/github"],
            "models": ["GET /api/v1/models", "GET /api/v1/models/local", "GET /api/v1/models/custom", "GET /api/v1/models/abacus/refresh"],
            "projects": ["GET /api/v1/projects", "POST /api/v1/projects", "DELETE /api/v1/projects/{id}", "PUT /api/v1/projects/settings", "PUT /api/v1/projects/rename", "POST /api/v1/projects/regenerate-key", "GET /api/v1/projects/by-key/{key}"],
            "files": ["GET /api/v1/projects/{id}/tree", "GET /api/v1/projects/{id}/read-file", "POST /api/v1/projects/{id}/save-file", "POST /api/v1/projects/{id}/search-files"],
            "git": ["POST /api/v1/projects/{id}/git"],
            "context": ["GET /api/v1/projects/{id}/context", "POST /api/v1/projects/{id}/context/compress", "POST /api/v1/projects/{id}/context/milestone"],
        }
    }

@router.post("/keys/add")
async def add_key(req: AddKeyRequest):
    """Валидация и добавление API-ключа провайдера."""
    _log("ADD_KEY", source="api", details={"provider": req.provider})
    result = await keys_manager.add_key(
        provider_id=req.provider,
        api_key=req.api_key,
        models=req.models if req.models else None,
        api_base=req.api_base if req.api_base else None,
    )
    if not result["success"]:
        _log("ADD_KEY", source="api", level="error", error=result["error"], details={"provider": req.provider})
        raise HTTPException(400, result["error"])
    _log("ADD_KEY", source="api", level="success", details={"provider": req.provider})
    await keys_manager.sync_to_db()
    return result

@router.delete("/keys/{provider_id}")
async def remove_key(provider_id: str):
    """Удаление API-ключа провайдера."""
    _log("REMOVE_KEY", source="api", details={"provider_id": provider_id})
    if keys_manager.remove_key(provider_id):
        _log("REMOVE_KEY", source="api", level="success", details={"provider_id": provider_id})
        await keys_manager.sync_to_db()
        return {"success": True, "provider": provider_id}
    _log("REMOVE_KEY", source="api", level="error", error=f"Provider {provider_id} not found", details={"provider_id": provider_id})
    raise HTTPException(404, f"Провайдер {provider_id} не найден")

@router.get("/keys/providers")
async def get_providers():
    """Список всех провайдеров с их статусом."""
    _api("GET", "/api/v1/keys/providers")
    return {
        "providers": PROVIDER_DEFS,
        "configured": keys_manager.get_provider_status(),
    }

@router.get("/keys/github")
async def get_github_status():
    """Статус GitHub интеграции."""
    _api("GET", "/api/v1/keys/github")
    return keys_manager.get_github_status()

@router.post("/keys/github")
async def set_github_token(req: GitHubTokenRequest):
    """Установка и валидация GitHub токена."""
    _log("SET_GITHUB_TOKEN", source="api", details={"enabled": req.enabled})
    validation = await keys_manager.validate_github_token(req.token)
    if validation["status"] != "valid":
        _log("SET_GITHUB_TOKEN", source="api", level="error", error=validation["error"])
        raise HTTPException(400, validation["error"])
    keys_manager.set_github_token(req.token, req.enabled)
    _log("SET_GITHUB_TOKEN", source="api", level="success", details={"user": validation.get("user"), "enabled": req.enabled})
    await keys_manager.sync_to_db()
    return {"success": True, "user": validation["user"]}

@router.put("/keys/github/toggle")
async def toggle_github(req: ToggleGitHubRequest):
    """Включение/отключение GitHub интеграции."""
    _log("TOGGLE_GITHUB", source="api", details={"enabled": req.enabled})
    result = keys_manager.toggle_github(req.enabled)
    await keys_manager.sync_to_db()
    return result

@router.get("/keys/expo")
async def get_expo_status():
    """Статус Expo интеграции."""
    _api("GET", "/api/v1/keys/expo")
    return keys_manager.get_expo_status()

@router.post("/keys/expo")
async def set_expo_token(req: ExpoTokenRequest):
    """Установка Expo токена для EAS Build."""
    _log("SET_EXPO_TOKEN", source="api", details={"enabled": req.enabled})
    keys_manager.set_expo_token(req.token, req.enabled)
    _log("SET_EXPO_TOKEN", source="api", level="success", details={"enabled": req.enabled})
    await keys_manager.sync_to_db()
    return {"success": True}

@router.put("/keys/expo/toggle")
async def toggle_expo(req: ToggleExpoRequest):
    """Включение/отключение Expo интеграции."""
    _log("TOGGLE_EXPO", source="api", details={"enabled": req.enabled})
    result = keys_manager.toggle_expo(req.enabled)
    await keys_manager.sync_to_db()
    return result

@router.put("/keys/{provider_id}/toggle")
async def toggle_provider(provider_id: str, req: ToggleProviderRequest):
    """Включение/отключение провайдера (модели скрываются из списка)."""
    _log("TOGGLE_PROVIDER", source="api", details={"provider_id": provider_id, "enabled": req.enabled})
    result = keys_manager.toggle_provider(provider_id, req.enabled)
    if not result["success"]:
        raise HTTPException(404, result["error"])
    await keys_manager.sync_to_db()
    return result


# ═══════════════════════════════════════════════════════════════
# PROMPT DRAFTS (Анкеты — подготовка к созданию проекта)
# ═══════════════════════════════════════════════════════════════

@router.get("/drafts")
async def get_drafts():
    """Список всех черновиков анкет."""
    _api("GET", "/api/v1/drafts")
    drafts = await list_prompt_drafts()
    return {"drafts": drafts, "count": len(drafts)}

@router.post("/drafts")
async def create_draft(req: CreateDraftRequest):
    """Создать новый черновик анкеты."""
    _log("CREATE_DRAFT", source="api", details={"title": req.title, "template": req.template})
    draft = await create_prompt_draft(title=req.title or "Новый проект", template=req.template)
    return draft

@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: int):
    """Получить полный черновик по ID."""
    _api("GET", f"/api/v1/drafts/{draft_id}")
    draft = await get_prompt_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Черновик не найден")
    return draft

@router.put("/drafts/{draft_id}")
async def update_draft(draft_id: int, req: UpdateDraftRequest):
    """Обновить черновик анкеты."""
    _log("UPDATE_DRAFT", source="api", details={"draft_id": draft_id})
    kwargs = {}
    if req.title: kwargs["title"] = req.title
    if req.template: kwargs["template"] = req.template
    if req.answers: kwargs["answers"] = req.answers
    if req.generated_prompt: kwargs["generated_prompt"] = req.generated_prompt
    if req.discussion: kwargs["discussion"] = req.discussion
    if req.current_step >= 0: kwargs["current_step"] = req.current_step
    if req.status: kwargs["status"] = req.status
    draft = await update_prompt_draft(draft_id, **kwargs)
    if not draft:
        raise HTTPException(404, "Черновик не найден")
    return draft

@router.delete("/drafts/{draft_id}")
async def remove_draft(draft_id: int):
    """Удалить черновик."""
    _log("DELETE_DRAFT", source="api", details={"draft_id": draft_id})
    if not await delete_prompt_draft(draft_id):
        raise HTTPException(404, "Черновик не найден")
    return {"success": True, "draft_id": draft_id}

@router.post("/drafts/{draft_id}/generate-prompt")
async def generate_draft_prompt(draft_id: int):
    """Сгенерировать финальный промпт из ответов анкеты (через ИИ)."""
    _log("GENERATE_PROMPT", source="api", details={"draft_id": draft_id})
    draft = await get_prompt_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Черновик не найден")
    answers = draft.get("answers", {})
    template = draft.get("template", "")
    title = draft.get("title", "")

    # Составляем промпт из ответов
    sections = []
    step_labels = {
        "idea": "Идея проекта",
        "audience": "Целевая аудитория",
        "features": "Ключевой функционал",
        "environment": "Среда и деплой",
        "repository": "Репозиторий и Git",
        "references": "Примеры и референсы",
        "tech_requirements": "Технические требования",
        "design": "Дизайн и UI/UX",
        "extras": "Дополнительные требования",
        "platforms": "Платформы",
        "native_modules": "Нативные модули",
        "state_mgmt": "Управление состоянием",
        "rendering": "Рендеринг",
    }
    for step_id, label in step_labels.items():
        answer = answers.get(step_id, "").strip()
        if answer:
            sections.append(f"## {label}\n{answer}")

    prompt_parts = []
    if title:
        prompt_parts.append(f"# Проект: {title}")
    if template:
        prompt_parts.append(f"**Шаблон:** {template}")
    if sections:
        prompt_parts.append("\n\n".join(sections))

    generated = "\n\n".join(prompt_parts)

    # Сохраняем сгенерированный промпт
    await update_prompt_draft(draft_id, generated_prompt=generated, status="ready")
    return {"success": True, "generated_prompt": generated, "status": "ready"}

@router.post("/drafts/{draft_id}/convert-to-project")
async def convert_draft_to_project(draft_id: int):
    """Конвертировать черновик анкеты в проект."""
    _log("CONVERT_DRAFT_TO_PROJECT", source="api", details={"draft_id": draft_id})
    draft = await get_prompt_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Черновик не найден")

    title = draft.get("title", "Без названия")
    template = draft.get("template", "")
    generated = draft.get("generated_prompt", "")
    answers = draft.get("answers", {})

    # Извлекаем данные из ответов анкеты
    ideas = answers.get("references", "") or answers.get("extras", "")
    github_repo = answers.get("repository", "") or ""

    # Создаём проект
    projects_dir = CONFIG["system"]["projects_dir"]
    project_path = f"{projects_dir}/{title.replace(' ', '_').lower()}"
    result = await create_project(
        title, project_path,
        description=answers.get("idea", ""),
        base_prompt=generated,
        ideas=ideas,
        github_repo=github_repo,
        template=template or None,
    )

    if not result:
        raise HTTPException(400, "Проект с таким именем уже существует")

    # Помечаем черновик как конвертированный
    await update_prompt_draft(draft_id, status="converted")

    return {"success": True, "project": result, "draft_id": draft_id}


@router.get("/models")
async def get_all_models_endpoint(probed_only: bool = Query(False)):
    """Список всех доступных моделей (платные + локальные + бесплатные + кастомные).
    probed_only=true — только модели, прошедшие probe-валидацию.
    """
    _api("GET", "/api/v1/models")
    all_models = keys_manager.get_all_models()

    # Если запрошены только проверенные модели — фильтруем по кэшу probe
    if probed_only and keys_manager._probed_model_ids:
        filtered = [
            m for m in all_models
            if m["id"] in keys_manager._probed_model_ids
            or m.get("type") in ("local", "custom")  # local/custom всегда показываем
        ]
        return {"models": filtered}

    return {"models": all_models}


@router.get("/models/probed")
async def get_models_probed_endpoint():
    """Список моделей, прошедших probe (из кэша + БД)."""
    _api("GET", "/api/v1/models/probed")
    return {
        "probed_model_ids": list(keys_manager._probed_model_ids),
        "failed_probe_ids": list(keys_manager._failed_probe_ids),
        "has_been_probed": bool(keys_manager._probed_model_ids or keys_manager._failed_probe_ids),
    }


@router.post("/models/validate/{provider_id}")
async def revalidate_provider(provider_id: str):
    """Повторная валидация ключа провайдера."""
    _log("REVALIDATE_PROVIDER", source="api", details={"provider_id": provider_id})
    config = keys_manager.providers.get(provider_id)
    if not config:
        _log("REVALIDATE_PROVIDER", source="api", level="error", error=f"Provider {provider_id} not found")
        raise HTTPException(404, f"Провайдер {provider_id} не настроен")
    result = await keys_manager.validate_key(
        provider_id, config["api_key"], config["models"][0] if config.get("models") else None
    )
    keys_manager.providers[provider_id]["status"] = result["status"]
    keys_manager._save_keys()
    return result


@router.get("/models/abacus/refresh")
async def refresh_abacus_models():
    """Загрузить актуальный список моделей с Abacus.AI RouteLLM API."""
    _log("REFRESH_ABACUS_MODELS", source="api")
    result = await keys_manager.fetch_abacus_models()
    if not result["success"]:
        _log("REFRESH_ABACUS_MODELS", source="api", level="error", error=result["error"])
    else:
        _log("REFRESH_ABACUS_MODELS", source="api", level="success", details={"count": result["count"]})
    return result


# ═══════════════════════════════════════════════════════════════
# LOCAL MODELS
# ═══════════════════════════════════════════════════════════════

@router.get("/models/local")
async def list_local_models():
    """Список сохранённых локальных моделей."""
    _api("GET", "/api/v1/models/local")
    return {
        "models": keys_manager.local_models,
        "providers": LOCAL_PROVIDERS,
    }

@router.post("/models/local/discover")
async def discover_local_models(req: DiscoverLocalModelsRequest):
    """Автообнаружение моделей на локальном сервере (Ollama, LM Studio и т.д.)."""
    _log("DISCOVER_LOCAL_MODELS", source="api", details={"provider_key": req.provider_key})
    result = await keys_manager.discover_local_models(
        provider_key=req.provider_key,
        base_url=req.base_url if req.base_url else None,
    )
    return result

@router.post("/models/local")
async def add_local_model(req: AddLocalModelRequest):
    """Ручное добавление локальной модели."""
    _log("ADD_LOCAL_MODEL", source="api", details={"provider_key": req.provider_key, "model_name": req.model_name})
    result = await keys_manager.add_local_model(
        provider_key=req.provider_key,
        model_name=req.model_name,
        base_url=req.base_url,
        display_name=req.display_name,
    )
    if not result["success"]:
        _log("ADD_LOCAL_MODEL", source="api", level="error", error=result["error"], details={"provider_key": req.provider_key, "model_name": req.model_name})
        raise HTTPException(400, result["error"])
    _log("ADD_LOCAL_MODEL", source="api", level="success", details={"provider_key": req.provider_key, "model_name": req.model_name})
    return result

@router.delete("/models/local/{model_id}")
async def remove_local_model(model_id: str):
    """Удаление локальной модели."""
    _log("REMOVE_LOCAL_MODEL", source="api", details={"model_id": model_id})
    if keys_manager.remove_local_model(model_id):
        _log("REMOVE_LOCAL_MODEL", source="api", level="success", details={"model_id": model_id})
        return {"success": True, "model_id": model_id}
    _log("REMOVE_LOCAL_MODEL", source="api", level="error", error=f"Local model {model_id} not found")
    raise HTTPException(404, f"Локальная модель {model_id} не найдена")


# ═══════════════════════════════════════════════════════════════
# CUSTOM MODELS (force connect)
# ═══════════════════════════════════════════════════════════════

@router.get("/models/custom")
async def list_custom_models():
    """Список кастомных (принудительно подключённых) моделей."""
    _api("GET", "/api/v1/models/custom")
    return {"models": keys_manager.custom_models}

@router.post("/models/custom")
async def add_custom_model(req: AddCustomModelRequest):
    """Принудительное добавление модели по URL (force connect)."""
    _log("ADD_CUSTOM_MODEL", source="api", details={"name": req.name, "api_base": req.api_base})
    result = await keys_manager.add_custom_model(
        name=req.name,
        api_base=req.api_base,
        api_key=req.api_key,
        model_id=req.model_id,
        litellm_prefix=req.litellm_prefix,
    )
    if not result["success"]:
        _log("ADD_CUSTOM_MODEL", source="api", level="error", error=result["error"], details={"name": req.name})
        raise HTTPException(400, result["error"])
    _log("ADD_CUSTOM_MODEL", source="api", level="success", details={"name": req.name})
    return result

@router.delete("/models/custom/{model_id}")
async def remove_custom_model(model_id: str):
    """Удаление кастомной модели."""
    _log("REMOVE_CUSTOM_MODEL", source="api", details={"model_id": model_id})
    if keys_manager.remove_custom_model(model_id):
        _log("REMOVE_CUSTOM_MODEL", source="api", level="success", details={"model_id": model_id})
        return {"success": True, "model_id": model_id}
    _log("REMOVE_CUSTOM_MODEL", source="api", level="error", error=f"Custom model {model_id} not found")
    raise HTTPException(404, f"Кастомная модель {model_id} не найдена")


# ═══════════════════════════════════════════════════════════════
# PROJECTS
# ═══════════════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects():
    _api("GET", "/api/v1/projects")
    return await get_all_projects()

@router.post("/projects")
async def create_project_endpoint(req: CreateProjectRequest):
    from core.memory import CONFIG
    _log("CREATE_PROJECT", source="api", details={"name": req.name, "template": req.template})
    projects_dir = CONFIG["system"]["projects_dir"]
    project_path = f"{projects_dir}/{req.name.replace(' ', '_').lower()}"
    result = await create_project(req.name, project_path, description=req.description, base_prompt=req.base_prompt, ideas=req.ideas, github_repo=req.github_repo, github_token=req.github_token, local_path=req.local_path, template=req.template)
    if not result:
        _log("CREATE_PROJECT", source="api", level="error", error="Project already exists", details={"name": req.name})
        raise HTTPException(400, "Проект с таким именем уже существует")
    _log("CREATE_PROJECT", source="api", level="success", details={"name": req.name, "project_id": result.get("id")})
    return result

@router.delete("/projects/{project_id}")
async def delete_project_endpoint(project_id: int):
    _log("DELETE_PROJECT", source="api", project_id=project_id)
    if await delete_project(project_id):
        _log("DELETE_PROJECT", source="api", level="success", project_id=project_id)
        return {"success": True}
    _log("DELETE_PROJECT", source="api", level="error", error="Project not found", project_id=project_id)
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/progress")
async def update_progress(req: UpdateProgressRequest):
    _log("UPDATE_PROGRESS", source="api", project_id=req.project_id, details={"progress": req.progress})
    if await update_project_progress(req.project_id, req.progress):
        return {"success": True, "progress": req.progress}
    _log("UPDATE_PROGRESS", source="api", level="error", error="Project not found", project_id=req.project_id)
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/{project_id}/progress")
async def update_progress_by_id(project_id: int, body: dict = Body(default={})):
    """Update project progress by ID in URL path."""
    progress = body.get("progress", 0)
    _log("UPDATE_PROGRESS", source="api", project_id=project_id, details={"progress": progress})
    if await update_project_progress(project_id, progress):
        return {"success": True, "progress": progress}
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/models")
async def update_models(req: UpdateModelsRequest):
    _log("UPDATE_MODELS", source="api", project_id=req.project_id, details={"model_ids": req.model_ids})
    if await update_project_models(req.project_id, req.model_ids):
        return {"success": True, "model_ids": req.model_ids}
    _log("UPDATE_MODELS", source="api", level="error", error="Project not found", project_id=req.project_id)
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/settings")
async def update_project_settings(req: UpdateProjectSettingsRequest):
    """Update project description, prompt, ideas."""
    from core.memory import async_session, Project, select
    _log("UPDATE_PROJECT_SETTINGS", source="api", project_id=req.project_id)
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Project).where(Project.id == req.project_id))
            project = result.scalar_one_or_none()
            if not project:
                raise HTTPException(404, "Проект не найден")
            project.description = req.description
            project.base_prompt = req.base_prompt
            project.ideas = req.ideas
            project.github_repo = req.github_repo
            project.github_token = req.github_token
            project.local_path = req.local_path
            project.logo = req.logo
            project.design = req.design
            return {"success": True}


class RenameProjectRequest(BaseModel):
    project_id: int
    new_name: str


@router.put("/projects/rename")
async def rename_project(req: RenameProjectRequest):
    """Переименовать проект."""
    from core.memory import async_session, Project, select
    _log("RENAME_PROJECT", source="api", project_id=req.project_id, details={"new_name": req.new_name})
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(400, "Название не может быть пустым")
    async with async_session() as session:
        async with session.begin():
            # Check uniqueness
            existing = await session.execute(select(Project).where(Project.name == new_name))
            if existing.scalar_one_or_none():
                raise HTTPException(400, "Проект с таким названием уже существует")
            # Find and update
            result = await session.execute(select(Project).where(Project.id == req.project_id))
            project = result.scalar_one_or_none()
            if not project:
                raise HTTPException(404, "Проект не найден")
            project.name = new_name
            return {"success": True, "new_name": new_name}


class RegenerateKeyRequest(BaseModel):
    project_id: int


@router.post("/projects/regenerate-key")
async def regenerate_project_key(req: RegenerateKeyRequest):
    """Перегенерировать UUID ключ проекта."""
    import uuid as _uuid
    from core.memory import async_session, Project, select
    _log("REGENERATE_PROJECT_KEY", source="api", project_id=req.project_id)
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Project).where(Project.id == req.project_id))
            project = result.scalar_one_or_none()
            if not project:
                raise HTTPException(404, "Проект не найден")
            new_key = str(_uuid.uuid4())
            project.uuid_key = new_key
            return {"success": True, "uuid_key": new_key}


@router.get("/projects/by-key/{key}")
async def get_project_by_key(key: str):
    """Получить проект по UUID ключу."""
    from core.memory import async_session, Project, select
    _api("GET", f"/api/v1/projects/by-key/{key[:8]}...")
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.uuid_key == key))
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(404, "Проект не найден")
        return {"id": p.id, "name": p.name, "description": p.description, "progress": p.progress}


# ═══════════════════════════════════════════════════════════════
# FILE OPERATIONS
# ═══════════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/tree")
async def get_project_tree(project_id: int):
    """Получить дерево файлов проекта (вложенная структура)."""
    _api("GET", f"/api/v1/projects/{project_id}/tree", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    project_path = project["path"]
    if not os.path.isdir(project_path):
        return {"tree": [], "error": "Папка проекта не найдена"}

    def build_tree(path, rel_path=""):
        items = []
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return items
        skip_dirs = {'.git', '__pycache__', 'node_modules', '.next', 'venv', '.venv', '.idea', '.vscode', 'dist', 'build', '.cache'}
        skip_exts = {'.pyc', '.pyo', '.so', '.dll', '.exe', '.bin', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot'}
        for name in entries:
            if name.startswith('.') and name != '.env':
                continue
            full = os.path.join(path, name)
            rel = os.path.join(rel_path, name) if rel_path else name
            rel = rel.replace("\\", "/")
            if os.path.isdir(full) and name not in skip_dirs:
                children = build_tree(full, rel)
                items.append({"name": name, "path": rel, "type": "dir", "children": children})
            elif os.path.isfile(full) and not any(name.endswith(ext) for ext in skip_exts):
                items.append({"name": name, "path": rel, "type": "file"})
        return items

    tree = build_tree(project_path)
    return {"tree": tree}

@router.get("/projects/{project_id}/read-file")
async def read_file(project_id: int, path: str):
    """Прочитать содержимое файла проекта (UTF-8). Лимит — MAX_FILE_BYTES."""
    _log("READ_FILE", source="api", project_id=project_id, details={"path": path})
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    full_path = os.path.join(project["path"], path)
    # Security: prevent path traversal (resolve symlinks too)
    real_path = os.path.realpath(full_path)
    real_project = os.path.realpath(project["path"])
    if os.path.commonpath([real_path, real_project]) != real_project:
        raise HTTPException(403, "Доступ запрещён")
    if not os.path.isfile(full_path):
        raise HTTPException(404, "Файл не найден")
    try:
        size = os.path.getsize(full_path)
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, f"Файл превышает лимит {MAX_FILE_BYTES // (1024 * 1024)} MB ({size} байт)")
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "size": size}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/projects/{project_id}/save-file")
async def save_file(project_id: int, path: str = Body(...), content: str = Body("")):
    """Сохранить/создать файл в проекте. Лимит — MAX_FILE_BYTES."""
    _log("SAVE_FILE", source="api", project_id=project_id, details={"path": path, "size": len(content)})
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise HTTPException(413, f"Контент превышает лимит {MAX_FILE_BYTES // (1024 * 1024)} MB")
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    full_path = os.path.join(project["path"], path)
    real_path = os.path.realpath(full_path)
    real_project = os.path.realpath(project["path"])
    # Common-path check is robust against `/foo` vs `/foobar` substring tricks
    if os.path.commonpath([real_path, real_project]) != real_project:
        raise HTTPException(403, "Доступ запрещён")
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": path}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/projects/{project_id}/search-files")
async def search_files(req: SearchFilesRequest):
    """Поиск текста/кода по файлам проекта (grep)."""
    _log("SEARCH_FILES", source="api", project_id=req.project_id, details={"query": req.query, "pattern": req.file_pattern})
    project = await get_project(req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    project_path = project["path"]
    if not os.path.isdir(project_path):
        return {"results": [], "query": req.query, "total": 0}

    query = req.query.lower()
    file_pattern = req.file_pattern if req.file_pattern else ""
    results = []
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.next', 'venv', '.venv', '.idea', '.vscode', 'dist', 'build', '.cache'}
    skip_exts = {'.pyc', '.pyo', '.so', '.dll', '.exe', '.bin', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot'}
    max_file_size = 500 * 1024  # 500 KB

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
        for f in files:
            if any(f.endswith(ext) for ext in skip_exts):
                continue
            if file_pattern and not fnmatch.fnmatch(f, file_pattern):
                continue

            full_path = os.path.join(root, f)
            try:
                if os.path.getsize(full_path) > max_file_size:
                    continue
                with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                rel_path = os.path.relpath(full_path, project_path).replace("\\", "/")
                for line_num, line in enumerate(lines, 1):
                    try:
                        if re.search(query, line, re.IGNORECASE):
                            results.append({
                                "file": rel_path,
                                "line": line_num,
                                "text": line.rstrip()[:200],
                                "match_start": max(0, line.lower().find(query.split("|")[0].split("(")[0].lower()) - 40),
                            })
                        if len(results) >= req.max_results:
                            return {"results": results, "query": req.query, "total": len(results), "truncated": True}
                    except re.error:
                        # Invalid regex — fall back to substring match
                        if query in line.lower():
                            results.append({
                                "file": rel_path,
                                "line": line_num,
                                "text": line.rstrip()[:200],
                                "match_start": max(0, line.lower().find(query) - 40),
                            })
                            if len(results) >= req.max_results:
                                return {"results": results, "query": req.query, "total": len(results), "truncated": True}
            except (OSError, PermissionError):
                continue

    return {"results": results, "query": req.query, "total": len(results)}


# ═══════════════════════════════════════════════════════════════
# SKILLS
# ═══════════════════════════════════════════════════════════════

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


@router.get("/skills")
async def list_skills():
    """List all available skills from the skills/ directory."""
    _api("GET", "/api/v1/skills")
    if not os.path.isdir(SKILLS_DIR):
        return {"skills": [], "groups": []}

    skills = []
    groups = set()
    seen = set()

    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, entry)
        if not os.path.isdir(skill_path):
            continue
        skill_md = os.path.join(skill_path, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue

        # Extract skill metadata from SKILL.md (first line is usually the title)
        try:
            with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline().strip().lstrip("#").strip()
            title = first_line if first_line else entry
        except Exception:
            title = entry

        # Check for _meta.json
        meta_path = os.path.join(skill_path, "_meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass

        skills.append({
            "name": entry,
            "title": meta.get("title", title),
            "description": meta.get("description", "")[:200],
            "group": meta.get("group", ""),
        })
        group = meta.get("group", "")
        if group:
            groups.add(group)

    return {"skills": skills, "groups": sorted(groups)}


@router.get("/skills/{skill_name}")
async def get_skill(skill_name: str):
    """Get skill details by name."""
    _api("GET", f"/api/v1/skills/{skill_name}")
    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if not os.path.isfile(skill_path):
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    try:
        with open(skill_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"name": skill_name, "content": content}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/skills/{skill_name}/content")
async def get_skill_content(skill_name: str):
    """Get full skill content (all files in skill directory)."""
    _api("POST", f"/api/v1/skills/{skill_name}/content")
    skill_dir = os.path.join(SKILLS_DIR, skill_name)
    if not os.path.isdir(skill_dir):
        raise HTTPException(404, f"Skill '{skill_name}' not found")

    files = {}
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for root, dirs, filenames in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fname in filenames:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, skill_dir).replace("\\", "/")
            try:
                size = os.path.getsize(fpath)
                if size > 2 * 1024 * 1024:  # skip files > 2MB
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    files[rel] = f.read()
            except Exception:
                continue

    return {"name": skill_name, "files": files, "file_count": len(files)}


# ═══════════════════════════════════════════════════════════════
# MODEL SILENT PROBE
# ═══════════════════════════════════════════════════════════════

@router.post("/models/probe")
async def probe_models():
    """Silent probe: send minimal request to all configured models. Returns which ones responded."""
    _log("PROBE_MODELS", source="api")
    responded = []
    failed = []

    # Collect all models with API keys
    all_models = keys_manager.get_all_models()
    # Deduplicate by provider — we only need one model per provider to test connectivity
    probed_providers = set()
    for model in all_models:
        provider_id = model.get("provider_id", "")
        api_base = model.get("api_base", "")
        api_key = model.get("api_key", "")
        model_id = model.get("model_id", model.get("id", ""))
        is_local = model.get("is_local", False)

        if not provider_id or provider_id in probed_providers:
            continue
        if not api_key or is_local:
            continue

        probed_providers.add(provider_id)

        try:
            import httpx
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
            base = api_base.rstrip("/")
            if not base.endswith("/chat/completions"):
                base = base.rstrip("/") + "/chat/completions"

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(base, json=payload, headers=headers)
                if resp.status_code in (200, 429):  # 429 = rate limited but alive
                    responded.append({"provider": provider_id, "model": model_id, "status": "ok"})
                else:
                    failed.append({"provider": provider_id, "model": model_id, "status": f"HTTP {resp.status_code}"})
        except Exception as e:
            failed.append({"provider": provider_id, "model": model_id, "status": str(e)[:100]})

    return {"responded": responded, "failed": failed}


# ═══════════════════════════════════════════════════════════════
# MAIN CHAT CLEAR
# ═══════════════════════════════════════════════════════════════

@router.delete("/chat/main")
async def clear_main_chat():
    """Clear main screen chat history (messages without project, older than 10 days)."""
    _log("CLEAR_MAIN_CHAT", source="api")
    deleted = await clear_main_chat_history(days=10)
    _log("CLEAR_MAIN_CHAT", source="api", level="success", details={"deleted": deleted})
    return {"success": True, "deleted": deleted}


# ═══════════════════════════════════════════════════════════════
# GIT OPERATIONS
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/git")
async def git_operation(project_id: int, req: GitOperationRequest):
    """Git операции: commit, push, pull, log, status, diff."""
    _log(f"GIT_{req.operation.upper()}", source="api", project_id=project_id, details={"operation": req.operation})
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    cwd = project["path"]
    if not os.path.isdir(os.path.join(cwd, ".git")):
        raise HTTPException(400, "Проект не является Git репозиторием")

    try:
        if req.operation == "status":
            result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=cwd, timeout=10)
            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            return {"operation": "status", "output": lines, "raw": result.stdout}

        elif req.operation == "log":
            result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--format=%h|%ai|%s"],
                capture_output=True, text=True, cwd=cwd, timeout=10
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append({"hash": parts[0], "date": parts[1].strip(), "message": parts[2].strip()})
            return {"operation": "log", "commits": commits}

        elif req.operation == "diff":
            result = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True, cwd=cwd, timeout=10)
            diff_full = subprocess.run(["git", "diff"], capture_output=True, text=True, cwd=cwd, timeout=15)
            return {"operation": "diff", "stat": result.stdout, "diff": diff_full.stdout[:10000]}

        elif req.operation == "commit":
            if not req.message:
                raise HTTPException(400, "Сообщение коммита обязательно")
            if req.auto_add:
                subprocess.run(["git", "add", "-A"], capture_output=True, text=True, cwd=cwd, timeout=10)
            result = subprocess.run(
                ["git", "commit", "-m", req.message],
                capture_output=True, text=True, cwd=cwd, timeout=15
            )
            return {"operation": "commit", "output": result.stdout.strip() or result.stderr.strip(), "success": result.returncode == 0}

        elif req.operation == "push":
            result = subprocess.run(["git", "push"], capture_output=True, text=True, cwd=cwd, timeout=30)
            return {"operation": "push", "output": result.stdout.strip() or result.stderr.strip(), "success": result.returncode == 0}

        elif req.operation == "pull":
            result = subprocess.run(["git", "pull"], capture_output=True, text=True, cwd=cwd, timeout=30)
            return {"operation": "pull", "output": result.stdout.strip() or result.stderr.strip(), "success": result.returncode == 0}

        else:
            raise HTTPException(400, f"Неизвестная операция: {req.operation}")

    except subprocess.TimeoutExpired:
        _log(f"GIT_{req.operation.upper()}", source="api", level="error", error="Timeout", project_id=req.project_id)
        raise HTTPException(408, "Операция превысила таймаут")
    except Exception as e:
        _log(f"GIT_{req.operation.upper()}", source="api", level="error", error=str(e), project_id=req.project_id)
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════
# PACKAGE MANAGER
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/packages")
async def run_package_command(req: RunPackageRequest):
    """Управление пакетами: pip install, npm install, и т.д.

    Команда проверяется по white-list префиксам, парсится через shlex (не shell)
    и выполняется без shell=True, чтобы исключить инъекции через ';', '&', backticks и т.п.
    """
    _log("PACKAGE_OPERATION", source="api", project_id=req.project_id, details={"command": req.command})
    project = await get_project(req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    cwd = project["path"]
    os.makedirs(cwd, exist_ok=True)

    cmd = req.command.strip()
    if not cmd:
        raise HTTPException(400, "Команда не указана")

    # Reject any shell metachars before deciding anything else.
    if any(c in cmd for c in _PKG_FORBIDDEN_CHARS):
        raise HTTPException(400, "В команде есть запрещённые спецсимволы")

    # Security: only allow safe package commands
    allowed_prefixes = ["pip install", "pip uninstall", "pip list", "pip show",
                         "npm install", "npm uninstall", "npm list", "npm run",
                         "python -m pip", "python3 -m pip", "uv pip"]
    if not any(cmd.startswith(p) for p in allowed_prefixes):
        raise HTTPException(400, "Команда не разрешена. Разрешены: pip, npm")

    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise HTTPException(400, f"Невалидная команда: {e}")
    if not argv:
        raise HTTPException(400, "Команда не указана")

    try:
        # shell=False + argv list — никакой интерпретации спецсимволов
        result = subprocess.run(
            argv, shell=False, capture_output=True, text=True, cwd=cwd, timeout=MAX_PACKAGE_TIMEOUT
        )
        return {
            "command": cmd,
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, f"Установка превысила таймаут ({MAX_PACKAGE_TIMEOUT} сек)")
    except FileNotFoundError as e:
        raise HTTPException(400, f"Утилита не найдена: {e.filename}")
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════
# PROJECT TEMPLATES
# ═══════════════════════════════════════════════════════════════

TEMPLATES = {
    "fastapi": {
        "name": "FastAPI",
        "description": "FastAPI + Uvicorn + SQLAlchemy",
        "files": {
            "main.py": '''"""FastAPI Application"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="{name}", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to {name}", "version": "0.1.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
''',
            "requirements.txt": "fastapi\nuvicorn\nsqlalchemy\npydantic\n",
            ".gitignore": "__pycache__/\n*.pyc\n.env\nvenv/\n",
        }
    },
    "flask": {
        "name": "Flask",
        "description": "Flask + SQLAlchemy",
        "files": {
            "app.py": '''"""Flask Application"""
from flask import Flask, jsonify

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me"

@app.route("/")
def root():
    return jsonify({"message": "Welcome to {name}", "version": "0.1.0"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
''',
            "requirements.txt": "flask\nflask-sqlalchemy\n",
            ".gitignore": "__pycache__/\n*.pyc\n.env\nvenv/\n",
        }
    },
    "react": {
        "name": "React",
        "description": "React + Vite",
        "files": {
            "package.json": '''{{
  "name": "{name}",
  "version": "0.1.0",
  "private": true,
  "dependencies": {{
    "react": "^18.2.0",
    "react-dom": "^18.2.0"
  }},
  "devDependencies": {{
    "@vitejs/plugin-react": "^4.2.0",
    "vite": "^5.0.0"
  }},
  "scripts": {{
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  }}
}}
''',
            "index.html": '''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width"/>
<title>{name}</title></head>
<body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body>
</html>''',
            "src/main.jsx": '''import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
ReactDOM.createRoot(document.getElementById('root')).render(<App />)''',
            "src/App.jsx": '''import React from 'react'
export default function App() {
  return <div><h1>{name}</h1><p>Welcome!</p></div>
}''',
            ".gitignore": "node_modules/\ndist/\n.env\n",
        }
    },
    "nextjs": {
        "name": "Next.js",
        "description": "Next.js 14 App Router",
        "files": {
            "package.json": '''{{
  "name": "{name}",
  "version": "0.1.0",
  "private": true,
  "scripts": {{
    "dev": "next dev",
    "build": "next build",
    "start": "next start"
  }},
  "dependencies": {{
    "next": "^14.0.0",
    "react": "^18.2.0",
    "react-dom": "^18.2.0"
  }}
}}
''',
            "app/layout.js": '''export const metadata = {{ title: "{name}" }}
export default function RootLayout({{ children }}) {{
  return <html><body>{{children}}</body></html>
}}''',
            "app/page.js": '''export default function Home() {{
  return <main><h1>{name}</h1><p>Welcome!</p></main>
}}''',
            ".gitignore": "node_modules/\n.next/\n.env*\n",
        }
    },
    "expo": {
        "name": "Expo SDK 53",
        "description": "Expo SDK 53 Universal App (iOS + Android + Web)",
        "files": {
            "package.json": '''{{
  "name": "{name}",
  "version": "1.0.0",
  "main": "expo-router/entry",
  "scripts": {{
    "start": "expo start",
    "android": "expo start --android",
    "ios": "expo start --ios",
    "web": "expo start --web"
  }},
  "dependencies": {{
    "expo": "~53.0.0",
    "expo-router": "~4.0.0",
    "expo-status-bar": "~2.0.0",
    "react": "18.3.1",
    "react-native": "0.76.5",
    "react-native-safe-area-context": "5.3.0",
    "react-native-screens": "~4.10.0",
    "expo-linking": "~7.0.0",
    "expo-constants": "~17.0.0",
    "@expo/vector-icons": "^14.1.0"
  }},
  "devDependencies": {{
    "@types/react": "~18.3.12",
    "typescript": "~5.3.3"
  }}
}}
''',
            "app/_layout.tsx": '''import {{ Stack }} from "expo-router";
import {{ StatusBar }} from "expo-status-bar";
import {{ useColorScheme }} from "react-native";

export default function RootLayout() {{
  const colorScheme = useColorScheme();
  return (
    <>
      <StatusBar style="auto" />
      <Stack screenOptions={{
        headerStyle: {{ backgroundColor: colorScheme === "dark" ? "#1a1a1a" : "#fff" }},
        headerTintColor: colorScheme === "dark" ? "#fff" : "#000",
      }} />
    </>
  );
}}
''',
            "app/index.tsx": '''import {{ View, Text, StyleSheet }} from "react-native";

export default function HomeScreen() {{
  return (
    <View style={{styles.container}}>
      <Text style={{styles.title}}>{name}</Text>
      <Text style={{styles.subtitle}}>Expo SDK 53 Universal App</Text>
    </View>
  );
}}

const styles = StyleSheet.create({{
  container: {{
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
  }},
  title: {{
    fontSize: 28,
    fontWeight: "bold",
  }},
  subtitle: {{
    fontSize: 16,
    color: "#888",
    marginTop: 8,
  }},
}});
''',
            "tsconfig.json": '''{{
  "extends": "expo/tsconfig.base",
  "compilerOptions": {{
    "strict": true,
    "paths": {{
      "@/*": ["./*"]
    }}
  }}
}}
''',
            "app.json": '''{{
  "expo": {{
    "name": "{name}",
    "slug": "{name}".toLowerCase().replace(/\\s+/g, "-"),
    "version": "1.0.0",
    "orientation": "default",
    "userInterfaceStyle": "automatic",
    "newArchEnabled": true,
    "scheme": "{name}".toLowerCase().replace(/\\s+/g, "-"),
    "ios": {{
      "supportsTablet": true,
      "bundleIdentifier": "com.fosved.{name}".toLowerCase().replace(/\\s+/g, "")
    }},
    "android": {{
      "adaptiveIcon": {{
        "backgroundColor": "#1a1a1a"
      }},
      "package": "com.fosved.{name}".toLowerCase().replace(/\\s+/g, "")
    }},
    "web": {{
      "bundler": "metro",
      "output": "static",
      "favicon": "./assets/favicon.png"
    }},
    "plugins": ["expo-router"]
  }}
}}
''',
            ".gitignore": "node_modules/\n.expo/\n*.jks\n*.keystore\n.env\n.DS_Store\n",
        }
    },
    "python-cli": {
        "name": "Python CLI",
        "description": "Python CLI приложение с argparse",
        "files": {
            "main.py": '''"""{name} — CLI Application"""
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(description="{name}")
    parser.add_argument("--version", action="version", version="{name} 0.1.0")
    parser.add_argument("command", nargs="?", default="hello", help="Command to run")
    args = parser.parse_args()

    if args.command == "hello":
        print("Hello from {name}!")
    else:
        print(f"Unknown command: {{args.command}}")
        sys.exit(1)

if __name__ == "__main__":
    main()
''',
            "requirements.txt": "",
            ".gitignore": "__pycache__/\n*.pyc\nvenv/\n",
        }
    },
}

@router.get("/templates")
async def list_templates():
    """Список доступных шаблонов."""
    _api("GET", "/api/v1/templates")
    return {"templates": [
        {"id": tid, "name": t["name"], "description": t["description"]}
        for tid, t in TEMPLATES.items()
    ]}

@router.post("/projects/create-from-template")
async def create_from_template(req: CreateFromTemplateRequest):
    """Создать проект из шаблона."""
    _log("CREATE_FROM_TEMPLATE", source="api", details={"name": req.name, "template": req.template})
    template = TEMPLATES.get(req.template)
    if not template:
        raise HTTPException(400, f"Шаблон '{req.template}' не найден")

    projects_dir = CONFIG["system"]["projects_dir"]
    project_path = req.path or f"{projects_dir}/{req.name.replace(' ', '_').lower()}"

    # Create project in DB
    result = await create_project(req.name, project_path)
    if not result:
        raise HTTPException(400, "Проект с таким именем уже существует")

    # Save project settings (description, base_prompt, ideas)
    if req.description or req.base_prompt or req.ideas:
        from core.memory import async_session, Project, select
        async with async_session() as session:
            async with session.begin():
                db_result = await session.execute(select(Project).where(Project.id == result["id"]))
                db_project = db_result.scalar_one_or_none()
                if db_project:
                    db_project.description = req.description
                    db_project.base_prompt = req.base_prompt
                    db_project.ideas = req.ideas

    # Write template files
    os.makedirs(project_path, exist_ok=True)
    for file_path, content in template["files"].items():
        full_path = os.path.join(project_path, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content.format(name=req.name))

    # Init git
    subprocess.run(["git", "init"], capture_output=True, cwd=project_path, timeout=5)
    subprocess.run(["git", "add", "-A"], capture_output=True, cwd=project_path, timeout=5)
    subprocess.run(["git", "commit", "-m", f"Init: {req.name} from {template['name']} template"],
                   capture_output=True, cwd=project_path, timeout=10)

    return {"success": True, "project": result, "template": template["name"]}


# ═══════════════════════════════════════════════════════════════
# PROJECT ARCHIVE
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/archive")
async def archive_project(req: ArchiveProjectRequest):
    """Архивировать проект: создать мастер-промпт, запаковать файлы."""
    _log("ARCHIVE_PROJECT", source="api", project_id=req.project_id, details={"description": req.description})
    project = await get_project(req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    project_path = project["path"]
    if not os.path.isdir(project_path):
        raise HTTPException(400, "Папка проекта не найдена")

    # 1. Собрать историю чата проекта
    history = await get_history(req.project_id, limit=200)

    # 2. Собрать структуру файлов
    file_list = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'node_modules', '.next', 'venv', '.venv', '.idea', '.vscode', 'dist', 'build'} and not d.startswith('.')]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), project_path).replace("\\", "/")
            file_list.append(rel)

    # 3. Создать мастер-промпт на основе истории
    master_prompt = _generate_master_prompt(project["name"], history, file_list)

    # 4. Создать ZIP архив. Используем ручной обход вместо shutil.make_archive,
    #    чтобы пропустить symlink'и и пути, выходящие за project_path
    #    (защита от data-exfil через подсунутый symlink).
    archives_dir = os.path.join(CONFIG["system"].get("archives_dir", "./archives"))
    os.makedirs(archives_dir, exist_ok=True)
    archive_name = f"{project['name'].replace(' ', '_')}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    zip_path = os.path.join(archives_dir, f"{archive_name}.zip")

    project_real = os.path.realpath(project_path)
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.next', 'venv', '.venv', '.idea', '.vscode', 'dist', 'build'}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(project_path, followlinks=False):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
            for f in files:
                full = os.path.join(root, f)
                # skip symlinks entirely — they can point outside the project
                if os.path.islink(full):
                    continue
                real = os.path.realpath(full)
                if os.path.commonpath([real, project_real]) != project_real:
                    continue
                arcname = os.path.relpath(full, project_path)
                try:
                    zf.write(full, arcname)
                except (OSError, ValueError):
                    continue

    # 5. Сохранить в БД
    archive_record = await save_project_archive(
        project_id=req.project_id,
        project_name=project["name"],
        description=req.description,
        master_prompt=master_prompt,
        file_list=json.dumps(file_list),
        file_count=len(file_list),
        archive_path=zip_path,
    )

    return {
        "success": True,
        "archive_id": archive_record["id"],
        "archive_name": archive_name,
        "file_count": len(file_list),
        "description": req.description,
    }

@router.get("/archives")
async def list_archives():
    """Список всех архивов."""
    _api("GET", "/api/v1/archives")
    return await get_all_archives()

@router.get("/archives/{archive_id}")
async def get_archive_detail(archive_id: int):
    """Детали архива (с мастер-промптом)."""
    _log("GET_ARCHIVE_DETAIL", source="api", details={"archive_id": archive_id})
    archive = await get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Архив не найден")
    return archive

@router.get("/archives/{archive_id}/download")
async def download_archive(archive_id: int):
    """Скачать ZIP архив."""
    _log("DOWNLOAD_ARCHIVE", source="api", details={"archive_id": archive_id})
    archive = await get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Архив не найден")
    zip_path = archive.get("archive_path", "")
    if not zip_path or not os.path.isfile(zip_path):
        raise HTTPException(404, "ZIP файл не найден")
    return FileResponse(zip_path, filename=os.path.basename(zip_path), media_type="application/zip")


def _generate_master_prompt(project_name: str, history: list, file_list: list) -> str:
    """Сгенерировать мастер-промпт из истории чата и файлов проекта."""
    lines = [
        f"# МАСТЕР-ПРОМПТ ПРОЕКТА: {project_name}",
        f"# Дата архивации: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Описание проекта",
    ]

    user_messages = [m for m in history if m["role"] == "user"]
    ai_messages = [m for m in history if m["role"] == "ai"]

    if user_messages:
        lines.append("### Ключевые задачи (из истории):")
        for msg in user_messages[:10]:
            text = msg["content"][:200]
            lines.append(f"- {text}")
        lines.append("")

    if ai_messages:
        lines.append("### Решения и подходы:")
        for msg in ai_messages[-5:]:
            text = msg["content"][:300]
            lines.append(f"- {text}")
        lines.append("")

    lines.extend([
        "## Структура проекта",
        f"Всего файлов: {len(file_list)}",
        "",
    ])
    for f in sorted(file_list)[:50]:
        lines.append(f"- {f}")
    if len(file_list) > 50:
        lines.append(f"... и ещё {len(file_list) - 50} файлов")
    lines.append("")

    lines.extend([
        "## Технический контекст",
        "При возобновлении работы над этим проектом, учти:",
        "- Все ранее найденные ошибки и их решения описаны выше",
        "- Структура файлов отражает финальное состояние проекта",
        "- Используй этот промпт как контекст для продолжения разработки",
        "",
    ])

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# IDEAS
# ═══════════════════════════════════════════════════════════════

@router.get("/ideas")
async def list_ideas():
    _api("GET", "/api/v1/ideas")
    return await get_all_ideas()

@router.post("/ideas")
async def create_idea(req: AddIdeaRequest):
    """Анализировать репозиторий и сохранить идею."""
    from core.ideas_injector import IdeasInjector
    _log("CREATE_IDEA", source="api", details={"repo_url": req.repo_url})
    injector = IdeasInjector()
    try:
        result = await injector.process_idea(req.repo_url)
        name = req.repo_url.split("/")[-1].replace(".git", "")
        return {"success": True, "analysis": result, "name": name}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.delete("/ideas/{idea_id}")
async def delete_idea_endpoint(idea_id: int):
    _log("DELETE_IDEA", source="api", details={"idea_id": idea_id})
    if await delete_idea(idea_id):
        return {"success": True}
    raise HTTPException(404, "Идея не найдена")


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

@router.get("/stats")
async def get_stats():
    _api("GET", "/api/v1/stats")
    projects = await get_all_projects()
    ideas = await get_all_ideas()
    archives = await get_all_archives()
    return {
        "projects_count": len(projects),
        "ideas_count": len(ideas),
        "archives_count": len(archives),
        "messages_count": await get_message_count(None),
        "routing_decisions": len(await get_routing_stats()),
    }

@router.get("/config")
async def get_config():
    _api("GET", "/api/v1/config")
    api_key = CONFIG["llm"].get("api_key", "")
    return {
        "llm": {
            "default_model": CONFIG["llm"].get("default_model", "not set"),
            "router_model": CONFIG["llm"].get("router_model", "not set"),
            "api_base": CONFIG["llm"].get("api_base", "not set"),
            # Never expose any portion of the key — caller may not be authenticated.
            "api_key": "configured" if api_key else "not set",
        }
    }


# ═══════════════════════════════════════════════════════════════
# THREADS — removed (threads deprecated, ChatThread table kept for DB compatibility)
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# CONTEXT COMPRESSION
# ═══════════════════════════════════════════════════════════════

class MilestoneRequest(BaseModel):
    project_id: int
    title: str

@router.get("/projects/{project_id}/context")
async def get_context_info(project_id: int):
    from core.memory import get_history, get_message_count, get_context_snapshots
    _api("GET", f"/api/v1/projects/{project_id}/context", project_id=project_id)
    history = await get_history(project_id)
    snapshots = await get_context_snapshots(project_id)
    return {
        "stats": {
            "messages_count": len(history),
            "compress_threshold": 30,
            "needs_compress": len(history) > 30,
            "snapshots_count": len(snapshots),
        },
        "snapshots": snapshots,
    }

@router.post("/projects/{project_id}/context/compress")
async def compress_context(project_id: int):
    """Ручное сжатие контекста через API. LLM-based с regex fallback + DB cleanup."""
    from core.context_compressor import ContextCompressor
    from core.memory import get_history
    _log("COMPRESS_CONTEXT", source="api", project_id=project_id)
    compressor = ContextCompressor()
    history = await get_history(project_id)
    if not compressor.should_compress(history):
        return {"success": False, "message": f"Сжатие не требуется ({len(history)} <= 30)"}
    # Try LLM compression
    comp_model = ContextCompressor.get_compression_model_config()
    summary, remaining, was_llm = await compressor.compress_and_cleanup(
        history, project_id, model_config=comp_model
    )
    method = "LLM" if was_llm else "regex"
    return {
        "success": bool(summary),
        "message": f"Сжато ({method}): {len(history) - len(remaining)} сообщений архивировано" if summary else "Сжатие не удалось",
        "method": method,
        "summary": summary,
        "messages_removed": len(history) - len(remaining) if summary else 0,
        "messages_kept": len(remaining),
    }

@router.post("/projects/{project_id}/context/milestone")
async def create_milestone(req: MilestoneRequest):
    """Создать milestone (точку сохранения контекста). Не удаляет сообщения — только сохраняет snapshot."""
    from core.memory import get_history, save_context_snapshot
    from core.context_compressor import ContextCompressor
    _log("CREATE_MILESTONE", source="api", project_id=req.project_id, details={"title": req.title})
    history = await get_history(req.project_id)
    compressor = ContextCompressor()
    # LLM-based summary for milestone
    comp_model = ContextCompressor.get_compression_model_config()
    summary, _, was_llm = await compressor.compress(
        history, project_id=req.project_id, use_llm=bool(comp_model), model_config=comp_model
    ) if history else ("", history, False)
    method = "LLM" if was_llm else "regex"
    await save_context_snapshot(
        project_id=req.project_id,
        thread_id=None,
        snapshot_type="milestone",
        title=req.title,
        summary=summary or f"Текущий контекст: {len(history)} сообщений",
        key_decisions="",
        file_changes="",
        errors_fixed="",
        message_count_before=len(history),
        message_count_after=len(history),
    )
    return {"success": True, "message": f"Мilestone '{req.title}' сохранён", "summary": summary}

@router.get("/projects/{project_id}/context/snapshots")
async def list_snapshots(project_id: int):
    from core.memory import get_context_snapshots
    _api("GET", f"/api/v1/projects/{project_id}/context/snapshots", project_id=project_id)
    return await get_context_snapshots(project_id)

@router.delete("/projects/{project_id}/context/snapshots/{snapshot_id}")
async def delete_snapshot_endpoint(project_id: int, snapshot_id: int):
    from core.memory import delete_context_snapshot
    _log("DELETE_SNAPSHOT", source="api", project_id=project_id, details={"snapshot_id": snapshot_id})
    if await delete_context_snapshot(snapshot_id):
        return {"success": True}
    raise HTTPException(404, "Снепшот не найден")


# ═══════════════════════════════════════════════════════════════
# APK BUILDER
# ═══════════════════════════════════════════════════════════════

class APKConfigRequest(BaseModel):
    project_id: int
    app_name: str = ""
    package_id: str = ""
    app_version: str = "1.0.0"
    app_color: str = "#8B1A1A"
    app_icon_prompt: str = ""
    build_type: str = "debug"  # "debug" or "release"
    android_min_sdk: int = 24
    android_target_sdk: int = 34


class APKIconRequest(BaseModel):
    prompt: str = ""
    color: str = "#8B1A1A"


@router.get("/projects/{project_id}/apk/strategy")
async def get_apk_strategy(project_id: int):
    """Get APK build strategy info for the project's template."""
    from core.apk_builder import APKBuildConfig
    _api("GET", f"/api/v1/projects/{project_id}/apk/strategy", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    template = project.get("template", "")
    config = APKBuildConfig()
    strategy = config.get_strategy(template)
    if not strategy:
        return {"supported": False, "template": template, "message": f"Шаблон '{template}' не поддерживает сборку APK"}
    return {
        "supported": True,
        "template": template,
        "strategy": strategy,
        "available_strategies": list(APKBuildConfig.TEMPLATE_STRATEGIES.keys()),
    }


@router.post("/projects/{project_id}/apk/check-env")
async def check_apk_environment(project_id: int):
    """Check if build tools are installed for the project's template."""
    from core.apk_builder import APKBuilder
    _log("CHECK_APK_ENV", source="api", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    template = project.get("template", "")
    builder = APKBuilder()
    result = await builder.check_environment(template)
    return result


@router.post("/projects/{project_id}/apk/generate-icon")
async def generate_apk_icon(project_id: int, req: APKIconRequest):
    """Generate app icon using AI (z-ai-generate) and return base64 preview."""
    import tempfile
    import base64
    from core.apk_builder import APKBuilder
    _log("GENERATE_APK_ICON", source="api", project_id=project_id, details={"prompt": req.prompt[:100]})
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")

    # Generate icon to temp file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # exec form (no shell) — req.prompt не интерпретируется как shell-строка
        process = await asyncio.create_subprocess_exec(
            "z-ai-generate", "-p", req.prompt, "-o", tmp_path, "-s", "1024x1024",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)

        if process.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            with open(tmp_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return {
                "success": True,
                "icon_base64": f"data:image/png;base64,{b64}",
                "prompt_used": req.prompt,
                "icon_size": os.path.getsize(tmp_path),
            }
        else:
            err = stderr.decode("utf-8", errors="replace")[:200]
            return {"success": False, "error": f"Генерация не удалась: {err}"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Таймаут генерации (120s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/projects/{project_id}/apk/init")
async def init_apk_platform(project_id: int):
    """Initialize Android platform for the project (first-time setup)."""
    from core.apk_builder import APKBuilder, APKBuildConfig
    from core.executor import CommandExecutor
    _log("INIT_APK_PLATFORM", source="api", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    template = project.get("template", "")
    project_path = project.get("path", "")
    if not project_path:
        return {"success": False, "error": "У проекта не указан локальный путь"}

    # Load saved config or use defaults
    apk_data = json.loads(project.get("apk_config", "{}")) if project.get("apk_config") else {}
    config = APKBuildConfig(apk_data)
    builder = APKBuilder(CommandExecutor())
    return await builder.init_platform(project_path, template, config)


@router.post("/projects/{project_id}/apk/build")
async def build_apk(req: APKConfigRequest):
    """Build APK for the project."""
    from core.apk_builder import APKBuilder, APKBuildConfig
    from core.executor import CommandExecutor
    _log("BUILD_APK", source="api", project_id=req.project_id, details={"app_name": req.app_name, "build_type": req.build_type})
    project = await get_project(req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    template = project.get("template", "")
    project_path = project.get("path", "")
    if not project_path:
        return {"success": False, "error": "У проекта не указан локальный путь"}

    # Merge saved config with request data
    apk_data = json.loads(project.get("apk_config", "{}")) if project.get("apk_config") else {}
    apk_data.update({
        "app_name": req.app_name or apk_data.get("app_name", project.get("name", "MyApp")),
        "package_id": req.package_id or apk_data.get("package_id", "com.example.app"),
        "app_version": req.app_version,
        "app_color": req.app_color,
        "app_icon_prompt": req.app_icon_prompt,
        "build_type": req.build_type,
        "android_min_sdk": req.android_min_sdk,
        "android_target_sdk": req.android_target_sdk,
    })
    config = APKBuildConfig(apk_data)

    # Validate
    errors = config.validate(template)
    if errors:
        return {"success": False, "error": "; ".join(errors)}

    # Save config to project
    from core.memory import async_session, Project, select
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Project).where(Project.id == req.project_id))
            project_obj = result.scalar_one_or_none()
            if project_obj:
                project_obj.apk_config = json.dumps(apk_data, ensure_ascii=False)

    builder = APKBuilder(CommandExecutor())
    app_desc = project.get("description", "") or project.get("name", "")
    return await builder.build(project_path, template, config, app_description=app_desc)


@router.get("/projects/{project_id}/apk/config")
async def get_apk_config(project_id: int):
    """Get saved APK build config for the project."""
    _api("GET", f"/api/v1/projects/{project_id}/apk/config", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    apk_data = json.loads(project.get("apk_config", "{}")) if project.get("apk_config") else {}
    from core.apk_builder import APKBuildConfig
    config = APKBuildConfig(apk_data)
    return config.to_dict()


@router.put("/projects/{project_id}/apk/config")
async def save_apk_config(project_id: int, req: APKConfigRequest):
    """Save APK build config for the project."""
    _log("SAVE_APK_CONFIG", source="api", project_id=project_id)
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    apk_data = json.loads(project.get("apk_config", "{}")) if project.get("apk_config") else {}
    apk_data.update({
        "app_name": req.app_name,
        "package_id": req.package_id,
        "app_version": req.app_version,
        "app_color": req.app_color,
        "build_type": req.build_type,
        "android_min_sdk": req.android_min_sdk,
        "android_target_sdk": req.android_target_sdk,
    })
    await update_project_field_persist(project_id, "apk_config", json.dumps(apk_data, ensure_ascii=False))
    return {"success": True, "message": "Конфигурация APK сохранена"}


async def update_project_field_persist(project_id: int, field: str, value: str):
    """Generic: update a single project field in DB."""
    from core.memory import async_session, Project, select
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Project).where(Project.id == project_id))
            project_obj = result.scalar_one_or_none()
            if project_obj:
                setattr(project_obj, field, value)

# ═══════════════════════════════════════════════════════════════
# ACTION LOGGING API
# ═══════════════════════════════════════════════════════════════

@router.get("/logs")
async def get_logs(limit: int = 200, level: str = None, source: str = None,
                  project_id: int = None, since: str = None):
    """Получить записи лога действий."""
    return action_logger.get_entries(limit=limit, level=level, source=source,
                                       project_id=project_id, since=since)

@router.get("/logs/stats")
async def get_log_stats():
    """Статистика логов."""
    return action_logger.get_stats()

@router.get("/logs/errors")
async def get_log_errors(limit: int = 50, project_id: int = None):
    """Получить только ошибки."""
    return action_logger.get_errors(limit=limit, project_id=project_id)

@router.delete("/logs")
async def clear_logs():
    """Очистить логи в памяти."""
    action_logger.clear_memory()
    return {"success": True, "message": "Логи в памяти очищены (файл на диске сохранён)"}

@router.get("/logs/file")
async def get_log_file(limit: int = 500):
    """Прочитать логи из файла на диске."""
    return {"entries": action_logger.read_log_file(limit=limit)}


# ═══════════════════════════════════════════════════════════════
# EXTENDED STATS (расширенная статистика проекта)
# ═══════════════════════════════════════════════════════════════

@router.get("/stats/extended")
async def get_extended_stats():
    """Расширенная статистика: модели, ошибки, файлы проекта, GitHub, APK."""
    from core.memory import get_all_projects, CONFIG
    import os as _os

    stats = {}

    # 1. Все проекты с путями
    try:
        projects = await get_all_projects()
        stats["projects"] = []
        for p in projects:
            pinfo = {
                "id": p.get("id"),
                "name": p.get("name"),
                "path": p.get("path", ""),
                "github_repo": p.get("github_repo", ""),
                "local_path": p.get("local_path", ""),
                "progress": p.get("progress", 0),
                "template": p.get("template", ""),
                "selected_models": p.get("selected_models", "[]"),
                "message_count": p.get("message_count", 0),
            }
            stats["projects"].append(pinfo)
    except Exception:
        stats["projects"] = []

    # 2. Статистика логов
    try:
        log_stats = action_logger.get_stats()
        stats["logs"] = log_stats
    except Exception:
        stats["logs"] = {"total": 0, "errors": 0}

    # 3. Модели — статистика по ошибкам из логов
    try:
        model_errors = {}
        model_success = {}
        for entry in action_logger._entries:
            src = entry.get("source", "")
            lvl = entry.get("level", "")
            action = entry.get("action", "")
            # Извлекаем имя модели из логов агента
            if src == "agent" and "model=" in action:
                # Простой парсинг: ищем patterns вроде "stream_llm_response: model=xxx"
                model_name = "unknown"
                for kw in ["model=", "Модель:", "Ответ от", "Переключаюсь на"]:
                    idx = action.find(kw)
                    if idx >= 0:
                        rest = action[idx + len(kw):].strip().split(",")[0].split(" ")[0]
                        if rest:
                            model_name = rest
                            break
                if lvl == "error":
                    model_errors[model_name] = model_errors.get(model_name, 0) + 1
                elif lvl in ("success", "info"):
                    model_success[model_name] = model_success.get(model_name, 0) + 1

        stats["model_stats"] = {
            "errors": model_errors,
            "success": model_success,
            "worst_model": max(model_errors, key=model_errors.get) if model_errors else None,
            "best_model": max(model_success, key=model_success.get) if model_success else None,
        }
    except Exception:
        stats["model_stats"] = {"errors": {}, "success": {}, "worst_model": None, "best_model": None}

    # 4. Файлы проектов — путь к APK (если есть)
    try:
        apk_paths = []
        projects_dir = CONFIG.get("system", {}).get("projects_dir", "projects")
        for root, dirs, files in _os.walk(projects_dir):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", "venv", ".next", "build", "dist"}]
            for f in files:
                if f.endswith(".apk"):
                    apk_paths.append(_os.path.join(root, f))
        stats["apk_paths"] = apk_paths
    except Exception:
        stats["apk_paths"] = []

    # 5. Системная информация
    try:
        import platform
        stats["system"] = {
            "os": platform.system(),
            "python": platform.python_version(),
            "hostname": platform.node(),
        }
    except Exception:
        stats["system"] = {}

    return stats


# ═══════════════════════════════════════════════════════════════
# DEBUG MODE API (красная кнопка ТЕСТ)
# ═══════════════════════════════════════════════════════════════

class ClientLogEntry(BaseModel):
    type: str = ""  # "js_error", "fetch_error", "fetch_slow", "ui_action", "console_warn"
    message: str = ""
    url: str = ""
    line: int = None
    col: int = None
    stack: str = ""
    details: dict = {}


@router.post("/debug/start")
async def debug_start():
    """Включить debug-режим — тотальное логирование."""
    result = action_logger.start_debug_session()
    action_logger.log("DEBUG_START_API", level="warning", source="api")
    return result


@router.post("/debug/stop")
async def debug_stop():
    """Выключить debug-режим и вернуть собранный лог."""
    result = action_logger.stop_debug_session()
    return result


@router.get("/debug/status")
async def debug_status():
    """Статус текущей debug-сессии."""
    return action_logger.get_debug_status()


@router.post("/debug/client-log")
async def debug_client_log(entry: ClientLogEntry):
    """Принать лог с клиента (JS errors, fetch failures, и т.д.)."""
    action_logger.add_client_log(entry.model_dump())
    return {"ok": True}


@router.post("/debug/analyze")
async def debug_analyze():
    """Остановить debug и отправить лог на AI-анализ."""
    import litellm

    # Собрать лог
    debug_data = action_logger.stop_debug_session()
    server_entries = debug_data.get("entries", [])
    client_errors = debug_data.get("client_errors", [])

    if not server_entries and not client_errors:
        return {"analysis": "Нет данных для анализа — сессия была пустой.", "bugs": []}

    # Формируем сводку для AI
    summary_lines = [
        f"=== DEBUG SESSION REPORT ===",
        f"Duration: {debug_data.get('duration_sec', 0)}s",
        f"Server entries: {len(server_entries)}",
        f"Client errors: {len(client_errors)}",
        "",
        "--- SERVER LOG (errors & warnings) ---",
    ]

    # Только ошибки и предупреждения с сервера
    for e in server_entries:
        if e.get("level") in ("error", "warning"):
            line = f"[{e.get('time', '')}] [{e.get('source', '')}] {e.get('action', '')}"
            if e.get("error"):
                line += f" | ERROR: {e['error']}"
            if e.get("details"):
                line += f" | Details: {str(e['details'])[:300]}"
            if e.get("stack_trace"):
                line += f"\n  STACK: {e['stack_trace']}"
            summary_lines.append(line)

    summary_lines.append("")
    summary_lines.append("--- CLIENT ERRORS (JS) ---")
    for e in client_errors:
        line = f"[{e.get('time', '')}] [{e.get('type', '')}] {e.get('message', '')}"
        if e.get("url"):
            line += f" at {e['url']}:{e.get('line', '?')}:{e.get('col', '?')}"
        if e.get("stack"):
            line += f"\n  STACK: {e['stack'][:500]}"
        summary_lines.append(line)

    summary_lines.append("")
    summary_lines.append("--- ALL ACTIONS (timeline) ---")
    for e in server_entries:
        lvl_marker = {"error": "!!", "warning": "!?", "success": "OK", "info": ">>", "debug": "--"}.get(e.get("level", "info"), ">>")
        line = f"[{e.get('time', '')}] {lvl_marker} [{e.get('source', '')}] {e.get('action', '')}"
        if e.get("error"):
            line += f" | ERR: {e['error'][:200]}"
        summary_lines.append(line)

    summary_text = "\n".join(summary_lines)

    # Отправляем в AI для анализа
    analysis_prompt = f"""Ты — эксперт по отладке web-приложений (FastAPI + WebSocket + vanilla JS).
Проанализируй лог debug-сессии приложения Fosved Coder и найди все баги, ошибки и проблемные места.

{summary_text}

Ответь в формате:
1. НАЙДЕННЫЕ БАГИ — список конкретных багов с описанием и建议 по исправлению
2. ПРИЧИНЫ — вероятные причины каждого бага
3. ИСПРАВЛЕНИЯ — конкретный код для исправления (файл и изменения)
4. СТАТИСТИКА — краткая сводка: всего ошибок, предупреждений, действий

Если багов нет — напиши "Багов не обнаружено" и дай краткую сводку сессии."""

    try:
        # Используем первую доступную модель
        models = keys_manager.get_all_models()
        model_id = None
        for m in models:
            if m.get("status") in ("valid", "available") and not m.get("is_free"):
                model_id = m.get("id") or m.get("model_id")
                break
        if not model_id and models:
            model_id = models[0].get("id") or models[0].get("model_id")

        if not model_id:
            return {
                "analysis": "Нет доступных моделей ИИ для анализа. Лог собран и сохранён.",
                "bugs": [],
                "raw_log": summary_text[:5000],
            }

        response = await litellm.acompletion(
            model=model_id,
            messages=[
                {"role": "system", "content": "Ты — эксперт по отладке. Отвечай на русском. Будь конкретным и кратким."},
                {"role": "user", "content": analysis_prompt},
            ],
            max_tokens=4000,
            temperature=0.1,
            timeout=60,
        )
        analysis = response.choices[0].message.content if response.choices else "Не удалось получить анализ"

        return {
            "analysis": analysis,
            "duration_sec": debug_data.get("duration_sec", 0),
            "server_entries": len(server_entries),
            "client_errors": len(client_errors),
            "errors_count": sum(1 for e in server_entries if e.get("level") == "error"),
            "warnings_count": sum(1 for e in server_entries if e.get("level") == "warning"),
        }

    except Exception as e:
        return {
            "analysis": f"Ошибка AI-анализа: {str(e)[:300]}. Лог собран и сохранён в файл debug_session.jsonl",
            "bugs": [],
            "raw_log": summary_text[:3000],
            "error": str(e)[:300],
        }


@router.post("/debug/push-to-github")
async def debug_push_to_github():
    """
    Остановить debug-сессию, собрать логи, сохранить в файл и git push на GitHub.
    Возвращает {pushed, log_file, github_url, ...} или {error}.
    """
    from datetime import datetime, timezone

    # 1. Собрать debug-лог
    debug_data = action_logger.stop_debug_session()
    server_entries = debug_data.get("entries", [])
    client_errors = debug_data.get("client_errors", [])

    # 2. Формируем отчёт в Markdown
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    session_id = now.strftime("%Y%m%d_%H%M%S")
    log_filename = f"debug_{session_id}.md"
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs")

    lines = [
        f"# Debug Session — {timestamp}",
        "",
        f"- **Длительность:** {debug_data.get('duration_sec', 0)} сек",
        f"- **Событий сервера:** {len(server_entries)}",
        f"- **JS ошибок (клиент):** {len(client_errors)}",
        f"- **Ошибок API:** {sum(1 for e in server_entries if e.get('level') == 'error')}",
        f"- **Предупреждений:** {sum(1 for e in server_entries if e.get('level') == 'warning')}",
        "",
        "---",
        "",
        "## Ошибки и Предупреждения",
        "",
    ]

    errors_found = False
    for e in server_entries:
        if e.get("level") in ("error", "warning"):
            errors_found = True
            lvl = "ERROR" if e["level"] == "error" else "WARN"
            line = f"**[{e.get('time', '')}] [{lvl}] [{e.get('source', '')}]** `{e.get('action', '')}`"
            if e.get("error"):
                line += f"\n  > {e['error'][:300]}"
            if e.get("stack_trace"):
                line += f"\n```\n{e['stack_trace'][:1000]}\n```"
            if e.get("details"):
                d = str(e["details"])[:300]
                if d and d != "{}":
                    line += f"\n  Details: {d}"
            lines.append(line)
            lines.append("")

    if not errors_found:
        lines.append("_Ошибок не обнаружено._")
        lines.append("")

    # JS ошибки
    if client_errors:
        lines.append("## JS Ошибки (клиент)")
        lines.append("")
        for e in client_errors:
            line = f"**[{e.get('time', '')}] [{e.get('type', '')}]** {e.get('message', '')[:200]}"
            if e.get("url"):
                line += f"  — `{e['url']}`"
            if e.get("line"):
                line += f":{e.get('line')}:{e.get('col', '?')}"
            if e.get("stack"):
                line += f"\n```\n{e['stack'][:500]}\n```"
            lines.append(line)
            lines.append("")

    # Таймлайн всех действий
    lines.append("---")
    lines.append("")
    lines.append("## Полный таймлайн")
    lines.append("")
    for e in server_entries:
        lvl_marker = {"error": "ERR", "warning": "WRN", "success": "OK ", "info": "INF", "debug": "DBG"}.get(e.get("level", "info"), "INF")
        line = f"`{e.get('time', '')}` [{lvl_marker}] [{e.get('source', '')}] {e.get('action', '')}"
        if e.get("details"):
            d = str(e["details"])[:150]
            if d and d != "{}":
                line += f"  _{d}_"
        lines.append(line)

    report_text = "\n".join(lines)

    # 3. Сохраняем отчёт на диск
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_filename)
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(report_text)
    except Exception as save_err:
        return {"error": f"Не удалось сохранить лог: {save_err}", "raw_log": report_text[:5000]}

    # 4. Git push на GitHub (если подключён)
    github_url = ""
    pushed = False
    try:
        gh_token = keys_manager.github_token
        gh_enabled = keys_manager.github_enabled

        if gh_token and gh_enabled:
            # Ищем репозиторий (проект с .git или корень Fosved Coder)
            from core.memory import get_all_projects
            projects = await get_all_projects()
            repo_path = None
            for p in projects:
                ppath = p.get("path", "")
                if ppath and os.path.isdir(os.path.join(ppath, ".git")):
                    repo_path = ppath
                    break

            if not repo_path:
                fc_root = os.path.dirname(os.path.dirname(__file__))
                if os.path.isdir(os.path.join(fc_root, ".git")):
                    repo_path = fc_root

            if repo_path:
                # Копируем файл лога в репозиторий
                repo_log_path = os.path.join(repo_path, log_filename)
                shutil.copy2(log_path, repo_log_path)

                # git add + commit + push через subprocess
                import subprocess as sp
                commit_msg = f"debug: log session {session_id} ({len(server_entries)} events)"

                def run_git(args):
                    return sp.run(
                        ["git", "-C", repo_path] + args,
                        capture_output=True, text=True, timeout=30
                    )

                run_git(["add", log_filename])
                commit_result = run_git(["commit", "-m", commit_msg, "--allow-empty"])
                commit_out = (commit_result.stdout + commit_result.stderr).strip()

                if "nothing to commit" not in commit_out.lower():
                    push_result = run_git(["push"])
                    if push_result.returncode == 0:
                        pushed = True
                        # Получаем remote URL
                        remote_result = run_git(["remote", "get-url", "origin"])
                        remote_url = remote_result.stdout.strip()
                        if remote_url and "@" in remote_url:
                            github_url = remote_url.split("@")[-1].replace(".git", "")
                            github_url = "https://" + github_url + "/blob/main/" + log_filename
                        elif remote_url.startswith("http"):
                            github_url = remote_url.replace(".git", "") + "/blob/main/" + log_filename
                    else:
                        action_logger.log("debug_push_failed", level="error", source="debug",
                                           error=push_result.stderr[:200])
                else:
                    pushed = True
            else:
                action_logger.log("debug_push_no_repo", level="warning", source="debug")
    except Exception as git_err:
        action_logger.log("debug_push_error", level="error", source="debug",
                           error=str(git_err)[:200])

    return {
        "pushed": pushed,
        "log_file": log_filename,
        "log_path": log_path,
        "github_url": github_url,
        "duration_sec": debug_data.get("duration_sec", 0),
        "server_entries": len(server_entries),
        "client_errors": len(client_errors),
        "errors_count": sum(1 for e in server_entries if e.get("level") == "error"),
        "warnings_count": sum(1 for e in server_entries if e.get("level") == "warning"),
        "raw_log": report_text[:5000] if not pushed else None,
    }


# ═══════════════════════════════════════════════════════
# SETTINGS (persist to config.yaml on server)
# ═══════════════════════════════════════════════════════

class UpdateSettingsRequest(BaseModel):
    default_model: str = ""

@router.get("/settings")
async def get_settings():
    """Get current server settings (LLM config)."""
    _api("GET", "/api/v1/settings")
    from core.memory import CONFIG
    return {
        "default_model": CONFIG.get("llm", {}).get("default_model", ""),
        "temperature": CONFIG.get("llm", {}).get("temperature", 0.2),
        "max_tokens": CONFIG.get("llm", {}).get("max_tokens", 4096),
    }

@router.post("/settings")
async def update_settings(req: UpdateSettingsRequest):
    """Update server settings and persist to config.yaml."""
    _log("UPDATE_SETTINGS", source="api", details={"default_model": req.default_model})
    from core.memory import CONFIG
    import yaml as _yaml
    
    CONFIG["llm"]["default_model"] = req.default_model
    
    # Persist to config.yaml
    try:
        config_path = "config.yaml"
        existing_data = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                existing_data = _yaml.safe_load(f) or {}
        if "llm" not in existing_data:
            existing_data["llm"] = {}
        existing_data["llm"]["default_model"] = req.default_model
        # Keep other fields
        for section in ["system", "security"]:
            if section in CONFIG and section not in existing_data:
                existing_data[section] = CONFIG[section]
        with open(config_path, "w", encoding="utf-8") as f:
            _yaml.dump(existing_data, f, default_flow_style=False, allow_unicode=True)
        _log("SETTINGS_SAVED", source="api", level="success")
    except Exception as e:
        _log("SETTINGS_SAVE_ERROR", source="api", level="error", error=str(e))
    return {"success": True, "default_model": req.default_model}


# ═══════════════════════════════════════════════════════
# QUESTIONNAIRE (Анкеты — Фаза 3.1)
# ═══════════════════════════════════════════════════════

class CreateQuestionnaireRequest(BaseModel):
    title: str = ""
    project_id: Optional[str] = None

class UpdateQuestionnaireRequest(BaseModel):
    questions: list = []
    project_card: dict = {}
    status: str = ""
    title: str = ""


@router.post("/questionnaire/create")
async def create_questionnaire_endpoint(req: CreateQuestionnaireRequest):
    """Создать новую анкету."""
    _log("CREATE_QUESTIONNAIRE", source="api", details={"title": req.title, "project_id": req.project_id})
    q_id = await save_questionnaire({
        "title": req.title,
        "project_id": req.project_id,
    })
    return {"id": q_id, "status": "created"}


@router.get("/questionnaire/{q_id}")
async def get_questionnaire_endpoint(q_id: str):
    """Получить анкету по ID."""
    _api("GET", f"/api/v1/questionnaire/{q_id}")
    q = await get_questionnaire(q_id)
    if not q:
        raise HTTPException(404, "Анкета не найдена")
    return q


@router.put("/questionnaire/{q_id}")
async def update_questionnaire_endpoint(q_id: str, req: UpdateQuestionnaireRequest):
    """Обновить анкету (добавить ответы, обновить статус)."""
    _log("UPDATE_QUESTIONNAIRE", source="api", details={"q_id": q_id})
    data = {"id": q_id}
    if req.questions:
        data["questions"] = req.questions
    if req.project_card:
        data["project_card"] = req.project_card
    if req.status:
        data["status"] = req.status
    if req.title:
        data["title"] = req.title
    result_id = await save_questionnaire(data)
    return {"id": result_id, "status": "updated"}


@router.get("/questionnaires")
async def list_questionnaires_endpoint(project_id: Optional[str] = Query(default=None)):
    """Список всех анкет (опциональный фильтр по project_id)."""
    _api("GET", "/api/v1/questionnaires")
    if project_id:
        questionnaires = await get_questionnaires_by_project(project_id)
    else:
        # Получить все анкеты — вызываем через session напрямую
        from core.memory import Questionnaire, async_session, select
        import json
        async with async_session() as session:
            result = await session.execute(
                select(Questionnaire).order_by(Questionnaire.created_at.desc())
            )
            questionnaires = [
                {
                    "id": q.id,
                    "project_id": q.project_id,
                    "title": q.title,
                    "questions": json.loads(q.questions) if q.questions else [],
                    "project_card": json.loads(q.project_card) if q.project_card else {},
                    "status": q.status,
                    "created_at": q.created_at,
                    "updated_at": q.updated_at,
                }
                for q in result.scalars().all()
            ]
    return {"questionnaires": questionnaires, "count": len(questionnaires)}


@router.delete("/questionnaire/{q_id}")
async def delete_questionnaire_endpoint(q_id: str):
    """Удалить анкету."""
    _log("DELETE_QUESTIONNAIRE", source="api", details={"q_id": q_id})
    if not await delete_questionnaire(q_id):
        raise HTTPException(404, "Анкета не найдена")
    return {"success": True, "id": q_id}


# ═══════════════════════════════════════════════════════
# MODEL PROBING
# ═══════════════════════════════════════════════════════

@router.post("/probe-models")
async def probe_models_endpoint():
    """Запустить silent model probing (фоновая задача)."""
    _log("PROBE_MODELS", source="api")
    from core.agent import probe_models
    import asyncio

    # Запускаем probing в фоне, не блокируя ответ
    async def _run_probe():
        try:
            results = await probe_models()
            await save_probed_models(results)
            _log("PROBE_MODELS_DONE", source="api", level="success", details={"count": len(results)})
        except Exception as e:
            _log("PROBE_MODELS_ERROR", source="api", level="error", error=str(e)[:200])

    asyncio.create_task(_run_probe())
    return {"status": "started", "message": "Пробинг моделей запущен в фоне"}


@router.get("/probed-models")
async def get_probed_models_endpoint():
    """Получить кэшированные результаты model probing."""
    _api("GET", "/api/v1/probed-models")
    models = await get_probed_models()
    return {"models": models, "count": len(models)}


# ═══════════════════════════════════════════════════════
# HUB CHAT (главный экран — SSE streaming)
# ═══════════════════════════════════════════════════════

class HubChatRequest(BaseModel):
    prompt: str
    model_id: Optional[str] = None


@router.post("/hub/chat")
async def hub_chat_endpoint(req: HubChatRequest):
    """Чат главного экрана (Hub). Возвращает SSE stream."""
    _log("HUB_CHAT", source="api", details={"prompt_len": len(req.prompt), "model_id": req.model_id})
    from core.agent import handle_hub_message

    async def _stream():
        # Фейковый websocket для handle_hub_message
        collected = []

        class _FakeWS:
            async def send_json(self, data):
                collected.append(data)

        fake_ws = _FakeWS()
        await handle_hub_message(req.prompt, fake_ws, model_id=req.model_id)

        # Отправляем все собранные сообщения как SSE
        for msg in collected:
            msg_type = msg.get("type", "")
            if msg_type == "chunk":
                content = msg.get("content", "")
                yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"
            elif msg_type == "tool_call":
                yield f"data: {json.dumps({'type': 'tool_call', 'name': msg.get('name', ''), 'args': msg.get('arguments', '')}, ensure_ascii=False)}\n\n"
            elif msg_type == "tool_result":
                yield f"data: {json.dumps({'type': 'tool_result', 'name': msg.get('name', ''), 'result': msg.get('result', '')[:500]}, ensure_ascii=False)}\n\n"
            elif msg_type == "error":
                yield f"data: {json.dumps({'type': 'error', 'content': msg.get('content', '')}, ensure_ascii=False)}\n\n"
            elif msg_type == "done":
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        # Если ничего не было собрано, отправляем пустой ответ
        if not collected:
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
# MEMORY / OBSERVATIONS (claude-mem inspired)
# ═══════════════════════════════════════════════════════════════

@router.get("/memory/stats")
async def memory_stats(project_id: int | None = Query(None)):
    """Статистика системы памяти."""
    _api("GET", "/api/v1/memory/stats")
    from core.observation_manager import get_memory_stats
    return await get_memory_stats(project_id=project_id)


@router.get("/memory/search")
async def memory_search(
    q: str = Query("", min_length=1),
    project_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    type: str | None = Query(None),
    hours: int | None = Query(None),
):
    """Поиск по наблюдениям (Layer 1: compact index)."""
    _api("GET", "/api/v1/memory/search")
    from core.observation_manager import search_observations
    obs_types = [type] if type else None
    results = await search_observations(
        query=q, project_id=project_id, limit=limit,
        obs_types=obs_types, hours=hours,
    )
    return {"results": results, "count": len(results)}


@router.get("/memory/observations")
async def memory_get_observations(
    ids: str = Query("", description="Comma-separated observation IDs"),
):
    """Layer 3: полные данные для выбранных наблюдений."""
    _api("GET", "/api/v1/memory/observations")
    from core.observation_manager import get_observation_details
    if not ids:
        return {"observations": []}
    try:
        obs_ids = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid IDs format")
    results = await get_observation_details(obs_ids)
    return {"observations": results, "count": len(results)}


@router.get("/memory/timeline")
async def memory_timeline(
    project_id: int | None = Query(None),
    around_id: int | None = Query(None),
    before_hours: int = Query(2),
    after_hours: int = Query(2),
    limit: int = Query(30, ge=1, le=100),
):
    """Layer 2: хронологический контекст вокруг наблюдения."""
    _api("GET", "/api/v1/memory/timeline")
    from core.observation_manager import get_recent_timeline
    results = await get_recent_timeline(
        project_id=project_id, around_obs_id=around_id,
        before_hours=before_hours, after_hours=after_hours, limit=limit,
    )
    return {"timeline": results, "count": len(results)}


@router.get("/memory/summaries")
async def memory_summaries(
    project_id: int | None = Query(None),
    limit: int = Query(5, ge=1, le=20),
):
    """Последние резюме сессий."""
    _api("GET", "/api/v1/memory/summaries")
    from core.observation_manager import get_recent_summaries
    results = await get_recent_summaries(project_id=project_id, limit=limit)
    return {"summaries": results, "count": len(results)}


@router.get("/memory/context")
async def memory_context(
    project_id: int | None = Query(None),
    max_tokens: int = Query(500, ge=100, le=2000),
):
    """Собрать контекст из памяти для инъекции в промпт."""
    _api("GET", "/api/v1/memory/context")
    from core.observation_manager import assemble_context
    context = await assemble_context(project_id=project_id, max_tokens=max_tokens)
    return {"context": context, "tokens_estimate": len(context) // 3}
