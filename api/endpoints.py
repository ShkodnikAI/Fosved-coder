from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/v1")

# ═══════════════════════════════════════════════════════════════
# Pydantic Schemas
# ═══════════════════════════════════════════════════════════════

class TaskInput(BaseModel):
    prompt: str
    project_id: Optional[int] = None
    priority: Optional[int] = 5  # 1=low, 5=medium, 10=high

class TaskResult(BaseModel):
    status: str
    output: str = ""
    files_changed: list[str] = []
    build_result: str = ""
    error: str = ""

class ProjectCreate(BaseModel):
    name: str
    path: Optional[str] = None

class IdeaInput(BaseModel):
    repo_url: str

class ApprovalRequest(BaseModel):
    request_id: str
    approved: bool

class ProjectResponse(BaseModel):
    id: int
    name: str
    path: str
    created_at: str

class IdeaResponse(BaseModel):
    id: int
    repo_url: str
    name: str
    summary: str
    created_at: str

class StatsResponse(BaseModel):
    projects_count: int = 0
    ideas_count: int = 0
    messages_count: int = 0
    routing_decisions: int = 0

# ═══════════════════════════════════════════════════════════════
# Projects Endpoints
# ═══════════════════════════════════════════════════════════════

@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects():
    """Get all projects"""
    from core.memory import get_all_projects
    projects = await get_all_projects()
    return [ProjectResponse(**p) for p in projects]

@router.post("/projects", response_model=ProjectResponse)
async def create_project(data: ProjectCreate):
    """Create a new project"""
    from core.memory import create_project, CONFIG
    import os

    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")

    path = data.path or os.path.join(CONFIG["system"]["projects_dir"], name)

    project = await create_project(name, path)
    if project is None:
        raise HTTPException(status_code=409, detail="Project with this name or path already exists")

    return ProjectResponse(**project)

@router.delete("/projects/{project_id}")
async def remove_project(project_id: int):
    """Delete a project and all its data"""
    from core.memory import delete_project
    success = await delete_project(project_id)
    if not success:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted", "project_id": project_id}

# ═══════════════════════════════════════════════════════════════
# Ideas Endpoints
# ═══════════════════════════════════════════════════════════════

@router.get("/ideas", response_model=list[IdeaResponse])
async def list_ideas():
    """Get all ideas"""
    from core.memory import get_all_ideas
    ideas = await get_all_ideas()
    return [IdeaResponse(**i) for i in ideas]

@router.post("/ideas")
async def add_idea(data: IdeaInput):
    """Analyze a GitHub repository and save as idea"""
    from core.ideas_injector import IdeasInjector

    if not data.repo_url.strip():
        raise HTTPException(status_code=400, detail="Repository URL cannot be empty")

    injector = IdeasInjector()
    result = await injector.process_idea(data.repo_url.strip())

    return {"status": "success", "analysis": result, "repo_url": data.repo_url}

@router.delete("/ideas/{idea_id}")
async def remove_idea(idea_id: int):
    """Delete an idea"""
    from core.memory import delete_idea
    success = await delete_idea(idea_id)
    if not success:
        raise HTTPException(status_code=404, detail="Idea not found")
    return {"status": "deleted", "idea_id": idea_id}

# ═══════════════════════════════════════════════════════════════
# Task Endpoint (for external AI-Office)
# ═══════════════════════════════════════════════════════════════

@router.post("/task", response_model=TaskResult)
async def handle_office_task(data: TaskInput):
    """Accept a coding task from an external AI agent"""
    from core.router import HybridRouter
    from core.agent import stream_llm_response
    from core.memory import get_repo_map, save_message, get_history

    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    router = HybridRouter()

    # Get repo map if project specified
    repo_map = None
    if data.project_id:
        cached = await get_repo_map(data.project_id)
        if cached:
            repo_map = cached["content"]

    # Route the task
    route_result = await router.route_task(data.prompt, repo_map or "")

    # Execute subtasks and collect results
    full_output = ""
    files_changed = []

    for subtask in route_result.subtasks:
        full_output += f"\n[{subtask.reason}] -> {subtask.model}\n"

        # Build context messages
        history = []
        if data.project_id:
            history = await get_history(data.project_id, limit=10)

        # Simple non-streaming call for API mode
        import litellm
        from core.memory import CONFIG
        litellm.suppress_debug_info = True

        try:
            messages = [{"role": "system", "content": f"Ты Fosved Coder AI ассистент. Контекст проекта:\n{repo_map or 'Нет контекста'}"}]
            messages += history
            messages += [{"role": "user", "content": subtask.prompt}]

            response = await litellm.acompletion(
                model=subtask.model,
                messages=messages,
                api_base=CONFIG["llm"]["api_base"],
                api_key=CONFIG["llm"]["api_key"],
                temperature=CONFIG["llm"]["temperature"],
                max_tokens=CONFIG["llm"].get("max_tokens", 4096)
            )
            output = response.choices[0].message.content
            full_output += output + "\n"
        except Exception as e:
            full_output += f"[ERROR with {subtask.model}]: {e}\n"

    return TaskResult(
        status="success",
        output=full_output.strip(),
        files_changed=files_changed,
        build_result="completed"
    )

# ═══════════════════════════════════════════════════════════════
# Approval Endpoint (for cyborg mode)
# ═══════════════════════════════════════════════════════════════

@router.post("/approve/{request_id}")
async def approve_command(request_id: str, data: ApprovalRequest):
    """Approve or reject a pending critical command"""
    from core.executor import CommandExecutor

    executor = CommandExecutor()

    if data.approved:
        # Execute approved command
        return {"status": "approved", "request_id": request_id}
    else:
        return {"status": "rejected", "request_id": request_id}

# ═══════════════════════════════════════════════════════════════
# Stats Endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/stats", response_model=StatsResponse)
async def get_system_stats():
    """Get system statistics"""
    from core.memory import get_all_projects, get_all_ideas, get_message_count, get_routing_stats
    from sqlalchemy import func
    from core.memory import async_session, ChatHistory, RoutingStat

    projects = await get_all_projects()
    ideas = await get_all_ideas()
    messages = await get_message_count(None)

    async with async_session() as session:
        result = await session.execute(select(func.count(RoutingStat.id)))
        routing_count = result.scalar() or 0

    return StatsResponse(
        projects_count=len(projects),
        ideas_count=len(ideas),
        messages_count=messages,
        routing_decisions=routing_count
    )

# ═══════════════════════════════════════════════════════════════
# Config Endpoint (safe — no API key exposed)
# ═══════════════════════════════════════════════════════════════

@router.get("/config")
async def get_config():
    """Get current configuration (API key hidden)"""
    from core.memory import CONFIG
    safe_config = {
        "llm": {
            "default_model": CONFIG["llm"]["default_model"],
            "router_model": CONFIG["llm"]["router_model"],
            "api_base": CONFIG["llm"]["api_base"],
            "api_key": "***" if CONFIG["llm"].get("api_key") else "(not set)",
            "temperature": CONFIG["llm"].get("temperature", 0.2),
            "max_tokens": CONFIG["llm"].get("max_tokens", 4096),
        },
        "system": CONFIG.get("system", {}),
    }
    return safe_config
