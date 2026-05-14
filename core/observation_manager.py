"""
FOSVED CODER V2.0 — Observation Manager (вдохновлено claude-mem)

Система памяти: захватывает наблюдения (tool use, ошибки, решения),
сжимает через LLM, хранит в БД и извлекает релевантный контекст
при старте сессии.

Архитектура:
  Observation — сжатая запись о действии (tool call, ошибка, решение)
  SessionSummary — AI-сгенерированное резюме сессии
  3-layer search: search() → timeline() → get_details()
"""

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, select, delete, func, String, Boolean, Integer, Float, Index, and_, or_, desc, asc, text as sa_text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from core.memory import Base, async_session, engine, IS_POSTGRES
from core.action_logger import get_logger

logger = get_logger()

# ═══════════════════════════════════════════════════════════════
# DATABASE MODELS
# ═══════════════════════════════════════════════════════════════

class Observation(Base):
    """Сжатая запись о действии — основная 'память' системы."""
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    obs_type: Mapped[str] = mapped_column(String(30), index=True, default="tool_use")
    # Типы: tool_use, error, bugfix, decision, file_edit, command, insight

    content: Mapped[str] = mapped_column(Text, default="")
    raw_content: Mapped[str] = mapped_column(Text, default="")

    tool_name: Mapped[str] = mapped_column(String(100), default="")
    model_id: Mapped[str] = mapped_column(String(100), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)

    is_compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SessionSummary(Base):
    """AI-сгенерированное резюме работы за сессию."""
    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    summary: Mapped[str] = mapped_column(Text, default="")
    key_decisions: Mapped[str] = mapped_column(Text, default="[]")
    files_modified: Mapped[str] = mapped_column(Text, default="[]")
    errors_fixed: Mapped[str] = mapped_column(Text, default="[]")

    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    tokens_total: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


# ═══════════════════════════════════════════════════════════════
# TABLE CREATION
# ═══════════════════════════════════════════════════════════════

async def ensure_observation_tables():
    """Создать таблицы observations и session_summaries."""
    async with engine.begin() as conn:
        if IS_POSTGRES:
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS observations (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(36) DEFAULT '',
                    project_id INTEGER DEFAULT NULL,
                    obs_type VARCHAR(30) DEFAULT 'tool_use',
                    content TEXT DEFAULT '',
                    raw_content TEXT DEFAULT '',
                    tool_name VARCHAR(100) DEFAULT '',
                    model_id VARCHAR(100) DEFAULT '',
                    file_path VARCHAR(500) DEFAULT '',
                    tokens_used INTEGER DEFAULT 0,
                    is_compressed BOOLEAN DEFAULT FALSE,
                    is_private BOOLEAN DEFAULT FALSE,
                    relevance_score FLOAT DEFAULT 0.0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(36) DEFAULT '',
                    project_id INTEGER DEFAULT NULL,
                    summary TEXT DEFAULT '',
                    key_decisions TEXT DEFAULT '[]',
                    files_modified TEXT DEFAULT '[]',
                    errors_fixed TEXT DEFAULT '[]',
                    observation_count INTEGER DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    tokens_total INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(project_id)",
                "CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id)",
                "CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(obs_type)",
                "CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_sum_project ON session_summaries(project_id)",
                "CREATE INDEX IF NOT EXISTS idx_sum_session ON session_summaries(session_id)",
            ]:
                try:
                    await conn.execute(sa_text(idx_sql))
                except Exception:
                    pass
            try:
                await conn.execute(sa_text("""
                    CREATE INDEX IF NOT EXISTS idx_obs_content_fts ON observations
                    USING gin(to_tsvector('english', content))
                """))
            except Exception:
                pass
        else:
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    project_id INTEGER DEFAULT NULL,
                    obs_type TEXT DEFAULT 'tool_use',
                    content TEXT DEFAULT '',
                    raw_content TEXT DEFAULT '',
                    tool_name TEXT DEFAULT '',
                    model_id TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    tokens_used INTEGER DEFAULT 0,
                    is_compressed INTEGER DEFAULT 0,
                    is_private INTEGER DEFAULT 0,
                    relevance_score REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    project_id INTEGER DEFAULT NULL,
                    summary TEXT DEFAULT '',
                    key_decisions TEXT DEFAULT '[]',
                    files_modified TEXT DEFAULT '[]',
                    errors_fixed TEXT DEFAULT '[]',
                    observation_count INTEGER DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    tokens_total INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            try:
                await conn.execute(sa_text("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                        content, tool_name, file_path, obs_type,
                        content=observations, content_rowid=id
                    )
                """))
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# OBSERVATION CRUD
# ═══════════════════════════════════════════════════════════════

async def store_observation(
    session_id: str,
    content: str,
    obs_type: str = "tool_use",
    project_id: int | None = None,
    tool_name: str = "",
    model_id: str = "",
    file_path: str = "",
    raw_content: str = "",
    is_private: bool = False,
    is_compressed: bool = False,
    tokens_used: int = 0,
) -> int:
    """Сохранить наблюдение. Возвращает ID."""
    # Strip <private> content before saving for privacy
    content, _has_private = strip_private_tags(content)
    if raw_content:
        raw_content, _ = strip_private_tags(raw_content)
    if _has_private:
        is_private = True
    async with async_session() as session:
        async with session.begin():
            obs = Observation(
                session_id=session_id,
                project_id=project_id,
                obs_type=obs_type,
                content=content,
                raw_content=raw_content[:10000] if raw_content else "",
                tool_name=tool_name[:100],
                model_id=model_id[:100],
                file_path=file_path[:500],
                tokens_used=tokens_used,
                is_compressed=is_compressed,
                is_private=is_private,
            )
            session.add(obs)
            await session.flush()
            obs_id = obs.id

    if not IS_POSTGRES and content:
        try:
            async with engine.begin() as conn:
                await conn.execute(sa_text(
                    "INSERT INTO observations_fts(rowid, content, tool_name, file_path, obs_type) VALUES (?, ?, ?, ?, ?)"
                ), (obs_id, content, tool_name, file_path, obs_type))
        except Exception:
            pass

    return obs_id


async def get_observation(obs_id: int) -> dict | None:
    async with async_session() as session:
        result = await session.execute(
            select(Observation).where(Observation.id == obs_id)
        )
        obs = result.scalar_one_or_none()
        if obs:
            return _obs_to_dict(obs)
    return None


async def get_observations_by_session(session_id: str, limit: int = 50) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(Observation)
            .where(Observation.session_id == session_id, Observation.is_private == False)
            .order_by(desc(Observation.created_at))
            .limit(limit)
        )
        return [_obs_to_dict(o) for o in result.scalars().all()]


async def get_observations_by_project(
    project_id: int, limit: int = 50,
    obs_type: str | None = None, hours: int | None = None,
) -> list[dict]:
    conditions = [Observation.project_id == project_id, Observation.is_private == False]
    if obs_type:
        conditions.append(Observation.obs_type == obs_type)
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        conditions.append(Observation.created_at >= cutoff)

    async with async_session() as session:
        result = await session.execute(
            select(Observation).where(and_(*conditions))
            .order_by(desc(Observation.created_at)).limit(limit)
        )
        return [_obs_to_dict(o) for o in result.scalars().all()]


# ═══════════════════════════════════════════════════════════════
# SEARCH — 3-layer progressive disclosure (claude-mem pattern)
# ═══════════════════════════════════════════════════════════════

async def search_observations(
    query: str, project_id: int | None = None,
    limit: int = 20, obs_types: list[str] | None = None,
    hours: int | None = None,
) -> list[dict]:
    """Layer 1: Compact search — ~50-100 tokens per result."""
    if not query or len(query.strip()) < 2:
        return []

    query_clean = query.strip()[:200]
    conditions = [Observation.is_private == False]

    if project_id:
        conditions.append(Observation.project_id == project_id)
    if obs_types:
        conditions.append(Observation.obs_type.in_(obs_types))
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        conditions.append(Observation.created_at >= cutoff)

    if IS_POSTGRES:
        search_term = query_clean.replace("'", "''")
        conditions.append(
            sa_text("to_tsvector('english', observations.content) @@ plainto_tsquery('english', :q)")
        )
        async with async_session() as session:
            result = await session.execute(
                select(Observation).where(and_(*conditions))
                .order_by(desc(Observation.created_at)).limit(limit),
                {"q": search_term}
            )
            observations = result.scalars().all()
    else:
        fts_query = " OR ".join(query_clean.split())
        try:
            async with async_session() as session:
                fts_result = await session.execute(
                    sa_text("SELECT rowid FROM observations_fts WHERE observations_fts MATCH :q ORDER BY rank LIMIT :limit"),
                    {"q": fts_query, "limit": limit}
                )
                rowids = [row[0] for row in fts_result.fetchall()]
                if rowids:
                    conditions.append(Observation.id.in_(rowids))
                result = await session.execute(
                    select(Observation).where(and_(*conditions))
                    .order_by(desc(Observation.created_at)).limit(limit)
                )
                observations = result.scalars().all()
        except Exception:
            like_pattern = f"%{query_clean}%"
            conditions.append(Observation.content.ilike(like_pattern))
            async with async_session() as session:
                result = await session.execute(
                    select(Observation).where(and_(*conditions))
                    .order_by(desc(Observation.created_at)).limit(limit)
                )
                observations = result.scalars().all()

    return [
        {
            "id": o.id, "obs_type": o.obs_type,
            "content_preview": o.content[:150] + ("..." if len(o.content) > 150 else ""),
            "tool_name": o.tool_name, "file_path": o.file_path,
            "created_at": o.created_at.isoformat() if o.created_at else "",
        }
        for o in observations
    ]


async def get_observation_details(obs_ids: list[int]) -> list[dict]:
    """Layer 3: Full details for selected observations."""
    if not obs_ids:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(Observation).where(Observation.id.in_(obs_ids), Observation.is_private == False)
            .order_by(desc(Observation.created_at))
        )
        return [_obs_to_dict(o) for o in result.scalars().all()]


async def get_recent_timeline(
    project_id: int | None = None,
    around_obs_id: int | None = None,
    before_hours: int = 2, after_hours: int = 2, limit: int = 30,
) -> list[dict]:
    """Layer 2: Chronological context around an observation."""
    if around_obs_id:
        target = await get_observation(around_obs_id)
        if not target:
            return []
        center_time = datetime.fromisoformat(target["created_at"])
        start_time = center_time - timedelta(hours=before_hours)
        end_time = center_time + timedelta(hours=after_hours)
    else:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=before_hours)

    conditions = [Observation.is_private == False,
                  Observation.created_at >= start_time,
                  Observation.created_at <= end_time]
    if project_id:
        conditions.append(Observation.project_id == project_id)

    async with async_session() as session:
        result = await session.execute(
            select(Observation).where(and_(*conditions))
            .order_by(asc(Observation.created_at)).limit(limit)
        )
        return [_obs_to_dict(o) for o in result.scalars().all()]


# ═══════════════════════════════════════════════════════════════
# SESSION SUMMARY
# ═══════════════════════════════════════════════════════════════

async def save_session_summary(
    session_id: str, project_id: int | None = None,
    summary: str = "", key_decisions: list[str] | None = None,
    files_modified: list[str] | None = None,
    errors_fixed: list[str] | None = None,
    observation_count: int = 0, message_count: int = 0,
    tokens_total: int = 0,
) -> int:
    async with async_session() as session:
        async with session.begin():
            ss = SessionSummary(
                session_id=session_id, project_id=project_id,
                summary=summary[:5000],
                key_decisions=json.dumps(key_decisions or [], ensure_ascii=False),
                files_modified=json.dumps(files_modified or [], ensure_ascii=False),
                errors_fixed=json.dumps(errors_fixed or [], ensure_ascii=False),
                observation_count=observation_count,
                message_count=message_count, tokens_total=tokens_total,
            )
            session.add(ss)
            await session.flush()
            return ss.id


async def get_recent_summaries(project_id: int | None = None, limit: int = 5) -> list[dict]:
    conditions = []
    if project_id:
        conditions.append(SessionSummary.project_id == project_id)
    async with async_session() as session:
        result = await session.execute(
            select(SessionSummary)
            .where(and_(*conditions) if conditions else True)
            .order_by(desc(SessionSummary.created_at)).limit(limit)
        )
        return [
            {
                "id": s.id, "session_id": s.session_id, "project_id": s.project_id,
                "summary": s.summary,
                "key_decisions": json.loads(s.key_decisions) if s.key_decisions else [],
                "files_modified": json.loads(s.files_modified) if s.files_modified else [],
                "errors_fixed": json.loads(s.errors_fixed) if s.errors_fixed else [],
                "observation_count": s.observation_count, "message_count": s.message_count,
                "tokens_total": s.tokens_total,
                "created_at": s.created_at.isoformat() if s.created_at else "",
            }
            for s in result.scalars().all()
        ]


# ═══════════════════════════════════════════════════════════════
# CONTEXT ASSEMBLY — inject into system prompt
# ═══════════════════════════════════════════════════════════════

async def assemble_context(project_id: int | None = None, max_tokens: int = 500) -> str:
    """Собрать релевантный контекст из прошлых сессий."""
    parts = []

    summaries = await get_recent_summaries(project_id=project_id, limit=3)
    if summaries:
        parts.append("=== Предыдущие сессии (резюме) ===")
        for i, s in enumerate(summaries, 1):
            summary_text = s["summary"][:200]
            if summary_text:
                parts.append(f"[{i}] {summary_text}")
            if s.get("key_decisions"):
                for dec in s["key_decisions"][:3]:
                    parts.append(f"    Решение: {dec[:80]}")

    if project_id:
        recent = await get_observations_by_project(project_id, limit=10, hours=24)
    else:
        async with async_session() as session:
            result = await session.execute(
                select(Observation).where(Observation.is_private == False)
                .order_by(desc(Observation.created_at)).limit(10)
            )
            recent = [_obs_to_dict(o) for o in result.scalars().all()]

    if recent:
        parts.append("")
        parts.append("=== Последние действия ===")
        for o in recent[:8]:
            preview = o["content"][:120]
            parts.append(f"[{o['obs_type']}] {preview}")

    context = "\n".join(parts)
    if len(context) > max_tokens * 3:
        context = context[:max_tokens * 3] + "\n... (обрезано)"
    return context


# ═══════════════════════════════════════════════════════════════
# AI COMPRESSION — async background task
# ═══════════════════════════════════════════════════════════════

async def compress_observation_async(obs_id: int, raw_content: str, tool_name: str):
    """Сжать наблюдение через LLM (background task)."""
    if not raw_content or len(raw_content) < 50:
        return
    try:
        import litellm
        from core.memory import CONFIG
        model = CONFIG["llm"].get("default_model", "")
        api_base = CONFIG["llm"].get("api_base", "")
        api_key = CONFIG["llm"].get("api_key", "")
        if not model or not api_key:
            return
        prompt = (
            "Сожми это наблюдение о работе AI-ассистента в 1-3 предложения. "
            "Сохраняй суть: что было сделано, какой результат.\n\n"
            f"Инструмент: {tool_name}\nДанные:\n{raw_content[:2000]}"
        )
        response = await litellm.acompletion(
            model=model, messages=[{"role": "user", "content": prompt}],
            api_base=api_base if api_base else None, api_key=api_key,
            max_tokens=150, temperature=0.1, timeout=30,
        )
        compressed = response.choices[0].message.content.strip() if response.choices else ""
        if compressed:
            tokens = response.usage.total_tokens if response.usage else 0
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(Observation).where(Observation.id == obs_id)
                    )
                    obs = result.scalar_one_or_none()
                    if obs:
                        obs.content = compressed[:1000]
                        obs.is_compressed = True
                        obs.tokens_used = tokens
    except Exception as e:
        try:
            logger.log(f"obs_compress_error: {str(e)[:100]}", level="warning", source="observation")
        except Exception:
            pass


async def generate_session_summary_async(session_id: str, project_id: int | None = None):
    """Сгенерировать резюме сессии через LLM (background task)."""
    try:
        observations = await get_observations_by_session(session_id, limit=50)
        if not observations:
            return
        obs_text = "\n".join(f"[{o['obs_type']}] {o['content']}" for o in observations[:30])
        if not obs_text.strip():
            return
        import litellm
        from core.memory import CONFIG
        model = CONFIG["llm"].get("default_model", "")
        api_base = CONFIG["llm"].get("api_base", "")
        api_key = CONFIG["llm"].get("api_key", "")
        if not model or not api_key:
            return
        prompt = (
            "Создай краткое резюме этой сессии AI-ассистента (3-5 предложений).\n"
            f"Наблюдения:\n{obs_text[:3000]}"
        )
        response = await litellm.acompletion(
            model=model, messages=[{"role": "user", "content": prompt}],
            api_base=api_base if api_base else None, api_key=api_key,
            max_tokens=300, temperature=0.1, timeout=30,
        )
        summary_text = response.choices[0].message.content.strip() if response.choices else ""
        if summary_text:
            files_modified = list(set(
                o.get("file_path", "") for o in observations if o.get("file_path")
            ))[:20]
            key_decisions = [
                o["content"][:100] for o in observations if o["obs_type"] == "decision"
            ][:10]
            tokens = response.usage.total_tokens if response.usage else 0
            await save_session_summary(
                session_id=session_id, project_id=project_id,
                summary=summary_text[:2000],
                key_decisions=key_decisions, files_modified=files_modified,
                observation_count=len(observations), tokens_total=tokens,
            )
    except Exception as e:
        try:
            logger.log(f"session_summary_error: {str(e)[:100]}", level="warning", source="observation")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# STATISTICS & CLEANUP
# ═══════════════════════════════════════════════════════════════

async def get_memory_stats(project_id: int | None = None) -> dict:
    async with async_session() as session:
        obs_conds = [Observation.is_private == False]
        sum_conds = []
        if project_id:
            obs_conds.append(Observation.project_id == project_id)
            sum_conds.append(SessionSummary.project_id == project_id)
        obs_count = (await session.execute(
            select(func.count(Observation.id)).where(and_(*obs_conds))
        )).scalar() or 0
        sum_count = (await session.execute(
            select(func.count(SessionSummary.id)).where(and_(*sum_conds) if sum_conds else True)
        )).scalar() or 0
        total_tokens = (await session.execute(
            select(func.coalesce(func.sum(Observation.tokens_used), 0)).where(and_(*obs_conds))
        )).scalar() or 0
        type_result = await session.execute(
            select(Observation.obs_type, func.count(Observation.id))
            .where(and_(*obs_conds)).group_by(Observation.obs_type)
        )
        by_type = {row[0]: row[1] for row in type_result.fetchall()}
        last_result = await session.execute(
            select(Observation.created_at).where(and_(*obs_conds))
            .order_by(desc(Observation.created_at)).limit(1)
        )
        last_obs = last_result.scalar()
        return {
            "total_observations": obs_count,
            "total_summaries": sum_count,
            "total_tokens": total_tokens,
            "by_type": by_type,
            "last_observation": last_obs.isoformat() if last_obs else "",
        }


async def cleanup_old_observations(days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(

                delete(Observation).where(Observation.created_at < cutoff)
            )
            return result.rowcount


_PRIVACY_TAG_RE = re.compile(r'<private>.*?</private>', re.DOTALL)

def strip_private_content(text: str) -> str:
    """Strip <private>...</private> tagged content for privacy."""
    return _PRIVACY_TAG_RE.sub('[PRIVATE]', text)


def strip_private_tags(text: str) -> tuple[str, bool]:
    has_private = bool(re.search(r'<private>.*?</private>', text, re.DOTALL))
    cleaned = re.sub(r'<private>.*?</private>', '[PRIVATE]', text, flags=re.DOTALL)
    return cleaned.strip(), has_private


def _obs_to_dict(obs: Observation) -> dict:
    return {
        "id": obs.id, "session_id": obs.session_id, "project_id": obs.project_id,
        "obs_type": obs.obs_type, "content": obs.content,
        "tool_name": obs.tool_name, "model_id": obs.model_id,
        "file_path": obs.file_path, "tokens_used": obs.tokens_used,
        "is_compressed": obs.is_compressed, "is_private": obs.is_private,
        "relevance_score": obs.relevance_score,
        "created_at": obs.created_at.isoformat() if obs.created_at else "",
    }
