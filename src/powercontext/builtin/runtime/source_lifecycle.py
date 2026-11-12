"""Source lifecycle propagation for Memory and Topic Memory.

The public surface of this module is ``SourceLifecycleEvent`` (a strictly
validated event value) and ``SourceLifecycleError``; the runtime provides
``runtime.lifecycle.for_scope(scope_id)`` with ``register_source`` /
``register_memory`` / ``register_topic``, ``apply`` and ``resolve``.

Dependency state is kept per Scope in a dedicated table, so registrations,
events and the resulting availability survive a runtime restart.  Native Memory
entries are deactivated and reactivated through the Memory service, and Topic
Memory recall is filtered through the runtime's own Topic search callable so that
withdrawn topics never reach native search or Context assembly.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.sources.models import SourceRef

logger = logging.getLogger(__name__)

TABLE_NAME = "pc_source_lifecycle"
_POLICIES = ("all", "any")
_PURPOSES = ("generation", "audit")

_POSITIVE = Annotated[int, Field(gt=0, strict=True)]
_NON_BLANK = Annotated[str, Field(strict=True)]


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event, or resolution request was rejected."""


class SourceLifecycleEvent(BaseModel):
    """One Source lifecycle version transition."""

    model_config = ConfigDict(extra="forbid")

    node_id: _NON_BLANK
    revision: _POSITIVE
    sequence: _POSITIVE
    available: Annotated[bool, Field(strict=True)]
    reason: _NON_BLANK

    @field_validator("node_id", "reason")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("blank identifier")
        return value


_CACHE: dict[str, dict[str, Any]] = {}
_INACTIVE_TOPICS: dict[str, set[str]] = {}
_READY_DATABASES: set[int] = set()


def _cache_active_topics(scope_id: str, state: dict[str, Any]) -> None:
    inactive: set[str] = set()
    for node_id, node in state.get("nodes", {}).items():
        if node["kind"] != "topic" or node.get("active", True):
            continue
        inactive.add(str(node["reference"]["artifact_id"]))
    if inactive:
        _INACTIVE_TOPICS[scope_id] = inactive
    else:
        _INACTIVE_TOPICS.pop(scope_id, None)


async def _ensure_table(database: Any) -> None:
    key = id(database)
    if key in _READY_DATABASES:
        return
    async with database.transaction() as connection:
        await connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} "
                "(scope_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
        )
    _READY_DATABASES.add(key)


def _empty_state() -> dict[str, Any]:
    return {"nodes": {}, "events": {}}


async def _load_state(runtime: Any, scope_id: str) -> dict[str, Any]:
    cached = _CACHE.get(scope_id)
    if cached is not None:
        return cached
    database = runtime._provider.database
    await _ensure_table(database)
    async with database.connection() as connection:
        result = await connection.execute(
            text(f"SELECT state FROM {TABLE_NAME} WHERE scope_id = :scope"),
            {"scope": scope_id},
        )
        raw = result.scalar_one_or_none()
    state = _empty_state() if raw is None else json.loads(raw)
    state.setdefault("nodes", {})
    state.setdefault("events", {})
    _CACHE[scope_id] = state
    return state


async def _save_state(runtime: Any, scope_id: str, state: dict[str, Any]) -> None:
    database = runtime._provider.database
    await _ensure_table(database)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    async with database.transaction() as connection:
        await connection.execute(
            text(
                f"INSERT INTO {TABLE_NAME} (scope_id, state) VALUES (:scope, :state) "
                "ON CONFLICT(scope_id) DO UPDATE SET state = excluded.state"
            ),
            {"scope": scope_id, "state": payload},
        )
    _CACHE[scope_id] = state
    _cache_active_topics(scope_id, state)


async def _preload(runtime: Any) -> None:
    """Load every persisted Scope graph so recall filtering works after a restart."""

    database = runtime._provider.database
    await _ensure_table(database)
    async with database.connection() as connection:
        result = await connection.execute(text(f"SELECT scope_id, state FROM {TABLE_NAME}"))
        rows = result.fetchall()
    for scope_id, raw in rows:
        try:
            state = json.loads(raw)
        except Exception:  # pragma: no cover - defensive
            continue
        scope_id = str(scope_id)
        state.setdefault("nodes", {})
        state.setdefault("events", {})
        _CACHE[scope_id] = state
        _cache_active_topics(scope_id, state)


def _evaluate(state: dict[str, Any]) -> dict[str, bool]:
    nodes = state["nodes"]
    active: dict[str, bool] = {}
    for node_id, node in nodes.items():
        if node["kind"] == "source":
            active[node_id] = bool(node.get("active", True))
    pending = [node_id for node_id, node in nodes.items() if node["kind"] != "source"]
    progress = True
    while pending and progress:
        progress = False
        for node_id in list(pending):
            node = nodes[node_id]
            parents = node.get("parents", [])
            if all(parent in active for parent in parents):
                values = [active[parent] for parent in parents]
                active[node_id] = all(values) if node.get("policy") == "all" else any(values)
                pending.remove(node_id)
                progress = True
    for node_id in pending:  # pragma: no cover - registration rejects cycles
        active[node_id] = False
    return active


class ScopedSourceLifecycleApplication:
    """Lifecycle registrations, events, and resolution for one Scope."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    # -- registration ------------------------------------------------------
    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        _require_node_id(node_id)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise SourceLifecycleError("source revision must be a positive integer")
        state = copy.deepcopy(await _load_state(self._runtime, self.scope_id))
        node = state["nodes"].get(node_id)
        if node is not None:
            if (
                node["kind"] == "source"
                and node["reference"] == source_ref.model_dump(mode="json")
                and node["revision"] == revision
            ):
                return
            raise SourceLifecycleError(f"node already registered with another binding: {node_id}")
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type, source_ref.source_id
            )
        except Exception as error:
            raise SourceLifecycleError(f"unreadable source: {source_ref}") from error
        state["nodes"][node_id] = {
            "kind": "source",
            "reference": source_ref.model_dump(mode="json"),
            "revision": revision,
            "parents": [],
            "policy": "all",
            "active": True,
        }
        await _save_state(self._runtime, self.scope_id, state)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...] = (),
        policy: str = "all",
    ) -> None:
        await self._register_derived("memory", node_id, citation, parents, policy)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...] = (),
        policy: str = "all",
    ) -> None:
        await self._register_derived("topic", node_id, artifact_ref, parents, policy)

    async def _register_derived(
        self,
        kind: str,
        node_id: str,
        reference: Any,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        _require_node_id(node_id)
        if policy not in _POLICIES:
            raise SourceLifecycleError(f"unsupported dependency policy: {policy}")
        state = copy.deepcopy(await _load_state(self._runtime, self.scope_id))
        node = state["nodes"].get(node_id)
        if node is not None:
            if (
                node["kind"] == kind
                and node["reference"] == reference.model_dump(mode="json")
                and node["parents"] == list(parents)
                and node["policy"] == policy
            ):
                return
            raise SourceLifecycleError(f"node already registered with another binding: {node_id}")
        if not parents:
            raise SourceLifecycleError(f"derived node requires at least one parent: {node_id}")
        if node_id in parents:
            raise SourceLifecycleError(f"node cannot depend on itself: {node_id}")
        if len(set(parents)) != len(parents):
            raise SourceLifecycleError(f"duplicate parent: {node_id}")
        for parent in parents:
            if parent not in state["nodes"]:
                raise SourceLifecycleError(f"unknown parent: {parent}")
        await self._check_reference(kind, reference)
        node_record = {
            "kind": kind,
            "reference": reference.model_dump(mode="json"),
            "revision": None,
            "parents": list(parents),
            "policy": policy,
            "active": True,
        }
        state["nodes"][node_id] = node_record
        current = _evaluate(state)
        node_record["active"] = current[node_id]
        if kind == "memory" and not node_record["active"]:
            await self._set_memory_entry_state(reference, active=False, reason="registered without support")
        await _save_state(self._runtime, self.scope_id, state)

    async def _check_reference(self, kind: str, reference: Any) -> None:
        try:
            if kind == "memory":
                await self._runtime.memory.for_scope(self.scope_id).get(
                    _get_memory_request_class()(citation=reference)
                )
            else:
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    "topic-memory", reference.artifact_id, reference.revision
                )
        except Exception as error:
            raise SourceLifecycleError(f"unreadable {kind} reference: {reference}") from error

    # -- events ------------------------------------------------------------
    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        state = copy.deepcopy(await _load_state(self._runtime, self.scope_id))
        node = state["nodes"].get(event.node_id)
        if node is None or node["kind"] != "source":
            raise SourceLifecycleError(f"unknown lifecycle source: {event.node_id}")
        if node["revision"] != event.revision:
            raise SourceLifecycleError(f"lifecycle revision mismatch: {event.node_id}")
        previous_event = state["events"].get(event.node_id)
        if previous_event is not None:
            if event.sequence < previous_event["sequence"]:
                raise SourceLifecycleError(f"lifecycle sequence regressed: {event.node_id}")
            if event.sequence == previous_event["sequence"]:
                if (
                    previous_event["available"] != event.available
                    or previous_event["reason"] != event.reason
                ):
                    raise SourceLifecycleError(f"conflicting lifecycle event: {event.node_id}")
                return sorted(node_id for node_id, active in _evaluate(state).items() if active)
        before = _evaluate(state)
        node["active"] = bool(event.available)
        after = _evaluate(state)
        nodes = state["nodes"]
        for node_id, available in after.items():
            if available == before.get(node_id):
                continue
            if nodes[node_id]["kind"] == "memory":
                await self._set_memory_entry_state(
                    nodes[node_id]["reference"], active=available, reason=event.reason
                )
            else:
                nodes[node_id]["active"] = available
        for node_id in nodes:
            nodes[node_id]["active"] = after[node_id]
        state["events"][event.node_id] = {
            "sequence": event.sequence,
            "available": bool(event.available),
            "reason": event.reason,
            "revision": event.revision,
        }
        await _save_state(self._runtime, self.scope_id, state)
        return sorted(node_id for node_id, active in after.items() if active)

    # -- resolution --------------------------------------------------------
    async def resolve(self, node_id: str, /, *, purpose: str = "generation") -> Any:
        if purpose not in _PURPOSES:
            raise SourceLifecycleError(f"unknown resolution purpose: {purpose}")
        state = await _load_state(self._runtime, self.scope_id)
        node = state["nodes"].get(node_id)
        if node is None:
            raise SourceLifecycleError(f"unknown lifecycle node: {node_id}")
        if purpose == "generation" and not node.get("active", False):
            raise SourceLifecycleError(f"node is not available for generation: {node_id}")
        return _restore_reference(node["kind"], node["reference"])

    async def available(self) -> list[str]:
        state = await _load_state(self._runtime, self.scope_id)
        return sorted(node_id for node_id, active in _evaluate(state).items() if active)

    # -- native Memory state ----------------------------------------------
    async def _set_memory_entry_state(self, reference: dict[str, Any], *, active: bool, reason: str) -> None:
        citation = _restore_reference("memory", reference)
        runtime = self._runtime
        async with runtime._context(self.scope_id) as context:
            async with runtime._locked(self.scope_id):
                service = context.artifacts.memory
                memory, entries = await service.head_entries(context.artifacts.memory_artifact_id)
                versions = [entry for entry in entries if entry.entry_id == citation.entry_id]
                if not versions:
                    raise SourceLifecycleError(f"memory entry is not present: {citation.entry_id}")
                version = versions[0]
                if active:
                    await service.reactivate(memory, entries=(version,), reason=reason)
                else:
                    await service.forget(memory, entries=(version,), reason=reason)


def _require_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id or not node_id.strip():
        raise SourceLifecycleError("node identifier must be a non-blank string")


def _get_memory_request_class() -> Any:
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest

    return GetMemoryEntryRequest


def _restore_reference(kind: str, payload: dict[str, Any]) -> Any:
    if kind == "source":
        return SourceRef.model_validate(payload)
    if kind == "memory":
        return MemoryCitation.model_validate(payload)
    return ArtifactRef.model_validate(payload)


def _install_topic_recall_filter(runtime: Any) -> None:
    search = getattr(runtime, "_topic_memory_search", None)
    if search is None:
        return

    async def filtered(scope_id: str, *args: Any, **kwargs: Any):
        result = await search(scope_id, *args, **kwargs)
        inactive = _INACTIVE_TOPICS.get(scope_id)
        if not inactive:
            return result
        hits = tuple(hit for hit in result.hits if hit.artifact_ref.artifact_id not in inactive)
        if len(hits) == len(result.hits):
            return result
        return result.model_copy(update={"hits": hits})

    runtime._topic_memory_search = filtered


class SourceLifecycleApplication:
    """Entry point exposed as ``runtime.lifecycle``."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)


def install_source_lifecycle() -> None:
    """Expose ``runtime.lifecycle`` and prepare Topic recall filtering."""

    from .application import BuiltinRuntime

    if getattr(BuiltinRuntime, "_source_lifecycle_installed", False):
        return
    BuiltinRuntime.lifecycle = property(lambda self: SourceLifecycleApplication(self))
    BuiltinRuntime._source_lifecycle_installed = True

    from . import composition as _composition

    original = _composition.open_builtin_runtime

    def open_builtin_runtime(config: Any, /, **kwargs: Any):
        return _opening_with_lifecycle(original, config, kwargs)

    open_builtin_runtime.__doc__ = original.__doc__
    open_builtin_runtime.__name__ = getattr(original, "__name__", "open_builtin_runtime")
    _composition.open_builtin_runtime = open_builtin_runtime
    import sys

    package = sys.modules.get(__package__)
    if package is not None:
        package.open_builtin_runtime = open_builtin_runtime


@contextlib.asynccontextmanager
async def _opening_with_lifecycle(original: Any, config: Any, kwargs: dict[str, Any]):
    async with original(config, **kwargs) as runtime:
        try:
            await _preload(runtime)
        except Exception:  # pragma: no cover - defensive
            logger.debug("lifecycle preload failed", exc_info=True)
        _install_topic_recall_filter(runtime)
        yield runtime
