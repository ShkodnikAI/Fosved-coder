from pydantic import BaseModel
from core.memory import CONFIG, save_routing_stat
from core.keys_manager import keys_manager
import json
import litellm

litellm.suppress_debug_info = True


class SubTask(BaseModel):
    prompt: str
    model: str
    reason: str


class RouterResult(BaseModel):
    subtasks: list[SubTask]
    routing_method: str  # "keyword", "ai_manager", or "fallback"


class HybridRouter:
    """
    Hybrid task router: keyword matching first, AI-based routing as fallback.

    В v2.0 используется через agent.py::_route_with_priority()
    для выбора оптимальной модели из приоритетного списка проекта.
    """

    KEYWORDS_SIMPLE = [
        "fix typo", "формат", "xml", "json", "тест", "docstring",
        "комментарий", "простой", "trivial", "rename", "lint",
        "semicolon", "indent", "whitespace", "небольшой"
    ]

    KEYWORDS_COMPLEX = [
        "архитектур", "refactor", "redesign", "систем", "framework",
        "engine", "парсером", "compiler", "параллельн", "микросервис",
        "database schema", "модель данных", "api дизайн", "security",
        "аутентификац", "интеграц", "многопоточ", "async"
    ]

    async def route_task(self, user_prompt: str, project_context: str = "") -> RouterResult:
        """Route task using hybrid approach: keywords first, then AI manager."""
        prompt_lower = user_prompt.lower()

        # Get available models from keys_manager
        all_models = keys_manager.get_all_models()
        paid_models = [m for m in all_models if m["type"] == "paid" and m["status"] in ("valid", "rate_limited")]
        default_model = paid_models[0]["id"] if paid_models else CONFIG["llm"].get("default_model", "gpt-4o")

        # Find a cheap/free model for simple tasks
        free_models = [m for m in all_models if m["type"] == "free" and m["status"] == "available"]
        cheap_model = free_models[0]["id"] if free_models else default_model

        # Step 1: Keyword matching — simple tasks → cheap model
        for kw in self.KEYWORDS_SIMPLE:
            if kw in prompt_lower:
                return RouterResult(
                    subtasks=[SubTask(
                        prompt=user_prompt,
                        model=cheap_model,
                        reason=f"Keyword match: '{kw}' — routine task"
                    )],
                    routing_method="keyword"
                )

        # Step 2: Keyword matching — complex tasks → expensive model
        for kw in self.KEYWORDS_COMPLEX:
            if kw in prompt_lower:
                return RouterResult(
                    subtasks=[SubTask(
                        prompt=user_prompt,
                        model=default_model,
                        reason=f"Keyword match: '{kw}' — complex task"
                    )],
                    routing_method="keyword"
                )

        # Step 3: AI Manager fallback for ambiguous tasks
        return await self._ai_route(user_prompt, project_context, default_model, cheap_model)

    async def _ai_route(self, user_prompt: str, project_context: str, default_model: str, cheap_model: str) -> RouterResult:
        """Use AI model to analyze and route the task."""
        available_models = (
            f"Дорогие: {default_model}\n"
            f"Дешёвые: {cheap_model}"
        )

        # Get model config for the cheap model to use as router
        router_config = keys_manager.get_model_config(cheap_model)
        if not router_config:
            # Fallback to first available paid model
            router_config = keys_manager.get_model_config(default_model)

        system_prompt = f"""Ты ИИ-менеджер. Проанализируй задачу разработчика и реши, 
какую модель использовать. Доступные модели:
{available_models}

Правила:
- Рутинные задачи (форматирование, простые правки, тесты) -> дешёвая модель
- Сложные задачи (архитектура, рефакторинг, сложная логика) -> дорогая модель

Ответь ТОЛЬКО валидным JSON (без markdown обёрток):
{{"subtasks": [{{"prompt": "подзадача", "model": "имя_модели", "reason": "почему"}}]}}"""

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Задача: {user_prompt}\n\nКонтекст проекта: {project_context[:2000]}"}
            ]

            kwargs = {
                "model": router_config["model"] if router_config else cheap_model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 500
            }
            if router_config:
                if router_config.get("api_key"):
                    kwargs["api_key"] = router_config["api_key"]
                if router_config.get("api_base"):
                    kwargs["api_base"] = router_config["api_base"]

            response = await litellm.acompletion(**kwargs)

            content = response.choices[0].message.content.strip()

            # Try to extract JSON from response (handle markdown code blocks)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            data = json.loads(content)
            subtasks = [SubTask(**st) for st in data["subtasks"]]

            # Save routing stat for analytics
            for st in subtasks:
                await save_routing_stat(user_prompt[:100], st.model, st.reason, True)

            return RouterResult(subtasks=subtasks, routing_method="ai_manager")

        except (json.JSONDecodeError, KeyError, Exception) as e:
            # Fallback to default model on any parsing/API error
            await save_routing_stat(
                user_prompt[:100],
                default_model,
                f"AI routing failed: {e}",
                False
            )

            return RouterResult(
                subtasks=[SubTask(
                    prompt=user_prompt,
                    model=default_model,
                    reason="Fallback: AI routing failed"
                )],
                routing_method="fallback"
            )
