import os, json, asyncio, re
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="Fosved Coder")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

BASE = Path(__file__).resolve().parent
from api.endpoints import router as api_router
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(BASE / "ui" / "static")), name="static")

@app.get("/")
async def index():
    return FileResponse(str(BASE / "ui" / "templates" / "index.html"))

def _load_projects():
    pf = BASE / "data" / "projects.json"
    if pf.exists():
        try:
            with open(pf, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    stop_event = asyncio.Event()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type", "")
            if mtype == "stop":
                stop_event.set()
                continue
            if mtype != "chat":
                continue
            stop_event.clear()
            message = msg.get("message", "")
            project_id = msg.get("project_id", "")
            model_id = msg.get("model", "")
            messages = []
            if project_id:
                projs = _load_projects()
                proj = next((p for p in projs if p["id"] == project_id), None)
                if proj:
                    parts = []
                    if proj.get("instructions"):
                        parts.append("INSTRUCTIONS:\n" + proj["instructions"])
                    if proj.get("prompt"):
                        parts.append("PROJECT DESCRIPTION:\n" + proj["prompt"])
                    if proj.get("ideas"):
                        parts.append("PROJECT IDEAS:\n" + ", ".join(proj["ideas"]))
                    if proj.get("github_repo"):
                        parts.append("GITHUB REPO: " + proj["github_repo"])
                    folder = proj.get("folder", "")
                    if folder and os.path.isdir(folder):
                        skip = {".git","__pycache__","node_modules",".venv","venv",".idea",".vscode","dist","build",".next"}
                        flist = []
                        def _walk(dp, prefix=""):
                            try:
                                items = sorted(os.listdir(dp))
                                for d in [e for e in items if os.path.isdir(os.path.join(dp,e)) and e not in skip]:
                                    flist.append(prefix + d + "/")
                                    _walk(os.path.join(dp,d), prefix + d + "/")
                                for f in [e for e in items if os.path.isfile(os.path.join(dp,e)) and not e.endswith((".pyc",))]:
                                    flist.append(prefix + f)
                            except PermissionError:
                                pass
                        _walk(folder)
                        if flist:
                            parts.append("PROJECT FILES:\n" + "\n".join(flist[:200]))
                            parts.append("\nWhen user asks to read a file, use format: [READ:filepath]\nExample: [READ:src/main.py]")
                    if parts:
                        messages.append({"role": "system", "content": "\n\n".join(parts)})
            enriched_message = message
            folder_path = ""
            if project_id:
                projs = _load_projects()
                proj = next((p for p in projs if p["id"] == project_id), None)
                if proj:
                    folder_path = proj.get("folder", "")
            if folder_path:
                read_pat = re.compile(r'\[READ:(.+?)\]')
                def _replace_read(m):
                    fp = m.group(1).strip()
                    full = os.path.normpath(os.path.join(folder_path, fp))
                    if not full.startswith(os.path.normpath(folder_path)):
                        return "[ACCESS DENIED: " + fp + "]"
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            return "\n--- FILE: " + fp + " ---\n" + f.read() + "\n--- END FILE ---\n"
                    except Exception as e:
                        return "[ERROR reading " + fp + ": " + str(e) + "]"
                enriched_message = read_pat.sub(_replace_read, message)
                if "[READ:" not in message:
                    refs = re.findall(r'(?:read|open|show|view|display|cat)\s+(?:the\s+)?(?:file\s+)?[`\'"]?([\w\./\-]+)[`\'"]?', message, re.IGNORECASE)
                    extra = ""
                    for ref in refs[:5]:
                        full = os.path.normpath(os.path.join(folder_path, ref))
                        if not full.startswith(os.path.normpath(folder_path)):
                            continue
                        try:
                            with open(full, "r", encoding="utf-8") as f:
                                extra += "\n--- FILE: " + ref + " ---\n" + f.read() + "\n--- END FILE ---\n"
                        except Exception:
                            pass
                    if extra:
                        enriched_message = message + "\n\n[Auto-read files:]\n" + extra
            messages.append({"role": "user", "content": enriched_message})
            from core.chat import stream_chat
            from core import chat_history
            chat_history.save_message(project_id, "user", message)
            chat_history.save_message(project_id, "ai", "")
            accumulated = []
            async def on_token(content):
                if not stop_event.is_set():
                    accumulated.append(content)
                    await ws.send_text(json.dumps({"type": "token", "content": content}))
            async def on_done():
                full = "".join(accumulated)
                chat_history.update_last_ai(project_id, full)
                await ws.send_text(json.dumps({"type": "done"}))
            async def on_error(err_msg):
                chat_history.save_message(project_id, "system", err_msg)
                await ws.send_text(json.dumps({"type": "error", "message": err_msg}))
            await stream_chat(model_id, messages, on_token, on_done, on_error, stop_event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

if __name__ == "__main__":
    uvicorn.run("run:app", host="0.0.0.0", port=8000, reload=False)