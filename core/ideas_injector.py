import aiohttp
import json
import re
from core.memory import CONFIG
from core.agent import stream_llm_response
import litellm

litellm.suppress_debug_info = True


class IdeasInjector:
    """Downloads and analyzes GitHub repositories to reduce AI hallucinations"""

    MAX_FILES = CONFIG["system"].get("max_idea_files", 10)
    MAX_FILE_SIZE = CONFIG["system"].get("max_file_size_kb", 50) * 1024  # bytes
    GITHUB_API = "https://api.github.com"

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
        # Step 1: Parse URL
        parsed = self._parse_repo_url(repo_url)
        if not parsed:
            return (
                "Ошибка: не удалось распознать ссылку. "
                "Ожидается формат: https://github.com/owner/repo"
            )

        owner, repo = parsed

        # Step 2: Get repo info via GitHub API
        repo_info = await self._fetch_repo_info(owner, repo)
        if not repo_info:
            return f"Ошибка: репозиторий {owner}/{repo} не найден или недоступен"

        # Step 3: Get file tree
        file_tree = await self._fetch_file_tree(owner, repo)

        # Step 4: Download key files
        key_files = await self._download_key_files(owner, repo, file_tree)

        # Step 5: Analyze with AI
        analysis = await self._analyze_with_ai(owner, repo, repo_info, key_files)

        # Step 6: Save to database
        await self._save_to_db(repo_url, f"{owner}/{repo}", analysis, repo_info)

        return analysis

    async def _fetch_repo_info(self, owner: str, repo: str) -> dict | None:
        """Fetch repository metadata from GitHub API"""
        url = f"{self.GITHUB_API}/repos/{owner}/{repo}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def _fetch_file_tree(self, owner: str, repo: str) -> list[dict]:
        """Fetch repository file tree via GitHub API"""
        url = f"{self.GITHUB_API}/repos/{owner}/{repo}/git/trees/main?recursive=1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [
                            item for item in data.get("tree", []) if item["type"] == "blob"
                        ]
                    # Try 'master' branch
                    url2 = (
                        f"{self.GITHUB_API}/repos/{owner}/{repo}"
                        f"/git/trees/master?recursive=1"
                    )
                    async with session.get(
                        url2, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp2:
                        if resp2.status == 200:
                            data = await resp2.json()
                            return [
                                item
                                for item in data.get("tree", [])
                                if item["type"] == "blob"
                            ]
                    return []
        except Exception:
            return []

    async def _download_key_files(
        self, owner: str, repo: str, file_tree: list[dict]
    ) -> dict[str, str]:
        """Download contents of key files from the repository"""
        interesting_extensions = {
            ".py", ".ts", ".js", ".tsx", ".jsx", ".md", ".txt",
            ".yaml", ".yml", ".json", ".toml", ".rs", ".go", ".java",
        }

        # Priority files (README first)
        priority_files: list[str] = []
        other_files: list[str] = []

        for item in file_tree:
            path = item["path"]
            ext = (
                "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
            )
            size = item.get("size", 0)

            if size > self.MAX_FILE_SIZE:
                continue

            basename = path.lower()
            if any(kw in basename for kw in ("readme", "license", "changelog")):
                priority_files.append(path)
            elif ext in interesting_extensions:
                other_files.append(path)

        # Take priority files + top other files
        selected = priority_files + other_files[: self.MAX_FILES - len(priority_files)]
        selected = selected[: self.MAX_FILES]

        contents: dict[str, str] = {}
        async with aiohttp.ClientSession() as session:
            for path in selected:
                file_url = (
                    f"{self.GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
                )
                try:
                    async with session.get(
                        file_url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "content" in data:
                                import base64

                                file_content = base64.b64decode(
                                    data["content"]
                                ).decode("utf-8", errors="replace")
                                contents[path] = file_content[:3000]  # Truncate large files
                except Exception:
                    continue

        return contents

    async def _analyze_with_ai(
        self, owner: str, repo: str, repo_info: dict, key_files: dict[str, str]
    ) -> str:
        """Use cheap AI to analyze the repository"""
        description = repo_info.get("description", "Нет описания")
        language = repo_info.get("language", "Неизвестен")
        stars = repo_info.get("stargazers_count", 0)
        topics = repo_info.get("topics", [])

        # Build context from files
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

        try:
            response = await litellm.acompletion(
                model=CONFIG["llm"]["router_model"],
                messages=[{"role": "user", "content": analysis_prompt}],
                api_base=CONFIG["llm"]["api_base"],
                api_key=CONFIG["llm"]["api_key"],
                temperature=0.3,
                max_tokens=1500,
            )
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

    async def _save_to_db(
        self, repo_url: str, name: str, summary: str, repo_info: dict
    ):
        """Save idea analysis to database"""
        try:
            from core.memory import async_session, Idea
            from sqlalchemy import select

            async with async_session() as session:
                async with session.begin():
                    # Check if already exists
                    result = await session.execute(
                        select(Idea).where(Idea.repo_url == repo_url)
                    )
                    existing = result.scalar_one_or_none()

                    if existing:
                        existing.summary = summary
                        existing.name = name
                    else:
                        session.add(
                            Idea(
                                repo_url=repo_url,
                                name=name,
                                summary=summary,
                                raw_data=json.dumps(
                                    {
                                        "description": repo_info.get("description", ""),
                                        "language": repo_info.get("language", ""),
                                        "stars": repo_info.get("stargazers_count", 0),
                                        "topics": repo_info.get("topics", []),
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        )
        except Exception as e:
            print(f"Warning: Could not save idea to DB: {e}")
