"""
Fosved Coder v2.0 — Keys Manager
Управление API-ключами, валидация, провайдеры, бесплатные модели, локальные модели.
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
    "gemini": {
        "name": "Google AI (Gemini)",
        "litellm_prefix": "gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "suggested_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    },
    "zai": {
        "name": "Z.AI",
        "litellm_prefix": "openai",
        "api_base": "https://chat.z.ai/v1",
        "suggested_models": ["default"],
        "is_custom": True,
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

# Локальные провайдеры по умолчанию
LOCAL_PROVIDERS = {
    "ollama": {
        "name": "Ollama",
        "default_base_url": "http://localhost:11434/v1",
        "litellm_prefix": "openai",
    },
    "lmstudio": {
        "name": "LM Studio",
        "default_base_url": "http://localhost:1234/v1",
        "litellm_prefix": "openai",
    },
    "vllm": {
        "name": "vLLM",
        "default_base_url": "http://localhost:8000/v1",
        "litellm_prefix": "openai",
    },
    "llamacpp": {
        "name": "llama.cpp",
        "default_base_url": "http://localhost:8080/v1",
        "litellm_prefix": "openai",
    },
    "custom_local": {
        "name": "Кастомный",
        "default_base_url": "http://localhost:5000/v1",
        "litellm_prefix": "openai",
    },
}

KEYS_FILE = "keys.yaml"

# Mapping: env var name -> provider_id
ENV_KEY_MAP = {
    "OPENROUTER_API_KEY": "openrouter",
    "ANTHROPIC_API_KEY": "claude",
    "OPENAI_API_KEY": "openai",
    "XAI_API_KEY": "grok",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "MINIMAX_API_KEY": "minimax",
}


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
        self.local_models: list = []  # [{id, name, model, provider_name, base_url}]
        self.custom_models: list = []  # [{id, name, model, api_base, api_key}]
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
                self.local_models = data.get("local_models", [])
                self.custom_models = data.get("custom_models", [])
                github = data.get("github", {})
                self.github_token = github.get("token", "")
                self.github_enabled = github.get("enabled", False)
                self.github_user = github.get("user", "")
            except Exception:
                self.providers = {}
                self.local_models = []
                self.custom_models = []

        # Load API keys from environment variables (Render, Railway, etc.)
        self._load_env_keys()

    def _load_env_keys(self):
        """Populate providers from environment variables if not already set."""
        for env_var, provider_id in ENV_KEY_MAP.items():
            api_key = os.environ.get(env_var, "")
            if not api_key:
                continue
            provider_def = PROVIDER_DEFS.get(provider_id)
            if not provider_def:
                continue
            # Env var overrides only if no key saved, or if saved key is empty/invalid
            existing = self.providers.get(provider_id, {})
            existing_key = existing.get("api_key", "")
            if not existing_key or existing.get("status") == "invalid":
                self.providers[provider_id] = {
                    "api_key": api_key,
                    "api_base": provider_def["api_base"],
                    "litellm_prefix": provider_def["litellm_prefix"],
                    "models": provider_def["suggested_models"],
                    "status": "valid",  # Assume valid, startup_validation will re-check
                }
        # GitHub token from env
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        if gh_token and not self.github_token:
            self.github_token = gh_token
            self.github_enabled = True

    def _save_keys(self):
        data = {
            "providers": self.providers,
            "local_models": self.local_models,
            "custom_models": self.custom_models,
            "github": {
                "token": self.github_token,
                "enabled": self.github_enabled,
                "user": self.github_user,
            },
        }
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    # ─── Validation ──────────────────────────────────────────

    async def validate_key(self, provider_id: str, api_key: str, model: str = None, api_base: str = None) -> dict:
        """
        Валидация API-ключа минимальным запросом.
        Returns: {"status": "valid"|"invalid"|"rate_limited", "error": str}
        """
        provider = PROVIDER_DEFS.get(provider_id)
        if not provider:
            return {"status": "invalid", "error": f"Неизвестный провайдер: {provider_id}"}

        test_model = model or provider["suggested_models"][0]
        litellm_model = f"{provider['litellm_prefix']}/{test_model}"
        base_url = api_base or provider["api_base"]

        try:
            response = await litellm.acompletion(
                model=litellm_model,
                messages=[{"role": "user", "content": "hi"}],
                api_key=api_key,
                api_base=base_url,
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
                return {"status": "valid", "error": ""}
            elif "404" in error_str or "not found" in error_str or "model not found" in error_str:
                # 404 может означать что модель не найдена, а не что ключ неверный
                return {"status": "valid", "error": "Модель может быть недоступна"}
            elif "connection" in error_str or "connect" in error_str:
                return {"status": "invalid", "error": f"Не удалось подключиться к {provider.get('name', provider_id)}"}
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

        test_model = (models or provider["suggested_models"])[0] if (models or provider["suggested_models"]) else None
        if not test_model:
            return {"success": False, "status": "invalid", "error": "Не указана ни одна модель", "provider": provider_id}

        validation = await self.validate_key(provider_id, api_key, test_model, api_base)

        if validation["status"] == "invalid":
            # Only fail on clear authentication errors, not connectivity issues
            err_lower = validation.get("error", "").lower()
            if "не удалось подключиться" in err_lower or "connection" in err_lower or "timeout" in err_lower:
                # Connection issue — save key anyway and mark as rate_limited for retry
                validation["status"] = "rate_limited"
                validation["error"] = "Ошибка подключения — ключ сохранён, проверьте позже"
            elif "Неверный" in validation["error"] or "unauthorized" in validation["error"] or "401" in validation["error"]:
                return {"success": False, "status": "invalid", "error": validation["error"], "provider": provider_id}
            # For other errors (404, model not found, etc.), save key anyway

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

    # ─── Local Models ────────────────────────────────────────

    async def discover_local_models(self, provider_key: str = "ollama", base_url: str = None) -> dict:
        """
        Автообнаружение моделей на локальном сервере.
        Проверяет стандартные эндпоинты: /v1/models или /api/tags.
        """
        provider_info = LOCAL_PROVIDERS.get(provider_key)
        if not provider_info:
            return {"success": False, "error": f"Неизвестный локальный провайдер: {provider_key}"}

        url = base_url or provider_info["default_base_url"]

        # Убираем /v1 если есть, для ollama используем /api/tags
        if provider_key == "ollama":
            check_url = url.replace("/v1", "").rstrip("/") + "/api/tags"
            headers = {}
        else:
            check_url = url.rstrip("/") + "/models"
            headers = {}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    check_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return {"success": False, "error": f"Сервер недоступен: {resp.status}", "provider": provider_key, "base_url": url}

                    data = await resp.json()

                    # Ollama format: {"models": [{"name": "...", ...}]}
                    # OpenAI format: {"data": [{"id": "...", ...}]}
                    models = []
                    if provider_key == "ollama" and "models" in data:
                        for m in data["models"]:
                            model_name = m.get("name", "")
                            # Убираем теги версий для отображения
                            display_name = model_name.split(":")[0]
                            models.append({
                                "id": f"local_{provider_key}_{model_name.replace(':', '_')}",
                                "name": display_name,
                                "model": model_name,
                                "provider": provider_key,
                                "provider_name": provider_info["name"],
                                "base_url": url.replace("/v1", "").rstrip("/"),  # Ollama needs base without /v1
                                "litellm_prefix": "ollama" if provider_key == "ollama" else "openai",
                            })
                    elif "data" in data:
                        for m in data["data"]:
                            model_id = m.get("id", "")
                            models.append({
                                "id": f"local_{provider_key}_{model_id.replace('/', '_').replace(':', '_')}",
                                "name": model_id,
                                "model": model_id,
                                "provider": provider_key,
                                "provider_name": provider_info["name"],
                                "base_url": url,
                                "litellm_prefix": provider_info["litellm_prefix"],
                            })

                    if not models:
                        return {"success": False, "error": "Модели не найдены на сервере", "provider": provider_key}

                    # Добавляем найденные модели, не дублируя
                    existing_ids = {m["id"] for m in self.local_models}
                    added = 0
                    for m in models:
                        if m["id"] not in existing_ids:
                            self.local_models.append(m)
                            existing_ids.add(m["id"])
                            added += 1

                    if added > 0:
                        self._save_keys()

                    return {
                        "success": True,
                        "models": models,
                        "added": added,
                        "total": len(self.local_models),
                        "provider": provider_key,
                        "provider_name": provider_info["name"],
                    }

        except aiohttp.ClientError:
            return {"success": False, "error": f"Не удалось подключиться к {url}", "provider": provider_key}
        except Exception as e:
            return {"success": False, "error": str(e)[:150], "provider": provider_key}

    async def add_local_model(self, provider_key: str, model_name: str, base_url: str, display_name: str = None) -> dict:
        """Ручное добавление локальной модели."""
        provider_info = LOCAL_PROVIDERS.get(provider_key)
        if not provider_info:
            return {"success": False, "error": f"Неизвестный провайдер: {provider_key}"}

        model_id = f"local_{provider_key}_{model_name.replace('/', '_').replace(':', '_')}"

        # Проверяем дубликат
        if any(m["id"] == model_id for m in self.local_models):
            return {"success": False, "error": "Модель уже добавлена"}

        model_entry = {
            "id": model_id,
            "name": display_name or model_name,
            "model": model_name,
            "provider": provider_key,
            "provider_name": provider_info["name"],
            "base_url": base_url,
            "litellm_prefix": "ollama" if provider_key == "ollama" else "openai",
        }

        self.local_models.append(model_entry)
        self._save_keys()

        return {"success": True, "model": model_entry}

    def remove_local_model(self, model_id: str) -> bool:
        before = len(self.local_models)
        self.local_models = [m for m in self.local_models if m["id"] != model_id]
        if len(self.local_models) < before:
            self._save_keys()
            return True
        return False

    # ─── Custom Models (force connect) ───────────────────────

    async def add_custom_model(self, name: str, api_base: str, api_key: str = "", model_id: str = "", litellm_prefix: str = "openai") -> dict:
        """
        Принудительное добавление любой модели по URL.
        """
        custom_id = f"custom_{name.replace(' ', '_').lower()}_{model_id.replace('/', '_').replace(':', '_')}" if model_id else f"custom_{name.replace(' ', '_').lower()}"

        # Проверяем дубликат
        if any(m["id"] == custom_id for m in self.custom_models):
            return {"success": False, "error": "Модель уже добавлена"}

        # Пробуем валидацию если api_base указан
        status = "valid"
        error = ""
        if api_base and model_id:
            try:
                litellm_model = f"{litellm_prefix}/{model_id}"
                response = await litellm.acompletion(
                    model=litellm_model,
                    messages=[{"role": "user", "content": "hi"}],
                    api_key=api_key or "not-needed",
                    api_base=api_base,
                    max_tokens=1,
                    temperature=0,
                    timeout=10,
                )
                if not response or not response.choices:
                    status = "invalid"
                    error = "Пустой ответ"
            except Exception as e:
                err_str = str(e).lower()
                if "connect" in err_str:
                    status = "invalid"
                    error = "Не удалось подключиться"
                elif "401" in err_str or "unauthorized" in err_str:
                    status = "invalid"
                    error = "Неверный ключ"
                else:
                    status = "valid"  # Пробуем всё равно
                    error = ""

        entry = {
            "id": custom_id,
            "name": name,
            "model": model_id or name,
            "api_base": api_base,
            "api_key": api_key,
            "litellm_prefix": litellm_prefix,
            "status": status,
            "error": error,
        }

        self.custom_models.append(entry)
        self._save_keys()

        return {"success": True, "model": entry}

    def remove_custom_model(self, model_id: str) -> bool:
        before = len(self.custom_models)
        self.custom_models = [m for m in self.custom_models if m["id"] != model_id]
        if len(self.custom_models) < before:
            self._save_keys()
            return True
        return False

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

        # Проверяем локальные модели
        local_results = {}
        for lm in self.local_models:
            local_results[lm["id"]] = {"status": "available", "name": lm["name"]}

        # GitHub
        if self.github_enabled and self.github_token:
            gh_val = await self.validate_github_token(self.github_token)
            if gh_val["status"] == "valid":
                self.github_user = gh_val.get("user", "")
            else:
                self.github_enabled = False
            self._save_keys()

        self._save_keys()
        results["local"] = local_results
        return results

    # ─── Model Access ────────────────────────────────────────

    def get_all_models(self) -> list[dict]:
        """
        Все доступные модели: платные → локальные → OpenRouter(inline key) → бесплатные → кастомные.
        Returns: [{id, name, model, provider, provider_name, type, status}]
        """
        models = []

        # 1. Платные модели из настроенных провайдеров (с валидными ключами)
        for provider_id, config in self.providers.items():
            provider_def = PROVIDER_DEFS.get(provider_id, {})
            prefix = config.get("litellm_prefix", provider_id)
            status = config.get("status", "not_configured")

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

        # 2. Локальные модели
        for lm in self.local_models:
            models.append({
                "id": lm["id"],
                "name": lm["name"],
                "model": lm["model"],
                "provider": lm.get("provider", "local"),
                "provider_name": lm.get("provider_name", "Локальная"),
                "type": "local",
                "status": "available",
                "base_url": lm.get("base_url", ""),
            })

        # 3. Бесплатные модели через OpenRouter
        openrouter_config = self.providers.get("openrouter", {})
        has_openrouter_key = bool(openrouter_config.get("api_key"))
        openrouter_status = openrouter_config.get("status", "not_configured")

        for fm in FREE_MODELS:
            available = has_openrouter_key and openrouter_status in ("valid", "rate_limited")
            models.append({
                "id": fm["id"],
                "name": fm["name"],
                "model": fm["model"],
                "provider": fm["provider"],
                "provider_name": PROVIDER_DEFS.get(fm["provider"], {}).get("name", fm["provider"]),
                "type": "free",
                "status": "available" if available else "no_key",
            })

        # 4. Кастомные модели (force connect)
        for cm in self.custom_models:
            models.append({
                "id": cm["id"],
                "name": cm["name"],
                "model": cm["model"],
                "provider": "custom",
                "provider_name": "Кастомная",
                "type": "custom",
                "status": cm.get("status", "valid"),
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

        # Локальные модели
        for lm in self.local_models:
            if lm["id"] == model_id:
                prefix = lm.get("litellm_prefix", "openai")
                base_url = lm.get("base_url", "")
                return {
                    "model": f"{prefix}/{lm['model']}",
                    "api_key": "",  # Локальные не нуждаются в ключе
                    "api_base": base_url,
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

        # Кастомные модели
        for cm in self.custom_models:
            if cm["id"] == model_id:
                prefix = cm.get("litellm_prefix", "openai")
                return {
                    "model": f"{prefix}/{cm['model']}",
                    "api_key": cm.get("api_key", ""),
                    "api_base": cm.get("api_base", ""),
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
