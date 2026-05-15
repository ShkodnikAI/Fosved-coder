"""
Fosved Coder V2.0 — Intelligent Router (Task 5)
Интеллектуальный маршрутизатор: классифицирует задачи пользователя
и направляет их на подходящую модель (бесплатная или лидер).

Классификация:
  ПРОСТЫЕ → бесплатная модель (из тех что прошли probe)
  СЛОЖНЫЕ → модель-лидер (GPT-4, Claude, Gemini Pro и т.д.)

Работает прозрачно: пользователь пишет как обычно, система сама решает.
Все действия отображаются в лог-панели.
"""
from core.action_logger import get_logger
logger = get_logger()


# ═══════════════════════════════════════════════════════════════
# КЛЮЧЕВЫЕ СЛОВА ДЛЯ КЛАССИФИКАЦИИ
# ═══════════════════════════════════════════════════════════════

# Сигналы ПРОСТОЙ задачи → отправка на бесплатную модель
SIMPLE_KEYWORDS = [
    # Приветствия
    "привет", "здравствуй", "хай", "добрый день", "доброе утро", "добрый вечер",
    "hello", "hi", "hey", "good morning", "good evening",
    # Форматирование / правка текста
    "переформулируй", "перепиши", "сократи", "расширь", "исправь опечатк",
    "format", "rephrase", "rewrite", "summarize", "summarise",
    "сделай короче", "сделай длиннее", "упрости текст",
    # Перевод
    "переведи", "перевод", "translate", "translation",
    "как будет по-английски", "how to say in", "как сказать",
    # Простой поиск / вопрос
    "что такое", "кто такой", "что значит", "what is", "who is", "define",
    " объясни прост", "расскажи прост",
    # Форматирование кода (не написание)
    "отформатируй", "прочитай", "explain this code", "что делает этот код",
    # Короткие вопросы
    "сколько будет", "как написать", "как использовать", "how to use",
    # Генерация текста (не кода)
    "напиши текст", "напиши письмо", "напиши email", "составь сообщение",
    "write an email", "write a message", "сочини",
]

# Сигналы СЛОЖНОЙ задачи → отправка на модель-лидера
COMPLEX_KEYWORDS = [
    # Написание кода
    "напиши код", "создай функцию", "реализуй", "напиши класс",
    "write code", "implement", "create function", "build",
    "code for", "function that", "class that", "напиши скрипт",
    "write a script", "разработай", "develop", "программ",
    # Архитектура
    "архитектур", "архитектура", "sp design", "design pattern",
    "структура проект", "project structure", "system design",
    # Анализ
    "проанализируй", "анализ", "analyze", "audit",
    "review код", "code review", "find bug", "найди ошибку",
    # Рефакторинг
    "рефактор", "refactor", "оптимизируй код", "optimize",
    "улучши производительность", "improve performance",
    # Дебаг сложных ошибок
    "дебаг", "debug", "почему не работает", "why doesn't",
    "ошибк", "error", "exception", "traceback", "fix bug",
    "исправь ошибку", "почему падает",
    # Интеграция
    "интеграц", "integration", "api", "endpoint",
    "подключи", "connect to", "настрой",
    # Базы данных
    "базу данных", "database", "sql", "миграц", "migration",
    "схему", "schema",
    # Тестирование
    "напиши тест", "write test", "test suite", "unit test",
    # Многопоточность / асинхронность
    "асинхрон", "async", "многопоточ", "multithread", "concurrent",
    # Безопасность
    "безопасност", "security", "аутентификац", "auth",
    "авторизац", "authorization",
]

# Модели-лидеры (приоритет): если доступна → использовать для сложных задач
LEADER_MODEL_PATTERNS = [
    "claude-opus", "claude-sonnet",   # Claude (Anthropic)
    "gpt-4", "gpt-5", "o3", "o4",    # OpenAI
    "gemini-2.5-pro", "gemini-3",     # Google Gemini Pro
    "grok-4", "grok-3",               # xAI Grok
    "deepseek-reasoner",              # DeepSeek Reasoner
    "route-llm",                       # Abacus RouteLLM (smart routing)
    "qwen3-coder", "qwen3-235b",     # Qwen coding
]


class IntelligentRouter:
    """
    Интеллектуальный маршрутизатор задач.
    Классифицирует входящее сообщение и выбирает оптимальную модель.
    """

    def __init__(self):
        self._classification_cache: dict = {}  # Кэш последних классификаций

    def classify(self, user_prompt: str) -> dict:
        """
        Классифицировать задачу пользователя.

        Returns:
            {
                "complexity": "simple" | "complex",
                "confidence": float (0.0 - 1.0),
                "reason": str,
                "keywords": list[str],
            }
        """
        prompt_lower = user_prompt.lower().strip()
        prompt_len = len(user_prompt)

        # ─── Быстрые проверки ───

        # 1. Очень короткие сообщения (< 20 символов) — скорее всего простые
        if prompt_len < 20 and not any(kw in prompt_lower for kw in COMPLEX_KEYWORDS):
            return self._result("simple", 0.8, "Короткое сообщение — вероятно простой запрос")

        # 2. Проверка на сложные ключевые слова (приоритет)
        complex_matches = [kw for kw in COMPLEX_KEYWORDS if kw in prompt_lower]

        # 3. Проверка на простые ключевые слова
        simple_matches = [kw for kw in SIMPLE_KEYWORDS if kw in prompt_lower]

        # 4. Приоритет сложных над простыми
        if complex_matches and not simple_matches:
            return self._result("complex", 0.85,
                               f"Сложная задача (ключевые слова: {', '.join(complex_matches[:3])})",
                               complex_matches)

        if simple_matches and not complex_matches:
            return self._result("simple", 0.8,
                               f"Простая задача (ключевые слова: {', '.join(simple_matches[:3])})",
                               simple_matches)

        # 5. Конфликт: есть и простые и сложные — смотрим что dominates
        if complex_matches and simple_matches:
            # Если сложных больше или сообщение длинное — сложная
            if len(complex_matches) >= len(simple_matches) or prompt_len > 200:
                return self._result("complex", 0.7,
                                   f"Смешанная задача → сложная (complex: {len(complex_matches)}, simple: {len(simple_matches)})",
                                   complex_matches)
            else:
                return self._result("simple", 0.6,
                                   f"Смешанная задача → простая (simple: {len(simple_matches)}, complex: {len(complex_matches)})",
                                   simple_matches)

        # 6. Евристики по длине и содержимому
        # Наличие блоков кода — сложная задача
        if "```" in user_prompt or user_prompt.count("\n") > 10:
            return self._result("complex", 0.75, "Содержит код или длинное сообщение")

        # Вопрос с кодом (фигурные скобки, угловые скобки, отступы)
        code_indicators = sum([
            1 for ch in prompt_lower
            if ch in "{}()<>;[]=+"
        ])
        if code_indicators > 5:
            return self._result("complex", 0.7, f"Техническое содержание (code symbols: {code_indicators})")

        # Средняя длина без ключевых слов — по умолчанию простая
        if prompt_len < 100:
            return self._result("simple", 0.5, "Короткий запрос без явных сигналов сложности")

        # Длинное сообщение без явных сигналов — assumed complex (better safe)
        if prompt_len > 300:
            return self._result("complex", 0.55, "Длинное сообщение — предполагаем сложную задачу")

        # По умолчанию — простая
        return self._result("simple", 0.4, "Нет явных сигналов — по умолчанию простая задача")

    def select_model(
        self,
        user_prompt: str,
        available_models: list[dict],
        user_preferred_model: str = None,
        probed_model_ids: set = None,
        failed_probe_ids: set = None,
        has_been_probed: bool = False,
        priority_models: list = None,
        in_project_context: bool = False,
    ) -> dict:
        """
        Выбрать модель на основе классификации задачи.

        Args:
            user_prompt: текст сообщения пользователя
            available_models: список моделей от keys_manager.get_all_models()
            user_preferred_model: ID модели, выбранной пользователем вручную
            probed_model_ids: множество ID моделей, прошедших probe
            failed_probe_ids: множество ID моделей, НЕ прошедших probe
            has_been_probed: True если probe когда-либо запускался
            priority_models: список ID моделей в порядке приоритета (от клиента)
            in_project_context: True если запрос в контексте проекта (кодовые задачи)

        Returns:
            {
                "model_id": str,
                "model_name": str,
                "complexity": str,
                "reason": str,
                "overridden": bool,  # True если маршрутизатор изменил выбор пользователя
            }
        """
        # Если пользователь явно выбрал модель — не перезаписываем
        # (маршрутизатор работает только когда модель не выбрана)
        if user_preferred_model:
            return {
                "model_id": user_preferred_model,
                "model_name": user_preferred_model,
                "complexity": "user_selected",
                "reason": "Модель выбрана пользователем вручную",
                "overridden": False,
            }

        # Если пользователь выставил приоритеты — берём первую приоритетную модель
        # Сначала из тех, что прошли probe, затем любую валидную
        if priority_models:
            probed_set = probed_model_ids or set()
            for pm_id in priority_models:
                # Сначала пробуем из проверенных
                if pm_id in probed_set:
                    m = next((x for x in available_models if x.get("id") == pm_id), None)
                    if m and m.get("status") in ("valid", "available", "rate_limited"):
                        return {
                            "model_id": m["id"],
                            "model_name": m.get("name", m["id"]),
                            "complexity": "priority",
                            "reason": f"Приоритетная модель #{priority_models.index(pm_id)+1}: {m.get('name', m['id'])}",
                            "overridden": True,
                        }
            # Если ни одна приоритетная не в проверенных — берём первую валидную
            # (пользователь явно указал приоритет — уважаем его выбор)
            for pm_id in priority_models:
                m = next((x for x in available_models if x.get("id") == pm_id), None)
                if m and m.get("status") in ("valid", "available", "rate_limited"):
                    return {
                        "model_id": m["id"],
                        "model_name": m.get("name", m["id"]),
                        "complexity": "priority",
                        "reason": f"Приоритетная модель #{priority_models.index(pm_id)+1} (непроверенная): {m.get('name', m['id'])}",
                        "overridden": True,
                    }

        classification = self.classify(user_prompt)
        complexity = classification["complexity"]

        # В контексте проекта — повышаем сложность (пользователь работает с кодом)
        if in_project_context and complexity == "simple":
            trivial_keywords = ["привет", "здравствуй", "хай", "спасибо", "пока", "hello", "hi"]
            if not any(kw in user_prompt.lower() for kw in trivial_keywords):
                if classification["confidence"] < 0.8:
                    complexity = "complex"
                    classification["reason"] += " (повышено: контекст проекта)"

        # ── ФИКС: если probe БЫЛ запущен, но НЕТ проверенных моделей ──
        # НЕ возвращаем пустой результат — позволяем fallback на любые валидные модели
        # (лучше попробовать любую модель, чем ничего)
        # Убираем ранний return — логика ниже сама обработает пустой probed_model_ids

        # ── Фильтрация моделей ──
        # Пропускаем только проваленные при probe, остальные — доступны

        # Разделяем модели на лидеров и бесплатные
        leader_models = []
        free_models = []

        for m in available_models:
            status = m.get("status", "")
            model_id = m.get("id", "")
            model_name = m.get("name", "")
            model_type = m.get("type", "")

            # Пропускаем неработающие модели
            if status in ("invalid", "no_key"):
                continue

            # Если probe запускался — пропускаем ТОЛЬКО проваленные модели
            # НЕ пропускаем непроверенные — они могут работать!
            if failed_probe_ids and model_id in failed_probe_ids:
                continue

            # Определяем: это модель-лидер?
            is_leader = any(
                pattern in model_id or pattern in model_name
                for pattern in LEADER_MODEL_PATTERNS
            )

            if is_leader and status in ("valid", "rate_limited"):
                leader_models.append(m)
            elif model_type == "free" and status in ("valid", "available", "rate_limited"):
                free_models.append(m)

        # Выбор модели (с fallback на любые валидные)
        if complexity == "simple":
            # Простая задача → сначала бесплатная, fallback на лидера
            if free_models:
                chosen = free_models[0]
                reason = f"Простая задача → бесплатная модель: {chosen['name']}"
            elif leader_models:
                chosen = leader_models[0]
                reason = f"Простая задача, но нет бесплатных → лидер: {chosen['name']}"
            else:
                # Fallback: любая валидная модель (не только проверенные)
                checked = [m for m in available_models
                           if m.get("status") in ("valid", "available", "rate_limited")
                           and m.get("id") not in (failed_probe_ids or set())]
                chosen = checked[0] if checked else None
                reason = f"Fallback: {chosen['name'] if chosen else 'нет доступных моделей'}"
        else:
            # Сложная задача → сначала лидер, fallback на любую платную
            if leader_models:
                chosen = leader_models[0]
                reason = f"Сложная задача → модель-лидер: {chosen['name']}"
            else:
                # Нет лидеров — берём любую валидную
                paid = [m for m in available_models
                        if m.get("status") in ("valid", "rate_limited")
                        and m.get("id") not in (failed_probe_ids or set())]
                chosen = paid[0] if paid else None
                reason = f"Сложная задача, лидеры недоступны → {chosen['name'] if chosen else 'нет доступных моделей'}"

        if not chosen:
            return {
                "model_id": "",
                "model_name": "",
                "complexity": complexity,
                "reason": "Нет доступных моделей",
                "overridden": False,
            }

        # Логируем решение маршрутизатора
        try:
            logger.log(
                "intelligent_route",
                level="info",
                source="intelligent_router",
                details={
                    "complexity": complexity,
                    "confidence": classification["confidence"],
                    "chosen_model": chosen["id"],
                    "reason": reason,
                },
            )
        except Exception:
            pass

        return {
            "model_id": chosen["id"],
            "model_name": chosen["name"],
            "complexity": complexity,
            "reason": reason,
            "overridden": True,
        }

    def _result(self, complexity: str, confidence: float, reason: str, keywords: list = None) -> dict:
        """Создать результат классификации."""
        result = {
            "complexity": complexity,
            "confidence": round(confidence, 2),
            "reason": reason,
            "keywords": keywords or [],
        }
        return result


# Глобальный экземпляр маршрутизатора
intelligent_router = IntelligentRouter()
