"""
Fosved Coder v2.0 — Context Compressor
Сжатие контекста для больших проектов: извлечение ключевых файлов, ошибок, решений.
"""
import re
from datetime import datetime


class ContextCompressor:
    """Compresses long chat histories into compact summaries for large projects."""

    MAX_MESSAGES_BEFORE_COMPRESS = 30
    KEEP_RECENT_MESSAGES = 10

    # Patterns to extract from history
    FILE_PATTERN = re.compile(r'`([^`]+\.(?:py|ts|js|tsx|jsx|yaml|yml|json|md|sql|sh|html|css))`')
    ERROR_PATTERN = re.compile(r'(?:Error|Exception|Traceback|Ошибка)[\s:]+(.+?)(?:\n|$)', re.IGNORECASE)
    DECISION_PATTERN = re.compile(r"(?:Решение|Решим|Будем|Let's|I'll|Сделаем)[\s:]*(.+?)(?:\.|\n|$)", re.IGNORECASE)

    def should_compress(self, messages: list[dict]) -> bool:
        """Check if context compression is needed."""
        return len(messages) > self.MAX_MESSAGES_BEFORE_COMPRESS

    def compress(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        Compress message history. Returns (compressed_summary, remaining_messages).
        Keeps the most recent KEEP_RECENT_MESSAGES messages uncompressed.
        """
        if not self.should_compress(messages):
            return "", messages

        old_messages = messages[:-self.KEEP_RECENT_MESSAGES]
        recent_messages = messages[-self.KEEP_RECENT_MESSAGES:]

        # Extract key information
        files_mentioned = set()
        errors_found = []
        decisions_made = []

        for msg in old_messages:
            text = msg.get("content", "")
            files_mentioned.update(self.FILE_PATTERN.findall(text))
            errors_found.extend(self.ERROR_PATTERN.findall(text)[:5])
            decisions_made.extend(self.DECISION_PATTERN.findall(text)[:5])

        # Build compressed summary
        summary_lines = [
            f"[Сжатый контекст — {datetime.now().strftime('%H:%M:%S')}]",
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

        summary = "\n".join(summary_lines)
        return summary, recent_messages

    def build_compressed_system_prompt(self, original_prompt: str, compressed_summary: str) -> str:
        """Inject compressed context into the system prompt."""
        if not compressed_summary:
            return original_prompt
        return original_prompt + "\n\n--- ПРЕДЫДУЩИЙ КОНТЕКСТ (СЖАТ) ---\n" + compressed_summary + "\n--- КОНЕЦ СЖАТОГО КОНТЕКСТА ---"
