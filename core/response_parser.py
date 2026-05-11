"""
core/response_parser.py — Парсер ответов модели
Извлекает инструкции из текстового ответа модели и превращает их в действия.

Поддерживаемые форматы:
1. <file path="путь">содержимое</file> — создать/перезаписать файл
2. ```lang:путь — создать/перезаписать файл через code block
3. ```diff — применить патч
4. <command>команда</command> — выполнить shell-команду
5. <git operation="commit/push" message="..."> — git операции

Модель НЕ знает что это "инструменты" — для неё это просто формат ответа.
"""

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from core.memory import get_project_token_by_path, git_push_with_token
from core.action_logger import get_logger

logger = get_logger()


@dataclass
class ParsedAction:
    """Одно распарсенное действие из ответа модели."""
    action_type: str  # "write_file", "apply_diff", "execute_command", "git_operation"
    path: str = ""  # путь к файлу (для write_file, apply_diff)
    content: str = ""  # содержимое файла или команда
    message: str = ""  # commit message (для git)
    operation: str = ""  # тип git операции: commit, push, commit_push
    raw_text: str = ""  # оригинальный текст из ответа
    line_start: int = 0  # позиция в ответе (для извлечения текста вокруг)
    line_end: int = 0


@dataclass
class ParseResult:
    """Результат парсинга всего ответа."""
    actions: list[ParsedAction] = field(default_factory=list)
    clean_text: str = ""  # текст ответа БЕЗ инструкций (для показа пользователю)


class ResponseParser:
    """Парсит ответ модели и извлекает инструкции."""

    # Regex паттерны для извлечения инструкций

    # <file path="путь">содержимое</file>
    RE_FILE_TAG = re.compile(
        r'<file\s+path\s*=\s*["\']([^"\']+)["\']\s*>(.*?)</file>',
        re.DOTALL,
    )

    # <command>shell команда</command>
    RE_COMMAND_TAG = re.compile(
        r'<command\s*>(.*?)</command>',
        re.DOTALL,
    )

    # <git operation="..." message="...">
    RE_GIT_TAG = re.compile(
        r'<git\s+operation\s*=\s*["\'](\w+)["\'](?:\s+message\s*=\s*["\']([^"\']*)["\'])?'
        r'\s*>(?:.*?)</git>',
        re.DOTALL,
    )

    # Code block с путём: ```lang:path
    RE_CODE_BLOCK_PATH = re.compile(
        r'```(\w+)\s*:\s*(\S+)\s*\n(.*?)```',
        re.DOTALL,
    )

    # Diff block: ```diff ... ```
    RE_DIFF_BLOCK = re.compile(
        r'```diff\s*\n(.*?)```',
        re.DOTALL,
    )

    def __init__(self, project_path: str | None = None):
        self.project_path = project_path

    def parse(self, text: str) -> ParseResult:
        """
        Парсит полный ответ модели.

        Извлекает все инструкции и возвращает их вместе с
        "чистым" текстом (без инструкций) для показа пользователю.
        """
        if not text or not text.strip():
            return ParseResult(clean_text=text or "")

        actions = []
        clean = text
        used_ranges: list[tuple[int, int]] = []  # (start, end) для очистки текста

        # 1. <file path="..."> — самый приоритетный формат
        for match in self.RE_FILE_TAG.finditer(text):
            path = match.group(1).strip()
            content = match.group(2)
            # Убираем ведущий/замыкающий перенос строки
            if content.startswith("\n"):
                content = content[1:]
            if content.endswith("\n"):
                content = content[:-1]

            action = ParsedAction(
                action_type="write_file",
                path=path,
                content=content,
                raw_text=match.group(0),
                line_start=match.start(),
                line_end=match.end(),
            )
            actions.append(action)
            used_ranges.append((match.start(), match.end()))

        # 2. Code blocks с путём: ```lang:path
        # Но пропускаем если этот блок уже захвачен <file>
        for match in self.RE_CODE_BLOCK_PATH.finditer(text):
            # Проверяем не пересекается ли с уже найденным <file>
            if self._overlaps(match.start(), match.end(), used_ranges):
                continue

            lang = match.group(1)
            path = match.group(2).strip()
            content = match.group(3).rstrip("\n")

            action = ParsedAction(
                action_type="write_file",
                path=path,
                content=content,
                raw_text=match.group(0),
                line_start=match.start(),
                line_end=match.end(),
            )
            actions.append(action)
            used_ranges.append((match.start(), match.end()))

        # 3. ```diff — патчи
        for match in self.RE_DIFF_BLOCK.finditer(text):
            if self._overlaps(match.start(), match.end(), used_ranges):
                continue

            diff_content = match.group(1)
            action = ParsedAction(
                action_type="apply_diff",
                content=diff_content,
                raw_text=match.group(0),
                line_start=match.start(),
                line_end=match.end(),
            )
            actions.append(action)
            used_ranges.append((match.start(), match.end()))

        # 4. <command> — shell команды
        for match in self.RE_COMMAND_TAG.finditer(text):
            if self._overlaps(match.start(), match.end(), used_ranges):
                continue

            command = match.group(1).strip()
            action = ParsedAction(
                action_type="execute_command",
                content=command,
                raw_text=match.group(0),
                line_start=match.start(),
                line_end=match.end(),
            )
            actions.append(action)
            used_ranges.append((match.start(), match.end()))

        # 5. <git operation="..." message="...">
        for match in self.RE_GIT_TAG.finditer(text):
            if self._overlaps(match.start(), match.end(), used_ranges):
                continue

            operation = match.group(1).strip()
            message = (match.group(2) or "").strip()
            action = ParsedAction(
                action_type="git_operation",
                operation=operation,
                message=message or "update",
                raw_text=match.group(0),
                line_start=match.start(),
                line_end=match.end(),
            )
            actions.append(action)
            used_ranges.append((match.start(), match.end()))

        # Строим чистый текст (убираем все инструкции)
        clean = self._remove_ranges(text, used_ranges)

        return ParseResult(actions=actions, clean_text=clean)

    def _overlaps(
        self, start: int, end: int, ranges: list[tuple[int, int]]
    ) -> bool:
        """Проверяет пересекается ли диапазон с уже использованными."""
        for rs, re_ in ranges:
            if start < re_ and end > rs:
                return True
        return False

    def _remove_ranges(
        self, text: str, ranges: list[tuple[int, int]]
    ) -> str:
        """Удаляет все диапазоны из текста, оставляя чистый ответ."""
        if not ranges:
            return text

        # Сортируем по позиции (от конца к началу для безопасного удаления)
        sorted_ranges = sorted(ranges, key=lambda r: r[0], reverse=True)

        result = text
        for start, end in sorted_ranges:
            # Убираем блок + окружающие пустые строки
            before = result[:start].rstrip("\n")
            after = result[end:].lstrip("\n")

            # Если до и после есть текст — соединяем переносом
            if before and after:
                result = before + "\n\n" + after
            elif before:
                result = before
            elif after:
                result = after
            else:
                result = ""

        return result.strip()


class ActionExecutor:
    """Выполняет распарсенные действия (write file, execute command, git)."""

    def __init__(self, project_path: str | None = None):
        self.project_path = project_path
        self._executor = None

    async def _get_executor(self):
        from core.executor import CommandExecutor
        if self._executor is None:
            self._executor = CommandExecutor()
        return self._executor

    def set_project(self, project_path: str):
        self.project_path = project_path

    async def execute(
        self, action: ParsedAction, websocket=None
    ) -> dict:
        """
        Выполнить одно распарсенное действие.

        Возвращает dict:
        {
            "success": bool,
            "action_type": str,
            "message": str,  # описание результата
            "path": str,     # путь файла (если применимо)
        }
        """
        if action.action_type == "write_file":
            return await self._write_file(action, websocket)
        elif action.action_type == "apply_diff":
            return await self._apply_diff(action, websocket)
        elif action.action_type == "execute_command":
            return await self._execute_command(action, websocket)
        elif action.action_type == "git_operation":
            return await self._git_operation(action, websocket)
        else:
            return {
                "success": False,
                "action_type": action.action_type,
                "message": f"Неизвестный тип действия: {action.action_type}",
            }

    async def _write_file(
        self, action: ParsedAction, websocket=None
    ) -> dict:
        """Записать файл в проект."""
        if not self.project_path:
            return {"success": False, "action_type": "write_file", "message": "Нет пути к проекту"}

        rel_path = action.path
        full_path = self._safe_resolve(rel_path)

        if not full_path:
            return {
                "success": False,
                "action_type": "write_file",
                "message": f"Путь вне проекта: {rel_path}",
                "path": rel_path,
            }

        try:
            # Создаём директории
            dir_path = os.path.dirname(full_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)

            # Пишем файл
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(action.content)

            char_count = len(action.content)
            logger.log(
                f"parser: write_file {rel_path} ({char_count} chars)",
                level="info", source="parser",
            )
            if websocket:
                try:
                    await websocket.send_json({
                        "type": "tool_call",
                        "tool": "write_file",
                        "args": {"path": rel_path, "size": char_count},
                        "status": "done",
                    })
                except Exception:
                    pass

            return {
                "success": True,
                "action_type": "write_file",
                "message": f"Файл {rel_path} записан ({char_count} симв.)",
                "path": rel_path,
            }

        except Exception as e:
            logger.log(
                f"parser: write_file error: {e}",
                level="error", source="parser",
            )
            return {
                "success": False,
                "action_type": "write_file",
                "message": f"Ошибка записи {rel_path}: {e}",
                "path": rel_path,
            }

    async def _apply_diff(
        self, action: ParsedAction, websocket=None
    ) -> dict:
        """Применить diff патч к файлу."""
        if not self.project_path:
            return {"success": False, "action_type": "apply_diff", "message": "Нет пути к проекту"}

        diff_text = action.content
        executor = await self._get_executor()

        # Пытаемся определить целевой файл из diff
        file_path = self._extract_diff_target(diff_text)
        if not file_path:
            # Пишем diff во временный файл и применяем через git apply
            return await self._apply_diff_via_git(diff_text, websocket)

        full_path = self._safe_resolve(file_path)
        if not full_path or not os.path.isfile(full_path):
            return {
                "success": False,
                "action_type": "apply_diff",
                "message": f"Файл для патча не найден: {file_path}",
                "path": file_path,
            }

        # Применяем патч программно
        try:
            lines = diff_text.split("\n")
            original_lines = []
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                original_lines = f.readlines()

            new_lines = self._patch_lines(original_lines, lines)

            with open(full_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            logger.log(
                f"parser: apply_diff {file_path}",
                level="info", source="parser",
            )
            return {
                "success": True,
                "action_type": "apply_diff",
                "message": f"Патч применён к {file_path}",
                "path": file_path,
            }

        except Exception as e:
            return {
                "success": False,
                "action_type": "apply_diff",
                "message": f"Ошибка применения патча: {e}",
                "path": file_path or "",
            }

    async def _apply_diff_via_git(
        self, diff_text: str, websocket=None
    ) -> dict:
        """Применить diff через git apply (fallback)."""
        executor = await self._get_executor()

        try:
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".diff", delete=False, encoding="utf-8"
            ) as f:
                f.write(diff_text)
                temp_path = f.name

            r = await executor.execute(
                f"git apply {shlex.quote(temp_path)}",
                cwd=self.project_path,
                need_approval=False,
                timeout=30,
            )
            os.unlink(temp_path)

            if r.get("exit_code") == 0:
                return {
                    "success": True,
                    "action_type": "apply_diff",
                    "message": "Diff применён через git apply",
                }
            else:
                return {
                    "success": False,
                    "action_type": "apply_diff",
                    "message": f"git apply не удался: {r.get('stderr', '')[:200]}",
                }
        except Exception as e:
            return {
                "success": False,
                "action_type": "apply_diff",
                "message": f"Ошибка: {e}",
            }

    async def _execute_command(
        self, action: ParsedAction, websocket=None
    ) -> dict:
        """Выполнить shell-команду."""
        if not action.content.strip():
            return {
                "success": False,
                "action_type": "execute_command",
                "message": "Пустая команда",
            }

        executor = await self._get_executor()

        # Логирование
        cmd_preview = action.content[:120]
        logger.log(
            f"parser: execute_command '{cmd_preview}'",
            level="info", source="parser",
        )
        if websocket:
            try:
                await websocket.send_json({
                    "type": "tool_call",
                    "tool": "execute_command",
                    "args": {"command": cmd_preview},
                    "status": "running",
                })
            except Exception:
                pass

        result = await executor.execute(
            action.content,
            cwd=self.project_path,
            need_approval=False,
            timeout=60,
        )

        output = ""
        if result.get("stdout"):
            output += result["stdout"][:5000]
        if result.get("stderr"):
            output += ("\n" if output else "") + result["stderr"][:2000]
        if not output:
            output = "(команда выполнена без вывода)"

        exit_code = result.get("exit_code", -1)
        success = exit_code == 0
        status = "OK" if success else f"exit code {exit_code}"

        if websocket:
            try:
                await websocket.send_json({
                    "type": "tool_call",
                    "tool": "execute_command",
                    "args": {"command": cmd_preview},
                    "status": "done",
                    "exit_code": exit_code,
                })
            except Exception:
                pass

        return {
            "success": success,
            "action_type": "execute_command",
            "message": f"[{status}]\n{output}",
        }

    async def _git_operation(
        self, action: ParsedAction, websocket=None
    ) -> dict:
        """
        Выполнить git операцию.
        Автоматически: git add -A → commit → push.
        НИКОГДА не force-push.
        """
        if not self.project_path:
            return {"success": False, "action_type": "git_operation", "message": "Нет пути к проекту"}

        executor = await self._get_executor()
        operation = action.operation.lower()
        message = action.message or "update"

        # Экранируем сообщение коммита
        safe_msg = shlex.quote(message)

        logger.log(
            f"parser: git_operation {operation} '{message[:50]}'",
            level="info", source="parser",
        )
        if websocket:
            try:
                await websocket.send_json({
                    "type": "tool_call",
                    "tool": "git_commit_push",
                    "args": {"message": message},
                    "status": "running",
                })
            except Exception:
                pass

        results = []

        if operation in ("commit", "commit_push", "push"):
            # git add -A
            r1 = await executor.execute(
                "git add -A",
                cwd=self.project_path,
                need_approval=False,
                timeout=30,
            )
            results.append(f"add: {'OK' if r1.get('exit_code') == 0 else r1.get('stderr', '')[:100]}")

            # git commit
            r2 = await executor.execute(
                f"git commit -m {safe_msg} --allow-empty",
                cwd=self.project_path,
                need_approval=False,
                timeout=30,
            )
            commit_out = (r2.get("stdout", "") + r2.get("stderr", "")).strip()
            if "nothing to commit" in commit_out.lower():
                results.append("commit: нет изменений")
            elif r2.get("exit_code") == 0:
                results.append(f"commit: OK")
            else:
                results.append(f"commit: {commit_out[:100]}")

        if operation in ("push", "commit_push"):
            # git push with project PAT token (НЕ force-push!)
            project_token = await get_project_token_by_path(self.project_path)
            push_out = await git_push_with_token(executor, self.project_path, project_token)
            push_out = push_out.strip()
            # Determine push result status from output
            if "error" in push_out.lower() or "fatal" in push_out.lower() or "denied" in push_out.lower():
                results.append(f"push: ОШИБКА — {push_out[:200]}")
            elif "Everything up-to-date" in push_out or "already up" in push_out:
                results.append("push: уже актуально")
            elif push_out:
                results.append("push: OK")
            else:
                results.append("push: OK")

        result_text = " | ".join(results)
        success = all("ОШИБКА" not in r for r in results)

        if websocket:
            try:
                await websocket.send_json({
                    "type": "tool_call",
                    "tool": "git_commit_push",
                    "args": {"message": message},
                    "status": "done",
                })
            except Exception:
                pass

        return {
            "success": success,
            "action_type": "git_operation",
            "message": result_text,
        }

    def _safe_resolve(self, rel_path: str) -> str | None:
        """Безопасно разрешить путь."""
        if not self.project_path or not rel_path:
            return None
        try:
            base = Path(self.project_path).resolve()
            target = (base / rel_path).resolve()
            target.relative_to(base)
            return str(target)
        except (ValueError, OSError):
            return None

    def _extract_diff_target(self, diff_text: str) -> str | None:
        """Извлечь целевой файл из diff (--- a/path)."""
        for line in diff_text.split("\n"):
            if line.startswith("--- a/"):
                return line[6:].strip()
            elif line.startswith("--- "):
                path = line[4:].strip()
                if path and not path.startswith("/dev/null"):
                    return path
        return None

    def _patch_lines(
        self, original: list[str], diff_lines: list[str]
    ) -> list[str]:
        """Применить unified diff к списку строк."""
        new_lines = []
        orig_idx = 0

        for diff_line in diff_lines:
            if diff_line.startswith("@@"):
                # Извлекаем начальную строку из hunk header
                # @@ -start,count +start,count @@
                match = re.search(r"^@@ -(\d+)(?:,(\d+))?", diff_line)
                if match:
                    orig_idx = int(match.group(1)) - 1
                continue
            elif diff_line.startswith("--- ") or diff_line.startswith("+++ "):
                continue
            elif diff_line.startswith(" "):
                # Контекстная строка — оставляем
                if orig_idx < len(original):
                    new_lines.append(original[orig_idx])
                orig_idx += 1
            elif diff_line.startswith("+"):
                # Добавленная строка
                new_lines.append(diff_line[1:] + "\n")
            elif diff_line.startswith("-"):
                # Удалённая строка — пропускаем
                orig_idx += 1

        # Добавляем оставшиеся строки оригинала
        while orig_idx < len(original):
            new_lines.append(original[orig_idx])
            orig_idx += 1

        return new_lines
