import json, os, asyncio, re, shutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse
from core.keys_manager import KeysManager
from core.chat_history import ChatHistory
from core.chat import stream_chat, get_available_models

router = APIRouter()
keys_mgr = KeysManager()
chat_hist = ChatHistory()
DATA_DIR = "data"
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")

def _load_projects():
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_projects(projects):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2, ensure_ascii=False)

# ===== KEYS =====
@router.get("/api/keys")
def list_keys():
    return keys_mgr.list_keys()

@router.post("/api/keys")
def add_key(data: dict):
    provider = data.get("provider", "").strip().lower()
    key_value = data.get("key", "").strip()
    name = data.get("name", "").strip()
    models = data.get("models", [])
    if not provider or not key_value:
        return JSONResponse({"error": "provider and key required"}, 400)
    keys_mgr.add_key(provider, key_value, name, models)
    return {"status": "ok"}

@router.delete("/api/keys/{provider}")
def remove_key(provider: str):
    return keys_mgr.remove_key(provider)

# ===== PROVIDERS =====
@router.get("/api/providers")
def list_providers():
    return [
        {"id": "claude", "name": "Claude (Anthropic)"},
        {"id": "openai", "name": "OpenAI"},
        {"id": "openrouter", "name": "OpenRouter"},
        {"id": "grok", "name": "Grok (xAI)"},
        {"id": "google", "name": "Google (Gemini)"},
        {"id": "deepseek", "name": "DeepSeek"},
        {"id": "minimax", "name": "MiniMax"},
        {"id": "custom", "name": "Custom"},
    ]

# ===== MODELS =====
@router.get("/api/models")
def list_models():
    return get_available_models()

# ===== GITHUB =====
@router.get("/api/github/user")
def github_user():
    token = keys_mgr.get_github_token()
    username = keys_mgr.get_github_username()
    return {"token": token, "username": username}

@router.post("/api/github/token")
def set_github_token(data: dict):
    token = data.get("token", "").strip()
    username = data.get("username", "").strip()
    keys_mgr.set_github_token(token, username)
    return {"status": "ok"}

# ===== PROJECTS =====
@router.get("/api/projects")
def list_projects():
    return list(_load_projects().values())

@router.get("/api/projects/{pid}")
def get_project(pid: str):
    projects = _load_projects()
    if pid in projects:
        return projects[pid]
    return JSONResponse({"error": "not found"}, 404)

@router.post("/api/projects")
def create_project(data: dict):
    projects = _load_projects()
    import uuid
    pid = str(uuid.uuid4())[:8]
    name = data.get("name", "New Project").strip()
    folder = data.get("folder", "").strip()
    projects[pid] = {
        "id": pid,
        "name": name,
        "folder": folder,
        "description": data.get("description", ""),
        "prompt": data.get("prompt", ""),
        "instructions": data.get("instructions", ""),
        "ideas": data.get("ideas", ""),
        "github_repo": data.get("github_repo", ""),
        "selected_model": data.get("selected_model", ""),
        "progress": data.get("progress", 0),
        "created": __import__("datetime").datetime.now().isoformat()
    }
    _save_projects(projects)
    # Create project folder if specified
    if folder:
        os.makedirs(folder, exist_ok=True)
    return projects[pid]

@router.put("/api/projects/{pid}")
def update_project(pid: str, data: dict):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    projects[pid].update(data)
    _save_projects(projects)
    return projects[pid]

@router.delete("/api/projects/{pid}")
def delete_project(pid: str):
    projects = _load_projects()
    if pid in projects:
        del projects[pid]
        _save_projects(projects)
        # Remove chat history
        hist_file = f"data/chat_history/{pid}.json"
        if os.path.exists(hist_file):
            os.remove(hist_file)
        return {"status": "ok"}
    return JSONResponse({"error": "not found"}, 404)

# ===== PROJECT FILES =====
@router.get("/api/projects/{pid}/tree")
def project_tree(pid: str):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder or not os.path.isdir(folder):
        return []
    tree = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__', 'node_modules', 'venv', '.git'))]
        level = root.replace(folder, "").count(os.sep)
        rel_path = os.path.relpath(root, folder)
        indent = "  " * level
        if level > 0:
            tree.append({"type": "dir", "name": os.path.basename(root), "path": rel_path, "level": level, "indent": indent})
        for f in sorted(files):
            fp = os.path.join(rel_path, f) if rel_path != "." else f
            tree.append({"type": "file", "name": f, "path": fp, "level": level, "indent": indent + "  "})
    return tree

@router.get("/api/projects/{pid}/read-file")
def read_project_file(pid: str, path: str = ""):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    full_path = os.path.normpath(os.path.join(folder, path))
    if not full_path.startswith(os.path.normpath(folder)):
        return JSONResponse({"error": "path traversal blocked"}, 403)
    if not os.path.isfile(full_path):
        return JSONResponse({"error": "file not found"}, 404)
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "lines": content.splitlines()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@router.post("/api/projects/{pid}/save-file")
def save_project_file(pid: str, data: dict):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    file_path = data.get("path", "")
    content = data.get("content", "")
    full_path = os.path.normpath(os.path.join(folder, file_path))
    if not full_path.startswith(os.path.normpath(folder)):
        return JSONResponse({"error": "path traversal blocked"}, 403)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok"}

# ===== THREADS =====
@router.get("/api/projects/{pid}/threads")
def list_threads(pid: str):
    return chat_hist.list_threads(pid)

@router.post("/api/projects/{pid}/threads")
def create_thread(pid: str, data: dict = None):
    if data is None:
        data = {}
    name = data.get("name", "")
    return chat_hist.create_thread(pid, name=name)

@router.put("/api/projects/{pid}/threads/{tid}")
def rename_thread(pid: str, tid: str, data: dict):
    new_name = data.get("name", "")
    return chat_hist.rename_thread(pid, tid, new_name)

@router.delete("/api/projects/{pid}/threads/{tid}")
def delete_thread(pid: str, tid: str):
    return chat_hist.delete_thread(pid, tid)

# ===== CHAT HISTORY =====
@router.get("/api/chat/{pid}")
def get_chat_history(pid: str, thread_id: str = "main"):
    return chat_hist.load_history(pid, thread_id)

@router.delete("/api/chat/{pid}")
def clear_chat_history(pid: str, thread_id: str = "main"):
    return chat_hist.clear_history(pid, thread_id)
