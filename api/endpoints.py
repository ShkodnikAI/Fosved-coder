"""
Fosved Coder v2.0 — REST API Endpoints
Включает управление ключами, моделями, проектами, локальные модели, кастомные модели.
Поиск файлов, гит, шаблоны, пакеты, архив.
"""
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import json
import fnmatch
import os
import asyncio
import subprocess
import shutil
import tempfile
from datetime import datetime

from core.memory import (
    CONFIG, create_project, get_all_projects, get_project,
    delete_project, update_project_progress, update_project_models,
    get_all_ideas, delete_idea, get_message_count,
    save_routing_stat, get_routing_stats, get_history, save_message,
    save_project_archive, get_all_archives, get_archive,
)
from core.keys_manager import keys_manager, PROVIDER_DEFS, LOCAL_PROVIDERS
from core.action_logger import get_logger

router = APIRouter(prefix="/api/v1")
action_logger = get_logger()


# ═══════════════════════════════════════════════════════════════
# SYSTEM HEALTH & STATUS
# ═══════════════════════════════════════════════════════════════

@router.get("/health")
async def health_check():
    """Проверка состояния системы: БД, API ключи, провайдеры."""
    try: action_logger.api_call("GET", "/api/v1/health")
    except Exception: pass
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
    try: action_logger.api_call("GET", "/api/v1/status")
    except Exception: pass
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

class ToggleGitHubRequest(BaseModel):
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


# ═══════════════════════════════════════════════════════════════
# KEYS & MODELS
# ═══════════════════════════════════════════════════════════════

@router.get("/")
async def api_root():
    """Корень API — список доступных endpoints."""
    try: action_logger.api_call("GET", "/api/v1/")
    except Exception: pass
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
    try: action_logger.log("ADD_KEY", source="api", details={"provider": req.provider})
    except Exception: pass
    result = await keys_manager.add_key(
        provider_id=req.provider,
        api_key=req.api_key,
        models=req.models if req.models else None,
        api_base=req.api_base if req.api_base else None,
    )
    if not result["success"]:
        try: action_logger.log("ADD_KEY", source="api", level="error", error=result["error"], details={"provider": req.provider})
        except Exception: pass
        raise HTTPException(400, result["error"])
    try: action_logger.log("ADD_KEY", source="api", level="success", details={"provider": req.provider})
    except Exception: pass
    return result

@router.delete("/keys/{provider_id}")
async def remove_key(provider_id: str):
    """Удаление API-ключа провайдера."""
    try: action_logger.log("REMOVE_KEY", source="api", details={"provider_id": provider_id})
    except Exception: pass
    if keys_manager.remove_key(provider_id):
        try: action_logger.log("REMOVE_KEY", source="api", level="success", details={"provider_id": provider_id})
        except Exception: pass
        return {"success": True, "provider": provider_id}
    try: action_logger.log("REMOVE_KEY", source="api", level="error", error=f"Provider {provider_id} not found", details={"provider_id": provider_id})
    except Exception: pass
    raise HTTPException(404, f"Провайдер {provider_id} не найден")

@router.get("/keys/providers")
async def get_providers():
    """Список всех провайдеров с их статусом."""
    try: action_logger.api_call("GET", "/api/v1/keys/providers")
    except Exception: pass
    return {
        "providers": PROVIDER_DEFS,
        "configured": keys_manager.get_provider_status(),
    }

@router.get("/keys/github")
async def get_github_status():
    """Статус GitHub интеграции."""
    try: action_logger.api_call("GET", "/api/v1/keys/github")
    except Exception: pass
    return keys_manager.get_github_status()

@router.post("/keys/github")
async def set_github_token(req: GitHubTokenRequest):
    """Установка и валидация GitHub токена."""
    try: action_logger.log("SET_GITHUB_TOKEN", source="api", details={"enabled": req.enabled})
    except Exception: pass
    validation = await keys_manager.validate_github_token(req.token)
    if validation["status"] != "valid":
        try: action_logger.log("SET_GITHUB_TOKEN", source="api", level="error", error=validation["error"])
        except Exception: pass
        raise HTTPException(400, validation["error"])
    keys_manager.set_github_token(req.token, req.enabled)
    try: action_logger.log("SET_GITHUB_TOKEN", source="api", level="success", details={"user": validation.get("user"), "enabled": req.enabled})
    except Exception: pass
    return {"success": True, "user": validation["user"]}

@router.put("/keys/github/toggle")
async def toggle_github(req: ToggleGitHubRequest):
    """Включение/отключение GitHub интеграции."""
    try: action_logger.log("TOGGLE_GITHUB", source="api", details={"enabled": req.enabled})
    except Exception: pass
    result = keys_manager.toggle_github(req.enabled)
    return result

@router.put("/keys/{provider_id}/toggle")
async def toggle_provider(provider_id: str, req: ToggleProviderRequest):
    """Включение/отключение провайдера (модели скрываются из списка)."""
    try: action_logger.log("TOGGLE_PROVIDER", source="api", details={"provider_id": provider_id, "enabled": req.enabled})
    except Exception: pass
    result = keys_manager.toggle_provider(provider_id, req.enabled)
    if not result["success"]:
        raise HTTPException(404, result["error"])
    return result


@router.get("/models")
async def get_all_models():
    """Список всех доступных моделей (платные + локальные + бесплатные + кастомные)."""
    try: action_logger.api_call("GET", "/api/v1/models")
    except Exception: pass
    return {"models": keys_manager.get_all_models()}

@router.post("/models/validate/{provider_id}")
async def revalidate_provider(provider_id: str):
    """Повторная валидация ключа провайдера."""
    try: action_logger.log("REVALIDATE_PROVIDER", source="api", details={"provider_id": provider_id})
    except Exception: pass
    config = keys_manager.providers.get(provider_id)
    if not config:
        try: action_logger.log("REVALIDATE_PROVIDER", source="api", level="error", error=f"Provider {provider_id} not found")
        except Exception: pass
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
    try: action_logger.log("REFRESH_ABACUS_MODELS", source="api")
    except Exception: pass
    result = await keys_manager.fetch_abacus_models()
    if not result["success"]:
        try: action_logger.log("REFRESH_ABACUS_MODELS", source="api", level="error", error=result["error"])
        except Exception: pass
    else:
        try: action_logger.log("REFRESH_ABACUS_MODELS", source="api", level="success", details={"count": result["count"]})
        except Exception: pass
    return result


# ═══════════════════════════════════════════════════════════════
# LOCAL MODELS
# ═══════════════════════════════════════════════════════════════

@router.get("/models/local")
async def list_local_models():
    """Список сохранённых локальных моделей."""
    try: action_logger.api_call("GET", "/api/v1/models/local")
    except Exception: pass
    return {
        "models": keys_manager.local_models,
        "providers": LOCAL_PROVIDERS,
    }

@router.post("/models/local/discover")
async def discover_local_models(req: DiscoverLocalModelsRequest):
    """Автообнаружение моделей на локальном сервере (Ollama, LM Studio и т.д.)."""
    try: action_logger.log("DISCOVER_LOCAL_MODELS", source="api", details={"provider_key": req.provider_key})
    except Exception: pass
    result = await keys_manager.discover_local_models(
        provider_key=req.provider_key,
        base_url=req.base_url if req.base_url else None,
    )
    return result

@router.post("/models/local")
async def add_local_model(req: AddLocalModelRequest):
    """Ручное добавление локальной модели."""
    try: action_logger.log("ADD_LOCAL_MODEL", source="api", details={"provider_key": req.provider_key, "model_name": req.model_name})
    except Exception: pass
    result = await keys_manager.add_local_model(
        provider_key=req.provider_key,
        model_name=req.model_name,
        base_url=req.base_url,
        display_name=req.display_name,
    )
    if not result["success"]:
        try: action_logger.log("ADD_LOCAL_MODEL", source="api", level="error", error=result["error"], details={"provider_key": req.provider_key, "model_name": req.model_name})
        except Exception: pass
        raise HTTPException(400, result["error"])
    try: action_logger.log("ADD_LOCAL_MODEL", source="api", level="success", details={"provider_key": req.provider_key, "model_name": req.model_name})
    except Exception: pass
    return result

@router.delete("/models/local/{model_id}")
async def remove_local_model(model_id: str):
    """Удаление локальной модели."""
    try: action_logger.log("REMOVE_LOCAL_MODEL", source="api", details={"model_id": model_id})
    except Exception: pass
    if keys_manager.remove_local_model(model_id):
        try: action_logger.log("REMOVE_LOCAL_MODEL", source="api", level="success", details={"model_id": model_id})
        except Exception: pass
        return {"success": True, "model_id": model_id}
    try: action_logger.log("REMOVE_LOCAL_MODEL", source="api", level="error", error=f"Local model {model_id} not found")
    except Exception: pass
    raise HTTPException(404, f"Локальная модель {model_id} не найдена")


# ═══════════════════════════════════════════════════════════════
# CUSTOM MODELS (force connect)
# ═══════════════════════════════════════════════════════════════

@router.get("/models/custom")
async def list_custom_models():
    """Список кастомных (принудительно подключённых) моделей."""
    try: action_logger.api_call("GET", "/api/v1/models/custom")
    except Exception: pass
    return {"models": keys_manager.custom_models}

@router.post("/models/custom")
async def add_custom_model(req: AddCustomModelRequest):
    """Принудительное добавление модели по URL (force connect)."""
    try: action_logger.log("ADD_CUSTOM_MODEL", source="api", details={"name": req.name, "api_base": req.api_base})
    except Exception: pass
    result = await keys_manager.add_custom_model(
        name=req.name,
        api_base=req.api_base,
        api_key=req.api_key,
        model_id=req.model_id,
        litellm_prefix=req.litellm_prefix,
    )
    if not result["success"]:
        try: action_logger.log("ADD_CUSTOM_MODEL", source="api", level="error", error=result["error"], details={"name": req.name})
        except Exception: pass
        raise HTTPException(400, result["error"])
    try: action_logger.log("ADD_CUSTOM_MODEL", source="api", level="success", details={"name": req.name})
    except Exception: pass
    return result

@router.delete("/models/custom/{model_id}")
async def remove_custom_model(model_id: str):
    """Удаление кастомной модели."""
    try: action_logger.log("REMOVE_CUSTOM_MODEL", source="api", details={"model_id": model_id})
    except Exception: pass
    if keys_manager.remove_custom_model(model_id):
        try: action_logger.log("REMOVE_CUSTOM_MODEL", source="api", level="success", details={"model_id": model_id})
        except Exception: pass
        return {"success": True, "model_id": model_id}
    try: action_logger.log("REMOVE_CUSTOM_MODEL", source="api", level="error", error=f"Custom model {model_id} not found")
    except Exception: pass
    raise HTTPException(404, f"Кастомная модель {model_id} не найдена")


# ═══════════════════════════════════════════════════════════════
# PROJECTS
# ═══════════════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects():
    try: action_logger.api_call("GET", "/api/v1/projects")
    except Exception: pass
    return await get_all_projects()

@router.post("/projects")
async def create_project_endpoint(req: CreateProjectRequest):
    from core.memory import CONFIG
    try: action_logger.log("CREATE_PROJECT", source="api", details={"name": req.name, "template": req.template})
    except Exception: pass
    projects_dir = CONFIG["system"]["projects_dir"]
    project_path = f"{projects_dir}/{req.name.replace(' ', '_').lower()}"
    result = await create_project(req.name, project_path, description=req.description, base_prompt=req.base_prompt, ideas=req.ideas, github_repo=req.github_repo, github_token=req.github_token, local_path=req.local_path, template=req.template)
    if not result:
        try: action_logger.log("CREATE_PROJECT", source="api", level="error", error="Project already exists", details={"name": req.name})
        except Exception: pass
        raise HTTPException(400, "Проект с таким именем уже существует")
    try: action_logger.log("CREATE_PROJECT", source="api", level="success", details={"name": req.name, "project_id": result.get("id")})
    except Exception: pass
    return result

@router.delete("/projects/{project_id}")
async def delete_project_endpoint(project_id: int):
    try: action_logger.log("DELETE_PROJECT", source="api", project_id=project_id)
    except Exception: pass
    if await delete_project(project_id):
        try: action_logger.log("DELETE_PROJECT", source="api", level="success", project_id=project_id)
        except Exception: pass
        return {"success": True}
    try: action_logger.log("DELETE_PROJECT", source="api", level="error", error="Project not found", project_id=project_id)
    except Exception: pass
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/progress")
async def update_progress(req: UpdateProgressRequest):
    try: action_logger.log("UPDATE_PROGRESS", source="api", project_id=req.project_id, details={"progress": req.progress})
    except Exception: pass
    if await update_project_progress(req.project_id, req.progress):
        return {"success": True, "progress": req.progress}
    try: action_logger.log("UPDATE_PROGRESS", source="api", level="error", error="Project not found", project_id=req.project_id)
    except Exception: pass
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/models")
async def update_models(req: UpdateModelsRequest):
    try: action_logger.log("UPDATE_MODELS", source="api", project_id=req.project_id, details={"model_ids": req.model_ids})
    except Exception: pass
    if await update_project_models(req.project_id, req.model_ids):
        return {"success": True, "model_ids": req.model_ids}
    try: action_logger.log("UPDATE_MODELS", source="api", level="error", error="Project not found", project_id=req.project_id)
    except Exception: pass
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/settings")
async def update_project_settings(req: UpdateProjectSettingsRequest):
    """Update project description, prompt, ideas."""
    from core.memory import async_session, Project, select
    try: action_logger.log("UPDATE_PROJECT_SETTINGS", source="api", project_id=req.project_id)
    except Exception: pass
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
            return {"success": True}


class RenameProjectRequest(BaseModel):
    project_id: int
    new_name: str


@router.put("/projects/rename")
async def rename_project(req: RenameProjectRequest):
    """Переименовать проект."""
    from core.memory import async_session, Project, select
    try: action_logger.log("RENAME_PROJECT", source="api", project_id=req.project_id, details={"new_name": req.new_name})
    except Exception: pass
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
    try: action_logger.log("REGENERATE_PROJECT_KEY", source="api", project_id=req.project_id)
    except Exception: pass
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
    try: action_logger.api_call("GET", f"/api/v1/projects/by-key/{key[:8]}...")
    except Exception: pass
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
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/tree", project_id=project_id)
    except Exception: pass
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
    """Прочитать содержимое файла проекта."""
    try: action_logger.log("READ_FILE", source="api", project_id=project_id, details={"path": path})
    except Exception: pass
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    full_path = os.path.join(project["path"], path)
    # Security: prevent path traversal
    real_path = os.path.realpath(full_path)
    real_project = os.path.realpath(project["path"])
    if not real_path.startswith(real_project):
        raise HTTPException(403, "Доступ запрещён")
    if not os.path.isfile(full_path):
        raise HTTPException(404, "Файл не найден")
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "size": os.path.getsize(full_path)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/projects/{project_id}/save-file")
async def save_file(project_id: int, path: str = Body(...), content: str = Body("")):
    """Сохранить/создать файл в проекте."""
    try: action_logger.log("SAVE_FILE", source="api", project_id=project_id, details={"path": path, "size": len(content)})
    except Exception: pass
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    full_path = os.path.join(project["path"], path)
    real_path = os.path.realpath(full_path)
    real_project = os.path.realpath(project["path"])
    if not real_path.startswith(real_project):
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
    try: action_logger.log("SEARCH_FILES", source="api", project_id=req.project_id, details={"query": req.query, "pattern": req.file_pattern})
    except Exception: pass
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
# GIT OPERATIONS
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/git")
async def git_operation(project_id: int, req: GitOperationRequest):
    """Git операции: commit, push, pull, log, status, diff."""
    try: action_logger.log(f"GIT_{req.operation.upper()}", source="api", project_id=project_id, details={"operation": req.operation})
    except Exception: pass
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
        try: action_logger.log(f"GIT_{req.operation.upper()}", source="api", level="error", error="Timeout", project_id=req.project_id)
        except Exception: pass
        raise HTTPException(408, "Операция превысила таймаут")
    except Exception as e:
        try: action_logger.log(f"GIT_{req.operation.upper()}", source="api", level="error", error=str(e), project_id=req.project_id)
        except Exception: pass
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════
# PACKAGE MANAGER
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/packages")
async def run_package_command(req: RunPackageRequest):
    """Управление пакетами: pip install, npm install, и т.д."""
    try: action_logger.log("PACKAGE_OPERATION", source="api", project_id=req.project_id, details={"command": req.command})
    except Exception: pass
    project = await get_project(req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    cwd = project["path"]
    os.makedirs(cwd, exist_ok=True)

    cmd = req.command.strip()
    if not cmd:
        raise HTTPException(400, "Команда не указана")

    # Security: only allow safe package commands
    allowed_prefixes = ["pip install", "pip uninstall", "pip list", "pip show",
                         "npm install", "npm uninstall", "npm list", "npm run",
                         "python -m pip", "python3 -m pip", "uv pip"]
    if not any(cmd.startswith(p) for p in allowed_prefixes):
        raise HTTPException(400, f"Команда не разрешена. Разрешены: pip, npm")

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=cwd, timeout=120
        )
        return {
            "command": cmd,
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "exit_code": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Установка превысила таймаут (120 сек)")
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
    try: action_logger.api_call("GET", "/api/v1/templates")
    except Exception: pass
    return {"templates": [
        {"id": tid, "name": t["name"], "description": t["description"]}
        for tid, t in TEMPLATES.items()
    ]}

@router.post("/projects/create-from-template")
async def create_from_template(req: CreateFromTemplateRequest):
    """Создать проект из шаблона."""
    try: action_logger.log("CREATE_FROM_TEMPLATE", source="api", details={"name": req.name, "template": req.template})
    except Exception: pass
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
    try: action_logger.log("ARCHIVE_PROJECT", source="api", project_id=req.project_id, details={"description": req.description})
    except Exception: pass
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

    # 4. Создать ZIP архив
    archives_dir = os.path.join(CONFIG["system"].get("archives_dir", "./archives"))
    os.makedirs(archives_dir, exist_ok=True)
    archive_name = f"{project['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    zip_path = os.path.join(archives_dir, f"{archive_name}.zip")

    shutil.make_archive(zip_path.replace(".zip", ""), "zip", project_path)

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
    try: action_logger.api_call("GET", "/api/v1/archives")
    except Exception: pass
    return await get_all_archives()

@router.get("/archives/{archive_id}")
async def get_archive_detail(archive_id: int):
    """Детали архива (с мастер-промптом)."""
    try: action_logger.log("GET_ARCHIVE_DETAIL", source="api", details={"archive_id": archive_id})
    except Exception: pass
    archive = await get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Архив не найден")
    return archive

@router.get("/archives/{archive_id}/download")
async def download_archive(archive_id: int):
    """Скачать ZIP архив."""
    try: action_logger.log("DOWNLOAD_ARCHIVE", source="api", details={"archive_id": archive_id})
    except Exception: pass
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
        f"# Дата архивации: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
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
    try: action_logger.api_call("GET", "/api/v1/ideas")
    except Exception: pass
    return await get_all_ideas()

@router.post("/ideas")
async def create_idea(req: AddIdeaRequest):
    """Анализировать репозиторий и сохранить идею."""
    from core.ideas_injector import IdeasInjector
    try: action_logger.log("CREATE_IDEA", source="api", details={"repo_url": req.repo_url})
    except Exception: pass
    injector = IdeasInjector()
    try:
        result = await injector.process_idea(req.repo_url)
        name = req.repo_url.split("/")[-1].replace(".git", "")
        return {"success": True, "analysis": result, "name": name}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.delete("/ideas/{idea_id}")
async def delete_idea_endpoint(idea_id: int):
    try: action_logger.log("DELETE_IDEA", source="api", details={"idea_id": idea_id})
    except Exception: pass
    if await delete_idea(idea_id):
        return {"success": True}
    raise HTTPException(404, "Идея не найдена")


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

@router.get("/stats")
async def get_stats():
    try: action_logger.api_call("GET", "/api/v1/stats")
    except Exception: pass
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
    try: action_logger.api_call("GET", "/api/v1/config")
    except Exception: pass
    return {
        "llm": {
            "default_model": CONFIG["llm"].get("default_model", "not set"),
            "router_model": CONFIG["llm"].get("router_model", "not set"),
            "api_base": CONFIG["llm"].get("api_base", "not set"),
            "api_key": CONFIG["llm"].get("api_key", "")[:12] + "..." if CONFIG["llm"].get("api_key") else "not set",
        }
    }


# ═══════════════════════════════════════════════════════════════
# THREADS
# ═══════════════════════════════════════════════════════════════

class CreateThreadRequest(BaseModel):
    project_id: int
    title: str = "Новый поток"
    parent_thread_id: int | None = None

class RenameThreadRequest(BaseModel):
    title: str

@router.post("/threads")
async def create_thread_endpoint(req: CreateThreadRequest):
    from core.memory import create_thread
    try: action_logger.log("CREATE_THREAD", source="api", project_id=req.project_id, details={"title": req.title})
    except Exception: pass
    result = await create_thread(req.project_id, req.title, req.parent_thread_id)
    return result

@router.get("/projects/{project_id}/threads")
async def list_threads(project_id: int):
    from core.memory import get_threads
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/threads", project_id=project_id)
    except Exception: pass
    return await get_threads(project_id)

@router.delete("/threads/{thread_id}")
async def delete_thread_endpoint(thread_id: int):
    from core.memory import delete_thread
    try: action_logger.log("DELETE_THREAD", source="api", details={"thread_id": thread_id})
    except Exception: pass
    if await delete_thread(thread_id):
        return {"success": True}
    raise HTTPException(404, "Поток не найден")

@router.get("/threads/{thread_id}/messages")
async def get_thread_messages_endpoint(thread_id: int):
    from core.memory import get_thread_messages
    try: action_logger.log("GET_THREAD_MESSAGES", source="api", details={"thread_id": thread_id})
    except Exception: pass
    messages = await get_thread_messages(thread_id)
    return {"messages": messages}

@router.put("/threads/{thread_id}/rename")
async def rename_thread_endpoint(thread_id: int, req: RenameThreadRequest):
    from core.memory import rename_thread
    try: action_logger.log("RENAME_THREAD", source="api", details={"thread_id": thread_id, "title": req.title})
    except Exception: pass
    if await rename_thread(thread_id, req.title):
        return {"success": True, "title": req.title}
    raise HTTPException(404, "Поток не найден")


# ═══════════════════════════════════════════════════════════════
# CONTEXT COMPRESSION
# ═══════════════════════════════════════════════════════════════

class MilestoneRequest(BaseModel):
    project_id: int
    title: str

@router.get("/projects/{project_id}/context")
async def get_context_info(project_id: int):
    from core.memory import get_history, get_message_count, get_context_snapshots
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/context", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("COMPRESS_CONTEXT", source="api", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("CREATE_MILESTONE", source="api", project_id=req.project_id, details={"title": req.title})
    except Exception: pass
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
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/context/snapshots", project_id=project_id)
    except Exception: pass
    return await get_context_snapshots(project_id)

@router.delete("/projects/{project_id}/context/snapshots/{snapshot_id}")
async def delete_snapshot_endpoint(project_id: int, snapshot_id: int):
    from core.memory import delete_context_snapshot
    try: action_logger.log("DELETE_SNAPSHOT", source="api", project_id=project_id, details={"snapshot_id": snapshot_id})
    except Exception: pass
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
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/apk/strategy", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("CHECK_APK_ENV", source="api", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("GENERATE_APK_ICON", source="api", project_id=project_id, details={"prompt": req.prompt[:100]})
    except Exception: pass
    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")

    # Generate icon to temp file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = f'z-ai-generate -p "{req.prompt}" -o "{tmp_path}" -s 1024x1024'
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
    try: action_logger.log("INIT_APK_PLATFORM", source="api", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("BUILD_APK", source="api", project_id=req.project_id, details={"app_name": req.app_name, "build_type": req.build_type})
    except Exception: pass
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
    try: action_logger.api_call("GET", f"/api/v1/projects/{project_id}/apk/config", project_id=project_id)
    except Exception: pass
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
    try: action_logger.log("SAVE_APK_CONFIG", source="api", project_id=project_id)
    except Exception: pass
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
    from datetime import datetime

    # 1. Собрать debug-лог
    debug_data = action_logger.stop_debug_session()
    server_entries = debug_data.get("entries", [])
    client_errors = debug_data.get("client_errors", [])

    # 2. Формируем отчёт в Markdown
    now = datetime.now()
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
    try: action_logger.api_call("GET", "/api/v1/settings")
    except Exception: pass
    from core.memory import CONFIG
    return {
        "default_model": CONFIG.get("llm", {}).get("default_model", ""),
        "temperature": CONFIG.get("llm", {}).get("temperature", 0.2),
        "max_tokens": CONFIG.get("llm", {}).get("max_tokens", 4096),
    }

@router.post("/settings")
async def update_settings(req: UpdateSettingsRequest):
    """Update server settings and persist to config.yaml."""
    try: action_logger.log("UPDATE_SETTINGS", source="api", details={"default_model": req.default_model})
    except Exception: pass
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
        try: action_logger.log("SETTINGS_SAVED", source="api", level="success")
        except Exception: pass
    except Exception as e:
        try: action_logger.log("SETTINGS_SAVE_ERROR", source="api", level="error", error=str(e))
        except Exception: pass
    
    return {"success": True, "default_model": req.default_model}
