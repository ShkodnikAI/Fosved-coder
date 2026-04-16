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
    description: str = ""
    base_prompt: str = ""
    ideas: str = ""
    github_repo: str = ""
    github_token: str = ""
    local_path: str = ""

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
    """Список всех доступных моделей (платные + локальные + бесплатные + кастомные)."""
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
# LOCAL MODELS
# ═══════════════════════════════════════════════════════════════

@router.get("/models/local")
async def list_local_models():
    """Список сохранённых локальных моделей."""
    return {
        "models": keys_manager.local_models,
        "providers": LOCAL_PROVIDERS,
    }

@router.post("/models/local/discover")
async def discover_local_models(req: DiscoverLocalModelsRequest):
    """Автообнаружение моделей на локальном сервере (Ollama, LM Studio и т.д.)."""
    result = await keys_manager.discover_local_models(
        provider_key=req.provider_key,
        base_url=req.base_url if req.base_url else None,
    )
    return result

@router.post("/models/local")
async def add_local_model(req: AddLocalModelRequest):
    """Ручное добавление локальной модели."""
    result = await keys_manager.add_local_model(
        provider_key=req.provider_key,
        model_name=req.model_name,
        base_url=req.base_url,
        display_name=req.display_name,
    )
    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result

@router.delete("/models/local/{model_id}")
async def remove_local_model(model_id: str):
    """Удаление локальной модели."""
    if keys_manager.remove_local_model(model_id):
        return {"success": True, "model_id": model_id}
    raise HTTPException(404, f"Локальная модель {model_id} не найдена")


# ═══════════════════════════════════════════════════════════════
# CUSTOM MODELS (force connect)
# ═══════════════════════════════════════════════════════════════

@router.get("/models/custom")
async def list_custom_models():
    """Список кастомных (принудительно подключённых) моделей."""
    return {"models": keys_manager.custom_models}

@router.post("/models/custom")
async def add_custom_model(req: AddCustomModelRequest):
    """Принудительное добавление модели по URL (force connect)."""
    result = await keys_manager.add_custom_model(
        name=req.name,
        api_base=req.api_base,
        api_key=req.api_key,
        model_id=req.model_id,
        litellm_prefix=req.litellm_prefix,
    )
    if not result["success"]:
        raise HTTPException(400, result["error"])
    return result

@router.delete("/models/custom/{model_id}")
async def remove_custom_model(model_id: str):
    """Удаление кастомной модели."""
    if keys_manager.remove_custom_model(model_id):
        return {"success": True, "model_id": model_id}
    raise HTTPException(404, f"Кастомная модель {model_id} не найдена")


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
    result = await create_project(req.name, project_path, description=req.description, base_prompt=req.base_prompt, ideas=req.ideas, github_repo=req.github_repo, github_token=req.github_token, local_path=req.local_path)
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

@router.put("/projects/settings")
async def update_project_settings(req: UpdateProjectSettingsRequest):
    """Update project description, prompt, ideas."""
    from core.memory import async_session, Project, select
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


# ═══════════════════════════════════════════════════════════════
# FILE OPERATIONS
# ═══════════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/tree")
async def get_project_tree(project_id: int):
    """Получить дерево файлов проекта (вложенная структура)."""
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
async def git_operation(req: GitOperationRequest):
    """Git операции: commit, push, pull, log, status, diff."""
    project = await get_project(req.project_id)
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
        raise HTTPException(408, "Операция превысила таймаут")
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════
# PACKAGE MANAGER
# ═══════════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/packages")
async def run_package_command(req: RunPackageRequest):
    """Управление пакетами: pip install, npm install, и т.д."""
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
    return {"templates": [
        {"id": tid, "name": t["name"], "description": t["description"]}
        for tid, t in TEMPLATES.items()
    ]}

@router.post("/projects/create-from-template")
async def create_from_template(req: CreateFromTemplateRequest):
    """Создать проект из шаблона."""
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
    return await get_all_archives()

@router.get("/archives/{archive_id}")
async def get_archive_detail(archive_id: int):
    """Детали архива (с мастер-промптом)."""
    archive = await get_archive(archive_id)
    if not archive:
        raise HTTPException(404, "Архив не найден")
    return archive

@router.get("/archives/{archive_id}/download")
async def download_archive(archive_id: int):
    """Скачать ZIP архив."""
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
    return await get_all_ideas()

@router.post("/ideas")
async def create_idea(req: AddIdeaRequest):
    """Анализировать репозиторий и сохранить идею."""
    from core.ideas_injector import IdeasInjector
    injector = IdeasInjector()
    try:
        result = await injector.process_idea(req.repo_url)
        name = req.repo_url.split("/")[-1].replace(".git", "")
        return {"success": True, "analysis": result, "name": name}
    except Exception as e:
        raise HTTPException(500, str(e))

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
    result = await create_thread(req.project_id, req.title, req.parent_thread_id)
    return result

@router.get("/projects/{project_id}/threads")
async def list_threads(project_id: int):
    from core.memory import get_threads
    return await get_threads(project_id)

@router.delete("/threads/{thread_id}")
async def delete_thread_endpoint(thread_id: int):
    from core.memory import delete_thread
    if await delete_thread(thread_id):
        return {"success": True}
    raise HTTPException(404, "Поток не найден")

@router.get("/threads/{thread_id}/messages")
async def get_thread_messages_endpoint(thread_id: int):
    from core.memory import get_thread_messages
    messages = await get_thread_messages(thread_id)
    return {"messages": messages}

@router.put("/threads/{thread_id}/rename")
async def rename_thread_endpoint(thread_id: int, req: RenameThreadRequest):
    from core.memory import rename_thread
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
    from core.context_compressor import ContextCompressor
    compressor = ContextCompressor()
    stats = await compressor.get_stats(project_id)
    snapshots = await compressor.get_snapshots(project_id)
    return {"stats": stats, "snapshots": snapshots}

@router.post("/projects/{project_id}/context/compress")
async def compress_context(project_id: int):
    from core.context_compressor import ContextCompressor
    compressor = ContextCompressor()
    result = await compressor.compress(project_id)
    return result

@router.post("/projects/{project_id}/context/milestone")
async def create_milestone(req: MilestoneRequest):
    from core.context_compressor import ContextCompressor
    compressor = ContextCompressor()
    result = await compressor.create_milestone(req.project_id, req.title)
    return result

@router.get("/projects/{project_id}/context/snapshots")
async def list_snapshots(project_id: int):
    from core.memory import get_context_snapshots
    return await get_context_snapshots(project_id)

@router.delete("/projects/{project_id}/context/snapshots/{snapshot_id}")
async def delete_snapshot_endpoint(project_id: int, snapshot_id: int):
    from core.memory import delete_context_snapshot
    if await delete_context_snapshot(snapshot_id):
        return {"success": True}
    raise HTTPException(404, "Снепшот не найден")
