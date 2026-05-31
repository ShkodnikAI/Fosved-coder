"""
MindCoder v2.0 — Context Compressor
Сжатие контекста для больших проектов: LLM-based + regex fallback.
"""
import re
import litellm
from datetime import datetime, timezone

from core.action_logger import get_logger
logger = get_logger()

from core.memory import CONFIG, save_context_snapshot, delete_old_messages


class ContextCompressor:
    """Compresses long chat histories into compact summaries for large projects."""

    MAX_MESSAGES_BEFORE_COMPRESS = 30
    KEEP_RECENT_MESSAGES = 10

    # Patterns to extract from history (fallback / enrichment)
    FILE_PATTERN = re.compile(r'`([^`]+\.(?:py|ts|js|tsx|jsx|yaml|yml|json|md|sql|sh|html|css|toml|cfg|ini|env))`')
    ERROR_PATTERN = re.compile(r'(?:Error|Exception|Traceback|Ошибка)[\s:]+(.+?)(?:\n|$)', re.IGNORECASE)
    DECISION_PATTERN = re.compile(r"(?:Решение|Решим|Будем|Let's|I'll|Сделаем)[\s:]*(.+?)(?:\.|\n|$)", re.IGNORECASE)

    # ─────────────────────────────────────────────
    #  PUBLIC API
    # ─────────────────────────────────────────────

    def should_compress(self, messages: list[dict]) -> bool:
        """Check if context compression is needed."""
        return len(messages) > self.MAX_MESSAGES_BEFORE_COMPRESS

    async def compress(self, messages: list[dict], project_id: int = None,
                       use_llm: bool = True, model_config: dict = None) -> tuple[str, list[dict], bool]:
        """
        Compress message history.
        Returns (compressed_summary, remaining_messages, was_llm_used).

        Strategy:
        1. If use_llm and model_config available → LLM-based smart compression
        2. Otherwise → regex fallback (current behaviour)
        """
        if not self.should_compress(messages):
            return "", messages, False

        old_count = len(messages)
        old_messages = messages[:-self.KEEP_RECENT_MESSAGES]
        recent_messages = messages[-self.KEEP_RECENT_MESSAGES:]

        try:
            logger.log("compress_start", level="info", source="compressor",
                       details={"project_id": project_id, "messages_before": old_count,
                                "messages_to_compress": len(old_messages), "use_llm": use_llm})
        except Exception:
            pass

        if use_llm and model_config:
            try:
                summary = await self._compress_with_llm(old_messages, model_config)
                if summary:
                    print(f"  [compressor] LLM сжатие выполнено: {len(old_messages)} → {len(summary)} символов")
                    try:
                        logger.log("compress_llm_success", level="success", source="compressor",
                                   details={"project_id": project_id,
                                            "messages_before": old_count, "messages_after": len(recent_messages),
                                            "summary_chars": len(summary), "method": "llm"})
                    except Exception:
                        pass
                    # Persist snapshot in DB
                    if project_id:
                        await self._save_snapshot(project_id, summary, len(messages), len(recent_messages), "auto_llm")
                    return summary, recent_messages, True
            except Exception as e:
                print(f"  [compressor] LLM сжатие не удалось: {e} — fallback на regex")
                try:
                    logger.log("compress_llm_failed", level="warning", source="compressor",
                               details={"project_id": project_id}, error=str(e))
                except Exception:
                    pass

        # Fallback: regex compression
        summary = self._compress_with_regex(old_messages)
        if project_id and summary:
            await self._save_snapshot(project_id, summary, len(messages), len(recent_messages), "auto_regex")
        try:
            logger.log("compress_regex_done", level="info", source="compressor",
                       details={"project_id": project_id, "method": "regex",
                                "messages_before": old_count, "messages_after": len(recent_messages),
                                "summary_chars": len(summary) if summary else 0})
        except Exception:
            pass
        return summary, recent_messages, False

    async def compress_and_cleanup(self, messages: list[dict], project_id: int,
                                    model_config: dict = None) -> tuple[str, list[dict], bool]:
        """
        Compress + actually delete old messages from DB.
        Returns (summary, remaining, was_llm).
        """
        summary, remaining, was_llm = await self.compress(
            messages, project_id=project_id, use_llm=True, model_config=model_config
        )
        if summary:
            archived = await delete_old_messages(project_id, keep_last=self.KEEP_RECENT_MESSAGES)
            print(f"  [compressor] Архивировано {archived} старых сообщений (сохранены для поиска)")
        return summary, remaining, was_llm

    def build_compressed_system_prompt(self, original_prompt: str, compressed_summary: str) -> str:
        """Inject compressed context into the system prompt."""
        if not compressed_summary:
            return original_prompt
        return original_prompt + "\n\n--- ПРЕДЫДУЩИЙ КОНТЕКСТ (СЖАТ) ---\n" + compressed_summary + "\n--- КОНЕЦ СЖАТОГО КОНТЕКСТА ---"

    # ─────────────────────────────────────────────
    #  LLM-BASED COMPRESSION
    # ─────────────────────────────────────────────

    async def _compress_with_llm(self, old_messages: list[dict], model_config: dict) -> str:
        """
        Use a fast LLM to create an intelligent summary of old messages.
        The model should be cheap and fast — we use a separate call.
        """
        # Build conversation text for LLM
        conversation_parts = []
        for msg in old_messages:
            role = msg.get("role", "user")
            role_label = {"user": "👤", "ai": "🤖", "assistant": "🤖"}.get(role, role)
            content = msg.get("content", "")
            # Truncate very long messages to keep the prompt reasonable
            if len(content) > 600:
                content = content[:600] + "..."
            conversation_parts.append(f"{role_label}: {content}")

        conversation_text = "\n".join(conversation_parts)
        message_count = len(old_messages)

        compression_prompt = f"""Проанализируй эту историю чата между пользователем и AI-помощником (кодинг ассистент).

{conversation_text}

---

Создай КОМПАКТНУЮ СУММАРИЗАЦИЮ этой истории. Формат — структурированный блок:

📝 ТЕМА: [основная тема/задача проекта за эти {message_count} сообщений]
📁 ФАЙЛЫ: [перечисли ключевые файлы, которые обсуждались или изменялись]
🐛 ОШИБКИ: [перечисли ошибки/баги, которые были найдены и исправлены]
✅ РЕШЕНИЯ: [ключевые архитектурные и кодовые решения, которые были приняты]
📋 ПРОГРЕСС: [что было сделано, что в процессе, что запланировано]

Правила:
- Будь кратким и конкретным — каждый пункт 1-2 предложения
- Упоминай только важные файлы (не каждый отдельный файл)
- Если что-то не релевантно (например, ошибок не было) — пропусти этот раздел
- Пиши на том языке, на котором велась беседа (чаще всего русский)
- Максимальная длина: 300 слов"""

        model = model_config.get("model", "")
        api_key = model_config.get("api_key", "")
        api_base = model_config.get("api_base", "")

        if not api_key:
            raise ValueError("Нет API ключа для LLM-сжатия")

        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": compression_prompt}],
            "temperature": 0.1,  # Low temp for factual summary
            "max_tokens": 800,   # Summary should be compact
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        response = await litellm.acompletion(**kwargs)
        summary = response.choices[0].message.content.strip()

        if not summary or len(summary) < 30:
            raise ValueError(f"LLM вернул слишком короткую суммаризацию: {len(summary)} символов")

        # Add metadata header
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        final = f"[Сжатый контекст — {now} | LLM-суммаризация {message_count} сообщений]\n\n{summary}"
        return final

    # ─────────────────────────────────────────────
    #  REGEX FALLBACK COMPRESSION
    # ─────────────────────────────────────────────

    def _compress_with_regex(self, old_messages: list[dict]) -> str:
        """Original regex-based compression (fallback when LLM unavailable)."""
        files_mentioned = set()
        errors_found = []
        decisions_made = []

        for msg in old_messages:
            text = msg.get("content", "")
            files_mentioned.update(self.FILE_PATTERN.findall(text))
            errors_found.extend(self.ERROR_PATTERN.findall(text)[:5])
            decisions_made.extend(self.DECISION_PATTERN.findall(text)[:5])

        summary_lines = [
            f"[Сжатый контекст — {datetime.now(timezone.utc).strftime('%H:%M:%S')} | Regex]",
            f"Предыдущих сообщений: {len(old_messages)}",
        ]

        if files_mentioned:
            summary_lines.append(f"Файлы ({len(files_mentioned)}): {', '.join(list(files_mentioned)[:20])}")
        if errors_found:
            summary_lines.append(f"Ошибки ({len(errors_found)}): {'; '.join(errors_found[:5])}")
        if decisions_made:
            summary_lines.append(f"Решения: {'; '.join(decisions_made[:5])}")

        # Add key user requests
        user_msgs = [m for m in old_messages if m["role"] == "user"]
        if user_msgs:
            summary_lines.append("Запросы пользователя:")
            for m in user_msgs[-5:]:
                text = m["content"][:150].replace("\n", " ")
                summary_lines.append(f"  - {text}")

        return "\n".join(summary_lines)

    # ─────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────

    async def _save_snapshot(self, project_id: int, summary: str,
                              before: int, after: int, snapshot_type: str):
        """Persist compression snapshot in DB."""
        try:
            await save_context_snapshot(
                project_id=project_id,
                thread_id=None,
                snapshot_type=snapshot_type,
                title=f"Сжатие контекста ({before} → {after})",
                summary=summary,
                key_decisions="",
                file_changes="",
                errors_fixed="",
                message_count_before=before,
                message_count_after=after,
            )
        except Exception as e:
            print(f"  [compressor] Не удалось сохранить snapshot: {e}")

    @staticmethod
    def get_compression_model_config() -> dict | None:
        """
        Find a suitable model config for compression.
        Priority: any valid key model → cheapest available.
        Returns model_config dict or None.
        """
        from core.keys_manager import keys_manager, PROVIDER_DEFS

        # 1. Try any model with a valid key (pick the first available)
        all_models = keys_manager.get_all_models()
        for m in all_models:
            if m.get("status") in ("valid", "available"):
                model_type = m.get("type", "")
                # Пропускаем модели без ключа (free но без ключа, local без сервера)
                if model_type in ("local",) and not m.get("base_url"):
                    continue
                config = keys_manager.get_model_config(m["id"])
                if config and config.get("api_key"):
                    return config

        return None
