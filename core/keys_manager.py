import json
import os
from datetime import datetime

class KeysManager:
    def __init__(self, data_dir="data"):
        self.data_dir = data_dir
        self.keys_file = os.path.join(data_dir, "keys.json")
        os.makedirs(data_dir, exist_ok=True)
        self.keys = self._load()

    def _load(self):
        if os.path.exists(self.keys_file):
            with open(self.keys_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self.keys_file, "w", encoding="utf-8") as f:
            json.dump(self.keys, f, indent=2, ensure_ascii=False)

    def list_keys(self):
        result = []
        for provider, data in self.keys.items():
            result.append({
                "provider": provider,
                "name": data.get("name", ""),
                "models": data.get("models", []),
                "created": data.get("created", "")
            })
        return result

    def add_key(self, provider, key_value, name="", models=None):
        if models is None:
            models = []
        self.keys[provider] = {
            "key": key_value,
            "name": name or provider,
            "models": models,
            "created": datetime.now().isoformat()
        }
        self._save()
        return {"status": "ok", "provider": provider}

    def remove_key(self, provider):
        if provider in self.keys:
            del self.keys[provider]
            self._save()
            return {"status": "ok"}
        return {"status": "not_found"}

    def get_key(self, provider):
        data = self.keys.get(provider, {})
        return data.get("key", "")

    def get_all_models(self):
        """Return list of models from all configured providers."""
        models = []
        for provider, data in self.keys.items():
            for model in data.get("models", []):
                models.append({
                    "id": model,
                    "provider": provider,
                    "name": model
                })
        return models

    def get_github_token(self):
        data = self.keys.get("github", {})
        return data.get("key", "")

    def set_github_token(self, token, username=""):
        self.keys["github"] = {
            "key": token,
            "name": username,
            "models": [],
            "created": datetime.now().isoformat()
        }
        self._save()

    def get_github_username(self):
        data = self.keys.get("github", {})
        return data.get("name", "")
