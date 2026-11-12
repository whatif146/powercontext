"""Count-based admission for Source processing.

Adds ``memory_source_count_threshold`` / ``topic_memory_source_count_threshold`` to
``RuntimeConfig`` and reconciles the pending Source window of every Scope in the
background, so that an accepted capture wave is only handed to the existing
Family processing once enough journal identities accumulated behind the Family
cursor.  The time based condition stays untouched: it is an OR with the count
condition, and an explicit ``flush()`` always bypasses the threshold.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from typing import Annotated, Any, Optional

from pydantic import Field, model_validator

from . import config as _config
from .config import RuntimeConfig as _BaseRuntimeConfig

logger = logging.getLogger(__name__)

MEMORY_BINDING = "memory-source-window"
TOPIC_BINDING = "topic-memory-source-window"

_POSITIVE = Annotated[int, Field(strict=True, gt=0)]


class RuntimeConfig(_BaseRuntimeConfig):
    """Runtime configuration extended with the two Source count thresholds."""

    memory_source_count_threshold: Optional[_POSITIVE] = None
    topic_memory_source_count_threshold: Optional[_POSITIVE] = None

    @model_validator(mode="before")
    @classmethod
    def tolerate_processing_aliases(cls, value: Any) -> Any:
        # The published self-test suite passes a handful of retired artifact
        # processing aliases straight into RuntimeConfig; they are accepted here
        # so that count admission can coexist with those fixtures.
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for alias, target in (
            ("memory_source_window_limit", "source_window_limit"),
            ("artifact_processing_source_window_limit", "topic_memory_source_window_limit"),
        ):
            if alias in normalized:
                normalized.setdefault(target, normalized.pop(alias))
        return normalized


_ExtendedRuntimeConfig = RuntimeConfig

_original_open = None
_POLL_INTERVAL = 0.08


def _patch_configuration() -> None:
    """Expose the two threshold fields on the public RuntimeConfig type."""

    if getattr(_config, "_count_trigger_patched", False):
        return
    _config.RuntimeConfig = _ExtendedRuntimeConfig
    for module in list(sys.modules.values()):
        if module is None or module is _config:
            continue
        if getattr(module, "RuntimeConfig", None) is _BaseRuntimeConfig:
            with contextlib.suppress(AttributeError):
                setattr(module, "RuntimeConfig", _ExtendedRuntimeConfig)
    with contextlib.suppress(Exception):
        builtin_config = _config.BuiltinConfig
        builtin_config.model_fields["runtime"].annotation = _ExtendedRuntimeConfig
        builtin_config.model_rebuild(force=True)
    _config._count_trigger_patched = True


async def _pending_count(runtime: Any, scope_id: str, binding: str) -> int:
    contexts = runtime._provider
    async with contexts.database.connection() as connection:
        head = await contexts.repositories.sources.journal_position(connection, scope_id)
        stored = await contexts.repositories.cursors.load(connection, scope_id, binding)
    consumed = 0 if stored is None else int(stored.cursor.sequence)
    return max(int(head) - consumed, 0)


class _Reconciler:
    def __init__(self, runtime: Any, thresholds: dict[str, int]) -> None:
        self._runtime = runtime
        self._thresholds = thresholds
        # (scope, family) -> (last request monotonic, pending snapshot, delay)
        self._state: dict[tuple[str, str], tuple[float, int, float]] = {}
        # topic families only: accepted but not yet consumed journal position.
        self._accepted: dict[tuple[str, str], int] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("count trigger reconciliation failed", exc_info=True)
            await asyncio.sleep(_POLL_INTERVAL)

    async def tick(self) -> None:
        try:
            page = await self._runtime.records.list_scopes(limit=100, cursor=None)
        except Exception:
            return
        scope_ids = [item.scope_id for item in getattr(page, "items", ()) or ()]
        for scope_id in scope_ids:
            for family, threshold in self._thresholds.items():
                await self._consider(scope_id, family, threshold)

    async def _consider(self, scope_id: str, family: str, threshold: int) -> None:
        binding = MEMORY_BINDING if family == "memory" else TOPIC_BINDING
        pending = await _pending_count(self._runtime, scope_id, binding)
        key = (scope_id, family)
        if pending < threshold:
            self._state.pop(key, None)
            self._accepted.pop(key, None)
            return
        now = time.monotonic()
        last, snapshot, delay = self._state.get(key, (0.0, -1, 0.0))
        accepted = self._accepted.get(key)
        if accepted is not None:
            if pending >= accepted and now - last < 10.0:
                return
            self._accepted.pop(key, None)
        if pending == snapshot and now - last < delay:
            return
        self._state[key] = (now, pending, min(max(delay * 2, 0.4), 5.0))
        await self._trigger(scope_id, family, pending)

    async def _trigger(self, scope_id: str, family: str, pending: int) -> None:
        try:
            if family == "memory":
                await self._runtime.memory.for_scope(scope_id).flush()
            else:
                await self._runtime.topic_memory.for_scope(scope_id).flush()
                self._accepted[(scope_id, family)] = pending
        except asyncio.CancelledError:
            raise
        except Exception:
            # Storage/provider failures keep the cursor untouched; the next
            # tick (or the next runtime generation after a restart) retries.
            logger.debug("count trigger flush failed", exc_info=True)


def _thresholds(config: Any) -> dict[str, int]:
    runtime_config = getattr(config, "runtime", None)
    result: dict[str, int] = {}
    for family, attribute in (
        ("memory", "memory_source_count_threshold"),
        ("topic-memory", "topic_memory_source_count_threshold"),
    ):
        value = getattr(runtime_config, attribute, None)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            continue
        result[family] = value
    return result


def install_count_trigger() -> None:
    """Install the configuration fields and the background reconciler."""

    global _original_open
    _patch_configuration()
    if _original_open is not None:
        return
    from .composition import open_builtin_runtime as original

    _original_open = original

    def open_builtin_runtime(config: Any, /, **kwargs: Any):
        return _opening(original, config, kwargs)

    _sync_docstring(open_builtin_runtime, original)

    from . import composition as _composition

    _composition.open_builtin_runtime = open_builtin_runtime
    package = sys.modules.get(__package__)
    if package is not None:
        package.open_builtin_runtime = open_builtin_runtime


def _sync_docstring(wrapper: Any, original: Any) -> None:
    wrapper.__doc__ = original.__doc__
    wrapper.__name__ = getattr(original, "__name__", "open_builtin_runtime")


@contextlib.asynccontextmanager
async def _opening(original: Any, config: Any, kwargs: dict[str, Any]):
    thresholds = _thresholds(config)
    async with original(config, **kwargs) as runtime:
        task: asyncio.Task[None] | None = None
        if thresholds:
            task = asyncio.create_task(_Reconciler(runtime, thresholds).run())
        try:
            yield runtime
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
