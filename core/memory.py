import os
from sqlalchemy import Text, select, delete, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime
import yaml

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()
engine = create_async_engine(CONFIG["system"]["db_url"], echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(unique=True, index=True)
    path: Mapped[str] = mapped_column(unique=True)
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
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow)

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

# ═══════════════════════════════════════════════════════════════
# INIT
# ═══════════════════════════════════════════════════════════════

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    os.makedirs(CONFIG["system"]["projects_dir"], exist_ok=True)
    os.makedirs(CONFIG["system"]["ideas_cache_dir"], exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# PROJECTS CRUD
# ═══════════════════════════════════════════════════════════════

async def create_project(name: str, path: str) -> dict:
    """Create a new project. Returns dict representation."""
    async with async_session() as session:
        async with session.begin():
            # Check for duplicates
            existing = await session.execute(
                select(Project).where((Project.name == name) | (Project.path == path))
            )
            if existing.scalar_one_or_none():
                return None  # Duplicate
            project = Project(name=name, path=path)
            session.add(project)
            session.flush()
            # Create the project directory on disk
            os.makedirs(path, exist_ok=True)
            return {"id": project.id, "name": project.name, "path": project.path, "created_at": str(project.created_at)}

async def get_all_projects() -> list[dict]:
    """Get all projects as list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(Project).order_by(Project.created_at.desc())
        )
        return [
            {"id": p.id, "name": p.name, "path": p.path, "created_at": str(p.created_at)}
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
            return {"id": p.id, "name": p.name, "path": p.path, "created_at": str(p.created_at)}
        return None

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

async def save_message(project_id: int | None, role: str, content: str):
    """Save a chat message."""
    async with async_session() as session:
        async with session.begin():
            session.add(ChatHistory(project_id=project_id, role=role, content=content))

async def get_history(project_id: int | None, limit: int = 50) -> list[dict]:
    """Get chat history for a project."""
    async with async_session() as session:
        result = await session.execute(
            select(ChatHistory).where(ChatHistory.project_id == project_id)
            .order_by(ChatHistory.timestamp.asc()).limit(limit)
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
