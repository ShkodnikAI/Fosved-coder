import os
from sqlalchemy import Text, select, delete, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime
import yaml

def load_config():
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    # Default config for Railway (where config.yaml is gitignored)
    return {
        "llm": {"default_model": "openrouter/anthropic/claude-3.5-sonnet", "router_model": "openrouter/google/gemini-2.0-flash-exp:free", "api_base": "https://openrouter.ai/api/v1", "api_key": "", "temperature": 0.2, "max_tokens": 4096},
        "system": {"db_url": "sqlite+aiosqlite:////app/data/fosved_coder.db", "projects_dir": "/app/data/projects", "ideas_cache_dir": "/app/data/.cache/ideas", "archives_dir": "/app/data/archives", "max_iterations": 3, "max_context_files": 20, "max_idea_files": 10, "max_file_size_kb": 50},
        "security": {"allowed_commands": ["git", "python", "pip", "npm", "node", "cat", "ls", "dir", "echo", "mkdir", "cd"], "blocked_patterns": ["rm -rf /", "DROP DATABASE", "FORMAT C:"]},
    }

CONFIG = load_config()

# Railway: use SQLite by default (Railway auto-sets DATABASE_URL, ignore it)
DB_URL = CONFIG["system"]["db_url"]

engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(unique=True, index=True)
    path: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    base_prompt: Mapped[str] = mapped_column(Text, default="")
    ideas: Mapped[str] = mapped_column(Text, default="")
    selected_models: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of model IDs
    github_repo: Mapped[str] = mapped_column(Text, default="")  # GitHub repository URL
    github_token: Mapped[str] = mapped_column(Text, default="")  # Individual GitHub token
    local_path: Mapped[str] = mapped_column(Text, default="")  # Custom local storage path
    progress: Mapped[int] = mapped_column(default=0)  # 0-100 percent
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class Idea(Base):
    __tablename__ = "ideas"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    repo_url: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    summary: Mapped[str] = mapped_column(Text, default="")
    raw_data: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class ChatHistory(Base):
    __tablename__ = "chat_history"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int | None] = mapped_column(nullable=True)
    thread_id: Mapped[int | None] = mapped_column(nullable=True, index=True, default=None)
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class ChatThread(Base):
    __tablename__ = "chat_threads"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(index=True)
    parent_id: Mapped[int | None] = mapped_column(nullable=True, default=None)
    title: Mapped[str] = mapped_column(default="Новый поток")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(index=True)
    thread_id: Mapped[int | None] = mapped_column(nullable=True, default=None)
    snapshot_type: Mapped[str] = mapped_column(default="auto")
    title: Mapped[str] = mapped_column(default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    key_decisions: Mapped[str] = mapped_column(Text, default="")
    file_changes: Mapped[str] = mapped_column(Text, default="")
    errors_fixed: Mapped[str] = mapped_column(Text, default="")
    message_count_before: Mapped[int] = mapped_column(default=0)
    message_count_after: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class RepoMap(Base):
    __tablename__ = "repo_maps"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    file_hash: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class RoutingStat(Base):
    __tablename__ = "routing_stats"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    prompt_summary: Mapped[str] = mapped_column(default="")
    model: Mapped[str] = mapped_column(default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(default=True)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class ProjectArchive(Base):
    __tablename__ = "project_archives"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(index=True)
    project_name: Mapped[str]
    description: Mapped[str] = mapped_column(Text, default="")
    master_prompt: Mapped[str] = mapped_column(Text, default="")
    file_list: Mapped[str] = mapped_column(Text, default="[]")
    file_count: Mapped[int] = mapped_column(default=0)
    archive_path: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

# ═══════════════════════════════════════════════════════════════
# INIT
# ═══════════════════════════════════════════════════════════════

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_db()
    os.makedirs(CONFIG["system"]["projects_dir"], exist_ok=True)
    os.makedirs(CONFIG["system"]["ideas_cache_dir"], exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# PROJECTS CRUD
# ═══════════════════════════════════════════════════════════════

async def create_project(name: str, path: str, description: str = "", base_prompt: str = "", ideas: str = "", github_repo: str = "", github_token: str = "", local_path: str = "") -> dict:
    """Create a new project. Returns dict representation."""
    import json
    async with async_session() as session:
        async with session.begin():
            existing = await session.execute(
                select(Project).where((Project.name == name) | (Project.path == path))
            )
            if existing.scalar_one_or_none():
                return None
            project = Project(name=name, path=path, description=description, base_prompt=base_prompt, ideas=ideas, github_repo=github_repo, github_token=github_token, local_path=local_path)
            session.add(project)
            await session.flush()
            await session.refresh(project)
            os.makedirs(path, exist_ok=True)
            return {"id": project.id, "name": project.name, "path": project.path, "description": project.description, "base_prompt": project.base_prompt, "ideas": project.ideas, "selected_models": project.selected_models, "github_repo": project.github_repo, "github_token": project.github_token, "local_path": project.local_path, "progress": project.progress, "created_at": str(project.created_at)}

async def get_all_projects() -> list[dict]:
    """Get all projects as list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(Project).order_by(Project.created_at.desc())
        )
        return [
            {"id": p.id, "name": p.name, "path": p.path, "description": p.description, "base_prompt": p.base_prompt, "ideas": p.ideas, "selected_models": p.selected_models, "github_repo": p.github_repo, "github_token": p.github_token, "local_path": p.local_path, "progress": p.progress, "created_at": str(p.created_at)}
            for p in result.scalars().all()
        ]

async def get_project(project_id: int) -> dict | None:
    """Get single project by ID."""
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        p = result.scalar_one_or_none()
        if p:
            return {"id": p.id, "name": p.name, "path": p.path, "description": p.description, "base_prompt": p.base_prompt, "ideas": p.ideas, "selected_models": p.selected_models, "github_repo": p.github_repo, "github_token": p.github_token, "local_path": p.local_path, "progress": p.progress, "created_at": str(p.created_at)}
        return None

async def migrate_db():
    """Add new columns if they don't exist (for upgrades)."""
    db_url = DB_URL
    if "sqlite" in db_url:
        import sqlite3
        db_file = db_url.split(":///")[-1] if ":///" in db_url else "fosved_coder.db"
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(projects)")
        existing = {row[1] for row in cursor.fetchall()}
        new_columns = [
            ("github_repo", "TEXT", "''"),
            ("github_token", "TEXT", "''"),
            ("local_path", "TEXT", "''"),
        ]
        for col_name, col_type, col_default in new_columns:
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_type} DEFAULT {col_default}")
        conn.commit()
        conn.close()
    elif "postgres" in db_url:
        from sqlalchemy import text, inspect
        async with engine.begin() as conn:
            insp = inspect(conn)
            existing = await conn.run_sync(lambda sync_conn: insp.get_columns("projects"))
            existing_names = {col["name"] for col in existing}
            new_columns = {
                "github_repo": "TEXT DEFAULT ''",
                "github_token": "TEXT DEFAULT ''",
                "local_path": "TEXT DEFAULT ''",
            }
            for col_name, col_def in new_columns.items():
                if col_name not in existing_names:
                    await conn.execute(text(f"ALTER TABLE projects ADD COLUMN IF NOT EXISTS {col_name} {col_def}"))

async def update_project_progress(project_id: int, progress: int) -> bool:
    """Update project progress (0-100)."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()
            if project:
                project.progress = max(0, min(100, progress))
                return True
            return False

async def update_project_models(project_id: int, model_ids: list) -> bool:
    """Update selected models for a project."""
    import json
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()
            if project:
                project.selected_models = json.dumps(model_ids)
                return True
            return False

async def delete_project(project_id: int) -> bool:
    """Delete a project and all related data (chat history, repo map)."""
    async with async_session() as session:
        async with session.begin():
            # Delete related chat history
            await session.execute(
                delete(ChatHistory).where(ChatHistory.project_id == project_id)
            )
            # Delete related repo map
            await session.execute(
                delete(RepoMap).where(RepoMap.project_id == project_id)
            )
            # Delete project
            result = await session.execute(
                select(Project).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()
            if project:
                await session.delete(project)
                return True
            return False

# ═══════════════════════════════════════════════════════════════
# IDEAS CRUD
# ═══════════════════════════════════════════════════════════════

async def save_idea(repo_url: str, name: str, summary: str, raw_data: str = "") -> dict:
    """Save or update an idea. Returns dict representation."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Idea).where(Idea.repo_url == repo_url)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.name = name
                existing.summary = summary
                existing.raw_data = raw_data
                return {"id": existing.id, "repo_url": existing.repo_url, "name": existing.name, "summary": existing.summary[:200], "created_at": str(existing.created_at)}
            else:
                idea = Idea(repo_url=repo_url, name=name, summary=summary, raw_data=raw_data)
                session.add(idea)
                session.flush()
                return {"id": idea.id, "repo_url": idea.repo_url, "name": idea.name, "summary": idea.summary[:200], "created_at": str(idea.created_at)}

async def get_all_ideas() -> list[dict]:
    """Get all ideas as list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(Idea).order_by(Idea.created_at.desc())
        )
        return [
            {"id": i.id, "repo_url": i.repo_url, "name": i.name, "summary": i.summary[:200], "created_at": str(i.created_at)}
            for i in result.scalars().all()
        ]

async def get_idea(idea_id: int) -> dict | None:
    """Get single idea by ID (full summary)."""
    async with async_session() as session:
        result = await session.execute(
            select(Idea).where(Idea.id == idea_id)
        )
        i = result.scalar_one_or_none()
        if i:
            return {"id": i.id, "repo_url": i.repo_url, "name": i.name, "summary": i.summary, "raw_data": i.raw_data, "created_at": str(i.created_at)}
        return None

async def delete_idea(idea_id: int) -> bool:
    """Delete an idea by ID."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Idea).where(Idea.id == idea_id)
            )
            idea = result.scalar_one_or_none()
            if idea:
                await session.delete(idea)
                return True
            return False

# ═══════════════════════════════════════════════════════════════
# CHAT HISTORY CRUD
# ═══════════════════════════════════════════════════════════════

async def save_message(project_id: int | None, role: str, content: str, thread_id: int | None = None):
    """Save a chat message."""
    async with async_session() as session:
        async with session.begin():
            session.add(ChatHistory(project_id=project_id, role=role, content=content, thread_id=thread_id))

async def get_history(project_id: int | None, limit: int = 50, thread_id: int | None = None) -> list[dict]:
    """Get chat history for a project, optionally filtered by thread_id."""
    async with async_session() as session:
        query = select(ChatHistory).where(ChatHistory.project_id == project_id)
        if thread_id is not None:
            query = query.where(ChatHistory.thread_id == thread_id)
        result = await session.execute(
            query.order_by(ChatHistory.timestamp.asc()).limit(limit)
        )
        return [{"role": m.role, "content": m.content} for m in result.scalars().all()]

async def clear_history(project_id: int | None):
    """Clear all chat history for a project."""
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                delete(ChatHistory).where(ChatHistory.project_id == project_id)
            )

async def get_message_count(project_id: int | None) -> int:
    """Count messages for a project."""
    async with async_session() as session:
        result = await session.execute(
            select(func.count(ChatHistory.id)).where(ChatHistory.project_id == project_id)
        )
        return result.scalar() or 0

# ═══════════════════════════════════════════════════════════════
# CHAT THREADS CRUD
# ═══════════════════════════════════════════════════════════════

async def create_thread(project_id: int, title: str = "Новый поток", parent_id: int | None = None) -> dict:
    """Create a new chat thread."""
    async with async_session() as session:
        async with session.begin():
            thread = ChatThread(project_id=project_id, title=title, parent_id=parent_id)
            session.add(thread)
            await session.flush()
            await session.refresh(thread)
            return {"id": thread.id, "project_id": thread.project_id, "parent_id": thread.parent_id, "title": thread.title, "created_at": str(thread.created_at)}

async def get_threads(project_id: int) -> list[dict]:
    """Get all threads for a project."""
    async with async_session() as session:
        result = await session.execute(
            select(ChatThread).where(ChatThread.project_id == project_id)
            .order_by(ChatThread.created_at.desc())
        )
        return [
            {"id": t.id, "project_id": t.project_id, "parent_id": t.parent_id, "title": t.title, "created_at": str(t.created_at)}
            for t in result.scalars().all()
        ]

async def get_thread(thread_id: int) -> dict | None:
    """Get a single thread by ID."""
    async with async_session() as session:
        result = await session.execute(
            select(ChatThread).where(ChatThread.id == thread_id)
        )
        t = result.scalar_one_or_none()
        if t:
            return {"id": t.id, "project_id": t.project_id, "parent_id": t.parent_id, "title": t.title, "created_at": str(t.created_at)}
        return None

async def rename_thread(thread_id: int, title: str) -> bool:
    """Rename a thread."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(ChatThread).where(ChatThread.id == thread_id)
            )
            thread = result.scalar_one_or_none()
            if thread:
                thread.title = title
                return True
            return False

async def delete_thread(thread_id: int) -> bool:
    """Delete a thread and all its messages."""
    async with async_session() as session:
        async with session.begin():
            # Удаляем все сообщения с этим thread_id
            await session.execute(
                delete(ChatHistory).where(ChatHistory.thread_id == thread_id)
            )
            # Удаляем сам поток
            result = await session.execute(
                select(ChatThread).where(ChatThread.id == thread_id)
            )
            thread = result.scalar_one_or_none()
            if thread:
                await session.delete(thread)
                return True
            return False

async def get_thread_messages(thread_id: int, limit: int = 50) -> list[dict]:
    """Get messages for a specific thread."""
    async with async_session() as session:
        result = await session.execute(
            select(ChatHistory).where(ChatHistory.thread_id == thread_id)
            .order_by(ChatHistory.timestamp.asc()).limit(limit)
        )
        return [{"role": m.role, "content": m.content, "timestamp": str(m.timestamp)} for m in result.scalars().all()]

# ═══════════════════════════════════════════════════════════════
# CONTEXT SNAPSHOTS CRUD
# ═══════════════════════════════════════════════════════════════

async def save_context_snapshot(
    project_id: int, thread_id: int | None, snapshot_type: str,
    title: str, summary: str, key_decisions: str,
    file_changes: str, errors_fixed: str,
    message_count_before: int, message_count_after: int
) -> dict:
    """Save a context snapshot."""
    async with async_session() as session:
        async with session.begin():
            snapshot = ContextSnapshot(
                project_id=project_id, thread_id=thread_id,
                snapshot_type=snapshot_type, title=title,
                summary=summary, key_decisions=key_decisions,
                file_changes=file_changes, errors_fixed=errors_fixed,
                message_count_before=message_count_before,
                message_count_after=message_count_after,
            )
            session.add(snapshot)
            await session.flush()
            await session.refresh(snapshot)
            return {
                "id": snapshot.id, "project_id": snapshot.project_id,
                "thread_id": snapshot.thread_id, "snapshot_type": snapshot.snapshot_type,
                "title": snapshot.title, "summary": snapshot.summary,
                "key_decisions": snapshot.key_decisions,
                "file_changes": snapshot.file_changes,
                "errors_fixed": snapshot.errors_fixed,
                "message_count_before": snapshot.message_count_before,
                "message_count_after": snapshot.message_count_after,
                "created_at": str(snapshot.created_at),
            }

async def get_context_snapshots(project_id: int, thread_id: int | None = None) -> list[dict]:
    """Get context snapshots for a project, optionally filtered by thread."""
    async with async_session() as session:
        query = select(ContextSnapshot).where(ContextSnapshot.project_id == project_id)
        if thread_id is not None:
            query = query.where(ContextSnapshot.thread_id == thread_id)
        query = query.order_by(ContextSnapshot.created_at.desc())
        result = await session.execute(query)
        return [
            {
                "id": s.id, "project_id": s.project_id, "thread_id": s.thread_id,
                "snapshot_type": s.snapshot_type, "title": s.title,
                "summary": s.summary, "key_decisions": s.key_decisions,
                "file_changes": s.file_changes, "errors_fixed": s.errors_fixed,
                "message_count_before": s.message_count_before,
                "message_count_after": s.message_count_after,
                "created_at": str(s.created_at),
            }
            for s in result.scalars().all()
        ]

async def delete_context_snapshot(snapshot_id: int) -> bool:
    """Delete a context snapshot."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(ContextSnapshot).where(ContextSnapshot.id == snapshot_id)
            )
            snapshot = result.scalar_one_or_none()
            if snapshot:
                await session.delete(snapshot)
                return True
            return False

async def delete_old_messages(project_id: int, keep_last: int = 10, thread_id: int | None = None) -> int:
    """Удалить старые сообщения, оставив последние N. Возвращает количество удалённых."""
    async with async_session() as session:
        async with session.begin():
            # Получаем ID последних keep_last сообщений
            query = select(ChatHistory.id).where(ChatHistory.project_id == project_id)
            if thread_id is not None:
                query = query.where(ChatHistory.thread_id == thread_id)
            query = query.order_by(ChatHistory.timestamp.desc()).limit(keep_last)
            result = await session.execute(query)
            keep_ids = [row[0] for row in result.all()]

            if not keep_ids:
                return 0

            # Удаляем все сообщения кроме последних
            del_query = delete(ChatHistory).where(ChatHistory.project_id == project_id)
            if thread_id is not None:
                del_query = del_query.where(ChatHistory.thread_id == thread_id)
            if keep_ids:
                del_query = del_query.where(ChatHistory.id.notin_(keep_ids))
            result = await session.execute(del_query)
            return result.rowcount

# ═══════════════════════════════════════════════════════════════
# REPO MAP CACHE
# ═══════════════════════════════════════════════════════════════

async def save_repo_map(project_id: int, content: str, file_hash: str):
    """Save or update repo map for a project (upsert)."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(RepoMap).where(RepoMap.project_id == project_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.content = content
                existing.file_hash = file_hash
                existing.updated_at = datetime.utcnow()
            else:
                session.add(RepoMap(project_id=project_id, content=content, file_hash=file_hash))

async def get_repo_map(project_id: int) -> dict | None:
    """Get cached repo map. Returns dict with 'hash' and 'content' or None."""
    async with async_session() as session:
        result = await session.execute(
            select(RepoMap).where(RepoMap.project_id == project_id)
        )
        entry = result.scalar_one_or_none()
        if entry:
            return {"hash": entry.file_hash, "content": entry.content}
        return None

# ═══════════════════════════════════════════════════════════════
# ROUTING STATS
# ═══════════════════════════════════════════════════════════════

async def save_routing_stat(prompt_summary: str, model: str, reason: str, success: bool):
    """Save a routing decision for analytics."""
    async with async_session() as session:
        async with session.begin():
            session.add(RoutingStat(
                prompt_summary=prompt_summary,
                model=model,
                reason=reason,
                success=success
            ))

async def get_routing_stats(limit: int = 100) -> list[dict]:
    """Get recent routing stats."""
    async with async_session() as session:
        result = await session.execute(
            select(RoutingStat).order_by(RoutingStat.timestamp.desc()).limit(limit)
        )
        return [
            {"id": s.id, "prompt_summary": s.prompt_summary, "model": s.model,
             "reason": s.reason, "success": s.success, "timestamp": str(s.timestamp)}
            for s in result.scalars().all()
        ]

# ═══════════════════════════════════════════════════════════════
# PROJECT ARCHIVES CRUD
# ═══════════════════════════════════════════════════════════════

async def save_project_archive(
    project_id: int, project_name: str, description: str,
    master_prompt: str, file_list: str, file_count: int, archive_path: str
) -> dict:
    """Save a project archive. Returns dict representation."""
    async with async_session() as session:
        async with session.begin():
            archive = ProjectArchive(
                project_id=project_id, project_name=project_name,
                description=description, master_prompt=master_prompt,
                file_list=file_list, file_count=file_count, archive_path=archive_path,
            )
            session.add(archive)
            await session.flush()
            await session.refresh(archive)
            return {
                "id": archive.id, "project_id": project_id,
                "project_name": project_name, "description": description,
                "file_count": file_count, "archive_path": archive_path,
                "created_at": str(archive.created_at),
            }

async def get_all_archives() -> list[dict]:
    """Get all archives as list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(ProjectArchive).order_by(ProjectArchive.created_at.desc())
        )
        return [
            {
                "id": a.id, "project_id": a.project_id,
                "project_name": a.project_name, "description": a.description,
                "file_count": a.file_count,
                "created_at": str(a.created_at),
            }
            for a in result.scalars().all()
        ]

async def get_archive(archive_id: int) -> dict | None:
    """Get single archive by ID (with master prompt)."""
    async with async_session() as session:
        result = await session.execute(
            select(ProjectArchive).where(ProjectArchive.id == archive_id)
        )
        a = result.scalar_one_or_none()
        if a:
            return {
                "id": a.id, "project_id": a.project_id,
                "project_name": a.project_name, "description": a.description,
                "master_prompt": a.master_prompt, "file_list": a.file_list,
                "file_count": a.file_count, "archive_path": a.archive_path,
                "created_at": str(a.created_at),
            }
        return None
