# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP cross-encoder reranking for coarse Memory search candidates."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score candidates through one HTTP service and merge every batch globally."""

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")  # noqa: TRY003
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate exactly once and select the global best ranks."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")  # noqa: TRY003
        if not isinstance(candidates, tuple) or not candidates:
            raise ValueError("candidates must be a non-empty tuple")  # noqa: TRY003
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= len(candidates):
            raise ValueError("limit must be between 1 and the candidate count")  # noqa: TRY003

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(start + offset + 1 for offset in range(len(batch)))
            payload = {
                "query": query,
                "candidates": [
                    {"rank": rank, "text": candidate.text}
                    for rank, candidate in zip(ranks, batch, strict=True)
                ],
            }
            response = await self._client.post(self._endpoint, json=payload)
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, ranks))

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        selected = tuple(ordered[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
            discarded_rank_count=len(candidates) - len(selected),
        )


def _parse_scores(response: httpx.Response, ranks: tuple[int, ...]) -> dict[int, float]:
    try:
        payload: Any = json.loads(response.content)
    except ValueError as exc:
        raise ValueError(f"reranker response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("reranker response must contain exactly the scores field")  # noqa: TRY003
    rows = payload["scores"]
    if not isinstance(rows, list):
        raise ValueError("reranker scores must be a list")  # noqa: TRY003

    received: dict[int, float] = {}
    expected = set(ranks)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"rank", "score"}:
            raise ValueError("each reranker score must contain exactly rank and score")  # noqa: TRY003
        rank = row["rank"]
        score = row["score"]
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError("reranker score rank must be an integer")  # noqa: TRY003
        if rank not in expected:
            raise ValueError(f"reranker score rank {rank!r} is not part of this batch")  # noqa: TRY003
        if rank in received:
            raise ValueError(f"reranker score rank {rank!r} was returned twice")  # noqa: TRY003
        if not isinstance(score, int | float) or isinstance(score, bool):
            raise ValueError("reranker score must be a number")  # noqa: TRY003
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("reranker score must be finite")  # noqa: TRY003
        received[rank] = value

    if set(received) != expected:
        raise ValueError("reranker scores must cover every rank of the batch exactly once")  # noqa: TRY003
    return received


__all__ = ["DEFAULT_POLICY_ID", "HTTPMemoryReranker"]
