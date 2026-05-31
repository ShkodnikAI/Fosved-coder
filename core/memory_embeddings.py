"""
MindCoder v2.0 — Embedding Engine для семантического поиска по observations.

Архитектура:
  - Lazy-load модели sentence-transformers (all-MiniLM-L6-v2, 384 dim)
  - Хранение эмбеддингов: BLOB в SQLite / PostgreSQL (numpy array → bytes)
  - Поиск: косинусное сходство через numpy (без pgvector dependency)
  - Комбинирование с FTS5 через Reciprocal Rank Fusion (RRF, k=60)

Почему без pgvector:
  - Упрощает деплой (не нужен extension в PostgreSQL)
  - Для <10K observations numpy-поиск <100ms
  - Храним BLOB (512 байт на 384-dim float32) — компактно
"""

import os
import asyncio
import struct
import math
from typing import Optional

import numpy as np

from core.action_logger import get_logger

logger = get_logger()

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output dimension
MODEL_CACHE_DIR = os.environ.get("EMBEDDING_CACHE_DIR", "data/embeddings")

# RRF constant (стандарт из статьи Craswell et al., 2009)
RRF_K = 60


# ═══════════════════════════════════════════════════════════════
# SINGLETON: Lazy-Loaded Embedding Model
# ═══════════════════════════════════════════════════════════════

_model = None
_model_loading = False
_model_ready = False
_model_error: Optional[str] = None


def get_embedding_model():
    """Вернуть загруженную модель sentence-transformers. Lazy-load, thread-safe-ish."""
    global _model, _model_loading, _model_ready, _model_error
    if _model_ready and _model is not None:
        return _model
    if _model_error is not None:
        return None
    if _model_loading:
        return None  # Уже грузится — подождём

    _model_loading = True
    try:
        from sentence_transformers import SentenceTransformer
        os.makedirs(MODEL_CACHE_DIR, exist_ok=True)
        print(f"  [embedding] Загрузка модели: {EMBEDDING_MODEL} (кеш: {MODEL_CACHE_DIR})...")
        _model = SentenceTransformer(
            EMBEDDING_MODEL,
            cache_folder=MODEL_CACHE_DIR,
        )
        _model_ready = True
        dim = _model.get_sentence_embedding_dimension()
        print(f"  [embedding] Модель загружена: {EMBEDDING_MODEL} (dim={dim})")
        return _model
    except ImportError:
        _model_error = "sentence-transformers не установлен (pip install sentence-transformers)"
        print(f"  [embedding] WARNING: {_model_error}")
        return None
    except Exception as e:
        _model_error = f"Ошибка загрузки модели: {e}"
        print(f"  [embedding] ERROR: {_model_error}")
        return None
    finally:
        _model_loading = False


def is_model_ready() -> bool:
    """Проверить, готова ли модель к использованию."""
    return _model_ready and _model is not None


# ═══════════════════════════════════════════════════════════════
# CORE: Embedding Computation
# ═══════════════════════════════════════════════════════════════

def compute_embedding(text: str) -> Optional[list[float]]:
    """Вычислить эмбеддинг для текста. Возвращает list[float] или None."""
    if not text or not text.strip():
        return None
    model = get_embedding_model()
    if model is None:
        return None
    try:
        embedding = model.encode(text[:2000], show_progress_bar=False, normalize_embeddings=True)
        return embedding.tolist()
    except Exception as e:
        print(f"  [embedding] compute error: {e}")
        return None


def compute_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Вычислить эмбеддинги для батча текстов. Оптимальнее для нескольких текстов."""
    if not texts:
        return []
    model = get_embedding_model()
    if model is None:
        return [None] * len(texts)
    try:
        # Фильтруем пустые, запоминаем индексы
        valid_indices = [i for i, t in enumerate(texts) if t and t.strip()]
        if not valid_indices:
            return [None] * len(texts)
        valid_texts = [texts[i][:2000] for i in valid_indices]
        embeddings = model.encode(valid_texts, show_progress_bar=False, normalize_embeddings=True)
        result = [None] * len(texts)
        for idx, emb in zip(valid_indices, embeddings):
            result[idx] = emb.tolist()
        return result
    except Exception as e:
        print(f"  [embedding] batch compute error: {e}")
        return [None] * len(texts)


# ═══════════════════════════════════════════════════════════════
# SERIALIZATION: numpy ↔ bytes (BLOB)
# ═══════════════════════════════════════════════════════════════

def embedding_to_bytes(embedding: list[float]) -> bytes:
    """Сериализовать эмбеддинг в bytes для хранения в БД."""
    arr = np.array(embedding, dtype=np.float32)
    return arr.tobytes()


def bytes_to_embedding(blob: bytes) -> list[float]:
    """Десериализовать bytes из БД в эмбеддинг."""
    arr = np.frombuffer(blob, dtype=np.float32)
    return arr.tolist()


# ═══════════════════════════════════════════════════════════════
# SIMILARITY: Cosine Search
# ═══════════════════════════════════════════════════════════════

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Косинусное сходство между двумя векторами. Векторы должны быть нормализованы."""
    arr_a = np.array(a, dtype=np.float32)
    arr_b = np.array(b, dtype=np.float32)
    dot = np.dot(arr_a, arr_b)
    norm_a = np.linalg.norm(arr_a)
    norm_b = np.linalg.norm(arr_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def top_k_similar(
    query_embedding: list[float],
    candidate_embeddings: list[tuple[int, list[float]]],
    k: int = 10,
    min_score: float = 0.3,
) -> list[tuple[int, float]]:
    """
    Найти top-K наиболее похожих кандидатов по косинусному сходству.

    Args:
        query_embedding: эмбеддинг запроса
        candidate_embeddings: список [(obs_id, embedding), ...]
        k: сколько результатов вернуть
        min_score: минимальный порог сходства (0..1)

    Returns:
        Список [(obs_id, score), ...] отсортированный по убыванию score
    """
    if not query_embedding or not candidate_embeddings:
        return []

    query_vec = np.array(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []

    # Векторизованный расчёт через матрицу
    ids = [c[0] for c in candidate_embeddings]
    vecs = np.array([c[1] for c in candidate_embeddings], dtype=np.float32)

    # Нормализация
    norms = np.linalg.norm(vecs, axis=1)
    norms[norms == 0] = 1e-10
    vecs_normalized = vecs / norms[:, np.newaxis]
    query_normalized = query_vec / query_norm

    # Dot product = cosine similarity (для нормализованных векторов)
    similarities = np.dot(vecs_normalized, query_normalized)

    # Фильтрация + сортировка
    results = []
    for i in np.argsort(similarities)[::-1]:
        score = float(similarities[i])
        if score < min_score:
            break  # Дальше только меньше
        results.append((int(ids[i]), score))
        if len(results) >= k:
            break

    return results


# ═══════════════════════════════════════════════════════════════
# RRF: Reciprocal Rank Fusion
# ═══════════════════════════════════════════════════════════════

def reciprocal_rank_fusion(
    fts_results: list[tuple[int, int]],
    vector_results: list[tuple[int, float]],
    k: int = RRF_K,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """
    Объединить результаты FTS5 и vector search через RRF.

    Args:
        fts_results: [(obs_id, fts_rank), ...] — rank 1 = лучшее
        vector_results: [(obs_id, score), ...] — higher = better
        k: RRF constant (стандарт: 60)
        limit: максимальное количество результатов

    Returns:
        [(obs_id, rrf_score), ...] отсортированные по убыванию score
    """
    scores: dict[int, float] = {}

    # FTS5: rank = позиция в выдаче (1-based)
    for rank, (obs_id, _fts_rank) in enumerate(fts_results, start=1):
        scores[obs_id] = scores.get(obs_id, 0.0) + 1.0 / (k + rank)

    # Vector: rank = позиция в выдаче (1-based)
    for rank, (obs_id, _score) in enumerate(vector_results, start=1):
        scores[obs_id] = scores.get(obs_id, 0.0) + 1.0 / (k + rank)

    # Сортировка по RRF score (убывание)
    sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_results[:limit]


# ═══════════════════════════════════════════════════════════════
# ASYNC WRAPPER: вычисление эмбеддинга в thread pool
# ═══════════════════════════════════════════════════════════════

async def compute_embedding_async(text: str) -> Optional[list[float]]:
    """Асинхронно вычислить эмбеддинг (не блокирует event loop)."""
    if not text or not text.strip():
        return None
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, compute_embedding, text)


async def compute_embeddings_async(texts: list[str]) -> list[Optional[list[float]]]:
    """Асинхронно вычислить эмбеддинги для батча."""
    if not texts:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, compute_embeddings_batch, texts)


async def preload_model_async():
    """Предзагрузить модель в фоне (вызывать при старте приложения)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_embedding_model)


# ═══════════════════════════════════════════════════════════════
# DATABASE: CRUD для observation_embeddings
# ═══════════════════════════════════════════════════════════════

async def ensure_embeddings_table():
    """Создать таблицу observation_embeddings (если не существует)."""
    from core.memory import IS_POSTGRES, engine
    from sqlalchemy import text as sa_text

    async with engine.begin() as conn:
        if IS_POSTGRES:
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS observation_embeddings (
                    obs_id INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
                    embedding BYTEA NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
        else:
            await conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS observation_embeddings (
                    obs_id INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
                    embedding BLOB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))


async def save_embedding(obs_id: int, embedding: list[float]) -> bool:
    """Сохранить эмбеддинг для observation."""
    from core.memory import IS_POSTGRES, engine
    from sqlalchemy import text as sa_text, select

    if not embedding:
        return False
    blob = embedding_to_bytes(embedding)
    try:
        async with engine.begin() as conn:
            # Upsert: INSERT OR REPLACE (SQLite) / INSERT ON CONFLICT (PostgreSQL)
            if IS_POSTGRES:
                await conn.execute(sa_text("""
                    INSERT INTO observation_embeddings (obs_id, embedding)
                    VALUES (:obs_id, :emb)
                    ON CONFLICT (obs_id) DO UPDATE SET embedding = :emb
                """), {"obs_id": obs_id, "emb": blob})
            else:
                await conn.execute(sa_text("""
                    INSERT OR REPLACE INTO observation_embeddings (obs_id, embedding)
                    VALUES (:obs_id, :emb)
                """), {"obs_id": obs_id, "emb": blob})
        return True
    except Exception as e:
        print(f"  [embedding] save error for obs_id={obs_id}: {e}")
        return False


async def save_embeddings_batch(items: list[tuple[int, list[float]]]) -> int:
    """Сохранить батч эмбеддингов. Возвращает количество сохранённых."""
    from core.memory import IS_POSTGRES, engine
    from sqlalchemy import text as sa_text

    if not items:
        return 0
    count = 0
    try:
        async with engine.begin() as conn:
            for obs_id, embedding in items:
                if not embedding:
                    continue
                blob = embedding_to_bytes(embedding)
                if IS_POSTGRES:
                    await conn.execute(sa_text("""
                        INSERT INTO observation_embeddings (obs_id, embedding)
                        VALUES (:obs_id, :emb)
                        ON CONFLICT (obs_id) DO UPDATE SET embedding = :emb
                    """), {"obs_id": obs_id, "emb": blob})
                else:
                    await conn.execute(sa_text("""
                        INSERT OR REPLACE INTO observation_embeddings (obs_id, embedding)
                        VALUES (:obs_id, :emb)
                    """), {"obs_id": obs_id, "emb": blob})
                count += 1
        return count
    except Exception as e:
        print(f"  [embedding] batch save error: {e}")
        return count


async def get_embedding(obs_id: int) -> Optional[list[float]]:
    """Получить эмбеддинг по obs_id."""
    from core.memory import engine
    from sqlalchemy import text as sa_text

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa_text("SELECT embedding FROM observation_embeddings WHERE obs_id = :obs_id"),
                {"obs_id": obs_id}
            )
            row = result.fetchone()
            if row and row[0]:
                return bytes_to_embedding(bytes(row[0]))
    except Exception as e:
        print(f"  [embedding] get error for obs_id={obs_id}: {e}")
    return None


async def get_embeddings_batch(obs_ids: list[int]) -> list[tuple[int, list[float]]]:
    """Получить эмбеддинги для списка obs_id. Возвращает [(obs_id, embedding), ...]."""
    from core.memory import engine
    from sqlalchemy import text as sa_text

    if not obs_ids:
        return []
    results = []
    try:
        async with engine.connect() as conn:
            if IS_POSTGRES:
                # PostgreSQL: ANY array
                placeholders = obs_ids  # passed via parameter
                result = await conn.execute(
                    sa_text(
                        "SELECT obs_id, embedding FROM observation_embeddings WHERE obs_id = ANY(:ids)"
                    ),
                    {"ids": obs_ids}
                )
            else:
                # SQLite: IN clause
                placeholders = ",".join("?" * len(obs_ids))
                result = await conn.execute(
                    sa_text(
                        f"SELECT obs_id, embedding FROM observation_embeddings WHERE obs_id IN ({placeholders})"
                    ),
                    obs_ids
                )
            for row in result.fetchall():
                if row[1]:
                    results.append((row[0], bytes_to_embedding(bytes(row[1]))))
    except Exception as e:
        print(f"  [embedding] batch get error: {e}")
    return results


async def delete_embedding(obs_id: int) -> bool:
    """Удалить эмбеддинг для observation."""
    from core.memory import engine
    from sqlalchemy import text as sa_text

    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa_text("DELETE FROM observation_embeddings WHERE obs_id = :obs_id"),
                {"obs_id": obs_id}
            )
        return True
    except Exception:
        return False


async def vector_search(
    query_embedding: list[float],
    project_id: Optional[int] = None,
    limit: int = 20,
    min_score: float = 0.3,
    obs_types: Optional[list[str]] = None,
    hours: Optional[int] = None,
) -> list[tuple[int, float]]:
    """
    Семантический поиск по observations через эмбеддинги.

    Returns: [(obs_id, score), ...] отсортированные по score (убывание).
    """
    from core.memory import IS_POSTGRES, engine, async_session
    from sqlalchemy import text as sa_text, select
    from core.observation_manager import Observation
    from datetime import datetime, timedelta, timezone

    if not query_embedding:
        return []

    # 1. Получить candidate obs_ids по фильтрам
    conditions = [Observation.is_private == False]
    if project_id:
        conditions.append(Observation.project_id == project_id)
    if obs_types:
        conditions.append(Observation.obs_type.in_(obs_types))
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        conditions.append(Observation.created_at >= cutoff)

    async with async_session() as session:
        result = await session.execute(
            select(Observation.id).where(*conditions).order_by(Observation.created_at.desc()).limit(500)
        )
        candidate_ids = [row[0] for row in result.all()]

    if not candidate_ids:
        return []

    # 2. Получить эмбеддинги для кандидатов
    candidate_embeddings = await get_embeddings_batch(candidate_ids)
    if not candidate_embeddings:
        return []

    # 3. top-K по косинусному сходству
    return top_k_similar(query_embedding, candidate_embeddings, k=limit, min_score=min_score)


# ═══════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════

async def get_embedding_stats() -> dict:
    """Статистика системы эмбеддингов."""
    from core.memory import engine
    from sqlalchemy import text as sa_text

    stats = {
        "model_ready": is_model_ready(),
        "model_name": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIM,
        "cached_count": 0,
        "error": _model_error,
    }
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa_text("SELECT COUNT(*) FROM observation_embeddings")
            )
            stats["cached_count"] = result.scalar() or 0
    except Exception:
        pass
    return stats
