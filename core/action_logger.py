"""
Fosved Coder — Action Logger
Тотальное логирование всех действий пользователя, API-вызовов, ошибок и AI-ответов.
Логи хранятся в памяти + в JSON-файле на диске.
Поддерживает debug_mode — усиленное логирование по кнопке ТЕСТ.
"""
import os
import json
import threading
from datetime import datetime
from collections import deque

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs")
LOG_FILE = os.path.join(LOG_DIR, "actions.jsonl")
DEBUG_LOG_FILE = os.path.join(LOG_DIR, "debug_session.jsonl")
MAX_MEMORY_ENTRIES = 2000   # максимум записей в памяти
MAX_FILE_SIZE_MB = 50       # максимальный размер файла логов
MAX_DEBUG_ENTRIES = 5000    # максимум записей в debug-сессии


class ActionLogger:
    """Тотальный логгер — singleton. Используй ActionLogger.get()."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._entries: deque = deque(maxlen=MAX_MEMORY_ENTRIES)
        self._file = None
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._ensure_log_dir()
        self._open_log_file()
        # ── Debug Mode (кнопка ТЕСТ) ──
        self._debug_mode = False
        self._debug_start_ts = None
        self._debug_entries: list = []  # отдельный буфер для debug-сессии (max MAX_DEBUG_ENTRIES)
        self._debug_client_logs: list = []  # логи с клиента (max MAX_DEBUG_ENTRIES)

    def _ensure_log_dir(self):
        os.makedirs(LOG_DIR, exist_ok=True)

    def _open_log_file(self):
        try:
            self._file = open(LOG_FILE, "a", encoding="utf-8")
        except Exception:
            self._file = None

    def _rotate_if_needed(self):
        if not self._file or not os.path.exists(LOG_FILE):
            return
        try:
            size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
            if size_mb > MAX_FILE_SIZE_MB:
                self._file.close()
                # Переименовать текущий в .old
                old_path = LOG_FILE + f".{self._session_id}.old"
                if os.path.exists(old_path):
                    os.remove(old_path)
                os.rename(LOG_FILE, old_path)
                self._open_log_file()
        except Exception:
            pass

    # ── Удобные методы ──

    def ws_message(self, direction: str, payload, project_id=None):
        """Логировать WebSocket сообщение.
        direction: "in" (от клиента) или "out" (к клиенту)
        В debug-режиме логирует полные тела сообщений.
        """
        details = {}
        if isinstance(payload, dict):
            msg_type = payload.get("type", payload.get("prompt", "")[:80] if isinstance(payload.get("prompt"), str) else "")
            details["type"] = str(msg_type)[:100]
            if payload.get("model"):
                details["model"] = str(payload["model"])[:60]
            if payload.get("mode"):
                details["mode"] = str(payload["mode"])[:20]
            if payload.get("project_id"):
                details["project_id"] = payload["project_id"]
            # В debug-режиме: полное тело (ограничено 2000 символов)
            if self._debug_mode:
                full_payload = json.dumps(payload, ensure_ascii=False)[:2000]
                details["full_payload"] = full_payload
            # Для системных команд
            if isinstance(payload, str) and payload.startswith("/"):
                details["type"] = payload[:80]
        else:
            details["type"] = str(payload)[:100]

        self.log(
            action=f"ws_{direction}",
            level="debug",
            source="ws",
            project_id=project_id,
            details=details,
        )

    def api_call(self, method: str, path: str, status_code: int = None,
                 project_id: int = None, error: str = None, details: dict = None):
        """Логировать REST API вызов."""
        level = "error" if (error or (status_code and status_code >= 400)) else "info"
        self.log(
            action=f"{method} {path}",
            level=level,
            source="api",
            project_id=project_id,
            details=details,
            error=error,
        )

    def ai_response(self, model: str, tokens: int = 0, success: bool = True,
                    error: str = None, project_id: int = None, duration_ms: float = 0):
        """Логировать AI ответ."""
        self.log(
            action=f"ai_response: {model}",
            level="success" if success else "error",
            source="agent",
            project_id=project_id,
            details={"model": model[:60], "tokens": tokens, "duration_ms": round(duration_ms, 1)},
            error=error,
        )

    def command_exec(self, cmd: str, exit_code: int = None, cwd: str = None,
                     project_id: int = None, error: str = None):
        """Логировать выполнение команды."""
        level = "error" if (error or (exit_code is not None and exit_code != 0)) else "success"
        self.log(
            action=f"exec: {cmd[:120]}",
            level=level,
            source="executor",
            project_id=project_id,
            details={"cwd": cwd, "exit_code": exit_code},
            error=error,
        )

    def user_action(self, action: str, project_id: int = None, details: dict = None):
        """Логировать действие пользователя (клик, ввод, переключение)."""
        self.log(
            action=action,
            level="info",
            source="ui",
            project_id=project_id,
            details=details,
        )

    # ═══════════════════════════════════════════════════════
    # DEBUG MODE (кнопка ТЕСТ — тотальное логирование)
    # ═══════════════════════════════════════════════════════

    @property
    def debug_mode(self) -> bool:
        return self._debug_mode

    def start_debug_session(self) -> dict:
        """Включить debug-режим. Возвращает информацию о сессии."""
        # Close any previous debug file to avoid leaking the fd on re-start
        if self._debug_file:
            try:
                self._debug_file.close()
            except Exception:
                pass
            self._debug_file = None
        self._debug_mode = True
        self._debug_start_ts = datetime.now().isoformat(timespec="milliseconds")
        self._debug_entries = []
        self._debug_client_logs = []
        # Открыть отдельный файл для debug-сессии
        try:
            self._debug_file = open(DEBUG_LOG_FILE, "w", encoding="utf-8")
        except Exception:
            self._debug_file = None
        self.log("DEBUG_SESSION_STARTED", level="warning", source="debug",
                 details={"debug_start": self._debug_start_ts})
        return {"started": True, "timestamp": self._debug_start_ts}

    def stop_debug_session(self) -> dict:
        """Выключить debug-режим. Возвращает собранный лог."""
        self._debug_mode = False
        self.log("DEBUG_SESSION_STOPPED", level="warning", source="debug",
                 details={"duration_sec": self._debug_duration_seconds(),
                          "entries_collected": len(self._debug_entries),
                          "client_errors": len(self._debug_client_logs)})
        # Закрыть debug-файл
        if self._debug_file:
            try:
                self._debug_file.close()
            except Exception:
                pass
            self._debug_file = None
        result = {
            "stopped": True,
            "start_ts": self._debug_start_ts,
            "end_ts": datetime.now().isoformat(timespec="milliseconds"),
            "duration_sec": self._debug_duration_seconds(),
            "server_entries": len(self._debug_entries),
            "client_logs": len(self._debug_client_logs),
            "entries": self._debug_entries,
            "client_errors": self._debug_client_logs,
        }
        # Сохраняем référence перед очисткой
        entries_snapshot = self._debug_entries[:]
        client_snapshot = self._debug_client_logs[:]
        self._debug_entries = []
        self._debug_client_logs = []
        self._debug_start_ts = None
        result["entries"] = entries_snapshot
        result["client_errors"] = client_snapshot
        return result

    def _debug_duration_seconds(self) -> float:
        """Длительность debug-сессии в секундах."""
        if not self._debug_start_ts:
            return 0
        try:
            start = datetime.fromisoformat(self._debug_start_ts)
            return round((datetime.now() - start).total_seconds(), 1)
        except Exception:
            return 0

    def add_client_log(self, client_log: dict):
        """Добавить лог с клиента (JS error, fetch fail, и т.д.)."""
        if not self._debug_mode:
            return
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "source": "client",
            **client_log,
        }
        self._debug_client_logs.append(entry)
        if len(self._debug_client_logs) > MAX_DEBUG_ENTRIES:
            self._debug_client_logs = self._debug_client_logs[-MAX_DEBUG_ENTRIES:]

    def log(self, action: str, level: str = "info", source: str = "",
            project_id: int = None, details: dict = None, error: str = None,
            stack_trace: str = None):
        """Записать событие в лог.

        Args:
            action: описание действия (например: "ws_chat_message", "api_apk_build", "error_model_timeout")
            level: info | success | warning | error | debug
            source: источник — "ws", "api", "agent", "auto_agent", "executor", "system"
            project_id: ID текущего проекта (если есть)
            details: доп. данные (dict)
            error: текст ошибки (если есть)
            stack_trace: стек-трейс (если есть, только в debug-режиме)
        """
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "session": self._session_id,
            "action": action,
            "level": level,
            "source": source,
            "project_id": project_id,
            "details": details or {},
        }
        if error:
            entry["error"] = str(error)[:500]
        if stack_trace and self._debug_mode:
            entry["stack_trace"] = str(stack_trace)[:2000]

        # В память
        self._entries.append(entry)

        # В debug-буфер (если активен)
        if self._debug_mode:
            self._debug_entries.append(entry)
            if len(self._debug_entries) > MAX_DEBUG_ENTRIES:
                self._debug_entries = self._debug_entries[-MAX_DEBUG_ENTRIES:]
            # В debug-файл
            if self._debug_file:
                try:
                    self._debug_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    self._debug_file.flush()
                except Exception:
                    pass

        # В обычный файл (всегда)
        if self._file:
            try:
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
            except Exception:
                pass

        # Проверить размер файла
        self._rotate_if_needed()

    def get_debug_status(self) -> dict:
        """Статус текущей debug-сессии."""
        return {
            "active": self._debug_mode,
            "start_ts": self._debug_start_ts,
            "duration_sec": self._debug_duration_seconds(),
            "server_entries": len(self._debug_entries),
            "client_errors": len(self._debug_client_logs),
        }

    # ── Чтение логов ──

    def get_entries(self, limit: int = 200, level: str = None,
                    source: str = None, action: str = None,
                    project_id: int = None, since: str = None) -> list[dict]:
        """Получить записи из памяти с фильтрами."""
        results = []
        for entry in reversed(self._entries):
            if len(results) >= limit:
                break
            if level and entry.get("level") != level:
                continue
            if source:
                entry_source = entry.get("source", "")
                allowed = [s.strip() for s in source.split(",") if s.strip()]
                if allowed and entry_source not in allowed:
                    continue
            if action and action.lower() not in entry.get("action", "").lower():
                continue
            if project_id and entry.get("project_id") != project_id:
                continue
            if since and entry.get("ts", "") < since:
                continue
            results.append(entry)
        return list(reversed(results))

    def get_stats(self) -> dict:
        """Статистика логов."""
        stats = {
            "total": len(self._entries),
            "session": self._session_id,
            "by_level": {},
            "by_source": {},
            "errors": 0,
            "file_size_mb": 0,
        }
        for entry in self._entries:
            lvl = entry.get("level", "info")
            src = entry.get("source", "unknown")
            stats["by_level"][lvl] = stats["by_level"].get(lvl, 0) + 1
            stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
            if lvl == "error":
                stats["errors"] += 1

        if os.path.exists(LOG_FILE):
            stats["file_size_mb"] = round(os.path.getsize(LOG_FILE) / (1024 * 1024), 2)

        return stats

    def get_errors(self, limit: int = 50, project_id: int = None) -> list[dict]:
        """Получить только ошибки."""
        return self.get_entries(limit=limit, level="error", project_id=project_id)

    def clear_memory(self):
        """Очистить логи в памяти (файл остаётся)."""
        self._entries.clear()

    def read_log_file(self, limit: int = 500) -> list[dict]:
        """Прочитать последние записи из файла на диске."""
        entries = []
        if not os.path.exists(LOG_FILE):
            return entries
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
        return entries


# Singleton
def get_logger() -> ActionLogger:
    return ActionLogger()
