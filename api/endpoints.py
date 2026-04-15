"""REST API — projects (with ideas, instructions, model select), keys, models."""
import json, uuid, shutil
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from core.keys_manager import keys_manager, PROVIDER_DEFS
import urllib.request, urllib.error, json as _json

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
            for k in ["name","description","prompt","instructions","ideas","selected_model","status","progress"]:
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
        result.append({
            "model_id": mid,
            "provider": um["provider_name"],
            "type": "user",
            "available": um["is_active"],
            "has_key": True,
        })
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
            result.append({
                "model_id": fm["model_id"],
                "provider": fm["provider"],
                "type": "free",
                "available": True,
                "has_key": False,
            })
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

@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    fpath = UPLOAD_DIR / file.filename
    with open(fpath, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    return {"status": "ok", "filename": file.filename, "size": fpath.stat().st_size}