"""
core/prompt_injector.py — "Прокладка" (Middleware)
Инжектирует контекст проекта (файлы, git status, структура) прямо в промпт модели.
Модель НЕ знает про инструменты — она просто видит контекст задачи и отвечает текстом.

Используется как fallback для моделей без tool calling,
а также как основной режим для prompt injection подхода.
"""

import os
import re
import shlex
from pathlib import Path

from core.action_logger import get_logger

logger = get_logger()

# Директории, которые всегда пропускаем при сканировании
SKIP_DIRS = {
    "venv", "__pycache__", "node_modules", ".git", ".cache",
    ".venv", "env", ".idea", ".vscode", "dist", "build",
    "__pypackages__", ".next", ".nuxt", ".gradle", "target",
    ".tox", ".mypy_cache", ".pytest_cache", "bin", "obj",
    ".turbo", ".vercel", ".netlify", "coverage", ".eggs",
}

# Максимальное кол-во символов одного файла в контексте
MAX_FILE_CHARS = 15000

# Максимальное кол-во файлов в контексте
MAX_FILES = 15

# Максимальный общий размер контекста файлов
MAX_TOTAL_CHARS = 80000


class PromptInjector:
    """Инжектирует контекст проекта в промпт модели."""

    def __init__(self, project_path: str | None = None):
        self.project_path = project_path
        self._file_cache: dict[str, str] = {}

    def set_project(self, project_path: str):
        """Установить путь к проекту."""
        self.project_path = project_path
        self._file_cache.clear()

    async def build_context(
        self,
        project_name: str = "",
        project_description: str = "",
        base_prompt: str = "",
        template: str = "",
        repo_map: str = "",
        include_git_status: bool = True,
        files_to_include: list[str] | None = None,
        include_skills: bool = False,
        skills_dir: str = "skills",
    ) -> str:
        """
        Построить полный контекст для инъекции в промпт.

        Возвращает строку с контекстом проекта, которая подставляется
        в системный промпт или пользовательское сообщение.
        """
        if not self.project_path or not os.path.isdir(self.project_path):
            return self._minimal_context(project_name, project_description)

        sections = []

        # 0. Скиллы (если запрошено)
        if include_skills:
            skill_context = self.build_skill_context(skills_dir)
            if skill_context:
                sections.append(skill_context)

        # 1. Базовая информация о проекте
        project_info = self._build_project_info(
            project_name, project_description, template
        )
        if project_info:
            sections.append(project_info)

        # 2. Git статус (если есть .git)
        if include_git_status:
            git_info = await self._build_git_context()
            if git_info:
                sections.append(git_info)

        # 3. Структура файлов (Repo Map)
        if repo_map:
            sections.append(f"СТРУКТУРА ПРОЕКТА:\n{repo_map}")
        else:
            file_tree = self._build_file_tree()
            if file_tree:
                sections.append(f"СТРУКТУРА ПРОЕКТА:\n{file_tree}")

        # 4. Содержимое конкретных файлов (если запрошено)
        if files_to_include:
            file_contents = self._build_file_contents(files_to_include)
            if file_contents:
                sections.append(file_contents)

        # 5. Инструкции пользователя (base_prompt)
        if base_prompt:
            sections.append(f"ИНСТРУКЦИИ ДЛЯ РАБОТЫ:\n{base_prompt}")

        # Собираем всё вместе
        context = "\n\n".join(sections)

        # Добавляем инструкции по форматированию ответа
        context += "\n\n" + self._response_format_instructions()

        return context

    def _minimal_context(
        self, project_name: str, project_description: str
    ) -> str:
        """Минимальный контекст когда нет пути к проекту."""
        parts = []
        if project_name:
            parts.append(f"ПРОЕКТ: {project_name}")
        if project_description:
            parts.append(f"ОПИСАНИЕ: {project_description}")
        return "\n".join(parts) if parts else ""

    def build_skill_context(self, skills_dir: str = "skills") -> str:
        """
        Сканирует директорию скиллов и строит контекст.

        Для каждого подкаталога в skills_dir ищет skill.md,
        извлекает первую строку (описание) и формирует список.

        Возвращает:
            ## ДОСТУПНЫЕ СКИЛЛЫ:
            - **skill-name**: description
            ...
        """
        if not self.project_path:
            # Пробуем как абсолютный путь
            scan_dir = skills_dir
        else:
            scan_dir = os.path.join(self.project_path, skills_dir)

        if not os.path.isdir(scan_dir):
            return ""

        entries = []
        try:
            for entry in sorted(os.listdir(scan_dir)):
                skill_path = os.path.join(scan_dir, entry)
                if not os.path.isdir(skill_path):
                    continue

                # Пропускаем служебные директории
                if entry.startswith(".") or entry.startswith("_"):
                    continue

                skill_md = os.path.join(skill_path, "skill.md")
                description = ""
                if os.path.isfile(skill_md):
                    try:
                        with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                            first_line = f.readline().strip()
                            if first_line:
                                description = first_line.lstrip("# ")
                    except Exception:
                        pass

                if description:
                    entries.append(f"- **{entry}**: {description}")
                else:
                    entries.append(f"- **{entry}**")
        except Exception as e:
            logger.log(
                f"prompt_injector: skill scan error: {e}",
                level="warning", source="injector",
            )
            return ""

        if not entries:
            return ""

        return "## ДОСТУПНЫЕ СКИЛЛЫ:\n" + "\n".join(entries)

    def build_questionnaire_context(self, questions: list[dict]) -> str:
        """
        Формирует контекст из ответов на вопросы.

        Принимает список словарей: {"question": str, "answer": str, "category": str}
        Группирует по категории и форматирует.

        Возвращает:
            ## ОТВЕТЫ НА ВОПРОСЫ:
            ### Категория
            **Q:** question
            **A:** answer
            ...
        """
        if not questions:
            return ""

        # Группируем по категории
        categories: dict[str, list[dict]] = {}
        for q in questions:
            cat = q.get("category", "Общее").strip() or "Общее"
            categories.setdefault(cat, []).append(q)

        sections = ["## ОТВЕТЫ НА ВОПРОСЫ:"]
        for cat, items in categories.items():
            sections.append(f"### {cat}")
            for item in items:
                question = item.get("question", "").strip()
                answer = item.get("answer", "").strip()
                if question:
                    sections.append(f"**Q:** {question}")
                    sections.append(f"**A:** {answer}")

        return "\n".join(sections)

    def _build_project_info(
        self, name: str, description: str, template: str
    ) -> str:
        """Секция: информация о проекте."""
        parts = []
        if name:
            parts.append(f"ПРОЕКТ: {name}")
        if description:
            parts.append(f"ОПИСАНИЕ: {description}")
        if template:
            parts.append(f"ШАБЛОН/ТЕХНОЛОГИЯ: {template}")
        if self.project_path:
            parts.append(f"ПУТЬ: {self.project_path}")
        return "\n".join(parts) if parts else ""

    async def _build_git_context(self) -> str:
        """Секция: git status, branch, последние коммиты."""
        from core.executor import CommandExecutor

        if not self.project_path:
            return ""

        # Проверяем что это git репозиторий
        git_dir = os.path.join(self.project_path, ".git")
        if not os.path.exists(git_dir):
            return ""

        executor = CommandExecutor()
        parts = []

        try:
            # Branch
            r = await executor.execute(
                "git branch --show-current",
                cwd=self.project_path,
                need_approval=False,
                timeout=10,
            )
            if r.get("exit_code") == 0 and r.get("stdout", "").strip():
                branch = r["stdout"].strip()
                parts.append(f"Git branch: {branch}")

            # Status (короткий)
            r = await executor.execute(
                "git status --short",
                cwd=self.project_path,
                need_approval=False,
                timeout=10,
            )
            if r.get("exit_code") == 0:
                status_lines = r["stdout"].strip().split("\n")
                modified = [l for l in status_lines if l.strip() and not l.startswith("?")]
                untracked = [l for l in status_lines if l.startswith("?")]
                if modified:
                    parts.append(
                        f"Изменённые файлы ({len(modified)}): " +
                        ", ".join(l.strip().split()[-1] for l in modified[:10])
                    )
                if untracked:
                    parts.append(
                        f"Новые файлы ({len(untracked)}): " +
                        ", ".join(l.strip().split()[-1] for l in untracked[:10])
                    )

            # Последние 5 коммитов (короткие)
            r = await executor.execute(
                "git log --oneline -5",
                cwd=self.project_path,
                need_approval=False,
                timeout=10,
            )
            if r.get("exit_code") == 0 and r.get("stdout", "").strip():
                log_lines = r["stdout"].strip().split("\n")
                parts.append("Последние коммиты:\n" + "\n".join(f"  {l}" for l in log_lines))

        except Exception as e:
            logger.log(f"prompt_injector: git context error: {e}", level="warning", source="injector")

        return "GIT ИНФОРМАЦИЯ:\n" + "\n".join(parts) if parts else ""

    def _build_file_tree(self, max_depth: int = 4) -> str:
        """Секция: дерево файлов проекта."""
        if not self.project_path:
            return ""

        entries = []
        try:
            for root, dirs, files in os.walk(self.project_path):
                # Пропускаем служебные директории
                dirs[:] = sorted(
                    d for d in dirs
                    if d not in SKIP_DIRS and not d.startswith(".")
                )

                rel = os.path.relpath(root, self.project_path)
                depth = rel.count(os.sep) if rel != "." else 0
                if depth > max_depth:
                    dirs.clear()
                    continue

                indent = "  " * depth
                dir_name = os.path.basename(root) if rel != "." else self.project_path
                entries.append(f"{indent}{dir_name}/")

                sub_indent = "  " * (depth + 1)
                for f in sorted(files)[:15]:
                    if f.startswith(".") or f.endswith((".pyc", ".class", ".pyo")):
                        continue
                    fpath = os.path.join(root, f)
                    try:
                        size = os.path.getsize(fpath)
                        if size > 1024 * 1024:  # > 1MB
                            entries.append(f"{sub_indent}{f} ({size // 1024 // 1024}MB)")
                        elif size > 1024:
                            entries.append(f"{sub_indent}{f} ({size // 1024}KB)")
                        else:
                            entries.append(f"{sub_indent}{f}")
                    except OSError:
                        entries.append(f"{sub_indent}{f}")

                # Лимит общего количества
                if len(entries) > 100:
                    entries.append("  ... (структура обрезана)")
                    dirs.clear()

        except Exception as e:
            logger.log(f"prompt_injector: file tree error: {e}", level="warning", source="injector")

        return "\n".join(entries) if entries else ""

    def _build_file_contents(
        self,
        file_paths: list[str],
        priority_files: list[str] | None = None,
    ) -> str:
        """
        Секция: содержимое конкретных файлов.

        Приоритетные файлы (priority_files) включаются ПЕРВЫМИ
        с повышенным лимитом символов (30000 вместо 15000).

        Формат:
        --- src/main.py ---
        (содержимое)
        --- END ---
        """
        if not self.project_path or not file_paths:
            return ""

        PRIORITY_MAX_FILE_CHARS = 30000

        # Строим итоговый список: приоритетные первые, затем остальные
        priority_set = set(priority_files) if priority_files else set()
        ordered_paths = []
        seen = set()

        # 1. Приоритетные файлы
        if priority_files:
            for p in priority_files:
                if p not in seen:
                    ordered_paths.append((p, True))
                    seen.add(p)

        # 2. Остальные файлы (без дублирования приоритетных)
        for path in file_paths:
            if path not in seen:
                ordered_paths.append((path, False))
                seen.add(path)

        parts = []
        total_chars = 0

        for idx, (path, is_priority) in enumerate(ordered_paths):
            if total_chars >= MAX_TOTAL_CHARS:
                remaining = len(ordered_paths) - idx
                parts.append(f"\n... (контекст обрезан, {remaining} файлов пропущено)")
                break

            if len(parts) >= MAX_FILES:
                remaining = len(ordered_paths) - idx
                parts.append(f"\n... (лимит {MAX_FILES} файлов, {remaining} пропущено)")
                break

            # Лимит символов для этого файла
            max_chars = PRIORITY_MAX_FILE_CHARS if is_priority else MAX_FILE_CHARS

            # Проверяем кеш
            if path in self._file_cache:
                content = self._file_cache[path]
            else:
                full_path = self._safe_resolve(path)
                if not full_path or not os.path.isfile(full_path):
                    parts.append(f"\n--- {path} ---\n(файл не найден)")
                    continue
                try:
                    if os.path.getsize(full_path) > 5 * 1024 * 1024:
                        parts.append(f"\n--- {path} ---\n(файл слишком большой > 5MB)")
                        continue
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    self._file_cache[path] = content
                except Exception as e:
                    parts.append(f"\n--- {path} ---\n(ошибка чтения: {e})")
                    continue

            # Обрезаем длинные файлы
            display_content = content
            if len(content) > max_chars:
                display_content = content[:max_chars] + f"\n\n... (обрезано, всего {len(content)} симв.)"

            parts.append(f"\n--- {path} ---\n{display_content}")
            total_chars += len(display_content)

        return "ФАЙЛЫ ПРОЕКТА:\n" + "\n".join(parts) if parts else ""

    def _safe_resolve(self, rel_path: str) -> str | None:
        """Безопасно разрешить относительный путь относительно project_path."""
        if not self.project_path or not rel_path:
            return None
        try:
            base = Path(self.project_path).resolve()
            target = (base / rel_path).resolve()
            target.relative_to(base)  # Проверка на traversal
            return str(target)
        except (ValueError, OSError):
            return None

    def _response_format_instructions(self) -> str:
        """
        Инструкции для модели по форматированию ответа.
        Модель использует эти форматы, а ResponseParser их парсит.
        """
        return """ФОРМАТ ОТВЕТА — ИНСТРУКЦИИ:
Для работы с файлами и командами используй следующие формуры:

1. Создать или перезаписать файл:
<file path="относительный/путь/файла.py">
содержимое файла...
</file>

2. Создать файл через code block (альтернативный формат):
```python:относительный/путь/файла.py
содержимое файла...
```

3. Показать изменения (diff):
```diff
--- a/файл.py
+++ b/файл.py
@@ -10,5 +10,5 @@
 старая строка
+новая строка
```

4. Выполнить команду:
<command>
shell-команда здесь
</command>

5. Git операции:
<git operation="commit" message="описание коммита">
</git>
(автоматически: git add -A → commit → push)

6. Прочитать файл:
<read file="относительный/путь/файла.py">
или
<read file="относительный/путь/файла.py"/>
Система прочитает файл и вернёт его содержимое.

7. Создать директорию:
<mkdir path="относительный/путь/директории">
или
<mkdir path="относительный/путь/директории"/>
Система создаст директорию (и все промежуточные).

После завершения работы с файлами система автоматически выполнит:
- git add -A
- git commit с описанием изменений
- git push
Без force-push. Если push не удался — будет ошибка, данные НЕ потеряются."""

    def clear_cache(self):
        """Очистить кеш файлов (например, после записи нового файла)."""
        self._file_cache.clear()

    def invalidate_file(self, path: str):
        """Удалить конкретный файл из кеша."""
        self._file_cache.pop(path, None)
