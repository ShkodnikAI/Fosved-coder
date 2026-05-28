"""
Fosved Coder v2.0 — Memory Decay (затухание памяти по кривой Эббингауза)

Архитектура:
  - Каждое observation имеет access_count и last_accessed_at
  - Score = base_relevance * recency_factor * access_boost
  - recency_factor = exp(-lambda * days_since_access)
  - access_boost = 1 + ln(access_count + 1) / ln(max_access + 1)
  - Eviction: удалять не по времени, а когда score < threshold
  - Фоновый asyncio-таск (раз в час) пересчитывает и эвиктит

Отличие от текущего cleanup_old_observations(days=30):
  - Жёсткий порог 30 дней → плавный decay score
  - Часто используемые факты «освежаются» и живут дольше
  - Редко используемые затухают быстрее
"""

import math
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text as sa_text, select, update, delete, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.memory import async_session, engine, IS_POSTGRES
from core.observation_manager import Observation
from core.action_logger import get_logger

logger = get_logger()

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

# Lambda (скорость затухания): e^(-lambda * days)
# lambda=0.01 → полузабыто за ~70 дней
# lambda=0.03 → полузабыто за ~23 дня
# lambda=0.005 → полузабыто за ~140 дней
DECAY_LAMBDA = 0.015  # Полузабытие за ~46 дней — компромисс

# Минимальный score для сохранения observation
EVICTION_THRESHOLD = 0.05

# Максимальный возраст (hard floor) — даже самые популярные observations
# старше этого удаляются. Защита от бесконечного роста.
MAX_AGE_DAYS = 180

# Интервал фонового decay loop (в секундах)
DECAY_LOOP_INTERVAL = 3600  # 1 час

# За один проход — максимум N observations на удаление (безопасность)
MAX_EVICT_PER_RUN = 100


# ═══════════════════════════════════════════════════════════════
# CORE: Decay Score Calculation
# ═══════════════════════════════════════════════════════════════

def calculate_decay_score(
    access_count: int,
    created_at: Optional[datetime],
    last_accessed_at: Optional[datetime],
    max_access: int = 50,
) -> float:
    """
    Рассчитать decay score для observation по кривой Эббингауза.

    Args:
        access_count: сколько раз observation было использовано (прочитано/поисково)
        created_at: время создания
        last_accessed_at: время последнего доступа
        max_access: нормализация (для access_boost, предотвращает overflow)

    Returns:
        score от 0.0 до ~1.0 (выше = более важно)
    """
    now = datetime.now(timezone.utc)

    # 1. Recency factor — экспоненциальное затухание от последнего доступа
    reference_time = last_accessed_at if last_accessed_at else created_at
    if reference_time:
        # Обеспечиваем timezone-aware datetime
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        days_since = (now - reference_time).total_seconds() / 86400.0
        days_since = max(0, days_since)
    else:
        days_since = 0

    recency_factor = math.exp(-DECAY_LAMBDA * days_since)

    # 2. Access boost — частые наблюдения «освежаются»
    if access_count > 0 and max_access > 0:
        access_boost = 1.0 + math.log(access_count + 1) / math.log(max_access + 1)
    else:
        access_boost = 1.0

    # 3. Итоговый score
    score = recency_factor * access_boost
    return round(min(score, 2.0), 6)  # Cap at 2.0 (access boost может поднять >1)


# ═══════════════════════════════════════════════════════════════
# DATABASE: Access Tracking
# ═══════════════════════════════════════════════════════════════

async def touch_observation(obs_id: int) -> bool:
    """
    Записать доступ к observation (увеличить access_count, обновить last_accessed_at).
    Вызывать при каждом использовании observation в контексте (search, assemble_context).
    """
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(Observation).where(Observation.id == obs_id)
                )
                obs = result.scalar_one_or_none()
                if obs:
                    obs.access_count = (obs.access_count or 0) + 1
                    obs.last_accessed_at = now
                    return True
        return False
    except Exception as e:
        print(f"  [decay] touch error for obs_id={obs_id}: {e}")
        return False


async def touch_observations_batch(obs_ids: list[int]) -> int:
    """Записать доступ к нескольким observations. Возвращает количество обновлённых."""
    if not obs_ids:
        return 0
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(Observation).where(Observation.id.in_(obs_ids))
                )
                count = 0
                for obs in result.scalars().all():
                    obs.access_count = (obs.access_count or 0) + 1
                    obs.last_accessed_at = now
                    count += 1
                return count
    except Exception as e:
        print(f"  [decay] batch touch error: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════
# EVICTION: Score-based Cleanup
# ═══════════════════════════════════════════════════════════════

async def evict_weak_observations(
    threshold: float = EVICTION_THRESHOLD,
    max_age_days: int = MAX_AGE_DAYS,
    max_evict: int = MAX_EVICT_PER_RUN,
    project_id: Optional[int] = None,
) -> dict:
    """
    Удалить observations с низким decay score.

    Возвращает {"evicted": int, "errors": int, "details": str}
    """
    now = datetime.now(timezone.utc)
    hard_cutoff = now - timedelta(days=max_age_days)

    conditions = [
        Observation.is_private == False,
    ]
    if project_id:
        conditions.append(Observation.project_id == project_id)

    evicted = 0
    errors = 0
    checked = 0

    try:
        async with async_session() as session:
            # Получить кандидатов на удаление
            result = await session.execute(
                select(Observation).where(*conditions)
                .order_by(Observation.created_at.asc())
                .limit(500)
            )
            candidates = result.scalars().all()

            obs_ids_to_delete = []
            for obs in candidates:
                checked += 1

                # Hard floor: удаление по возрасту (безусловное)
                if obs.created_at:
                    created = obs.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < hard_cutoff:
                        obs_ids_to_delete.append(obs.id)
                        continue

                # Score-based eviction
                score = calculate_decay_score(
                    access_count=obs.access_count or 0,
                    created_at=obs.created_at,
                    last_accessed_at=obs.last_accessed_at,
                )

                if score < threshold:
                    obs_ids_to_delete.append(obs.id)

                if len(obs_ids_to_delete) >= max_evict:
                    break

            # Удалить
            if obs_ids_to_delete:
                # Также удаляем эмбеддинги
                try:
                    from core.memory_embeddings import delete_embedding
                    for obs_id in obs_ids_to_delete:
                        try:
                            await delete_embedding(obs_id)
                        except Exception:
                            pass
                except Exception:
                    pass

                async with session.begin():
                    result = await session.execute(
                        delete(Observation).where(Observation.id.in_(obs_ids_to_delete))
                    )
                    evicted = result.rowcount

    except Exception as e:
        errors = 1
        print(f"  [decay] eviction error: {e}")

    details = f"checked={checked}, evicted={evicted}, threshold={threshold}"
    print(f"  [decay] eviction run: {details}")

    return {"evicted": evicted, "errors": errors, "checked": checked, "details": details}


async def get_decay_stats(project_id: Optional[int] = None) -> dict:
    """Статистика decay системы."""
    conditions = [Observation.is_private == False]
    if project_id:
        conditions.append(Observation.project_id == project_id)

    stats = {
        "total": 0,
        "with_access": 0,
        "high_score": 0,
        "low_score": 0,
        "avg_score": 0.0,
        "lambda": DECAY_LAMBDA,
        "eviction_threshold": EVICTION_THRESHOLD,
        "max_age_days": MAX_AGE_DAYS,
    }

    try:
        async with async_session() as session:
            result = await session.execute(
                select(Observation).where(*conditions)
                .order_by(Observation.created_at.desc())
                .limit(500)
            )
            observations = result.scalars().all()
            stats["total"] = len(observations)

            scores = []
            for obs in observations:
                if (obs.access_count or 0) > 0:
                    stats["with_access"] += 1
                score = calculate_decay_score(
                    access_count=obs.access_count or 0,
                    created_at=obs.created_at,
                    last_accessed_at=obs.last_accessed_at,
                )
                scores.append(score)
                if score >= 0.8:
                    stats["high_score"] += 1
                if score < EVICTION_THRESHOLD:
                    stats["low_score"] += 1

            if scores:
                stats["avg_score"] = round(sum(scores) / len(scores), 4)
    except Exception as e:
        print(f"  [decay] stats error: {e}")

    return stats


# ═══════════════════════════════════════════════════════════════
# BACKGROUND LOOP
# ═══════════════════════════════════════════════════════════════

async def decay_loop(interval_seconds: int = DECAY_LOOP_INTERVAL):
    """
    Фоновый asyncio-таск: периодически эвиктит слабые observations
    и очищает старые архивные сообщения из chat_history.

    Запускается из lifespan() как asyncio.create_task(decay_loop()).
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            print("  [decay] Periodic maintenance run...")

            # 1. Evict weak observations (score-based)
            result = await evict_weak_observations()
            if result["evicted"] > 0:
                print(f"  [decay] Evicted {result['evicted']} weak observations")
                try:
                    logger.log(
                        f"decay_eviction: {result['evicted']} observations removed",
                        level="info", source="decay",
                        details=result
                    )
                except Exception:
                    pass

            # 2. Purge old archived chat messages (actual DELETE, not just archive)
            try:
                from core.memory import ChatHistory
                cutoff_days = 14
                cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
                async with async_session() as session:
                    async with session.begin():
                        del_result = await session.execute(
                            delete(ChatHistory).where(
                                ChatHistory.archived == True,
                                ChatHistory.timestamp < cutoff,
                            )
                        )
                        purged = del_result.rowcount
                if purged > 0:
                    print(f"  [decay] Purged {purged} archived chat messages (> {cutoff_days}d)")
            except Exception as e:
                print(f"  [decay] chat purge error: {e}")

            # 3. Hard-delete observations older than MAX_AGE_DAYS (safety net)
            try:
                hard_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
                async with async_session() as session:
                    async with session.begin():
                        # Удаляем эмбеддинги для старых observations
                        old_result = await session.execute(
                            select(Observation.id).where(Observation.created_at < hard_cutoff)
                        )
                        old_ids = [row[0] for row in old_result.all()]
                        if old_ids:
                            try:
                                from core.memory_embeddings import delete_embedding
                                for oid in old_ids[:200]:
                                    try:
                                        await delete_embedding(oid)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            del_result = await session.execute(
                                delete(Observation).where(Observation.id.in_(old_ids))
                            )
                            if del_result.rowcount > 0:
                                print(f"  [decay] Hard-deleted {del_result.rowcount} observations > {MAX_AGE_DAYS}d")
            except Exception as e:
                print(f"  [decay] hard-age cleanup error: {e}")

            # 4. SQLite VACUUM (только для SQLite, раз в 6 часов)
            try:
                from core.memory import IS_POSTGRES
                if not IS_POSTGRES:
                    import random
                    if random.random() < 0.167:  # ~1/6 chance per run = раз в ~6 часов
                        async with engine.begin() as conn:
                            await conn.execute(sa_text("VACUUM"))
                        print("  [decay] SQLite VACUUM completed")
            except Exception as e:
                print(f"  [decay] VACUUM error: {e}")

        except asyncio.CancelledError:
            print("  [decay] Loop cancelled (shutdown)")
            break
        except Exception as e:
            print(f"  [decay] Loop error: {e}")
            try:
                await asyncio.sleep(60)  # Пауза перед повтором при ошибке
            except asyncio.CancelledError:
                break
