import os
import hashlib
from core.memory import CONFIG, save_repo_map, get_repo_map


class ContextManager:
    """Builds and caches compact repo maps for AI context."""

    IGNORED_DIRS = {
        "venv", "__pycache__", "node_modules", ".git", ".cache",
        "__pypackages__", ".venv", "env", ".idea", ".vscode",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        "target", "bin", "obj", ".next", ".nuxt"
    }

    INTERESTING_EXTENSIONS = {
        ".py", ".ts", ".js", ".tsx", ".jsx", ".yaml", ".yml", ".json",
        ".toml", ".cfg", ".ini", ".md", ".txt", ".html", ".css",
        ".sql", ".sh", ".bat", ".ps1"
    }

    MAX_FILES = CONFIG["system"].get("max_context_files", 20)
    MAX_LINES_PER_FILE = 50

    async def build_repo_map(self, project_path: str, project_id: int | None = None) -> str:
        """Build a compact representation of project structure with code signatures."""
        # Check cache first (compare file tree hash)
        if project_id:
            cached = await get_repo_map(project_id)
            current_hash = await self._compute_tree_hash(project_path)
            if cached and cached.get("hash") == current_hash:
                return cached["content"]

        lines = []
        file_count = 0

        for root, dirs, files in os.walk(project_path):
            # Filter out ignored directories in-place so os.walk skips them
            dirs[:] = [d for d in dirs if d not in self.IGNORED_DIRS and not d.startswith(".")]

            for filename in sorted(files):
                ext = os.path.splitext(filename)[1].lower()
                if ext not in self.INTERESTING_EXTENSIONS:
                    continue

                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, project_path).replace("\\", "/")

                try:
                    file_size = os.path.getsize(filepath)
                    file_size_kb = file_size / 1024

                    if file_count < self.MAX_FILES and file_size_kb < 100:
                        sigs = self._extract_signatures(filepath, ext)
                        if sigs:
                            lines.append(f"  {rel_path}")
                            for sig in sigs:
                                lines.append(f"    {sig}")
                        else:
                            lines.append(f"  {rel_path} ({file_size_kb:.1f} KB)")
                        file_count += 1
                    else:
                        lines.append(f"  {rel_path} ({file_size_kb:.1f} KB)")
                except (OSError, UnicodeDecodeError):
                    lines.append(f"  {rel_path} (unreadable)")

        if not lines:
            return "  (empty project or no source files found)"

        repo_map = os.path.basename(project_path) + "/\n" + "\n".join(lines)

        # Save to cache for future requests
        if project_id:
            tree_hash = await self._compute_tree_hash(project_path)
            await save_repo_map(project_id, repo_map, tree_hash)

        return repo_map

    def _extract_signatures(self, filepath: str, ext: str) -> list[str]:
        """Extract function/class/import signatures from a source file."""
        sigs = []
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                file_lines = f.readlines()[:self.MAX_LINES_PER_FILE]

            for line in file_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                    continue

                if ext == ".py":
                    if stripped.startswith("import ") or stripped.startswith("from "):
                        sigs.append(stripped[:80])
                    elif stripped.startswith("class "):
                        sigs.append(self._clean_py_sig(stripped))
                    elif stripped.startswith("def ") or stripped.startswith("async def "):
                        sigs.append(self._clean_py_sig(stripped))
                elif ext in {".ts", ".js", ".tsx", ".jsx"}:
                    if stripped.startswith("import ") or stripped.startswith("export "):
                        sigs.append(stripped[:80])
                    elif ("function " in stripped or "class " in stripped
                          or "interface " in stripped or "type " in stripped):
                        sig_line = stripped[:80]
                        if sig_line.endswith("{"):
                            sig_line = sig_line[:-1].strip()
                        sigs.append(sig_line)
                elif ext in {".yaml", ".yml", ".toml", ".json"}:
                    sigs.append(stripped[:80])

                if len(sigs) > 15:
                    sigs.append("  ... (truncated)")
                    break
        except Exception:
            pass

        return sigs

    def _clean_py_sig(self, line: str) -> str:
        """Clean a Python signature line for brevity."""
        line = line.rstrip(":")
        if line.endswith("("):
            line += ")"
        return line[:100]

    async def _compute_tree_hash(self, project_path: str) -> str:
        """Compute MD5 hash of the file tree (names + sizes + mtimes) for cache invalidation."""
        hasher = hashlib.md5()
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in self.IGNORED_DIRS and not d.startswith(".")]
            for filename in sorted(files):
                filepath = os.path.join(root, filename)
                try:
                    stat = os.stat(filepath)
                    hasher.update(f"{filename}:{stat.st_size}:{stat.st_mtime_ns}".encode())
                except OSError:
                    pass
        return hasher.hexdigest()

    async def read_file_safe(self, project_path: str, relative_path: str) -> str:
        """Safely read a file within the project directory (prevents directory traversal)."""
        # Resolve paths to prevent directory traversal attacks
        project_path = os.path.normpath(os.path.abspath(project_path))
        target_path = os.path.normpath(os.path.abspath(os.path.join(project_path, relative_path)))

        if not target_path.startswith(project_path + os.sep) and target_path != project_path:
            return "Ошибка: путь выходит за пределы проекта"

        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return f"Файл не найден: {relative_path}"
        except Exception as e:
            return f"Ошибка чтения: {e}"
