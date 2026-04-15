"""REST API - projects, keys, models, files, history, code generation."""
import json, uuid, shutil, os, re
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Query
from core.keys_manager import keys_manager, PROVIDER_DEFS
from core import chat_history

router = APIRouter(prefix="/api", tags=["api"])

DATA_DIR = Path(__file__).parent.parent / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROJECTS_FILE = DATA_DIR / "projects.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def _load_projects():
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _save_projects(projs):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projs, f, indent=2, ensure_ascii=False)

FREE_MODELS = [
    {"model_id":"gpt-4o-mini","provider":"OpenAI","type":"free"},
    {"model_id":"claude-3-haiku-20240307","provider":"Anthropic","type":"free"},
    {"model_id":"gemini-1.5-flash","provider":"Google","type":"free"},
    {"model_id":"deepseek-chat","provider":"DeepSeek","type":"free"},
    {"model_id":"meta-llama/llama-3-8b-instruct:free","provider":"OpenRouter","type":"free"},
    {"model_id":"mistralai/mistral-7b-instruct:free","provider":"OpenRouter","type":"free"},
    {"model_id":"google/gemma-2-9b-it:free","provider":"OpenRouter","type":"free"},
    {"model_id":"qwen/qwen-2-7b-instruct:free","provider":"OpenRouter","type":"free"},
    {"model_id":"huggingfaceh4/zephyr-7b-beta:free","provider":"OpenRouter","type":"free"},
    {"model_id":"openchat/openchat-7b:free","provider":"OpenRouter","type":"free"},
]

@router.get("/projects")
async def list_projects():
    return _load_projects()

@router.post("/projects")
async def create_project(
    name: str = Form(""),
    description: str = Form(""),
    prompt: str = Form(""),
    instructions: str = Form(""),
    ideas_json: str = Form("[]"),
    selected_model: str = Form(""),
    prompt_file: UploadFile = File(None),
):
    proj_id = str(uuid.uuid4())[:8]
    prompt_text = prompt
    if prompt_file and prompt_file.filename:
        fpath = UPLOAD_DIR / prompt_file.filename
        with open(fpath, "wb") as buf:
            shutil.copyfileobj(prompt_file.file, buf)
        try:
            prompt_text += "\n\n" + fpath.read_text(encoding="utf-8")
        except Exception:
            prompt_text += "\n\n[Attached: " + prompt_file.filename + "]"
    try:
        ideas = json.loads(ideas_json)
    except Exception:
        ideas = []
    project = {
        "id": proj_id,
        "name": name or "Untitled",
        "description": description,
        "prompt": prompt_text,
        "instructions": instructions,
        "ideas": ideas,
        "selected_model": selected_model,
        "status": "idle",
        "progress": 0,
        "created_at": datetime.now().isoformat(),
    }
    projs = _load_projects()
    projs.append(project)
    _save_projects(projs)
    return {"status": "ok", "project": project}

@router.put("/projects/{pid}")
async def update_project(pid: str, request: Request):
    data = await request.json()
    projs = _load_projects()
    updated = False
    for p in projs:
        if p["id"] == pid:
            for k in ["name","description","prompt","instructions","ideas","selected_model","status","progress","folder","github_repo"]:
                if k in data:
                    p[k] = data[k]
            updated = True
            break
    _save_projects(projs)
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "ok"}

@router.delete("/projects/{pid}")
async def delete_project(pid: str):
    projs = _load_projects()
    projs = [p for p in projs if p["id"] != pid]
    _save_projects(projs)
    return {"status": "ok"}

@router.get("/projects/{pid}/read-file")
async def read_project_file(pid: str, path: str = Query("")):
    projs = _load_projects()
    proj = next((p for p in projs if p["id"] == pid), None)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    folder = proj.get("folder", "")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail="Project folder not set")
    full_path = os.path.normpath(os.path.join(folder, path))
    if not full_path.startswith(os.path.normpath(folder)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found: " + path)
    skip_ext = [".pyc",".exe",".dll",".so",".bin",".png",".jpg",".jpeg",".gif",".zip",".tar",".gz"]
    ext = os.path.splitext(full_path)[1].lower()
    if ext in skip_ext:
        raise HTTPException(status_code=400, detail="Binary file, cannot read")
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"path": path, "content": content, "size": len(content), "lines": content.count("\n") + 1}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/projects/{pid}/tree")
async def project_tree(pid: str):
    projs = _load_projects()
    proj = next((p for p in projs if p["id"] == pid), None)
    folder = proj.get("folder", "") if proj else ""
    if not folder or not os.path.isdir(folder):
        return {"tree": [], "folder": folder}
    skip_dirs = {".git","__pycache__","node_modules",".venv","venv",".idea",".vscode","dist","build",".next"}
    result = []
    def walk(dp, prefix=""):
        try:
            items = sorted(os.listdir(dp))
            dirs = [e for e in items if os.path.isdir(os.path.join(dp, e)) and e not in skip_dirs]
            files = [e for e in items if os.path.isfile(os.path.join(dp, e)) and not e.endswith((".pyc",))]
            for d in dirs:
                rel = prefix + d + "/"
                result.append({"type": "dir", "path": rel})
                walk(os.path.join(dp, d), rel)
            for f in files:
                rel = prefix + f
                fp = os.path.join(dp, f)
                result.append({"type": "file", "path": rel, "size": os.path.getsize(fp)})
        except PermissionError:
            pass
    walk(folder)
    return {"tree": result, "folder": folder}

@router.get("/projects/{pid}/files")
async def list_project_files(pid: str):
    projs = _load_projects()
    proj = next((p for p in projs if p["id"] == pid), None)
    folder = proj.get("folder", "") if proj else ""
    if not folder or not os.path.isdir(folder):
        return {"files": [], "folder": folder}
    files = []
    skip = [".git","__pycache__","node_modules",".venv","venv"]
    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in filenames:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, folder).replace("\\", "/")
            if fn.endswith(".pyc"):
                continue
            files.append({"path": rel, "size": os.path.getsize(fp)})
    files.sort(key=lambda x: x["path"])
    return {"files": files, "folder": folder}

@router.post("/projects/{pid}/save-file")
async def save_project_file(pid: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    filepath = data.get("path", "")
    content = data.get("content", "")
    if not filepath:
        raise HTTPException(status_code=400, detail="path required")
    projs = _load_projects()
    proj = next((p for p in projs if p["id"] == pid), None)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    folder = proj.get("folder", "")
    if not folder:
        raise HTTPException(status_code=400, detail="Project folder not set")
    full_path = os.path.join(folder, filepath)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": full_path}

@router.post("/projects/{pid}/generate")
async def trigger_generate(pid: str):
    return {"status": "ok", "project_id": pid, "action": "generate"}

@router.get("/chat/{project_id}")
async def get_chat_history(project_id: str):
    return chat_history.load_history(project_id)

@router.delete("/chat/{project_id}")
async def clear_chat_history(project_id: str):
    chat_history.clear_history(project_id)
    return {"status": "ok"}

@router.get("/keys")
async def list_keys():
    return keys_manager.list_keys()

@router.post("/keys")
async def add_key(
    provider_id: str = Form(""),
    api_key: str = Form(""),
    models_json: str = Form("[]"),
):
    if not provider_id or not api_key:
        raise HTTPException(status_code=400, detail="provider_id and api_key required")
    try:
        models = json.loads(models_json)
    except Exception:
        models = []
    return keys_manager.add_key(provider_id, api_key, models=models)

@router.delete("/keys/{pid}")
async def remove_key(pid: str):
    return keys_manager.remove_key(pid)

@router.get("/models")
async def list_models():
    user_models = keys_manager.get_all_models()
    saved_keys = keys_manager.list_keys()
    has_openrouter = any(k["provider_id"] == "openrouter" and k.get("is_active", True) for k in saved_keys)
    result = []
    existing_ids = set()
    for um in user_models:
        mid = um["model_name"]
        result.append({"model_id": mid, "provider": um["provider_name"], "type": "user", "available": um["is_active"], "has_key": True})
        existing_ids.add(mid)
    free = list(FREE_MODELS)
    if has_openrouter:
        or_free = [m for m in free if m["provider"] == "OpenRouter"]
        other_free = [m for m in free if m["provider"] != "OpenRouter"][:4]
        free = or_free + other_free
    else:
        free = free[:4]
    for fm in free:
        if fm["model_id"] not in existing_ids:
            result.append({"model_id": fm["model_id"], "provider": fm["provider"], "type": "free", "available": True, "has_key": False})
    return result

@router.get("/providers")
async def list_providers():
    return [{"id": k, "name": v["name"], "base_url": v["base_url"]} for k, v in PROVIDER_DEFS.items()]

@router.get("/github/token")
async def get_github_token():
    return {"token": keys_manager.get_github_token()}

@router.post("/github/token")
async def set_github_token(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    keys_manager.set_github_token(data.get("token", ""))
    return {"status": "ok"}

@router.get("/github/user")
async def get_github_user():
    token = keys_manager.get_github_token()
    if not token:
        return {"user": "", "repos": []}
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request("https://api.github.com/user", headers={"Authorization": "token " + token, "User-Agent": "FosvedCoder"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
            username = data.get("login", "")
        req2 = urllib.request.Request("https://api.github.com/user/repos?sort=updated&per_page=10", headers={"Authorization": "token " + token, "User-Agent": "FosvedCoder"})
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            repos = [{"name": r.get("full_name",""), "url": r.get("html_url","")} for r in _json.loads(resp2.read().decode())]
        return {"user": username, "repos": repos}
    except Exception as e:
        return {"user": "invalid token", "repos": [], "error": str(e)}

@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    fpath = UPLOAD_DIR / file.filename
    with open(fpath, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    return {"status": "ok", "filename": file.filename, "size": fpath.stat().st_size}