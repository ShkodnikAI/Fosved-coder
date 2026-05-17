# Worklog

---
Task ID: 1
Agent: main
Task: Clean chat — remove all non-user/AI messages from chat areas

Work Log:
- Cloned repo from https://github.com/ShkodnikAI/Fosved-coder.git
- Audited all WebSocket message types in index.html (20+ types)
- Audited all `addMessage`/`addMessageTo` calls in frontend code
- Audited all `safe_ws_send`/`_send_log` calls in backend (agent.py, run.py, auto_agent.py)
- Identified 9 places where non-chat content entered the chat area
- Applied fixes to ui/templates/index.html (17 insertions, 17 deletions)
- Committed and pushed: 5d131e0

Stage Summary:
- Chat areas now contain ONLY: user bubbles + AI streaming bubbles + minimal typing dots
- All system messages, errors, tool calls, model switch notifications → log panel only
- Static HTML system messages removed from chat boxes
- WS connect welcome messages removed
- "Project: xxx" prefix removed from history loading
- WS reconnect error moved to log panel
- clearChat/goHome/deleteProject no longer add system messages
- Typing indicator simplified: only animated dots, model name shown in header badge
- CSS `:empty::after` placeholder for empty chat (not a .msg element)
- Backend (agent.py, run.py, auto_agent.py) required NO changes — already correctly routes via auto_log/tool_call
