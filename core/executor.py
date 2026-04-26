import asyncio
import re
import sys
import os
import shlex
from datetime import datetime

from core.action_logger import get_logger
logger = get_logger()


async def _kill_process_safely(process):
    """Kill subprocess and wait for it to exit. Never raises."""
    if process is None:
        return
    try:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass


class CommandExecutor:
    """Executes shell commands with safety checks and real-time output streaming"""

    # Patterns that require human approval
    CRITICAL_PATTERNS = [
        r"rm\s+(-[rfRF]+\s+)?/",
        r"rmdir\s+/[sS]",
        r"del\s+/[fqsFQS]",
        r"DROP\s+(TABLE|DATABASE|SCHEMA)",
        r"DELETE\s+FROM\s+\w+\s*;",
        r"FORMAT\s+[A-Z]:",
        r"shutdown",
        r"taskkill\s+/f",
        r"diskpart",
        r"reg\s+delete",
        r":(){ :|:& };:",  # fork bomb
    ]

    MAX_OUTPUT_LENGTH = 50000  # Max chars of output to capture
    COMMAND_TIMEOUT = 120  # seconds

    def __init__(self):
        self._pending_approvals: dict = {}

    def _is_critical(self, cmd: str) -> tuple[bool, str]:
        """Check if command matches any critical pattern. Returns (is_critical, matched_pattern)"""
        for pattern in self.CRITICAL_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return True, pattern
        return False, ""

    def _play_alert(self):
        """Play a beep sound to alert the user (Windows only)"""
        try:
            import winsound
            winsound.Beep(2500, 1000)  # 2500Hz for 1 second
        except ImportError:
            # Not on Windows — print to console
            print("\a" * 3)  # Terminal bell
        except Exception:
            pass

    async def execute(self, cmd: str, cwd: str | None = None, need_approval: bool = True, timeout: int = None) -> dict:
        """Execute a shell command and return the result"""

        if not cmd or not cmd.strip():
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "Пустая команда",
                "success": False,
                "cmd": cmd,
            }

        # Log command start
        try:
            logger.log(f"exec_start: {cmd[:200]}", level="info", source="executor",
                       details={"cwd": cwd, "timeout": timeout or self.COMMAND_TIMEOUT})
        except Exception:
            pass

        # Check for critical commands
        is_critical, pattern = self._is_critical(cmd)

        if is_critical and need_approval:
            request_id = f"req_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            self._play_alert()

            try:
                logger.log(f"exec_blocked: {cmd[:200]}", level="warning", source="executor",
                           details={"pattern": pattern, "request_id": request_id})
            except Exception:
                pass

            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "success": False,
                "cmd": cmd,
                "approval_required": True,
                "request_id": request_id,
                "reason": f"Критическая команда обнаружена: {pattern}",
                "message": "ВНИМАНИЕ: Эта команда может быть опасной! Для выполнения необходимо подтверждение.",
            }

        # Auto git checkpoint before potentially destructive commands
        if is_critical and not need_approval:
            await self._git_checkpoint(cwd)

        # Execute the command
        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout or self.COMMAND_TIMEOUT
                )
            except asyncio.TimeoutError:
                await _kill_process_safely(process)
                try:
                    logger.log(f"exec_timeout: {cmd[:200]}", level="error", source="executor",
                               details={"timeout": timeout or self.COMMAND_TIMEOUT, "cwd": cwd},
                               error="Command timed out")
                except Exception:
                    pass
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Команда превысила лимит времени ({timeout or self.COMMAND_TIMEOUT} сек)",
                    "success": False,
                    "cmd": cmd,
                }

            stdout_str = stdout.decode("utf-8", errors="replace")[: self.MAX_OUTPUT_LENGTH]
            stderr_str = stderr.decode("utf-8", errors="replace")[: self.MAX_OUTPUT_LENGTH]

            # Log command result
            try:
                output_preview = (stdout_str + stderr_str)[:300].replace("\n", " ")
                logger.command_exec(cmd, exit_code=process.returncode, cwd=cwd,
                                    error=stderr_str if process.returncode != 0 else None)
                logger.log(f"exec_result: {cmd[:120]}",
                           level="success" if process.returncode == 0 else "error",
                           source="executor",
                           details={"exit_code": process.returncode, "cwd": cwd,
                                    "output_preview": output_preview})
            except Exception:
                pass

            return {
                "exit_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "success": process.returncode == 0,
                "cmd": cmd,
            }

        except Exception as e:
            try:
                logger.log(f"exec_error: {cmd[:200]}", level="error", source="executor",
                           details={"cwd": cwd}, error=str(e))
            except Exception:
                pass
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False,
                "cmd": cmd,
            }
        finally:
            await _kill_process_safely(process)

    async def execute_approved(self, cmd: str, request_id: str, cwd: str | None = None) -> dict:
        """Execute a previously approved critical command"""
        # Remove from pending
        self._pending_approvals.pop(request_id, None)
        # Execute without approval check
        await self._git_checkpoint(cwd)
        return await self.execute(cmd, cwd=cwd, need_approval=False)

    async def execute_stream(self, cmd: str, cwd: str | None = None):
        """Execute command and yield output chunks in real-time (for WebSocket streaming)"""
        is_critical, pattern = self._is_critical(cmd)

        if is_critical:
            yield f"[БЛОКИРОВАНО] Критическая команда: {pattern}. Используйте approval workflow."
            return

        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

            try:
                while True:
                    chunk = await asyncio.wait_for(process.stdout.read(1024), timeout=self.COMMAND_TIMEOUT)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    yield text
            except asyncio.TimeoutError:
                yield f"\n[ТАЙМАУТ {self.COMMAND_TIMEOUT}с]"
                return

            await process.wait()
            yield f"\n[Exit code: {process.returncode}]"

        except Exception as e:
            yield f"[ОШИБКА] {e}"
        finally:
            await _kill_process_safely(process)

    async def _git_checkpoint(self, cwd: str | None):
        """Create a git checkpoint before dangerous operations"""
        if not cwd:
            return
        try:
            # Check if we're in a git repo
            check = await asyncio.create_subprocess_shell(
                "git rev-parse --is-inside-work-tree",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            await check.wait()
            if check.returncode != 0:
                return  # Not a git repo

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            commit_msg = f"[auto-checkpoint] before critical command at {timestamp}"

            cp = await asyncio.create_subprocess_shell(
                f"git add -A && git commit -m {shlex.quote(commit_msg)} --allow-empty",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                await asyncio.wait_for(cp.wait(), timeout=10)
            except asyncio.TimeoutError:
                await _kill_process_safely(cp)
        except Exception:
            pass  # Don't fail if checkpoint fails
