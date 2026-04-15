"""
Fosved Coder v2.0 — Keys Manager
Управление API-ключами, валидация, провайдеры, бесплатные модели.
"""
import os
import yaml
import asyncio
import aiohttp
import litellm
from typing import Optional

litellm.suppress_debug_info = True

# ═══════════════════════════════════════════════════════════════
# ОПРЕДЕЛЕНИЯ ПРОВАЙДЕРОВ
# ═══════════════════════════════════════════════════════════════

PROVIDER_DEFS = {
    "claude": {
        "name": "Claude (Anthropic)",
        "litellm_prefix": "anthropic",
        "api_base": "https://api.anthropic.com",
        "suggested_models": ["claude-sonnet-4-20250514", "claude-3.5-sonnet", "claude-3-opus", "claude-3-haiku"],
    },
    "openai": {
        "name": "OpenAI",
        "litellm_prefix": "openai",
        "api_base": "https://api.openai.com/v1",
        "suggested_models": ["gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-4-turbo"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "litellm_prefix": "openrouter",
        "api_base": "https://openrouter.ai/api/v1",
        "suggested_models": [
            "anthropic/claude-sonnet-4-20250514",
            "openai/gpt-4o",
            "google/gemini-2.5-flash-preview",
        ],
    },
    "grok": {
        "name": "Grok (xAI)",
        "litellm_prefix": "xai",
        "api_base": "https://api.x.ai/v1",
        "suggested_models": ["grok-3", "grok-3-mini", "grok-2"],
    },
    "minimax": {
        "name": "MiniMax",
        "litellm_prefix": "minimax",
        "api_base": "https://api.minimax.chat/v1",
        "suggested_models": ["minimax-abab6.5s-chat"],
    },
}

# Бесплатные модели (через OpenRouter)
FREE_MODELS = [
    {"id": "gemini-2-flash-free", "name": "Gemini 2.0 Flash", "model": "google/gemini-2.0-flash-exp:free", "provider": "openrouter"},
    {"id": "llama-4-maverick-free", "name": "Llama 4 Maverick", "model": "meta-llama/llama-4-maverick:free", "provider": "openrouter"},
    {"id": "qwen-2.5-72b-free", "name": "Qwen 2.5 72B", "model": "qwen/qwen-2.5-72b-instruct:free", "provider": "openrouter"},
    {"id": "deepseek-v3-free", "name": "DeepSeek V3", "model": "deepseek/deepseek-chat-v3-0324:free", "provider": "openrouter"},
    {"id": "mistral-large-free", "name": "Mistral Large", "model": "mistralai/mistral-large-2411:free", "provider": "openrouter"},
    {"id": "phi-4-free", "name": "Phi-4", "model": "microsoft/phi-4:free", "provider": "openrouter"},
    {"id": "gemini-2.5-flash-free", "name": "Gemini 2.5 Flash", "model": "google/gemini-2.5-flash-preview:free", "provider": "openrouter"},
    {"id": "llama-3.3-70b-free", "name": "Llama 3.3 70B", "model": "meta-llama/llama-3.3-70b-instruct:free", "provider": "openrouter"},
]

KEYS_FILE = "keys.yaml"


class KeysManager:
    """
    Управляет API-ключами, валидацией и доступностью моделей.

    Статусы ключей:
      - valid       — ключ работает (зелёный)
      - rate_limited — лимит превышен (жёлтый)
      - invalid     — ключ не работает (скрыт из списка)
      - not_configured — ключ не настроен
      - checking    — идёт проверка
    """

    def __init__(self):
        self.providers: dict = {}
        self.github_token: str = ""
        self.github_enabled: bool = False
        self.github_user: str = ""
        self._load_keys()

    # ─── Storage ─────────────────────────────────────────────

    def _load_keys(self):
        if os.path.exists(KEYS_FILE):
            try:
                with open(KEYS_FILE, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self.providers = data.get("providers", {})
                github = data.get("github", {})
                self.github_token = github.get("token", "")
                self.github_enabled = github.get("enabled", False)
                self.github_user = github.get("user", "")
            except Exception:
                self.providers = {}

    def _save_keys(self):
        data = {
            "providers": self.providers,
            "github": {
                "token": self.github_token,
                "enabled": self.github_enabled,
                "user": self.github_user,
            },
        }
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    # ─── Validation ──────────────────────────────────────────

    async def validate_key(self, provider_id: str, api_key: str, model: str = None) -> dict:
        """
        Валидация API-ключа минимальным запросом.
        Returns: {"status": "valid"|"invalid"|"rate_limited", "error": str}
        """
        provider = PROVIDER_DEFS.get(provider_id)
        if not provider:
            return {"status": "invalid", "error": f"Неизвестный провайдер: {provider_id}"}

        test_model = model or provider["suggested_models"][0]
        litellm_model = f"{provider['litellm_prefix']}/{test_model}"

        try:
            response = await litellm.acompletion(
                model=litellm_model,
                messages=[{"role": "user", "content": "hi"}],
                api_key=api_key,
                api_base=provider["api_base"],
                max_tokens=1,
                temperature=0,
                timeout=15,
            )
            if response and response.choices:
                return {"status": "valid", "error": ""}
            return {"status": "invalid", "error": "Пустой ответ от API"}

        except Exception as e:
            error_str = str(e).lower()
            if "401" in error_str or "unauthorized" in error_str or "authentication" in error_str:
                return {"status": "invalid", "error": "Неверный API ключ"}
            elif "429" in error_str or "rate" in error_str or "quota" in error_str:
                return {"status": "rate_limited", "error": "Лимит запросов исчерпан или нет средств"}
            elif "insufficient" in error_str or "billing" in error_str:
                return {"status": "rate_limited", "error": "Недостаточно средств на счёте"}
            elif "timeout" in error_str:
                return {"status": "valid", "error": ""}  # Таймаут при валидации = ключ скорее всего ок
            else:
                return {"status": "invalid", "error": f"Ошибка: {str(e)[:150]}"}

    async def validate_github_token(self, token: str) -> dict:
        """Валидация GitHub токена через /user endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {"status": "valid", "user": data.get("login", ""), "error": ""}
                    elif resp.status == 401:
                        return {"status": "invalid", "user": "", "error": "Неверный GitHub токен"}
                    elif resp.status == 403:
                        return {"status": "invalid", "user": "", "error": "Токен не имеет нужных прав"}
                    else:
                        return {"status": "invalid", "user": "", "error": f"GitHub API: {resp.status}"}
        except Exception as e:
            return {"status": "invalid", "user": "", "error": str(e)[:100]}

    # ─── Key Management ──────────────────────────────────────

    async def add_key(self, provider_id: str, api_key: str, models: list = None, api_base: str = None) -> dict:
        """
        Валидация и сохранение API-ключа.
        Returns: {"success": bool, "status": str, "error": str, "provider": str}
        """
        provider = PROVIDER_DEFS.get(provider_id)
        if not provider:
            return {"success": False, "status": "invalid", "error": f"Неизвестный провайдер: {provider_id}", "provider": provider_id}

        # Валидируем ключ
        test_model = (models or provider["suggested_models"])[0] if (models or provider["suggested_models"]) else None
        if not test_model:
            return {"success": False, "status": "invalid", "error": "Не указана ни одна модель", "provider": provider_id}

        validation = await self.validate_key(provider_id, api_key, test_model)

        if validation["status"] == "invalid" and "Неверный" in validation["error"]:
            return {"success": False, "status": "invalid", "error": validation["error"], "provider": provider_id}

        # Сохраняем ключ (даже если rate_limited — всё равно полезен)
        self.providers[provider_id] = {
            "api_key": api_key,
            "api_base": api_base or provider["api_base"],
            "litellm_prefix": provider["litellm_prefix"],
            "models": models or provider["suggested_models"],
            "status": validation["status"],
        }
        self._save_keys()

        return {
            "success": True,
            "status": validation["status"],
            "error": validation["error"],
            "provider": provider_id,
            "provider_name": provider["name"],
            "models": self.providers[provider_id]["models"],
        }

    def remove_key(self, provider_id: str) -> bool:
        if provider_id in self.providers:
            del self.providers[provider_id]
            self._save_keys()
            return True
        return False

    def update_provider_models(self, provider_id: str, models: list) -> dict | None:
        """Обновить список моделей для провайдера."""
        if provider_id not in self.providers:
            return None
        self.providers[provider_id]["models"] = models
        self._save_keys()
        return {"provider": provider_id, "models": models}

    def set_github_token(self, token: str, enabled: bool = True):
        self.github_token = token
        self.github_enabled = enabled
        self._save_keys()

    def toggle_github(self, enabled: bool) -> dict:
        self.github_enabled = enabled and bool(self.github_token)
        self._save_keys()
        return {"enabled": self.github_enabled, "has_token": bool(self.github_token)}

    # ─── Startup Validation ──────────────────────────────────

    async def startup_validation(self) -> dict:
        """
        Валидация всех сохранённых ключей при запуске.
        Возвращает {provider_id: {status, models}, ...}
        """
        results = {}

        for provider_id, config in list(self.providers.items()):
            api_key = config.get("api_key", "")
            if not api_key:
                self.providers[provider_id]["status"] = "invalid"
                results[provider_id] = {"status": "invalid", "models": config.get("models", [])}
                continue

            test_model = config["models"][0] if config.get("models") else None
            if not test_model:
                results[provider_id] = {"status": "invalid", "models": []}
                continue

            validation = await self.validate_key(provider_id, api_key, test_model)
            self.providers[provider_id]["status"] = validation["status"]
            results[provider_id] = {"status": validation["status"], "models": config.get("models", [])}

        # GitHub
        if self.github_enabled and self.github_token:
            gh_val = await self.validate_github_token(self.github_token)
            if gh_val["status"] == "valid":
                self.github_user = gh_val.get("user", "")
            else:
                self.github_enabled = False
            self._save_keys()

        self._save_keys()
        return results

    # ─── Model Access ────────────────────────────────────────

    def get_all_models(self) -> list[dict]:
        """
        Все доступные модели (платные + бесплатные).
        Returns: [{id, name, model, provider, provider_name, type, status}]
        """
        models = []

        # Платные модели из настроенных провайдеров
        for provider_id, config in self.providers.items():
            provider_def = PROVIDER_DEFS.get(provider_id, {})
            prefix = config.get("litellm_prefix", provider_id)
            status = config.get("status", "not_configured")

            if status == "invalid":
                continue  # Скрываем невалидные

            for model_name in config.get("models", []):
                models.append({
                    "id": f"{provider_id}__{model_name}",
                    "name": model_name,
                    "model": f"{prefix}/{model_name}",
                    "provider": provider_id,
                    "provider_name": provider_def.get("name", provider_id),
                    "type": "paid",
                    "status": status,
                })

        # Бесплатные модели
        for fm in FREE_MODELS:
            provider_config = self.providers.get(fm["provider"], {})
            has_key = bool(provider_config.get("api_key"))
            provider_status = provider_config.get("status", "not_configured")
            available = has_key and provider_status in ("valid", "rate_limited")

            models.append({
                "id": fm["id"],
                "name": fm["name"],
                "model": fm["model"],
                "provider": fm["provider"],
                "provider_name": PROVIDER_DEFS.get(fm["provider"], {}).get("name", fm["provider"]),
                "type": "free",
                "status": "available" if available else "no_provider",
            })

        return models

    def get_model_config(self, model_id: str) -> dict | None:
        """
        Получить конфиг для litellm по ID модели.
        Returns: {model, api_key, api_base} или None
        """
        # Платные модели
        for provider_id, config in self.providers.items():
            for model_name in config.get("models", []):
                if f"{provider_id}__{model_name}" == model_id:
                    prefix = config.get("litellm_prefix", provider_id)
                    return {
                        "model": f"{prefix}/{model_name}",
                        "api_key": config.get("api_key", ""),
                        "api_base": config.get("api_base", ""),
                    }

        # Бесплатные модели
        for fm in FREE_MODELS:
            if fm["id"] == model_id:
                provider_config = self.providers.get(fm["provider"], {})
                provider_def = PROVIDER_DEFS.get(fm["provider"], {})
                return {
                    "model": fm["model"],
                    "api_key": provider_config.get("api_key", ""),
                    "api_base": provider_config.get("api_base", provider_def.get("api_base", "")),
                }

        return None

    def get_provider_status(self) -> dict:
        """Статус всех провайдеров."""
        result = {}
        for pid, pdef in PROVIDER_DEFS.items():
            config = self.providers.get(pid, {})
            result[pid] = {
                "name": pdef["name"],
                "status": config.get("status", "not_configured"),
                "models_count": len(config.get("models", [])),
                "has_key": bool(config.get("api_key")),
            }
        return result

    def get_github_status(self) -> dict:
        return {
            "token": self.github_token[:8] + "..." if self.github_token else "",
            "enabled": self.github_enabled,
            "user": self.github_user,
            "has_token": bool(self.github_token),
        }


# Глобальный экземпляр
keys_manager = KeysManager()
