from pydantic import BaseModel
from core.memory import CONFIG, save_routing_stat
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
    """Hybrid task router: keyword matching first, AI-based routing as fallback."""

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

        # Step 1: Keyword matching — simple tasks → cheap model
        for kw in self.KEYWORDS_SIMPLE:
            if kw in prompt_lower:
                return RouterResult(
                    subtasks=[SubTask(
                        prompt=user_prompt,
                        model=CONFIG["llm"]["router_model"],
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
                        model=CONFIG["llm"]["default_model"],
                        reason=f"Keyword match: '{kw}' — complex task"
                    )],
                    routing_method="keyword"
                )

        # Step 3: AI Manager fallback for ambiguous tasks
        return await self._ai_route(user_prompt, project_context)

    async def _ai_route(self, user_prompt: str, project_context: str) -> RouterResult:
        """Use cheap AI model to analyze and route the task."""
        available_models = (
            f"Дорогие: {CONFIG['llm']['default_model']}\n"
            f"Дешёвые: {CONFIG['llm']['router_model']}"
        )

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

            response = await litellm.acompletion(
                model=CONFIG["llm"]["router_model"],
                messages=messages,
                api_base=CONFIG["llm"]["api_base"],
                api_key=CONFIG["llm"]["api_key"],
                temperature=0.1,
                max_tokens=500
            )

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
                CONFIG["llm"]["default_model"],
                f"AI routing failed: {e}",
                False
            )

            return RouterResult(
                subtasks=[SubTask(
                    prompt=user_prompt,
                    model=CONFIG["llm"]["default_model"],
                    reason="Fallback: AI routing failed"
                )],
                routing_method="fallback"
            )
