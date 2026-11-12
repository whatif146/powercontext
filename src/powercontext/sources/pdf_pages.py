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

"""Parse every physical page of a financial-report PDF and search it by page."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME = "pdf-page"
IDENTITY_PREFIX = "pdf:"

NonEmptyText = Annotated[str, Field(min_length=1)]
_TERM_PATTERN = re.compile(r"[a-z]+|\d+")
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have having
    he her here hers herself him himself his how i if in into is it its itself just let me more most my myself
    no nor not now of off on once only or other ou our ours ourselves out over own per same she should so some
    such than that the their theirs them themselves then there these they this those through to too under until
    up very was we were what when where which while who whom why will with would you your yours yourself
    yourselves s t
    """.split()
)


class PDFDocumentInput(BaseModel):
    """One complete PDF file identified by a caller-stable document id."""

    document_id: NonEmptyText
    content: bytes

    @field_validator("document_id")
    @classmethod
    def reject_blank_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """One physical page of a PDF revision."""

    document_id: NonEmptyText
    document_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    page_number: Annotated[int, Field(ge=1)]
    text: str


class PDFPageSource(Source):
    """Captured page text anchored to its document revision and physical page."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable, collision-free identity of one PDF page revision."""

    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if not isinstance(document_sha256, str) or len(document_sha256) != 64:
        raise ValueError("document_sha256 must be a 64-character digest")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    digest = hashlib.sha256(f"{document_id}\x00{document_sha256}\x00{page_number}".encode()).hexdigest()
    identity = f"{IDENTITY_PREFIX}{page_number}:{digest}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("PDF page source identity exceeds the maximum source id length")  # noqa: TRY003
    return identity


def _read_pages(content: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not isinstance(content, bytes | bytearray):
        raise ValueError("PDF content must be bytes")  # noqa: TRY003
    raw = bytes(content)
    if not raw.strip():
        raise ValueError("PDF content is empty")  # noqa: TRY003
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:  # noqa: BLE001 - encrypted input must fail closed
                raise ValueError("encrypted PDF requires a user password") from exc
            if not unlocked:
                raise ValueError("encrypted PDF requires a user password")  # noqa: TRY003
        pages: list[str] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - one page failure aborts the document
                raise ValueError(f"cannot extract PDF page text: {exc}") from exc
            pages.append(text)
        if not pages:
            raise ValueError("PDF contains no page")  # noqa: TRY003
        return pages
    except ValueError:
        raise
    except (PdfReadError, Exception) as exc:  # noqa: BLE001 - any parse failure is atomic
        raise ValueError(f"cannot parse PDF document: {exc}") from exc


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Return every physical page in order, with the digest of the whole input file."""

    if not isinstance(value, PDFDocumentInput):
        raise TypeError("parse_pdf_document expects a PDFDocumentInput")  # noqa: TRY003
    document_id = value.document_id
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    content = value.content
    if not isinstance(content, bytes | bytearray):
        raise ValueError("PDF content must be bytes")  # noqa: TRY003
    raw = bytes(content)
    digest = hashlib.sha256(raw).hexdigest()
    pages = _read_pages(raw)
    return tuple(
        PDFPageInput(document_id=document_id, document_sha256=digest, page_number=number, text=text)
        for number, text in enumerate(pages, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read one captured PDF page."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        if not isinstance(value, PDFPageInput):
            raise TypeError("PDF page adapter cannot resolve this input")  # noqa: TRY003
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        if not isinstance(source, PDFPageSource):
            raise TypeError("PDF page adapter cannot read this source")  # noqa: TRY003
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        if not isinstance(source, PDFPageSource):
            raise TypeError("PDF page projection expects a PDFPageSource")  # noqa: TRY003
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


def _terms(text: str) -> list[str]:
    lowered = text.lower().replace("\u2019", "'").replace("\u2013", " ")
    return _TERM_PATTERN.findall(lowered)


def _query_terms(question: str) -> tuple[str, ...]:
    return tuple(term for term in _terms(question) if term not in _STOPWORDS)


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to limit distinct page citations ranked for the raw question."""

    from powercontext.builtin.runtime import SearchMemoryRequest

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")  # noqa: TRY003
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")  # noqa: TRY003

    pool_size = max(limit * 10, 100)
    seen: dict[str, object] = {}
    hits = list((await memory.search(SearchMemoryRequest(query=question, mode="fts", limit=pool_size))).hits)
    for hit in hits:
        seen.setdefault(hit.entry_id, hit)

    terms = _query_terms(question)
    if len(seen) < pool_size and terms:
        vocabulary = Counter(terms)
        probes = [term for term, _ in sorted(vocabulary.items(), key=lambda item: (-len(item[0]), item[0]))[:5]]
        probes.append(" ".join(terms))
        for probe in probes:
            if not probe.strip():
                continue
            page = await memory.search(SearchMemoryRequest(query=probe, mode="fts", limit=pool_size))
            for hit in page.hits:
                seen.setdefault(hit.entry_id, hit)

    candidates = list(seen.values())
    if not candidates:
        return ()

    weighted = _idf(candidates, terms)
    scored = sorted(candidates, key=lambda hit: (-_score(hit.text, terms, weighted), hit.entry_id))
    selected = []
    pages = set()
    for hit in scored:
        page_key = _page_key(hit.text)
        if page_key in pages:
            continue
        pages.add(page_key)
        selected.append(hit)
        if len(selected) >= limit:
            break
    return tuple(
        MemoryCitation(memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id)
        for hit in selected
    )


def _page_key(text: str) -> str:
    first_line, _, remainder = text.partition("\n")
    return f"{first_line}\x00{hashlib.blake2b(remainder.encode(), digest_size=16).hexdigest()}"


def _idf(candidates, terms) -> dict[str, float]:
    counts: Counter[str] = Counter()
    for hit in candidates:
        tokens = set(_terms(hit.text))
        for term in terms:
            if term in tokens:
                counts[term] += 1
    total = max(len(candidates), 1)
    return {term: math.log(1.0 + total / (1.0 + counts[term])) for term in terms}


def _score(text: str, terms: tuple[str, ...], weighted: dict[str, float]) -> float:
    tokens = _terms(text)
    frequency = Counter(tokens)
    unique = set(tokens)
    score = 0.0
    for term in terms:
        if term in unique:
            score += weighted.get(term, 1.0) * (1.0 + min(frequency[term], 5) / 10.0)
    for position, term in enumerate(terms):
        if term.isdigit() and term in unique:
            score += 3.0
        if position + 1 < len(terms) and f"{term} {terms[position + 1]}" in text.lower():
            score += 2.0
    return score


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
