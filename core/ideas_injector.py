import aiohttp
import json
import re
from core.memory import CONFIG
from core.agent import stream_llm_response
from core.keys_manager import keys_manager
import litellm

litellm.suppress_debug_info = True


class IdeasInjector:
    """Downloads and analyzes GitHub repositories to reduce AI hallucinations"""

    MAX_FILES = CONFIG["system"].get("max_idea_files", 10)
    MAX_FILE_SIZE = CONFIG["system"].get("max_file_size_kb", 50) * 1024  # bytes
    GITHUB_API = "https://api.github.com"

    def _get_github_headers(self) -> dict:
        """Get GitHub API headers with token if available."""
        headers = {"Accept": "application/vnd.github.v3+json"}
        gh_status = keys_manager.get_github_status()
        if gh_status["has_token"] and gh_status["enabled"]:
            headers["Authorization"] = f"Bearer {keys_manager.github_token}"
        return headers

    def _parse_repo_url(self, url: str) -> tuple[str, str] | None:
        """Extract owner/repo from GitHub URL"""
        patterns = [
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
            r"github\.com/([^/]+)/([^/]+?)/tree/[^/]+$",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                owner = match.group(1)
                repo = match.group(2).strip("/")
                return owner, repo
        return None

    async def process_idea(self, repo_url: str) -> str:
        """Download repo info and analyze with AI"""
        parsed = self._parse_repo_url(repo_url)
        if not parsed:
            return (
                "Ошибка: не удалось распознать ссылку. "
                "Ожидается формат: https://github.com/owner/repo"
            )

        owner, repo = parsed
        repo_info = await self._fetch_repo_info(owner, repo)
        if not repo_info:
            return f"Ошибка: репозиторий {owner}/{repo} не найден или недоступен"

        file_tree = await self._fetch_file_tree(owner, repo)
        key_files = await self._download_key_files(owner, repo, file_tree)
        analysis = await self._analyze_with_ai(owner, repo, repo_info, key_files)
        await self._save_to_db(repo_url, f"{owner}/{repo}", analysis, repo_info)
        return analysis

    async def _fetch_repo_info(self, owner: str, repo: str) -> dict | None:
        url = f"{self.GITHUB_API}/repos/{owner}/{repo}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._get_github_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def _fetch_file_tree(self, owner: str, repo: str) -> list[dict]:
        url = f"{self.GITHUB_API}/repos/{owner}/{repo}/git/trees/main?recursive=1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._get_github_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [item for item in data.get("tree", []) if item["type"] == "blob"]
                    url2 = f"{self.GITHUB_API}/repos/{owner}/{repo}/git/trees/master?recursive=1"
                    async with session.get(url2, headers=self._get_github_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                        if resp2.status == 200:
                            data = await resp2.json()
                            return [item for item in data.get("tree", []) if item["type"] == "blob"]
                    return []
        except Exception:
            return []

    async def _download_key_files(self, owner: str, repo: str, file_tree: list[dict]) -> dict[str, str]:
        interesting_extensions = {
            ".py", ".ts", ".js", ".tsx", ".jsx", ".md", ".txt",
            ".yaml", ".yml", ".json", ".toml", ".rs", ".go", ".java",
        }

        priority_files: list[str] = []
        other_files: list[str] = []

        for item in file_tree:
            path = item["path"]
            ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
            size = item.get("size", 0)
            if size > self.MAX_FILE_SIZE:
                continue
            basename = path.lower()
            if any(kw in basename for kw in ("readme", "license", "changelog")):
                priority_files.append(path)
            elif ext in interesting_extensions:
                other_files.append(path)

        selected = priority_files + other_files[: self.MAX_FILES - len(priority_files)]
        selected = selected[: self.MAX_FILES]

        contents: dict[str, str] = {}
        async with aiohttp.ClientSession() as session:
            for path in selected:
                file_url = f"{self.GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
                try:
                    async with session.get(file_url, headers=self._get_github_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "content" in data:
                                import base64
                                file_content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                                contents[path] = file_content[:3000]
                except Exception:
                    continue
        return contents

    async def _analyze_with_ai(self, owner: str, repo: str, repo_info: dict, key_files: dict[str, str]) -> str:
        description = repo_info.get("description", "Нет описания")
        language = repo_info.get("language", "Неизвестен")
        stars = repo_info.get("stargazers_count", 0)
        topics = repo_info.get("topics", [])

        files_context = ""
        for path, content in key_files.items():
            files_context += f"\n--- {path} ---\n{content[:1500]}\n"

        analysis_prompt = f"""Проанализируй репозиторий {owner}/{repo} и напиши краткую выжимку:

Информация о репо:
- Описание: {description}
- Язык: {language}
- Звёзды: {stars}
- Топики: {', '.join(topics) if topics else 'нет'}

Ключевые файлы:
{files_context[:8000]}

Напиши структурированный ответ на русском:
1. Что делает проект (1-3 предложения)
2. Архитектура и ключевые файлы
3. Интересные решения, которые можно заимствовать
4. Стек технологий"""

        # Use keys_manager to get a model config
        all_models = keys_manager.get_all_models()
        # Prefer free models for analysis to save costs
        model_id = None
        for m in all_models:
            if m["type"] == "free" and m["status"] == "available":
                model_id = m["id"]
                break
        if not model_id and all_models:
            model_id = all_models[0]["id"]

        try:
            kwargs = {
                "messages": [{"role": "user", "content": analysis_prompt}],
                "temperature": 0.3,
                "max_tokens": 1500,
            }
            if model_id:
                model_config = keys_manager.get_model_config(model_id)
                if model_config:
                    kwargs["model"] = model_config["model"]
                    kwargs["api_key"] = model_config["api_key"]
                    kwargs["api_base"] = model_config["api_base"]
                else:
                    kwargs["model"] = CONFIG["llm"]["router_model"]
                    kwargs["api_key"] = CONFIG["llm"]["api_key"]
                    kwargs["api_base"] = CONFIG["llm"]["api_base"]
            else:
                kwargs["model"] = CONFIG["llm"]["router_model"]
                kwargs["api_key"] = CONFIG["llm"]["api_key"]
                kwargs["api_base"] = CONFIG["llm"]["api_base"]

            response = await litellm.acompletion(**kwargs)
            return response.choices[0].message.content.strip()
        except Exception as e:
            return (
                f"Ошибка анализа ИИ: {e}\n\n"
                f"Базовая информация о репо:\n"
                f"- {owner}/{repo}\n"
                f"- {description}\n"
                f"- Язык: {language}\n"
                f"- Звёзды: {stars}"
            )

    async def _save_to_db(self, repo_url: str, name: str, summary: str, repo_info: dict):
        try:
            from core.memory import async_session, Idea
            from sqlalchemy import select
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(select(Idea).where(Idea.repo_url == repo_url))
                    existing = result.scalar_one_or_none()
                    if existing:
                        existing.summary = summary
                        existing.name = name
                    else:
                        session.add(Idea(
                            repo_url=repo_url,
                            name=name,
                            summary=summary,
                            raw_data=json.dumps({
                                "description": repo_info.get("description", ""),
                                "language": repo_info.get("language", ""),
                                "stars": repo_info.get("stargazers_count", 0),
                                "topics": repo_info.get("topics", []),
                            }, ensure_ascii=False),
                        ))
        except Exception as e:
            print(f"Warning: Could not save idea to DB: {e}")
