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

from core.action_logger import get_logger
logger = get_logger()

litellm.suppress_debug_info = True

# ═══════════════════════════════════════════════════════════════
# ОПРЕДЕЛЕНИЯ ПРОВАЙДЕРОВ
# ═══════════════════════════════════════════════════════════════

PROVIDER_DEFS = {
    "claude": {
        "name": "Claude (Anthropic)",
        "litellm_prefix": "anthropic",
        "api_base": "https://api.anthropic.com",
        "suggested_models": [
            "claude-opus-4-7",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20241022",
        ],
    },
    "openai": {
        "name": "OpenAI",
        "litellm_prefix": "openai",
        "api_base": "https://api.openai.com/v1",
        "suggested_models": [
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "o3",
            "o3-mini",
            "gpt-4o",
            "gpt-4o-mini",
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "litellm_prefix": "openrouter",
        "api_base": "https://openrouter.ai/api/v1",
        "suggested_models": [
            "anthropic/claude-opus-4-7",
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4.1",
            "google/gemini-2.5-flash-preview",
        ],
    },
    "grok": {
        "name": "Grok (xAI)",
        "litellm_prefix": "xai",
        "api_base": "https://api.x.ai/v1",
        "suggested_models": ["grok-3", "grok-3-mini"],
    },
    "gemini": {
        "name": "Google AI (Gemini)",
        "litellm_prefix": "gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "suggested_models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "groq": {
        "name": "Groq (FREE tier)",
        "litellm_prefix": "openai",
        "api_base": "https://api.groq.com/openai/v1",
        "suggested_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "gpt-oss-120b",
            "qwen3-32b",
            "llama-4-scout-17b-16e-instruct",
        ],
        "is_free": True,
        "free_tier_info": "Бесплатно навсегда. ~30 RPM, 100K-500K токенов/день. 315+ TPS.",
    },
    "cerebras": {
        "name": "Cerebras (FREE tier)",
        "litellm_prefix": "openai",
        "api_base": "https://api.cerebras.ai/v1",
        "suggested_models": [
            "llama-3.3-70b",
            "llama-3.1-8b",
            "qwen3-235b-a22b",
        ],
        "is_free": True,
        "free_tier_info": "Бесплатно навсегда. ~1M токенов/день. 1000+ TPS.",
    },
    "sambanova": {
        "name": "SambaNova (FREE tier)",
        "litellm_prefix": "openai",
        "api_base": "https://api.sambanova.ai/v1",
        "suggested_models": [
            "Meta-Llama-3.3-70B-Instruct",
            "Qwen2.5-72B-Instruct",
        ],
        "is_free": True,
        "free_tier_info": "Бесплатно навсегда. 294 TPS. Llama 3.3 70B + Qwen.",
    },
    "zai": {
        "name": "Z.AI (GLM)",
        "litellm_prefix": "openai",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "suggested_models": ["glm-5.1", "glm-5-turbo", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air"],
        "is_custom": True,
    },
    "abacus": {
        "name": "Abacus.AI (RouteLLM)",
        "litellm_prefix": "openai",
        "api_base": "https://routellm.abacus.ai/v1",
        "is_custom": True,
        # RouteLLM: 1 ключ = 65+ моделей от всех провайдеров без наценки.
        # route-llm — умная маршрутизация (автоматически выбирает лучшую модель по сложности запроса)
        # -thinking — расширенное мышление (extended thinking) для поддерживаемых моделей
        # Динамический список моделей доступен через GET /v1/models
        "suggested_models": [
            # --- Умная маршрутизация (RouteLLM) ---
            "route-llm",  # Автоматический выбор лучшей модели по сложности запроса
            # --- OpenAI ---
            "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
            "gpt-5.3-codex",  # Специализированная модель для кода
            "gpt-5.2", "gpt-5.1", "gpt-5",
            "gpt-4.1", "gpt-4o",
            "o4-mini", "o3", "o3-pro",
            # --- Anthropic Claude ---
            "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
            "claude-sonnet-4-6", "claude-sonnet-4-5",
            "claude-haiku-4-5",
            # --- Google Gemini ---
            "gemini-3.1-pro", "gemini-3.1-flash-lite",
            "gemini-3-pro", "gemini-2.5-pro", "gemini-2.5-flash",
            # --- xAI Grok ---
            "grok-4.2", "grok-4.1-fast", "grok-4", "grok-code-fast",
            # --- Qwen ---
            "qwen3-235b-a22b", "qwen3-max", "qwen3-coder", "qwq-32b",
            # --- Meta Llama ---
            "llama-4-Maverick", "llama-3.3-70B", "llama-3.1-405B",
            # --- Abacus (собственные) ---
            "abacus-smaug2", "abacus-dracarys",
            # --- Kimi ---
            "kimi-k2.5",
            # --- GLM ---
            "glm-5", "glm-4.7", "glm-4.6", "glm-4.5",
        ],
        # Модели с поддержкой extended thinking (добавляется суффикс -thinking)
        "thinking_models": [
            "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
            "o3", "o3-pro",
        ],
        # Категории моделей для UI
        "model_categories": {
            "smart_routing": {"models": ["route-llm"], "label": "Умная маршрутизация"},
            "coding": {"models": ["gpt-5.3-codex", "grok-code-fast", "qwen3-coder", "claude-sonnet-4-6"], "label": "Код"},
            "reasoning": {"models": ["claude-opus-4-7", "o3-pro", "o3", "gemini-3.1-pro"], "label": "Рассуждения"},
            "fast": {"models": ["gpt-4.1-nano", "claude-haiku-4-5", "gemini-3.1-flash-lite", "grok-4.1-fast", "o4-mini"], "label": "Быстрые"},
        },
    },
}

# Бесплатные модели (через OpenRouter) — требуют OPENROUTER_API_KEY
# Обновлено 2026-04-22: актуальный список free-моделей
FREE_MODELS = [
    # --- ЛУЧШИЕ ДЛЯ КОДА ---
    {"id": "devstral-2512-free", "name": "Devstral 2 (123B)", "model": "mistralai/devstral-2512:free", "provider": "openrouter", "tags": ["coding"]},
    {"id": "qwen3-coder-480b-free", "name": "Qwen3 Coder 480B", "model": "qwen/qwen3-coder-480b-a35b-07-25:free", "provider": "openrouter", "tags": ["coding"]},
    {"id": "gpt-oss-120b-free", "name": "OpenAI GPT-OSS 120B", "model": "openai/gpt-oss-120b:free", "provider": "openrouter", "tags": ["coding"]},
    {"id": "nemotron-super-120b-free", "name": "Nemotron 3 Super 120B", "model": "nvidia/nemotron-3-super-120b-a12b:free", "provider": "openrouter", "tags": ["coding"]},
    # --- РАССУЖДЕНИЯ / GENERAL ---
    {"id": "hermes-3-405b-free", "name": "Hermes 3 Llama 405B", "model": "nousresearch/hermes-3-llama-3.1-405b:free", "provider": "openrouter"},
    {"id": "llama-3.3-70b-free", "name": "Llama 3.3 70B", "model": "meta-llama/llama-3.3-70b-instruct:free", "provider": "openrouter"},
    {"id": "qwen3-next-80b-free", "name": "Qwen3-Next 80B", "model": "qwen/qwen3-next-80b-a3b-instruct-2509:free", "provider": "openrouter"},
    {"id": "nemotron-nano-30b-free", "name": "Nemotron 3 Nano 30B", "model": "nvidia/nemotron-3-nano-30b-a3b:free", "provider": "openrouter"},
    {"id": "ling-2.6-flash-free", "name": "Ling 2.6 Flash 196B", "model": "inclusionai/ling-2.6-flash:free", "provider": "openrouter"},
    {"id": "arcee-trinity-free", "name": "Arcee Trinity Large", "model": "arcee-ai/trinity-large-preview:free", "provider": "openrouter"},
    # --- МУЛЬТИМОДАЛЬНЫЕ ---
    {"id": "gemma-4-31b-free", "name": "Gemma 4 31B", "model": "google/gemma-4-31b-it:free", "provider": "openrouter"},
    {"id": "gemma-4-26b-free", "name": "Gemma 4 26B", "model": "google/gemma-4-26b-a4b-it:free", "provider": "openrouter"},
    {"id": "gemma-3-27b-free", "name": "Gemma 3 27B", "model": "google/gemma-3-27b-it:free", "provider": "openrouter"},
    # --- МАЛЕНЬКИЕ / БЫСТРЫЕ ---
    {"id": "nemotron-nano-9b-free", "name": "Nemotron Nano 9B", "model": "nvidia/nemotron-nano-9b-v2:free", "provider": "openrouter"},
    {"id": "gpt-oss-20b-free", "name": "GPT-OSS 20B", "model": "openai/gpt-oss-20b:free", "provider": "openrouter"},
    {"id": "qwen3-8b-free", "name": "Qwen3 8B", "model": "qwen/qwen3-8b:free", "provider": "openrouter"},
    {"id": "glm-4.5-air-free", "name": "GLM 4.5 Air", "model": "z-ai/glm-4.5-air:free", "provider": "openrouter"},
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

KEYS_FILE = os.environ.get("KEYS_FILE_PATH", "keys.yaml")
LEGACY_KEYS_FILE = "data/keys.json"

# Mapping: env var name -> provider_id
ENV_KEY_MAP = {
    "OPENROUTER_API_KEY": "openrouter",
    "ANTHROPIC_API_KEY": "claude",
    "OPENAI_API_KEY": "openai",
    "XAI_API_KEY": "grok",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "GROQ_API_KEY": "groq",
    "CEREBRAS_API_KEY": "cerebras",
    "SAMBANOVA_API_KEY": "sambanova",
    "ZAI_API_KEY": "zai",
    "ABACUS_API_KEY": "abacus",
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
        loaded = False
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
                loaded = True
            except Exception:
                self.providers = {}
                self.local_models = []
                self.custom_models = []

        # Fallback: migrer legacy data/keys.json если keys.yaml пустой или отсутствует
        if not loaded or not self.providers:
            self._try_load_legacy_json()

        # Load API keys from environment variables (Render, Railway, etc.)
        self._load_env_keys()

    def _try_load_legacy_json(self):
        """Загрузить и мигрировать старый data/keys.json (legacy формат)."""
        if not os.path.exists(LEGACY_KEYS_FILE):
            return

        try:
            import json
            with open(LEGACY_KEYS_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)

            legacy_providers = legacy.get("providers", {})
            if not legacy_providers:
                return

            # Маппинг legacy провайдеров → PROVIDER_DEFS
            legacy_key_patterns = {
                "claude": "claude",
                "openai": "openai",
                "grok": "grok",
                "gemini": "gemini",
                "openrouter": "openrouter",
            }

            migrated = 0
            for prov_id, prov_data in legacy_providers.items():
                api_key = prov_data.get("api_key", "")
                if not api_key:
                    continue

                # Определяем реальный провайдер по формату ключа
                real_provider = prov_id
                if prov_id == "custom" or prov_id not in PROVIDER_DEFS:
                    if api_key.startswith("sk-or-v1"):
                        real_provider = "openrouter"
                    elif api_key.startswith("sk-ant-"):
                        real_provider = "claude"
                    elif api_key.startswith("sk-proj-") or api_key.startswith("sk-"):
                        real_provider = "openai"
                    elif api_key.startswith("xai-"):
                        real_provider = "grok"
                    else:
                        # Не можем определить — пропускаем
                        continue

                provider_def = PROVIDER_DEFS.get(real_provider)
                if not provider_def:
                    continue

                # Не перезаписываем если уже есть в keys.yaml
                if real_provider in self.providers and self.providers[real_provider].get("api_key"):
                    continue

                self.providers[real_provider] = {
                    "api_key": api_key,
                    "api_base": provider_def["api_base"],
                    "litellm_prefix": provider_def["litellm_prefix"],
                    "models": prov_data.get("models") or provider_def["suggested_models"],
                    "status": "valid",  # Будет перевалидирован при startup_validation
                }
                migrated += 1

            # GitHub токен
            gh_token = legacy.get("github_token", "")
            if gh_token and not self.github_token:
                self.github_token = gh_token
                self.github_enabled = True

            if migrated > 0:
                print(f"  [keys] Мигрировано {migrated} провайдеров из {LEGACY_KEYS_FILE}")
                self._save_keys()  # Сохраняем в keys.yaml

        except Exception as e:
            print(f"  [keys] Ошибка загрузки legacy keys: {e}")

    def _load_env_keys(self):
        """Populate providers from environment variables (highest priority — source of truth on cloud)."""
        env_loaded = 0
        for env_var, provider_id in ENV_KEY_MAP.items():
            api_key = os.environ.get(env_var, "")
            if not api_key:
                continue
            provider_def = PROVIDER_DEFS.get(provider_id)
            if not provider_def:
                continue
            # Env var ALWAYS overrides saved keys — on Render/Railway env vars are the source of truth
            existing = self.providers.get(provider_id, {})
            existing_key = existing.get("api_key", "")
            if api_key != existing_key or existing.get("status") in ("invalid", "not_configured", ""):
                self.providers[provider_id] = {
                    "api_key": api_key,
                    "api_base": provider_def["api_base"],
                    "litellm_prefix": provider_def["litellm_prefix"],
                    "models": existing.get("models") or provider_def["suggested_models"],
                    "status": "valid",  # Assume valid, startup_validation will re-check
                }
                env_loaded += 1
        if env_loaded:
            print(f"  [keys] Loaded {env_loaded} API key(s) from environment variables")
        # GitHub token from env
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        if gh_token and not self.github_token:
            self.github_token = gh_token
            self.github_enabled = True

    def _save_keys(self):
        try:
            # Ensure directory exists
            keys_dir = os.path.dirname(KEYS_FILE)
            if keys_dir:
                os.makedirs(keys_dir, exist_ok=True)
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
        except Exception as e:
            print(f"  [keys_manager] Warning: could not save keys to {KEYS_FILE}: {e}")
            try:
                logger.log("keys_save_error", level="error", source="keys_manager",
                           details={"file": KEYS_FILE}, error=str(e))
            except Exception:
                pass

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

        # For standard providers, let litellm handle the URL natively.
        # Only pass api_base for custom providers, free-tier providers, or explicit overrides.
        is_custom = provider.get("is_custom", False)
        is_free_tier = provider.get("is_free", False)
        default_base = provider["api_base"]
        explicit_base = api_base or default_base
        use_base = explicit_base if (is_custom or is_free_tier or explicit_base != default_base) else None

        try:
            kwargs = {
                "model": litellm_model,
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": api_key,
                "max_tokens": 1,
                "temperature": 0,
                "timeout": 15,
            }
            if use_base:
                kwargs["api_base"] = use_base
            response = await litellm.acompletion(**kwargs)
            if response and response.choices:
                try:
                    logger.log(f"key_validated", level="success", source="keys_manager",
                               details={"provider": provider_id, "model": test_model})
                except Exception:
                    pass
                return {"status": "valid", "error": ""}
            return {"status": "invalid", "error": "Пустой ответ от API"}

        except Exception as e:
            error_str = str(e).lower()
            try:
                logger.log(f"key_validation_error", level="warning", source="keys_manager",
                           details={"provider": provider_id, "model": test_model}, error=str(e)[:200])
            except Exception:
                pass
            if "401" in error_str or "unauthorized" in error_str or "authentication" in error_str:
                return {"status": "invalid", "error": "Неверный API ключ"}
            elif "429" in error_str or "rate" in error_str or "quota" in error_str:
                return {"status": "rate_limited", "error": "Лимит запросов исчерпан или нет средств"}
            elif "insufficient" in error_str or "billing" in error_str or "402" in error_str or "payment" in error_str or "credits" in error_str or "no credits" in error_str or "balance" in error_str:
                return {"status": "rate_limited", "error": "Недостаточно средств — бесплатные модели доступны"}
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

        try:
            logger.log(f"key_added", level="info", source="keys_manager",
                       details={"provider": provider_id, "status": validation["status"],
                                "models": len(models or provider["suggested_models"])})
        except Exception:
            pass

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
            try:
                logger.log(f"key_removed", level="info", source="keys_manager",
                           details={"provider": provider_id})
            except Exception:
                pass
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

    # ─── Dynamic Model Fetching ───────────────────────────────

    async def fetch_abacus_models(self, api_key: str = None) -> dict:
        """
        Загрузить актуальный список моделей с Abacus.AI RouteLLM API.
        GET https://routellm.abacus.ai/v1/models
        Returns: {"success": bool, "models": list, "count": int, "error": str}
        """
        config = self.providers.get("abacus", {})
        key = api_key or config.get("api_key", "")
        base_url = config.get("api_base", PROVIDER_DEFS["abacus"]["api_base"])

        if not key:
            return {"success": False, "models": [], "count": 0, "error": "Нет API ключа Abacus.AI"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return {"success": False, "models": [], "count": 0,
                                "error": f"API вернул {resp.status}: {error_text[:200]}"}

                    data = await resp.json()

                    # OpenAI-совместимый формат: {"data": [{"id": "model-name", ...}, ...]}
                    raw_models = data.get("data", [])
                    if not raw_models:
                        return {"success": False, "models": [], "count": 0,
                                "error": "API вернул пустой список моделей"}

                    # Извлекаем ID моделей, фильтруем мусор
                    model_ids = []
                    skip_prefixes = ("ft:", "file-", "babbage", "davinci", "curie", "ada", "text-")
                    for m in raw_models:
                        model_id = m.get("id", "")
                        if not model_id or len(model_id) < 2:
                            continue
                        if any(model_id.startswith(p) for p in skip_prefixes):
                            continue
                        model_ids.append(model_id)

                    if not model_ids:
                        return {"success": False, "models": [], "count": 0,
                                "error": "Не найдено подходящих моделей"}

                    # Обновляем список моделей провайдера
                    if self.providers.get("abacus"):
                        self.providers["abacus"]["models"] = model_ids
                        self._save_keys()

                    print(f"  [keys] Abacus.AI: загружено {len(model_ids)} моделей с API")
                    try:
                        logger.log("abacus_models_fetched", level="success", source="keys_manager",
                                   details={"count": len(model_ids)})
                    except Exception:
                        pass

                    return {
                        "success": True,
                        "models": model_ids,
                        "count": len(model_ids),
                        "error": "",
                    }

        except aiohttp.ClientError as e:
            return {"success": False, "models": [], "count": 0,
                    "error": f"Не удалось подключиться к Abacus.AI: {str(e)[:150]}"}
        except Exception as e:
            return {"success": False, "models": [], "count": 0,
                    "error": str(e)[:200]}

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
        Returns: [{id, name, model, provider, provider_name, type, status, category, thinking}]
        """
        models = []

        # 1. Платные модели из настроенных провайдеров (с валидными ключами)
        for provider_id, config in self.providers.items():
            provider_def = PROVIDER_DEFS.get(provider_id, {})
            prefix = config.get("litellm_prefix", provider_id)
            status = config.get("status", "not_configured")
            thinking_models = provider_def.get("thinking_models", [])
            categories = provider_def.get("model_categories", {})

            for model_name in config.get("models", []):
                # Determine category for this model
                category = ""
                for cat_id, cat_info in categories.items():
                    if model_name in cat_info.get("models", []):
                        category = cat_info.get("label", cat_id)
                        break

                models.append({
                    "id": f"{provider_id}__{model_name}",
                    "name": model_name,
                    "model": f"{prefix}/{model_name}",
                    "provider": provider_id,
                    "provider_name": provider_def.get("name", provider_id),
                    "type": "paid",
                    "status": status,
                    "category": category,
                    "thinking": model_name in thinking_models,
                })

            # Extended Thinking: генерируем -thinking варианты для Abacus
            if provider_id == "abacus" and thinking_models:
                abacus_models = config.get("models", [])
                for tm in thinking_models:
                    if tm in abacus_models:
                        models.append({
                            "id": f"{provider_id}__{tm}-thinking",
                            "name": f"{tm}-thinking",
                            "model": f"{prefix}/{tm}-thinking",
                            "provider": provider_id,
                            "provider_name": provider_def.get("name", provider_id),
                            "type": "paid",
                            "status": status,
                            "category": "Extended Thinking",
                            "thinking": True,
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

        # 3. Бесплатные модели (OpenRouter :free)
        or_key = self.providers.get("openrouter", {}).get("api_key", "")
        or_status = self.providers.get("openrouter", {}).get("status", "not_configured")
        for fm in FREE_MODELS:
            models.append({
                "id": fm["id"],
                "name": fm["name"],
                "model": fm["model"],
                "provider": "openrouter",
                "provider_name": "OpenRouter",
                "type": "free",
                "status": "valid" if or_key else "no_key",
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
        Returns: {model, api_key, api_base, thinking} или None
        
        Поддерживает форматы:
          - provider__model_name (напр. claude__claude-sonnet-4-20250514)
          - provider__model_name-thinking (Abacus extended thinking)
          - bare model_name (напр. claude-sonnet-4-20250514) — ищет по всем провайдерам
          - free model ID (напр. gemini-2.5-flash-free)
          - local/custom model ID
        
        api_base включается ТОЛЬКО для кастомных/нестандартных провайдеров.
        """
        # Abacus Extended Thinking: model_id = "abacus__claude-opus-4-7-thinking"
        _thinking = False
        if model_id.endswith("-thinking") and "__" in model_id:
            parts = model_id.rsplit("-thinking", 1)
            base_id = parts[0]
            # Проверяем что базовая модель существует у Abacus
            provider_part = base_id.split("__", 1)
            if len(provider_part) == 2:
                pid, mname = provider_part
                pdef = PROVIDER_DEFS.get(pid, {})
                if mname in pdef.get("thinking_models", []):
                    model_id = base_id  # Поиск будет по базовой модели
                    _thinking = True

        # 0. Bare model name fallback: если model_id не содержит "__" и не matches local/free/custom
        if "__" not in model_id and not model_id.startswith("local_") and not model_id.startswith("custom_"):
            # Проверяем — это может быть голое имя модели из config.yaml
            for provider_id, config in self.providers.items():
                if config.get("status") in ("valid", "rate_limited"):
                    for model_name in config.get("models", []):
                        if model_name == model_id:
                            # Найдено! Возвращаем конфиг
                            prefix = config.get("litellm_prefix", provider_id)
                            provider_def = PROVIDER_DEFS.get(provider_id, {})
                            litellm_name = f"{prefix}/{model_name}"
                            if _thinking:
                                litellm_name += "-thinking"
                            result = {
                                "model": litellm_name,
                                "api_key": config.get("api_key", ""),
                            }
                            if _thinking:
                                result["thinking"] = True
                            is_custom = provider_def.get("is_custom", False)
                            default_base = provider_def.get("api_base", "")
                            current_base = config.get("api_base", "")
                            if is_custom or (current_base and current_base != default_base):
                                result["api_base"] = current_base
                            return result

        # 1. Платные модели (format: provider__model_name)
        for provider_id, config in self.providers.items():
            for model_name in config.get("models", []):
                if f"{provider_id}__{model_name}" == model_id:
                    prefix = config.get("litellm_prefix", provider_id)
                    provider_def = PROVIDER_DEFS.get(provider_id, {})
                    litellm_name = f"{prefix}/{model_name}"
                    if _thinking:
                        litellm_name += "-thinking"
                    result = {
                        "model": litellm_name,
                        "api_key": config.get("api_key", ""),
                    }
                    if _thinking:
                        result["thinking"] = True
                    # Only include api_base for custom providers or when explicitly overridden
                    is_custom = provider_def.get("is_custom", False)
                    default_base = provider_def.get("api_base", "")
                    current_base = config.get("api_base", "")
                    if is_custom or (current_base and current_base != default_base):
                        result["api_base"] = current_base
                    return result

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

        # Бесплатные модели (OpenRouter :free)
        for fm in FREE_MODELS:
            if fm["id"] == model_id:
                or_key = self.providers.get("openrouter", {}).get("api_key", "")
                return {
                    "model": f"openrouter/{fm['model']}",
                    "api_key": or_key,
                    "api_base": "https://openrouter.ai/api/v1",
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
