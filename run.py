import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from core.memory import init_db, save_message, get_history
from core.agent import stream_llm_response

app = FastAPI(title="Fosved Coder")

@app.on_event("startup")
async def startup_event():
    await init_db()

@app.get("/")
async def get_index():
    return FileResponse("ui/templates/index.html")

app.mount("/static", StaticFiles(directory="ui/static"), name="static")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Пока работаем без выбора проекта (project_id = None)
    current_project_id = None
    
    try:
        while True:
            data = await websocket.receive_text()
            
            # Сохраняем запрос пользователя в БД
            await save_message(current_project_id, "user", data)
            
            # Достаем историю из БД (передаем контекст ИИ)
            history = await get_history(current_project_id)
            
            # Запускаем ИИ
            await stream_llm_response(data, history, websocket)
            
    except WebSocketDisconnect:
        pass