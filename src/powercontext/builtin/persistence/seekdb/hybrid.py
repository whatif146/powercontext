"""Single-statement native hybrid candidates for the embedded seekdb profile.

The frozen seekdb path asks the FULLTEXT index and the vector index separately, so
a ``mode="hybrid"`` Memory search issues two candidate statements and merges them in
the application.  This module fuses both candidate queries into one seekdb
statement: one FULLTEXT branch (``TEXT RETRIEVAL SCAN``) and one vector branch
(``VECTOR INDEX``), each keeping its own native ordering and candidate limit.

The two branches return rows tagged with the channel that produced them, and the
downstream ``fuse_rankings`` step receives exactly the same per-channel orderings
the frozen path produced, so retrieval quality cannot regress.
"""

from __future__ import annotations

import logging
from typing import Any

from pyobvector import VECTOR
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.memory import MemorySearchChannels
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex, memory_channel_hits
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

logger = logging.getLogger(__name__)

_FTS_BRANCH = """
SELECT m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id, v.text,
       MATCH(m.searchable_text) AGAINST (:query IN NATURAL LANGUAGE MODE) AS score
FROM pc_memory_entry_heads AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
{tags}
  AND MATCH(m.searchable_text) AGAINST (:query IN NATURAL LANGUAGE MODE)
ORDER BY score DESC, m.memory_artifact_id, m.entry_id, m.entry_version_id
LIMIT :candidate_limit
"""

_VECTOR_BRANCH = """
SELECT m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id, v.text,
       l2_distance(m.embedding, :query_vector) AS distance
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
{tags}
ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
LIMIT :candidate_limit
"""

_CANDIDATE_SQL = """SELECT 'fts' AS channel, f.memory_artifact_id, f.head_revision, f.entry_id,
       f.entry_version_id, f.text, f.score, NULL AS distance
FROM ({fts}) AS f
UNION ALL
SELECT 'vector', vec.memory_artifact_id, vec.head_revision, vec.entry_id, vec.entry_version_id,
       vec.text, NULL AS score, vec.distance
FROM ({vector}) AS vec"""

_STATEMENTS: dict[tuple[int, bool], Any] = {}


def _tag_clause(tagged: bool, alias: str = "m") -> str:
    return memory_tag_sql("m") if tagged else ""


def _statement(dimension: int, tagged: bool) -> Any:
    key = (dimension, tagged)
    cached = _STATEMENTS.get(key)
    if cached is not None:
        return cached
    sql = _CANDIDATE_SQL.format(
        fts=_FTS_BRANCH.format(tags=_tag_clause(tagged)),
        vector=_VECTOR_BRANCH.format(tags=_tag_clause(tagged)),
    )
    binds: list[Any] = [
        bindparam("memory_artifact_ids", expanding=True),
        bindparam("query_vector", type_=VECTOR(dimension)),
    ]
    if tagged:
        binds.append(bindparam("tag_keys", expanding=True))
        binds.append(bindparam("tag_hashes", expanding=True))
    statement = text(sql).bindparams(*binds)
    _STATEMENTS[key] = statement
    return statement


def _native_pair(index: CompositeMemoryIndex) -> tuple[Any, Any] | None:
    from powercontext.builtin.persistence.oceanbase.memory_index import (
        OceanBaseMemoryFTSIndex,
        OceanBaseMemoryVectorIndex,
    )

    fts = None
    vector = None
    for inner in index.indexes:
        if fts is None and isinstance(inner, OceanBaseMemoryFTSIndex):
            fts = inner
        elif vector is None and isinstance(inner, OceanBaseMemoryVectorIndex):
            vector = inner
    if fts is None or vector is None:
        return None
    return fts, vector


async def _native_hybrid_search(
    index: CompositeMemoryIndex,
    connection: AsyncConnection,
    scope_id: str,
    request: Any,
) -> MemorySearchChannels:
    from powercontext.builtin.artifacts.memory import CapabilityNotSupportedError

    pair = _native_pair(index)
    if pair is None:
        raise _Fallback()
    _, vector_index = pair
    profile = vector_index.profile
    if request.embedding_profile != profile or request.query_vector is None:
        raise CapabilityNotSupportedError("embedding-profile")
    if not await vector_index.vector_complete(connection, scope_id, request.memories, profile):
        raise CapabilityNotSupportedError("vector")
    statement = _statement(profile.dimension, request.tag_filter is not None)
    parameters = {
        "scope_id": scope_id,
        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
        "query": request.analyzed_query,
        "query_vector": _vector_literal(request.query_vector, profile.dimension),
        "candidate_limit": request.candidate_limit,
        **memory_tag_parameters(request.tag_filter),
    }
    rows = (await connection.execute(statement, parameters)).mappings().all()
    fts_rows = [row for row in rows if row["channel"] == "fts"]
    vector_rows = [row for row in rows if row["channel"] == "vector"]
    fts_rows.sort(
        key=lambda row: (
            -float(row["score"]),
            str(row["memory_artifact_id"]),
            str(row["entry_id"]),
            str(row["entry_version_id"]),
        )
    )
    vector_rows.sort(
        key=lambda row: (
            float(row["distance"]),
            str(row["memory_artifact_id"]),
            str(row["entry_id"]),
            str(row["entry_version_id"]),
        )
    )
    limit = request.candidate_limit
    return MemorySearchChannels(
        fts=memory_channel_hits(fts_rows[:limit], request.memories),
        vector=memory_channel_hits(vector_rows[:limit], request.memories),
    )


def _vector_literal(vector: Any, dimension: int) -> Any:
    if isinstance(vector, str):
        return vector
    return list(float(value) for value in vector[:dimension])


class _Fallback(Exception):
    """The composite index does not expose the embedded seekdb pair."""


def install() -> None:
    """Route ``mode="hybrid"`` candidate retrieval through one native statement."""

    if getattr(CompositeMemoryIndex, "_seekdb_hybrid_installed", False):
        return
    original = CompositeMemoryIndex.search

    async def search(
        self: CompositeMemoryIndex,
        connection: AsyncConnection,
        scope_id: str,
        request: Any,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "hybrid" and request.analyzed_query:
            try:
                return await _native_hybrid_search(self, connection, scope_id, request)
            except _Fallback:
                pass
        return await original(self, connection, scope_id, request)

    search.__doc__ = original.__doc__
    search.__name__ = getattr(original, "__name__", "search")
    CompositeMemoryIndex.search = search
    CompositeMemoryIndex._seekdb_hybrid_installed = True


install()
