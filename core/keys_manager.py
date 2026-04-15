"""Multi-provider API key manager with JSON storage."""
import json
from pathlib import Path

KEYS_FILE = Path(__file__).parent.parent / "data" / "keys.json"

PROVIDER_DEFS = {
    "claude":     {"name": "Anthropic Claude", "base_url": "https://api.anthropic.com"},
    "openai":     {"name": "OpenAI",           "base_url": "https://api.openai.com/v1"},
    "openrouter": {"name": "OpenRouter",       "base_url": "https://openrouter.ai/api/v1"},
    "grok":       {"name": "Grok (xAI)",       "base_url": "https://api.x.ai/v1"},
    "minimax":    {"name": "MiniMax",          "base_url": "https://api.minimax.chat/v1"},
    "google":     {"name": "Google Gemini",    "base_url": "https://generativelanguage.googleapis.com/v1"},
    "deepseek":   {"name": "DeepSeek",         "base_url": "https://api.deepseek.com/v1"},
    "custom":     {"name": "Custom Provider",  "base_url": ""},
}

class KeysManager:
    def __init__(self):
        self.keys_file = KEYS_FILE
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self):
        if self.keys_file.exists():
            try:
                with open(self.keys_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"providers": {}, "github_token": ""}
        return {"providers": {}, "github_token": ""}

    def _save(self):
        with open(self.keys_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def list_keys(self):
        result = []
        for pid, pdata in self._data.get("providers", {}).items():
            info = PROVIDER_DEFS.get(pid, {"name": pid, "base_url": ""})
            result.append({
                "provider_id": pid,
                "provider_name": info["name"],
                "base_url": info["base_url"],
                "api_key": pdata.get("api_key", ""),
                "models": pdata.get("models", []),
                "is_active": pdata.get("is_active", True),
            })
        return result

    def add_key(self, provider_id, api_key, models=None, is_active=True):
        if models is None:
            models = []
        self._data.setdefault("providers", {})[provider_id] = {
            "api_key": api_key,
            "models": models,
            "is_active": is_active,
        }
        self._save()
        return {"status": "ok", "provider_id": provider_id}

    def remove_key(self, provider_id):
        self._data.get("providers", {}).pop(provider_id, None)
        self._save()
        return {"status": "ok"}

    def toggle_key(self, provider_id, is_active):
        if provider_id in self._data.get("providers", {}):
            self._data["providers"][provider_id]["is_active"] = is_active
            self._save()
        return {"status": "ok"}

    def get_all_models(self):
        models = []
        for pid, pdata in self._data.get("providers", {}).items():
            info = PROVIDER_DEFS.get(pid, {"name": pid})
            for mname in pdata.get("models", []):
                models.append({
                    "provider_id": pid,
                    "provider_name": info["name"],
                    "model_name": mname,
                    "is_active": pdata.get("is_active", True),
                })
        return models

    def get_github_token(self):
        return self._data.get("github_token", "")

    def set_github_token(self, token):
        self._data["github_token"] = token
        self._save()
        return {"status": "ok"}

keys_manager = KeysManager()