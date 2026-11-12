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

"""Coalesce concurrent Embedding calls and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(frozen=True, slots=True)
class _Resolution:
    """One resolved text and the provider request that produced it, if any."""

    request_id: int | None
    vector: EmbeddingVector
    usage: InferenceUsage | None


class CoalescingEmbeddingModel:
    """Share in-flight vectors across callers and reuse cached successes."""

    profile: EmbeddingProfile

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if not isinstance(max_batch_size, int) or isinstance(max_batch_size, bool) or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(coalesce_ms, int | float) or isinstance(coalesce_ms, bool) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size
        self.profile = model.profile
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._futures: dict[str, asyncio.Future[_Resolution]] = {}
        self._queue: list[str] = []
        self._wakeup: asyncio.Event | None = None
        self._runner: asyncio.Task[None] | None = None
        self._request_counter = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per text, charging only requests this caller needed."""

        if not isinstance(texts, tuple):
            texts = tuple(texts)
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        self._start_runner()
        loop = asyncio.get_running_loop()
        slots: list[tuple[int, str]] = []
        waiters: list[asyncio.Future[_Resolution]] = []
        for index, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                continue
            future = self._futures.get(text)
            if future is None:
                future = loop.create_future()
                self._futures[text] = future
                self._queue.append(text)
                assert self._wakeup is not None
                self._wakeup.set()
            slots.append((index, text))
            waiters.append(future)

        resolved: tuple[_Resolution, ...] = ()
        if waiters:
            guarded = [asyncio.ensure_future(asyncio.shield(future)) for future in waiters]
            resolved = tuple(await asyncio.gather(*guarded))

        vectors: list[EmbeddingVector] = [() for _ in texts]
        self._apply_cache(texts, vectors)
        charges: dict[int, InferenceUsage] = {}
        for (index, text), resolution in zip(slots, resolved, strict=True):
            vectors[index] = resolution.vector
            if resolution.request_id is not None and resolution.usage is not None:
                charges[resolution.request_id] = resolution.usage
        if len(vectors) != len(texts):
            raise ValueError("coalesced embedding produced an unexpected vector count")  # noqa: TRY003

        usage = _merge_usage(charges)
        return EmbeddingResult(vectors=tuple(vectors), usage=usage)

    def _apply_cache(self, texts: tuple[str, ...], vectors: list[EmbeddingVector]) -> None:
        for index, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached is None:
                continue
            self._cache.move_to_end(text)
            vectors[index] = cached

    def _start_runner(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._wakeup = asyncio.Event()
        self._runner = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        assert self._wakeup is not None
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._queue:
                batch = self._queue[: self._max_batch_size]
                del self._queue[: len(batch)]
                room = self._max_batch_size - len(batch)
                if room > 0 and self._coalesce_ms > 0:
                    await asyncio.sleep(self._coalesce_ms / 1000.0)
                    extra = self._queue[:room]
                    del self._queue[: len(extra)]
                    batch.extend(extra)
                await self._dispatch(tuple(batch))

    async def _dispatch(self, batch: tuple[str, ...]) -> None:
        self._request_counter += 1
        request_id = self._request_counter
        try:
            result = await self._model.embed(batch)
        except BaseException as exc:  # noqa: BLE001 - one failed request must not poison the cache
            self._reject(batch, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return
        if len(result.vectors) != len(batch):
            self._reject(batch, ValueError("embedding provider returned an unexpected vector count"))
            return
        for text, vector in zip(batch, result.vectors, strict=True):
            self._store(text, vector)
            future = self._futures.pop(text, None)
            if future is not None and not future.done():
                future.set_result(_Resolution(request_id=request_id, vector=vector, usage=result.usage))

    def _reject(self, batch: tuple[str, ...], exc: BaseException) -> None:
        for text in batch:
            future = self._futures.pop(text, None)
            if future is not None and not future.done():
                future.set_exception(exc)

    def _store(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = tuple(vector)
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _merge_usage(charges: dict[int, InferenceUsage]) -> InferenceUsage:
    if not charges:
        return InferenceUsage(requests=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    requests = 0
    for usage in charges.values():
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = [
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescingEmbeddingModel",
]
