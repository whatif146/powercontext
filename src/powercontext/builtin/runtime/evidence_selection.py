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

"""Query-driven literal evidence selection for prepared context."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field, create_model

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from powercontext.builtin.runtime.prepared_context import (
        PreparedContextOrigin,
        PreparedMemoryCandidates,
    )

BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
TRUST = "untrusted_history"
_TERM = re.compile(r"[a-z]+|\d+")
_SPLIT = re.compile(r"[.;:!?]+\s+|\n+")
_STOPWORDS = frozenset(
    """
    a about above after again against all also an and any are as at be because been before being below between
    both but by can could did do does doing down during each few for from further had has have having he her
    here hers him himself his how i if in into is it its itself just let me more most my myself no nor not now
    of off on once only or other our ours out over own per same she should so some such than that the their
    them then there these they this those through to too under until up very was we were what when where which
    while who why will with would you your
    """.split()
)


@dataclass(frozen=True)
class EvidenceFragment:
    """One literal slice of an exact Memory entry version, with its character range."""

    address: object
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class EvidenceContextBuild:
    """Final context plus the literal fragments and their exact origins."""

    context: PreparedContext
    origins: tuple[object, ...]
    omissions: object
    code_origins: tuple[object, ...] = ()
    fragments: tuple[EvidenceFragment, ...] = ()


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(term for term in _TERM.findall(query.lower()) if term not in _STOPWORDS)


def _entry_segments(text: str) -> tuple[tuple[int, int], ...]:
    """Split one entry into literal sentence slices that never change the original chars."""

    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _SPLIT.finditer(text):
        end = match.end()
        if end <= start:
            continue
        bounds.append((start, end))
        start = end
    if start < len(text):
        bounds.append((start, len(text)))
    segments: list[tuple[int, int]] = []
    for begin, finish in bounds:
        while begin < finish and text[begin].isspace():
            begin += 1
        if begin >= finish:
            continue
        while finish - begin > 260:
            cut = text.rfind(" ", begin, begin + 260)
            if cut <= begin:
                cut = begin + 260
            segments.append((begin, cut))
            begin = cut
            while begin < finish and text[begin].isspace():
                begin += 1
        if finish > begin:
            segments.append((begin, finish))
    return tuple(segments)


def _segment_score(text: str, terms: tuple[str, ...]) -> float:
    if not terms:
        return 1.0
    tokens = set(_TERM.findall(text.lower()))
    matched = 0
    weight = 0.0
    for term in terms:
        if term in tokens:
            matched += 1
            weight += 4.0 if term.isdigit() else 1.0
    if not weight:
        return 0.0
    return weight * (1.0 + matched / len(terms)) / (1.0 + len(tokens) / 100.0)


def _merge_fragments(fragments: Sequence[EvidenceFragment]) -> tuple[EvidenceFragment, ...]:
    """Join only contiguous slices of the same entry so the delivered chars stay exact."""

    groups: dict[tuple[str, str, str], list[EvidenceFragment]] = {}
    order: list[tuple[str, str, str]] = []
    for fragment in fragments:
        key = (
            fragment.address.memory.scope_id,  # type: ignore[attr-defined]
            fragment.address.entry_id,  # type: ignore[attr-defined]
            fragment.address.entry_version_id,  # type: ignore[attr-defined]
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(fragment)
    merged: list[EvidenceFragment] = []
    for key in order:
        current: EvidenceFragment | None = None
        for fragment in sorted(groups[key], key=lambda item: item.start):
            if current is not None and fragment.start == current.end:
                current = EvidenceFragment(
                    address=current.address,
                    start=current.start,
                    end=fragment.end,
                    text=current.text + fragment.text,
                )
                continue
            if current is not None:
                merged.append(current)
            current = fragment
        if current is not None:
            merged.append(current)
    return tuple(merged)


def _render(fragments: Sequence[EvidenceFragment], trust_policy: str) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,  # type: ignore[attr-defined]
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),  # type: ignore[attr-defined]
                "entry_id": fragment.address.entry_id,  # type: ignore[attr-defined]
                "entry_version_id": fragment.address.entry_version_id,  # type: ignore[attr-defined]
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    envelope = {"trust": TRUST, "items": items}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((trust_policy, f"{BEGIN_MARKER}\n{encoded}\n{END_MARKER}"))


def _shrink(fragment: EvidenceFragment, max_bytes: int, trust_policy: str) -> EvidenceFragment | None:
    """Shorten one literal slice until the whole rendered context fits the budget."""

    if len(_render((fragment,), trust_policy).encode("utf-8")) <= max_bytes:
        return fragment
    lower = fragment.start + 1
    upper = fragment.end
    best: EvidenceFragment | None = None
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = EvidenceFragment(fragment.address, fragment.start, middle, fragment.text[: middle - fragment.start])
        if len(_render((candidate,), trust_policy).encode("utf-8")) <= max_bytes:
            best = candidate
            lower = middle + 1
        else:
            upper = middle - 1
    if best is None or not best.text.strip():
        return None
    return best


def select_evidence(request, *, current_scope_id, memory_candidates, entry_limit: int) -> EvidenceContextBuild:
    """Select literal entry slices for the request query inside the byte budget."""

    from powercontext.builtin.runtime.errors import PreparedContextInvariantError
    from powercontext.builtin.runtime.prepared_context import (
        MemoryEntryAddress,
        PreparedContextOmissions,
    )
    from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

    terms = _query_terms(request.query)
    scored: list[tuple[float, EvidenceFragment]] = []
    for group in memory_candidates:
        if not group.hits:
            continue
        if group.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        scope_id = group.scope_id or current_scope_id or ""
        for hit in group.hits:
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            if not hit.text.strip() or not hit.entry_id.strip() or not hit.entry_version_id.strip():
                continue
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            for start, end in _entry_segments(hit.text):
                text = hit.text[start:end]
                scored.append((_segment_score(text, terms), EvidenceFragment(address, start, end, text)))
    if not scored:
        return EvidenceContextBuild(
            context=PreparedContext(status="empty", content=None, content_bytes=0),
            origins=(),
            omissions=PreparedContextOmissions(),
        )

    scored.sort(key=lambda item: -item[0])
    kept: list[EvidenceFragment] = []
    for _, fragment in scored:
        merged = _merge_fragments((*kept, fragment))
        if len(merged) > entry_limit:
            continue
        if len(_render(merged, TRUST_POLICY).encode("utf-8")) <= request.max_bytes:
            kept = list(merged)
    if not kept:
        shrunk = _shrink(scored[0][1], request.max_bytes, TRUST_POLICY)
        if shrunk is None:
            return EvidenceContextBuild(
                context=PreparedContext(status="empty", content=None, content_bytes=0),
                origins=(),
                omissions=PreparedContextOmissions(),
            )
        kept = [shrunk]
    kept = list(_merge_fragments(kept))
    content = _render(kept, TRUST_POLICY)
    if len(content.encode("utf-8")) > request.max_bytes:
        return EvidenceContextBuild(
            context=PreparedContext(status="empty", content=None, content_bytes=0),
            origins=(),
            omissions=PreparedContextOmissions(),
        )
    return EvidenceContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in kept),
        omissions=PreparedContextOmissions(),
        fragments=tuple(kept),
    )


def _extend_request_model() -> None:
    """Add the default-off ``evidence_selection`` switch to the public request model."""

    from powercontext.builtin import runtime as package
    from powercontext.builtin.runtime import models as model_module
    from powercontext.builtin.runtime import prepared_context as context_module

    current = model_module.PrepareContextRequest
    if "evidence_selection" in current.model_fields:
        return
    extended = create_model(
        "PrepareContextRequest",
        __base__=current,
        evidence_selection=(bool, Field(default=False, strict=True)),
    )
    for holder, name in (
        (model_module, "PrepareContextRequest"),
        (package, "PrepareContextRequest"),
        (context_module, "PrepareContextRequest"),
    ):
        if getattr(holder, name, None) is current:
            setattr(holder, name, extended)


def install_evidence_selection() -> None:
    """Route ``evidence_selection`` requests to the literal fragment selector."""

    from powercontext.builtin.runtime import prepared_context as module

    _extend_request_model()

    builder = module.PreparedContextBuilder
    current = getattr(builder.build_scopes_result, "__evidence_selection__", False)
    if current:
        return
    original = builder.build_scopes_result

    def build_scopes_result(
        self,
        *,
        request,
        current_scope_id,
        memory_candidates=(),
        topic_memory_hits=(),
        experience_candidates=(),
        profile_candidates=(),
        code_candidates=(),
    ):
        if getattr(request, "evidence_selection", False) and request.assembly is None and not request.include_code:
            return select_evidence(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=tuple(memory_candidates),
                entry_limit=self.entry_limit,
            )
        return original(
            self,
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            topic_memory_hits=topic_memory_hits,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            code_candidates=code_candidates,
        )

    build_scopes_result.__evidence_selection__ = True
    build_scopes_result.__wrapped__ = original
    builder.build_scopes_result = build_scopes_result


__all__ = [
    "EvidenceContextBuild",
    "EvidenceFragment",
    "install_evidence_selection",
    "select_evidence",
]
