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

"""Context Pack: chunked, digest-addressed context bodies and prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512
INCREMENTAL_CHUNK_BYTES = 128


class ContextPackError(ValueError):
    """A Context Pack or delta violated the wire protocol or chunking contract."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(items: tuple[bytes, ...]) -> str:
    return "sha256:" + hashlib.sha256(_canonical({"schema": SCHEMA, "items": _encode_items(items)})).hexdigest()


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _normalize_items(items: object) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, str)) or not isinstance(items, (tuple, list)):
        raise ContextPackError("items-not-a-sequence")
    normalized: list[bytes] = []
    for item in items:
        if not isinstance(item, (bytes, bytearray)):
            raise ContextPackError("item-not-bytes")
        normalized.append(bytes(item))
    return tuple(normalized)


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items-not-a-list")
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("item-not-a-base64-string")
        try:
            items.append(base64.b64decode(value.encode("ascii"), validate=True))
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError("item-not-valid-base64") from error
    return tuple(items)


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ContextPackError("digest-not-a-sha256")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ContextPackError("digest-not-a-sha256")
    return value


def _require_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextPackError(f"{name}-not-a-non-negative-integer")
    return value


def _require_keys(payload: object, names: frozenset[str], label: str) -> dict:
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label}-not-an-object")
    if set(payload) != names:
        raise ContextPackError(f"{label}-fields-mismatch")
    return payload


@dataclass(frozen=True)
class ContextPack:
    """A complete context body as deterministic chunks plus its digest.

    The pack is rebuilt from the wire bytes alone: ``items`` concatenate to the exact body,
    ``digest`` addresses the chunk set, and ``byte_count`` is the serialized size of the pack
    including that field itself.
    """

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: object) -> ContextPack:
        normalized = _normalize_items(items)
        digest = _digest(normalized)
        payload = {"schema": SCHEMA, "items": _encode_items(normalized), "digest": digest, "byte_count": 0}
        size = len(_canonical(payload))
        for _ in range(8):
            candidate = len(_canonical({**payload, "byte_count": size}))
            if candidate == size:
                break
            size = candidate
        return cls(items=normalized, digest=digest, byte_count=size)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        if not isinstance(content, str):
            raise ContextPackError("content-not-text")
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
            raise ContextPackError("chunk-bytes-not-a-positive-integer")
        if not content:
            return cls.build(())
        chunks: list[bytes] = []
        buffer: list[str] = []
        size = 0
        for character in content:
            width = len(character.encode("utf-8"))
            if width > chunk_bytes:
                raise ContextPackError("chunk-bytes-too-small")
            if size + width > chunk_bytes:
                chunks.append("".join(buffer).encode("utf-8"))
                buffer = []
                size = 0
            buffer.append(character)
            size += width
            if character == "\n" and size >= chunk_bytes // 2:
                chunks.append("".join(buffer).encode("utf-8"))
                buffer = []
                size = 0
        if buffer:
            chunks.append("".join(buffer).encode("utf-8"))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        if not isinstance(raw, (bytes, bytearray)):
            raise ContextPackError("pack-not-bytes")
        try:
            payload = json.loads(bytes(raw))
        except (ValueError, UnicodeDecodeError) as error:
            raise ContextPackError("pack-not-json") from error
        payload = _require_keys(payload, frozenset({"schema", "items", "digest", "byte_count"}), "pack")
        if payload["schema"] != SCHEMA:
            raise ContextPackError("pack-schema-mismatch")
        items = _decode_items(payload["items"])
        digest = _require_digest(payload["digest"])
        if digest != _digest(items):
            raise ContextPackError("pack-digest-mismatch")
        byte_count = _require_count(payload["byte_count"], "byte-count")
        if byte_count != len(bytes(raw)):
            raise ContextPackError("pack-byte-count-mismatch")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """One delta: how many base chunks to keep, and the target chunks after them."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-endpoint-not-a-pack")
        prefix = 0
        limit = min(len(base.items), len(target.items))
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=_require_digest(base.digest),
            target_digest=_require_digest(target.digest),
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        if not isinstance(raw, (bytes, bytearray)):
            raise ContextPackError("delta-not-bytes")
        try:
            payload = json.loads(bytes(raw))
        except (ValueError, UnicodeDecodeError) as error:
            raise ContextPackError("delta-not-json") from error
        payload = _require_keys(
            payload,
            frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"}),
            "delta",
        )
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta-schema-mismatch")
        base_digest = _require_digest(payload["base_digest"])
        target_digest = _require_digest(payload["target_digest"])
        prefix_items = _require_count(payload["prefix_items"], "prefix-items")
        appended = _decode_items(payload["appended"])
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from an existing base pack and one delta."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-argument-not-a-pack")
    if not isinstance(delta.base_digest, str) or delta.base_digest != base.digest:
        raise ContextPackError("delta-base-digest-mismatch")
    prefix_items = _require_count(delta.prefix_items, "prefix-items")
    if prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-bounds")
    target = ContextPack.build((*base.items[:prefix_items], *delta.appended))
    if not isinstance(delta.target_digest, str) or target.digest != delta.target_digest:
        raise ContextPackError("delta-target-digest-mismatch")
    return target


def install_context_pack() -> None:
    """Expose ``ScopedContextApplication.prepare_incremental`` without touching its base path."""

    from powercontext.builtin.runtime.application import ScopedContextApplication

    if getattr(ScopedContextApplication, "__context_pack__", False):
        return

    async def prepare_incremental(self, request, base_pack=None, *, authorize_scopes=None):
        context = await self.prepare(request, authorize_scopes=authorize_scopes)
        target = ContextPack.from_content(context.content or "", chunk_bytes=INCREMENTAL_CHUNK_BYTES)
        if base_pack is None:
            return target
        if not isinstance(base_pack, ContextPack):
            raise ContextPackError("base-pack-not-a-pack")
        return ContextPackDelta.between(base_pack, target)

    prepare_incremental.__context_pack__ = True
    ScopedContextApplication.prepare_incremental = prepare_incremental
    ScopedContextApplication.__context_pack__ = True


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "DEFAULT_CHUNK_BYTES",
    "INCREMENTAL_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "SCHEMA",
    "apply_context_pack_delta",
    "install_context_pack",
]

# Importing this module is enough to wire the runtime method, so a caller that only
# knows the public pack types never has to remember a separate install step.
install_context_pack()
