import os
from sqlalchemy import Text, select
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

class Idea(Base):
    __tablename__ = "ideas"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    repo_url: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

class ChatHistory(Base):
    __tablename__ = "chat_history"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int | None] = mapped_column(nullable=True)
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    os.makedirs(CONFIG["system"]["projects_dir"], exist_ok=True)
    os.makedirs(CONFIG["system"]["ideas_cache_dir"], exist_ok=True)

async def save_message(project_id: int | None, role: str, content: str):
    async with async_session() as session:
        async with session.begin():
            session.add(ChatHistory(project_id=project_id, role=role, content=content))

async def get_history(project_id: int | None, limit: int = 50) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(ChatHistory).where(ChatHistory.project_id == project_id)
            .order_by(ChatHistory.timestamp.asc()).limit(limit)
        )
        return [{"role": m.role, "content": m.content} for m in result.scalars().all()]