import os, json, asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="Fosved Coder")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

            # Build messages array
            messages = []
            if project_id:
                projs = _load_projects()
                proj = next((p for p in projs if p["id"] == project_id), None)
                if proj:
                    parts = []
                    if proj.get("instructions"):
                        parts.append("INSTRUCTIONS:\n" + proj["instructions"])
                    if proj.get("prompt"):
                        parts.append("PROJECT CONTEXT:\n" + proj["prompt"])
                    if proj.get("ideas"):
                        parts.append("PROJECT IDEAS:\n" + ", ".join(proj["ideas"]))
                    if parts:
                        messages.append({"role": "system", "content": "\n\n".join(parts)})

            messages.append({"role": "user", "content": message})

            from core.chat import stream_chat
from core import chat_history

            async def on_token(content):
                if not stop_event.is_set():
                    await ws.send_text(json.dumps({"type": "token", "content": content}))

            async def on_done():
                await ws.send_text(json.dumps({"type": "done"}))

            async def on_error(err_msg):
                await ws.send_text(json.dumps({"type": "error", "message": err_msg}))

            await stream_chat(model_id, messages, on_token, on_done, on_error, stop_event)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass

if __name__ == "__main__":
    uvicorn.run("run:app", host="0.0.0.0", port=8000, reload=False)