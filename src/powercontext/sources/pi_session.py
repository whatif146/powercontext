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

"""Capture the raw visible events of one Pi session branch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    Source,
    TextEvidence,
)
from powercontext.sources.models import SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
IDENTITY_PREFIX = "pi:"
CHECKPOINT_KEY = "pi-session-history"

_CONTROL_TYPES = frozenset({"model_change", "thinking_level_change", "session"})


class PiSessionInput(BaseModel):
    """One Pi v3 JSONL session archive and the leaf whose branch to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event of the selected branch with its untouched record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """Captured Pi event text kept exactly as the session recorded it."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the stable identity of one session event."""

    for field, value in (("session_id", session_id), ("event_id", event_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")  # noqa: TRY003
    identity = f"{IDENTITY_PREFIX}{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("Pi event source identity exceeds the maximum source id length")  # noqa: TRY003
    return identity


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text_blocks(value: Any) -> str:
    """Join the visible text of a content value, ignoring thinking and images."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block.get("type") == "toolCall":
            name = block.get("name")
            call_id = block.get("id")
            arguments = block.get("arguments")
            parts.append(f"[tool_call {name} {call_id}] {_compact(arguments)}")
    return "\n".join(parts)


def _message_body(message: dict[str, Any]) -> tuple[str, str]:
    role = message.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("message role must be a non-empty string")  # noqa: TRY003
    if role == "toolResult":
        tool_name = message.get("toolName")
        tool_call_id = message.get("toolCallId")
        text = _text_blocks(message.get("content"))
        return role, f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == "bashExecution":
        command = message.get("command")
        output = message.get("output")
        return role, f"$ {command}\n{output}"
    return role, _text_blocks(message.get("content"))


def _event_projection(record: dict[str, Any], session_id: str) -> tuple[str, str, str] | None:
    """Return (event_type, role, body) for a visible record, else None."""

    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("record type must be a non-empty string")  # noqa: TRY003
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("message record must carry a message object")  # noqa: TRY003
        role, body = _message_body(message)
    elif event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        role, body = event_type, "" if summary is None else str(summary)
    elif event_type == "custom_message":
        role, body = "custom", _text_blocks(record.get("content"))
    else:
        return None
    if not body:
        return None
    return event_type, role, body


def _decode(content: bytes) -> str:
    if not isinstance(content, bytes | bytearray):
        raise ValueError("session content must be bytes")  # noqa: TRY003
    raw = bytes(content)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("session archive must be valid UTF-8") from exc


def _load_records(content: bytes) -> tuple[str, list[dict[str, Any]]]:
    text = _decode(content)
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"session archive line is not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError("session archive lines must be JSON objects")  # noqa: TRY003
        rows.append(row)
    if not rows:
        raise ValueError("session archive must start with a session header")  # noqa: TRY003

    header = rows[0]
    if header.get("type") != "session":
        raise ValueError("first record must be the session header")  # noqa: TRY003
    if header.get("version") != 3:
        raise ValueError("session header version must be 3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session header id must be a non-empty string")  # noqa: TRY003
    return session_id, rows[1:]


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return every visible event on the selected branch, ordered root to leaf."""

    if not isinstance(value, PiSessionInput):
        raise TypeError("parse_pi_session expects a PiSessionInput")  # noqa: TRY003
    session_id, rows = _load_records(value.content)

    by_id: dict[str, dict[str, Any]] = {}
    for record in rows:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("every record needs a non-empty id")  # noqa: TRY003
        if event_id in by_id:
            raise ValueError(f"duplicate record id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id.strip():
                raise ValueError("parentId must be null or a non-empty string")  # noqa: TRY003
            if parent_id not in by_id:
                raise ValueError(f"record {event_id!r} references a parent that does not exist yet")  # noqa: TRY003
        by_id[event_id] = record

    if not by_id:
        raise ValueError("session archive contains no records")  # noqa: TRY003

    if value.leaf_id is None:
        leaf_id = next(reversed(by_id))
    else:
        leaf_id = value.leaf_id
        if not isinstance(leaf_id, str) or leaf_id not in by_id:
            raise ValueError(f"leaf {leaf_id!r} does not exist in the session")  # noqa: TRY003

    chain: list[dict[str, Any]] = []
    cursor: str | None = leaf_id
    while cursor is not None:
        record = by_id.get(cursor)
        if record is None:
            raise ValueError(f"missing record {cursor!r} on the branch")  # noqa: TRY003
        chain.append(record)
        parent = record.get("parentId")
        if parent is not None and parent not in by_id:
            raise ValueError(f"missing parent {parent!r} on the branch")  # noqa: TRY003
        cursor = parent
    chain.reverse()

    events: list[PiEventInput] = []
    for record in chain:
        projected = _event_projection(record, session_id)
        if projected is None:
            continue
        event_type, role, body = projected
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=record.get("parentId"),
                event_type=event_type,
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class PiEventSourceAdapter:
    """Resolve and read one captured Pi event."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not isinstance(value, PiEventInput):
            raise TypeError("Pi event adapter cannot resolve this input")  # noqa: TRY003
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        if not isinstance(source, PiEventSource):
            raise TypeError("Pi event adapter cannot read this source")  # noqa: TRY003
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        if not isinstance(source, PiEventSource):
            raise TypeError("Pi event projection expects a PiEventSource")  # noqa: TRY003
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version="1",
    projections=(PiEventTextEvidenceProjection(),),
)


def _record_digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(_compact(record).encode("utf-8")).hexdigest()


def _imported_events(checkpoint: JsonValue | None) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    rows = checkpoint.get("events")
    if not isinstance(rows, list):
        return {}
    imported: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("checkpoint events must be objects")  # noqa: TRY003
        event_id = row.get("id")
        digest = row.get("digest")
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("checkpoint events must carry id and digest")  # noqa: TRY003
        imported[event_id] = digest
    return imported


class PiSessionConnector:
    """Import only the not-yet-captured visible events of one branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = self._path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        imported = _imported_events(session.checkpoint)

        for event in events:
            digest = _record_digest(event.record)
            known = imported.get(event.event_id)
            if known is not None and known != digest:
                raise ValueError(f"event {event.event_id!r} was imported with a different payload")  # noqa: TRY003

        session_id = events[0].session_id if events else _checkpoint_session_id(session.checkpoint)
        state = dict(imported)
        for event in events:
            if event.event_id in state:
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            state[event.event_id] = _record_digest(event.record)

        checkpoint: JsonValue = {
            "version": 1,
            "session_id": session_id,
            "events": [{"id": event_id, "digest": digest} for event_id, digest in state.items()],
        }
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)


def _checkpoint_session_id(checkpoint: JsonValue | None) -> str:
    if isinstance(checkpoint, dict):
        session_id = checkpoint.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    raise ValueError("session archive contains no visible event")  # noqa: TRY003


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
