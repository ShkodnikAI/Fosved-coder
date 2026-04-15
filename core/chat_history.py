"""Chat history — per-project persistence with JSON files."""
import json
from pathlib import Path
from datetime import datetime

HISTORY_DIR = Path(__file__).parent.parent / "data" / "chat_history"

def _ensure_dir():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

def _file_path(project_id):
    _ensure_dir()
    safe = project_id if project_id else "_general"
    return HISTORY_DIR / (safe + ".json")

def load_history(project_id):
    fp = _file_path(project_id)
    if fp.exists():
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_message(project_id, role, content):
    history = load_history(project_id)
    history.append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat(),
    })
    _save(project_id, history)

def _save(project_id, history):
    _ensure_dir()
    with open(_file_path(project_id), "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def update_last_ai(project_id, full_content):
    history = load_history(project_id)
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "ai":
            history[i]["content"] = full_content
            history[i]["timestamp"] = datetime.now().isoformat()
            break
    _save(project_id, history)

def clear_history(project_id):
    fp = _file_path(project_id)
    if fp.exists():
        fp.unlink()
    return []