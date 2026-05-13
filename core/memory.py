import os
import re
import uuid
from sqlalchemy import Text, select, delete, func, String, Boolean, ForeignKey, Column, DateTime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime, timedelta, timezone
import yaml

from core.action_logger import get_logger
logger = get_logger()

def load_config():
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    # Default config for cloud deployments
    return {
        "llm": {"default_model": "", "router_model": "", "api_base": "", "api_key": "", "temperature": 0.2, "max_tokens": 4096},
        "system": {"db_url": "", "projects_dir": "/app/data/projects", "ideas_cache_dir": "/app/data/.cache/ideas", "archives_dir": "/app/data/archives", "max_iterations": 3, "max_context_files": 20, "max_idea_files": 10, "max_file_size_kb": 50},
        "security": {"allowed_commands": ["git", "python", "pip", "npm", "node", "cat", "ls", "dir", "echo", "mkdir", "cd"], "blocked_patterns": ["rm -rf /", "DROP DATABASE", "FORMAT C:"]},
    }

CONFIG = load_config()

# ═══════════════════════════════════════════════════════════════
# DATABASE CONNECTION — PostgreSQL priority, SQLite fallback
# ═══════════════════════════════════════════════════════════════
def _resolve_db_url() -> tuple[str, bool]:
    """
    Resolve database URL with priority:
    1. DATABASE_URL env var (Supabase, Neon, Render Postgres, etc.)
    2. config.yaml db_url
    3. Fallback to SQLite (local development only)

    Returns: (db_url, is_postgres)
    """
    def _make_asyncpg_url(raw_url: str) -> str:
        """Convert any postgres:// or postgresql:// URL to asyncpg format.
        
        asyncpg uses 'ssl=require' instead of 'sslmode=require' (psycopg2 style).
        Also removes 'channel_binding' param which asyncpg doesn't support.
        """
        import re
        url = raw_url.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if "+asyncpg" not in url:
            url = "postgresql+asyncpg://" + url.split("://", 1)[1]
        # Convert psycopg2-style 'sslmode=require' → asyncpg-style 'ssl=require'
        url = re.sub(r'([?&])sslmode=', r'\1ssl=', url)
        # Remove channel_binding param (not supported by asyncpg)
        url = re.sub(r'[?&]channel_binding=[^&]*', '', url)
        # Ensure SSL is set for cloud providers (Neon, Supabase, Render)
        if "ssl=" not in url.lower():
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}ssl=require"
        return url

    # 1. Environment variable (highest priority — for cloud deployments)
    env_url = os.environ.get("DATABASE_URL", "")
    if env_url:
        if "postgres" in env_url:
            return _make_asyncpg_url(env_url), True
        elif "sqlite" in env_url:
            return env_url, False

    # 2. Config file
    config_url = CONFIG["system"].get("db_url", "")
    if config_url:
        if "postgres" in config_url:
            return _make_asyncpg_url(config_url), True
        elif "sqlite" in config_url:
            return config_url, False

    # 3. Fallback: SQLite (local development)
    print("  [db] DATABASE_URL не задан — используется SQLite (локальный режим)")
    return "sqlite+aiosqlite:///data/fosved_coder.db", False

DB_URL, IS_POSTGRES = _resolve_db_url()

# Engine settings based on DB type
if IS_POSTGRES:
    # Sanitize URL for logging (hide password)
    safe_url = DB_URL
    if "://" in safe_url:
        parts = safe_url.split("://", 1)
        auth_part = parts[1].split("@", 1)
        if len(auth_part) == 2 and ":" in auth_part[0]:
            user = auth_part[0].split(":")[0]
            safe_url = f"{parts[0]}://{user}:****@{auth_part[1]}"
    engine = create_async_engine(
        DB_URL,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=300,
        pool_pre_ping=True,  # Auto-detect stale connections
    )
    print(f"  [db] PostgreSQL подключен: {safe_url}")
else:
    engine = create_async_engine(DB_URL, echo=False)
    print(f"  [db] SQLite: {DB_URL}")

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
    uuid_key: Mapped[str] = mapped_column(String(36), unique=True, index=True, default="")  # Unique project key
    progress: Mapped[int] = mapped_column(default=0)  # 0-100 percent
    template: Mapped[str] = mapped_column(default="")  # Project template: fastapi, react, nextjs, etc.
    apk_config: Mapped[str] = mapped_column(Text, default="")  # JSON config for APK building
    logo: Mapped[str] = mapped_column(Text, default="")  # base64 image or URL
    design: Mapped[str] = mapped_column(Text, default="")  # JSON with design preferences
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Idea(Base):
    __tablename__ = "ideas"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    repo_url: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    summary: Mapped[str] = mapped_column(Text, default="")
    raw_data: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ChatHistory(Base):
    __tablename__ = "chat_history"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    thread_id: Mapped[int | None] = mapped_column(nullable=True, index=True, default=None)
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ChatThread(Base):
    __tablename__ = "chat_threads"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(index=True)
    parent_id: Mapped[int | None] = mapped_column(nullable=True, default=None)
    title: Mapped[str] = mapped_column(default="Новый поток")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RepoMap(Base):
    __tablename__ = "repo_maps"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    project_id: Mapped[int] = mapped_column(unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    file_hash: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RoutingStat(Base):
    __tablename__ = "routing_stats"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    prompt_summary: Mapped[str] = mapped_column(default="")
    model: Mapped[str] = mapped_column(default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(default=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class PromptDraft(Base):
    """Черновик промпта-анкеты — подготовка перед созданием проекта."""
    __tablename__ = "prompt_drafts"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(default="Новый проект")  # рабочее название/идея
    template: Mapped[str] = mapped_column(default="")  # fastapi, react, nextjs, expo, flask, python-cli, ""
    status: Mapped[str] = mapped_column(default="draft")  # draft | ready | converted
    # JSON: {step_id: answer, ...} — ответы на все шаги анкеты
    answers: Mapped[str] = mapped_column(Text, default="{}")
    # Сгенерированный финальный промпт (из ответов анкеты)
    generated_prompt: Mapped[str] = mapped_column(Text, default="")
    # JSON: [{role, content, timestamp}, ...] — контекст обсуждения с ИИ
    discussion: Mapped[str] = mapped_column(Text, default="[]")
    # Текущий шаг анкеты (0-based)
    current_step: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class SystemSetting(Base):
    """Системные настройки — персистентное хранилище ключей и конфигов в БД."""
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(primary_key=True)  # уникальный ключ
    value: Mapped[str] = mapped_column(Text, default="")  # JSON или текст
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Questionnaire(Base):
    """Анкета проекта — динамический чат-опросник для создания проекта (Фаза 3.1)."""
    __tablename__ = "questionnaires"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("projects.uuid_key"), nullable=True)
    title = Column(Text, default="")
    questions = Column(Text, default="[]")  # JSON: [{"question": "...", "answer": "...", "category": "..."}]
    project_card = Column(Text, default="{}")  # JSON: {"name": "...", "description": "...", ...}
    status = Column(String, default="draft")  # draft, completed, converted
    created_at = Column(String, default=lambda: datetime.now(timezone.utc).isoformat())
    updated_at = Column(String, default=lambda: datetime.now(timezone.utc).isoformat())

# ═══════════════════════════════════════════════════════════════
# INIT
# ═══════════════════════════════════════════════════════════════

async def check_db_connection(max_retries: int = 5, delay: float = 3.0) -> bool:
    """Проверить подключение к БД с повторными попытками. Возвращает True если OK."""
    import asyncio
    from sqlalchemy import text
    for attempt in range(1, max_retries + 1):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                result.scalar()
            return True
        except Exception as e:
            if attempt < max_retries:
                print(f"  [db] Попытка {attempt}/{max_retries} не удалась: {e}. Повтор через {delay}с...")
                await asyncio.sleep(delay)
            else:
                print(f"  [db] ОШИБКА: Не удалось подключиться к БД после {max_retries} попыток: {e}")
                try:
                    logger.log("db_connection_failed", level="error", source="db",
                               details={"attempts": max_retries}, error=str(e))
                except Exception:
                    pass
                return False
    return False


async def init_db():
    """Initialize database tables and directories."""
    db_type = 'PostgreSQL' if IS_POSTGRES else 'SQLite'
    print(f"  [db] Инициализация БД ({db_type})...")

    try:
        logger.log("db_init_start", level="info", source="db",
                   details={"db_type": db_type, "is_postgres": IS_POSTGRES})
    except Exception:
        pass

    # Verify connection first (with retries for cloud Postgres)
    connected = await check_db_connection(max_retries=5, delay=3.0)
    if not connected:
        if IS_POSTGRES:
            print("  [db] КРИТИЧЕСКАЯ ОШИБКА: PostgreSQL недоступен. Проверьте DATABASE_URL.")
            try:
                logger.log("db_connection_failed", level="error", source="db",
                           details={"db_type": db_type}, error="PostgreSQL unavailable after retries")
            except Exception:
                pass
        # For SQLite, try to continue anyway (might be a permission issue)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_db()

    # Report table count
    try:
        from sqlalchemy import text
        async with engine.connect() as conn:
            if IS_POSTGRES:
                result = await conn.execute(text(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
                ))
                count = result.scalar()
            else:
                result = await conn.execute(text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ))
                count = result.scalar()
            print(f"  [db] Таблиц создано: {count}")
            try:
                logger.log("db_init_complete", level="success", source="db",
                           details={"tables": count, "db_type": db_type})
            except Exception:
                pass
    except Exception as e:
        print(f"  [db] Предупреждение: не удалось подсчитать таблицы: {e}")
        try:
            logger.log("db_table_count_error", level="warning", source="db", error=str(e))
        except Exception:
            pass

    # Создаём директории только для SQLite (на облаке может не быть доступа к /app/data)
    if not IS_POSTGRES:
        try:
            os.makedirs(CONFIG["system"]["projects_dir"], exist_ok=True)
            os.makedirs(CONFIG["system"]["ideas_cache_dir"], exist_ok=True)
        except Exception as e:
            print(f"  [db] Предупреждение: не удалось создать директории: {e}")

# ═══════════════════════════════════════════════════════════════
# PROJECTS CRUD
# ═══════════════════════════════════════════════════════════════

async def create_project(name: str, path: str, description: str = "", base_prompt: str = "", ideas: str = "", github_repo: str = "", github_token: str = "", local_path: str = "", template: str = "") -> dict:
    """Create a new project. Returns dict representation."""
    import json
    async with async_session() as session:
        async with session.begin():
            existing = await session.execute(
                select(Project).where((Project.name == name) | (Project.path == path))
            )
            if existing.scalar_one_or_none():
                return None
            project = Project(name=name, path=path, description=description, base_prompt=base_prompt, ideas=ideas, github_repo=github_repo, github_token=github_token, local_path=local_path, uuid_key=str(uuid.uuid4()), template=template)
            session.add(project)
            await session.flush()
            await session.refresh(project)
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass  # На облаке директория может быть read-only
            return {"id": project.id, "name": project.name, "path": project.path, "description": project.description, "base_prompt": project.base_prompt, "ideas": project.ideas, "selected_models": project.selected_models, "github_repo": project.github_repo, "github_token": project.github_token, "local_path": project.local_path, "uuid_key": project.uuid_key, "progress": project.progress, "template": project.template, "apk_config": project.apk_config, "logo": project.logo, "design": project.design, "created_at": str(project.created_at)}

async def get_all_projects() -> list[dict]:
    """Get all projects as list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(Project).order_by(Project.created_at.desc())
        )
        return [
            {"id": p.id, "name": p.name, "path": p.path, "description": p.description, "base_prompt": p.base_prompt, "ideas": p.ideas, "selected_models": p.selected_models, "github_repo": p.github_repo, "github_token": p.github_token, "local_path": p.local_path, "uuid_key": p.uuid_key, "progress": p.progress, "template": p.template, "apk_config": p.apk_config, "logo": p.logo, "design": p.design, "created_at": str(p.created_at)}
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
            return {"id": p.id, "name": p.name, "path": p.path, "description": p.description, "base_prompt": p.base_prompt, "ideas": p.ideas, "selected_models": p.selected_models, "github_repo": p.github_repo, "github_token": p.github_token, "local_path": p.local_path, "uuid_key": p.uuid_key, "progress": p.progress, "template": p.template, "apk_config": p.apk_config, "logo": p.logo, "design": p.design, "created_at": str(p.created_at)}
        return None


async def get_project_token_by_path(project_path: str) -> str | None:
    """Get github_token for a project by its filesystem path."""
    if not project_path:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.path == project_path)
        )
        p = result.scalar_one_or_none()
        if p and p.github_token:
            return p.github_token
        return None


async def git_push_with_token(executor, project_path: str, project_token: str | None) -> str:
    """
    Perform git push, using project-specific PAT token if available.
    Temporarily sets the remote URL with embedded token, then restores
    the original URL for security. Falls back to normal push if no token.

    Args:
        executor: CommandExecutor instance with .execute() method
        project_path: Absolute path to the git repository
        project_token: GitHub PAT token string, or None

    Returns:
        Combined stdout + stderr from the git push command.
    """
    push_out = ""
    url_restored = False

    if project_token and project_path:
        try:
            # Get current remote URL
            r_remote = await executor.execute(
                "git remote get-url origin", cwd=project_path,
                need_approval=False, timeout=10,
            )
            remote_url = (r_remote.get("stdout", "") or "").strip()

            if remote_url and "github.com" in remote_url:
                # Build authenticated URL: replace https://...@github.com or https://github.com
                auth_url = re.sub(
                    r'https://[^@]+@github\.com',
                    f'https://{project_token}@github.com',
                    remote_url,
                )
                if "@github.com" not in auth_url:
                    auth_url = remote_url.replace(
                        "https://github.com",
                        f"https://{project_token}@github.com",
                    )

                # Set authenticated URL
                await executor.execute(
                    f"git remote set-url origin {auth_url}",
                    cwd=project_path, need_approval=False, timeout=10,
                )

                # Push with token
                r3 = await executor.execute(
                    "git push", cwd=project_path,
                    need_approval=False, timeout=30,
                )
                push_out = (r3.get("stdout", "") or "") + (r3.get("stderr", "") or "")

                # Restore original URL (security!)
                await executor.execute(
                    f"git remote set-url origin {remote_url}",
                    cwd=project_path, need_approval=False, timeout=10,
                )
                url_restored = True
        except Exception as e:
            logger.log(
                f"git_push_token_setup_error: {str(e)[:200]}",
                level="warning", source="memory",
            )

    # Fallback: normal push (no token, or token setup failed)
    if not url_restored:
        r3 = await executor.execute(
            "git push", cwd=project_path,
            need_approval=False, timeout=30,
        )
        push_out = (r3.get("stdout", "") or "") + (r3.get("stderr", "") or "")

    return push_out


async def migrate_db():
    """Add new columns if they don't exist (for upgrades)."""
    if IS_POSTGRES:
        from sqlalchemy import text
        async with engine.begin() as conn:
            new_columns = {
                "github_repo": "TEXT DEFAULT ''",
                "github_token": "TEXT DEFAULT ''",
                "local_path": "TEXT DEFAULT ''",
                "uuid_key": "VARCHAR(36) DEFAULT ''",
                "template": "TEXT DEFAULT 'react'",
                "apk_config": "TEXT DEFAULT NULL",
                "logo": "TEXT DEFAULT ''",
                "design": "TEXT DEFAULT ''",
            }
            for col_name, col_def in new_columns.items():
                try:
                    await conn.execute(text(f"ALTER TABLE projects ADD COLUMN IF NOT EXISTS {col_name} {col_def}"))
                except Exception:
                    pass  # Column may already exist
            # Generate UUID for existing projects that don't have one
            await conn.execute(text(
                "UPDATE projects SET uuid_key = gen_random_uuid()::text WHERE uuid_key = '' OR uuid_key IS NULL"
            ))
            # Create questionnaires table if not exists (PostgreSQL)
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS questionnaires (
                    id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(uuid_key),
                    title TEXT DEFAULT '',
                    questions TEXT DEFAULT '[]',
                    project_card TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'draft',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """))
            # Migrate TIMESTAMP → TIMESTAMPTZ for all datetime columns
            _tz_migrations = [
                "ALTER TABLE projects ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE ideas ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE chat_history ALTER COLUMN timestamp TYPE TIMESTAMPTZ USING timestamp AT TIME ZONE 'UTC'",
                "ALTER TABLE chat_threads ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE context_snapshots ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE repo_maps ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC'",
                "ALTER TABLE routing_stats ALTER COLUMN timestamp TYPE TIMESTAMPTZ USING timestamp AT TIME ZONE 'UTC'",
                "ALTER TABLE project_archives ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE prompt_drafts ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE prompt_drafts ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC'",
                "ALTER TABLE system_settings ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC'",
                "ALTER TABLE observations ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
                "ALTER TABLE session_summaries ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC'",
            ]
            for mig_sql in _tz_migrations:
                try:
                    await conn.execute(text(mig_sql))
                except Exception:
                    pass  # Column may already be TIMESTAMPTZ or table doesn't exist
    else:
        import sqlite3
        db_file = DB_URL.split(":///")[-1] if ":///" in DB_URL else "fosved_coder.db"
        try:
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(projects)")
            existing = {row[1] for row in cursor.fetchall()}
            new_columns = [
                ("github_repo", "TEXT", "''"),
                ("github_token", "TEXT", "''"),
                ("local_path", "TEXT", "''"),
                ("uuid_key", "VARCHAR(36)", "''"),
                ("template", "TEXT", "'react'"),
                ("apk_config", "TEXT", "NULL"),
                ("logo", "TEXT", "''"),
                ("design", "TEXT", "''"),
            ]
            for col_name, col_type, col_default in new_columns:
                if col_name not in existing:
                    cursor.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_type} DEFAULT {col_default}")
            # Generate UUID for existing SQLite projects
            cursor.execute("SELECT id FROM projects WHERE uuid_key = '' OR uuid_key IS NULL")
            rows = cursor.fetchall()
            for row in rows:
                cursor.execute(f"UPDATE projects SET uuid_key = '{uuid.uuid4()}' WHERE id = {row[0]}")
            # Create questionnaires table if not exists (SQLite)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS questionnaires (
                    id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(uuid_key),
                    title TEXT DEFAULT '',
                    questions TEXT DEFAULT '[]',
                    project_card TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'draft',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            pass

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
# PROMPT DRAFTS CRUD (Анкеты — подготовка к созданию проекта)
# ═══════════════════════════════════════════════════════════════

async def create_prompt_draft(title: str = "Новый проект", template: str = "") -> dict:
    """Создать новый черновик промпт-анкеты."""
    import json
    async with async_session() as session:
        async with session.begin():
            draft = PromptDraft(title=title, template=template)
            session.add(draft)
            await session.flush()
            return {
                "id": draft.id,
                "title": draft.title,
                "template": draft.template,
                "status": draft.status,
                "answers": draft.answers,
                "generated_prompt": draft.generated_prompt,
                "discussion": draft.discussion,
                "current_step": draft.current_step,
                "created_at": str(draft.created_at),
                "updated_at": str(draft.updated_at),
            }

async def get_prompt_draft(draft_id: int) -> dict | None:
    """Получить черновик по ID."""
    import json
    async with async_session() as session:
        result = await session.execute(select(PromptDraft).where(PromptDraft.id == draft_id))
        d = result.scalar_one_or_none()
        if d:
            return {
                "id": d.id,
                "title": d.title,
                "template": d.template,
                "status": d.status,
                "answers": json.loads(d.answers) if d.answers else {},
                "generated_prompt": d.generated_prompt or "",
                "discussion": json.loads(d.discussion) if d.discussion else [],
                "current_step": d.current_step,
                "created_at": str(d.created_at),
                "updated_at": str(d.updated_at),
            }
        return None

async def list_prompt_drafts() -> list[dict]:
    """Список всех черновиков (только draft и ready, без converted)."""
    import json
    async with async_session() as session:
        result = await session.execute(
            select(PromptDraft).where(PromptDraft.status != "converted").order_by(PromptDraft.updated_at.desc())
        )
        return [
            {
                "id": d.id,
                "title": d.title,
                "template": d.template,
                "status": d.status,
                "current_step": d.current_step,
                "created_at": str(d.created_at),
                "updated_at": str(d.updated_at),
            }
            for d in result.scalars().all()
        ]

async def update_prompt_draft(draft_id: int, **kwargs) -> dict | None:
    """Обновить черновик (title, template, answers, generated_prompt, discussion, current_step, status)."""
    import json
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(PromptDraft).where(PromptDraft.id == draft_id))
            draft = result.scalar_one_or_none()
            if not draft:
                return None
            if "title" in kwargs:
                draft.title = kwargs["title"]
            if "template" in kwargs:
                draft.template = kwargs["template"]
            if "answers" in kwargs:
                draft.answers = json.dumps(kwargs["answers"], ensure_ascii=False) if isinstance(kwargs["answers"], dict) else kwargs["answers"]
            if "generated_prompt" in kwargs:
                draft.generated_prompt = kwargs["generated_prompt"]
            if "discussion" in kwargs:
                draft.discussion = json.dumps(kwargs["discussion"], ensure_ascii=False) if isinstance(kwargs["discussion"], list) else kwargs["discussion"]
            if "current_step" in kwargs:
                draft.current_step = kwargs["current_step"]
            if "status" in kwargs:
                draft.status = kwargs["status"]
            draft.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return {
                "id": draft.id,
                "title": draft.title,
                "template": draft.template,
                "status": draft.status,
                "answers": json.loads(draft.answers) if draft.answers else {},
                "generated_prompt": draft.generated_prompt or "",
                "discussion": json.loads(draft.discussion) if draft.discussion else [],
                "current_step": draft.current_step,
                "created_at": str(draft.created_at),
                "updated_at": str(draft.updated_at),
            }

async def delete_prompt_draft(draft_id: int) -> bool:
    """Удалить черновик."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(PromptDraft).where(PromptDraft.id == draft_id))
            draft = result.scalar_one_or_none()
            if draft:
                await session.delete(draft)
                return True
            return False

# ═══════════════════════════════════════════════════════════════
# SYSTEM SETTINGS (Персистентное хранилище в БД)
# ═══════════════════════════════════════════════════════════════

async def get_system_setting(key: str) -> str | None:
    """Получить значение системной настройки."""
    async with async_session() as session:
        result = await session.execute(select(SystemSetting).where(SystemSetting.key == key))
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

async def set_system_setting(key: str, value: str):
    """Сохранить системную настройку (upsert)."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()
            if setting:
                setting.value = value
                setting.updated_at = datetime.now(timezone.utc)
            else:
                session.add(SystemSetting(key=key, value=value))

# ═══════════════════════════════════════════════════════════════
# CHAT HISTORY CRUD
# ═══════════════════════════════════════════════════════════════

async def save_message(project_id: int | None, role: str, content: str, thread_id: int | None = None):
    """Save a chat message. thread_id is accepted for backward compatibility but ignored."""
    async with async_session() as session:
        async with session.begin():
            session.add(ChatHistory(project_id=project_id, role=role, content=content, thread_id=None))

async def get_history(project_id: int | None, limit: int = 50, thread_id: int | None = None) -> list[dict]:
    """Get chat history for a project. thread_id is accepted for backward compatibility but ignored."""
    async with async_session() as session:
        query = select(ChatHistory).where(ChatHistory.project_id == project_id)
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

async def clear_main_chat_history(days: int = 10) -> int:
    """Delete main chat messages older than N days. Returns count deleted."""
    from sqlalchemy import text
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if IS_POSTGRES:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        "DELETE FROM chat_history WHERE project_id IS NULL AND timestamp < :cutoff"
                    ),
                    {"cutoff": cutoff},
                )
                return result.rowcount
    else:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(ChatHistory).where(
                        ChatHistory.project_id == None,
                        ChatHistory.timestamp < cutoff,
                    )
                )
                return result.rowcount

# ═══════════════════════════════════════════════════════════════
# CHAT THREADS — CRUD functions removed (threads deprecated)
# ChatThread ORM class kept above for backward compatibility with existing DBs.
# ═══════════════════════════════════════════════════════════════

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
                existing.updated_at = datetime.now(timezone.utc)
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

# ═══════════════════════════════════════════════════════════════
# QUESTIONNAIRE CRUD (Анкеты — Фаза 3.1)
# ═══════════════════════════════════════════════════════════════

async def save_questionnaire(data: dict) -> str:
    """Создать или обновить анкету. Возвращает id."""
    import json
    q_id = data.get("id")
    async with async_session() as session:
        async with session.begin():
            if q_id:
                result = await session.execute(
                    select(Questionnaire).where(Questionnaire.id == q_id)
                )
                q = result.scalar_one_or_none()
                if q:
                    if "title" in data:
                        q.title = data["title"]
                    if "project_id" in data:
                        q.project_id = data["project_id"]
                    if "questions" in data:
                        q.questions = json.dumps(data["questions"], ensure_ascii=False) if isinstance(data["questions"], list) else data["questions"]
                    if "project_card" in data:
                        q.project_card = json.dumps(data["project_card"], ensure_ascii=False) if isinstance(data["project_card"], dict) else data["project_card"]
                    if "status" in data:
                        q.status = data["status"]
                    q.updated_at = datetime.now(timezone.utc).isoformat()
                    return q.id
            # Создание новой анкеты
            questions = data.get("questions", [])
            project_card = data.get("project_card", {})
            q = Questionnaire(
                project_id=data.get("project_id"),
                title=data.get("title", ""),
                questions=json.dumps(questions, ensure_ascii=False) if isinstance(questions, list) else questions,
                project_card=json.dumps(project_card, ensure_ascii=False) if isinstance(project_card, dict) else project_card,
                status=data.get("status", "draft"),
            )
            session.add(q)
            await session.flush()
            return q.id


async def get_questionnaire(q_id: str) -> dict | None:
    """Получить анкету по ID."""
    import json
    async with async_session() as session:
        result = await session.execute(
            select(Questionnaire).where(Questionnaire.id == q_id)
        )
        q = result.scalar_one_or_none()
        if q:
            return {
                "id": q.id,
                "project_id": q.project_id,
                "title": q.title,
                "questions": json.loads(q.questions) if q.questions else [],
                "project_card": json.loads(q.project_card) if q.project_card else {},
                "status": q.status,
                "created_at": q.created_at,
                "updated_at": q.updated_at,
            }
        return None


async def get_questionnaires_by_project(project_id: str) -> list[dict]:
    """Получить список анкет для проекта по UUID ключу."""
    import json
    async with async_session() as session:
        result = await session.execute(
            select(Questionnaire)
            .where(Questionnaire.project_id == project_id)
            .order_by(Questionnaire.created_at.desc())
        )
        return [
            {
                "id": q.id,
                "project_id": q.project_id,
                "title": q.title,
                "questions": json.loads(q.questions) if q.questions else [],
                "project_card": json.loads(q.project_card) if q.project_card else {},
                "status": q.status,
                "created_at": q.created_at,
                "updated_at": q.updated_at,
            }
            for q in result.scalars().all()
        ]


async def delete_questionnaire(q_id: str) -> bool:
    """Удалить анкету по ID."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                select(Questionnaire).where(Questionnaire.id == q_id)
            )
            q = result.scalar_one_or_none()
            if q:
                await session.delete(q)
                return True
            return False


# ═══════════════════════════════════════════════════════════════
# PROBED MODELS (кэш результатов silent probing)
# ═══════════════════════════════════════════════════════════════

async def save_probed_models(models: list[dict]):
    """Сохранить результаты silent model probing в SystemSetting + обновить кэш KeysManager."""
    import json
    await set_system_setting("probed_models", json.dumps(models, ensure_ascii=False))
    # Задача 3: Обновить in-memory кэш для фильтрации в get_all_models()
    try:
        from core.keys_manager import keys_manager
        keys_manager.update_probed_model_ids(models)
    except Exception:
        pass


async def get_probed_models() -> list[dict]:
    """Получить кэшированные результаты probing из SystemSetting."""
    import json
    raw = await get_system_setting("probed_models")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []
