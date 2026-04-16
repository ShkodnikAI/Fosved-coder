import json
import os
from datetime import datetime

class ChatHistory:
    def __init__(self, data_dir="data/chat_history"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _file(self, project_id):
        return os.path.join(self.data_dir, f"{project_id}.json")

    def _load(self, project_id):
        f = self._file(project_id)
        if os.path.exists(f):
            with open(f, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {"threads": {}}

    def _save(self, project_id, data):
        f = self._file(project_id)
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    # --- Thread management ---
    def list_threads(self, project_id):
        data = self._load(project_id)
        threads = data.get("threads", {})
        result = []
        for tid, tdata in threads.items():
            msg_count = len(tdata.get("messages", []))
            result.append({
                "id": tid,
                "name": tdata.get("name", tid),
                "created": tdata.get("created", ""),
                "message_count": msg_count
            })
        # Sort by created date
        result.sort(key=lambda x: x.get("created", ""))
        # If no threads exist, create default
        if not result:
            default_id = "main"
            data["threads"][default_id] = {
                "name": "Основной",
                "created": datetime.now().isoformat(),
                "messages": []
            }
            self._save(project_id, data)
            return [{"id": default_id, "name": "Основной", "created": data["threads"][default_id]["created"], "message_count": 0}]
        return result

    def create_thread(self, project_id, thread_id=None, name=""):
        data = self._load(project_id)
        if thread_id is None:
            thread_id = f"thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if thread_id in data.get("threads", {}):
            return {"status": "exists", "id": thread_id}
        data.setdefault("threads", {})[thread_id] = {
            "name": name or f"Поток {len(data['threads']) + 1}",
            "created": datetime.now().isoformat(),
            "messages": []
        }
        self._save(project_id, data)
        return {"status": "ok", "id": thread_id, "name": data["threads"][thread_id]["name"]}

    def delete_thread(self, project_id, thread_id):
        data = self._load(project_id)
        threads = data.get("threads", {})
        if thread_id in threads:
            del threads[thread_id]
            # Keep at least one thread
            if not threads:
                threads["main"] = {
                    "name": "Основной",
                    "created": datetime.now().isoformat(),
                    "messages": []
                }
            self._save(project_id, data)
            return {"status": "ok"}
        return {"status": "not_found"}

    def rename_thread(self, project_id, thread_id, new_name):
        data = self._load(project_id)
        threads = data.get("threads", {})
        if thread_id in threads:
            threads[thread_id]["name"] = new_name
            self._save(project_id, data)
            return {"status": "ok"}
        return {"status": "not_found"}

    # --- Messages within a thread ---
    def load_history(self, project_id, thread_id="main"):
        data = self._load(project_id)
        threads = data.get("threads", {})
        if thread_id not in threads:
            # Fallback: return first thread's messages or empty
            if threads:
                first_key = list(threads.keys())[0]
                return threads[first_key].get("messages", [])
            return []
        return threads[thread_id].get("messages", [])

    def save_message(self, project_id, role, content, thread_id="main"):
        data = self._load(project_id)
        threads = data.setdefault("threads", {})
        if thread_id not in threads:
            threads[thread_id] = {
                "name": thread_id,
                "created": datetime.now().isoformat(),
                "messages": []
            }
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        threads[thread_id]["messages"].append(msg)
        self._save(project_id, data)
        return msg

    def update_last_ai(self, project_id, content, thread_id="main"):
        data = self._load(project_id)
        threads = data.get("threads", {})
        if thread_id in threads:
            messages = threads[thread_id].get("messages", [])
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "assistant":
                    messages[i]["content"] = content
                    threads[thread_id]["messages"] = messages
                    self._save(project_id, data)
                    return
        # If no assistant message found, create one
        self.save_message(project_id, "assistant", content, thread_id)

    def clear_history(self, project_id, thread_id="main"):
        data = self._load(project_id)
        threads = data.get("threads", {})
        if thread_id in threads:
            threads[thread_id]["messages"] = []
            self._save(project_id, data)
            return {"status": "ok"}
        return {"status": "not_found"}
