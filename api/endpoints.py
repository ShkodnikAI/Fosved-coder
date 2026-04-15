"""
Fosved Coder v2.0 — REST API Endpoints
Включает управление ключами, моделями, проектами, идеями, чатом.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import json

from core.memory import (
    CONFIG, create_project, get_all_projects, get_project,
    delete_project, update_project_progress, update_project_models,
    get_all_ideas, delete_idea, get_message_count,
    save_routing_stat, get_routing_stats,
)
from core.keys_manager import keys_manager, PROVIDER_DEFS

router = APIRouter(prefix="/api/v1")


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

class CreateProjectRequest(BaseModel):
    name: str

class UpdateProgressRequest(BaseModel):
    project_id: int
    progress: int

class UpdateModelsRequest(BaseModel):
    project_id: int
    model_ids: list[str]

class AddIdeaRequest(BaseModel):
    repo_url: str


# ═══════════════════════════════════════════════════════════════
# KEYS & MODELS
# ═══════════════════════════════════════════════════════════════

@router.post("/keys/add")
async def add_key(req: AddKeyRequest):
    """Валидация и добавление API-ключа провайдера."""
    result = await keys_manager.add_key(
        provider_id=req.provider,
        api_key=req.api_key,
        models=req.models if req.models else None,
        api_base=req.api_base if req.api_base else None,
    )
    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result

@router.delete("/keys/{provider_id}")
async def remove_key(provider_id: str):
    """Удаление API-ключа провайдера."""
    if keys_manager.remove_key(provider_id):
        return {"success": True, "provider": provider_id}
    raise HTTPException(404, f"Провайдер {provider_id} не найден")

@router.get("/keys/providers")
async def get_providers():
    """Список всех провайдеров с их статусом."""
    return {
        "providers": PROVIDER_DEFS,
        "configured": keys_manager.get_provider_status(),
    }

@router.get("/keys/github")
async def get_github_status():
    """Статус GitHub интеграции."""
    return keys_manager.get_github_status()

@router.post("/keys/github")
async def set_github_token(req: GitHubTokenRequest):
    """Установка и валидация GitHub токена."""
    validation = await keys_manager.validate_github_token(req.token)
    if validation["status"] != "valid":
        raise HTTPException(400, validation["error"])
    keys_manager.set_github_token(req.token, req.enabled)
    return {"success": True, "user": validation["user"]}

@router.put("/keys/github/toggle")
async def toggle_github(req: ToggleGitHubRequest):
    """Включение/отключение GitHub интеграции."""
    result = keys_manager.toggle_github(req.enabled)
    return result

@router.get("/models")
async def get_all_models():
    """Список всех доступных моделей (платные + бесплатные)."""
    return {"models": keys_manager.get_all_models()}

@router.post("/models/validate/{provider_id}")
async def revalidate_provider(provider_id: str):
    """Повторная валидация ключа провайдера."""
    config = keys_manager.providers.get(provider_id)
    if not config:
        raise HTTPException(404, f"Провайдер {provider_id} не настроен")
    result = await keys_manager.validate_key(
        provider_id, config["api_key"], config["models"][0] if config.get("models") else None
    )
    keys_manager.providers[provider_id]["status"] = result["status"]
    keys_manager._save_keys()
    return result


# ═══════════════════════════════════════════════════════════════
# PROJECTS
# ═══════════════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects():
    return await get_all_projects()

@router.post("/projects")
async def create_project_endpoint(req: CreateProjectRequest):
    from core.memory import CONFIG
    projects_dir = CONFIG["system"]["projects_dir"]
    project_path = f"{projects_dir}/{req.name.replace(' ', '_').lower()}"
    result = await create_project(req.name, project_path)
    if not result:
        raise HTTPException(400, "Проект с таким именем уже существует")
    return result

@router.delete("/projects/{project_id}")
async def delete_project_endpoint(project_id: int):
    if await delete_project(project_id):
        return {"success": True}
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/progress")
async def update_progress(req: UpdateProgressRequest):
    if await update_project_progress(req.project_id, req.progress):
        return {"success": True, "progress": req.progress}
    raise HTTPException(404, "Проект не найден")

@router.put("/projects/models")
async def update_models(req: UpdateModelsRequest):
    if await update_project_models(req.project_id, req.model_ids):
        return {"success": True, "model_ids": req.model_ids}
    raise HTTPException(404, "Проект не найден")


# ═══════════════════════════════════════════════════════════════
# IDEAS
# ═══════════════════════════════════════════════════════════════

@router.get("/ideas")
async def list_ideas():
    return await get_all_ideas()

@router.delete("/ideas/{idea_id}")
async def delete_idea_endpoint(idea_id: int):
    if await delete_idea(idea_id):
        return {"success": True}
    raise HTTPException(404, "Идея не найдена")


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════

@router.get("/stats")
async def get_stats():
    projects = await get_all_projects()
    ideas = await get_all_ideas()
    return {
        "projects_count": len(projects),
        "ideas_count": len(ideas),
        "messages_count": await get_message_count(None),
        "routing_decisions": len(await get_routing_stats()),
    }

@router.get("/config")
async def get_config():
    return {
        "llm": {
            "default_model": CONFIG["llm"].get("default_model", "not set"),
            "router_model": CONFIG["llm"].get("router_model", "not set"),
            "api_base": CONFIG["llm"].get("api_base", "not set"),
            "api_key": CONFIG["llm"].get("api_key", "")[:12] + "..." if CONFIG["llm"].get("api_key") else "not set",
        }
    }
