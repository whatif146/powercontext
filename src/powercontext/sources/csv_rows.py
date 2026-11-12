# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Row-level CSV Sources so that citations can be traced back to one record."""

from __future__ import annotations

import csv
import io
import json
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME = "csv-row"
IDENTITY_PREFIX = "csv:"

NonEmptyText = Annotated[str, Field(min_length=1)]


class CSVDocumentInput(BaseModel):
    """One offline CSV document identified by a caller-stable id and primary key."""

    document_id: NonEmptyText
    primary_key: NonEmptyText
    content: bytes

    @field_validator("document_id")
    @classmethod
    def reject_padded_document_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003
        return value

    @field_validator("primary_key")
    @classmethod
    def require_primary_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("primary_key must not be blank")  # noqa: TRY003
        return value


class CSVRowInput(BaseModel):
    """One captured CSV record addressed by its document and primary key value."""

    document_id: NonEmptyText
    primary_key: NonEmptyText
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """Captured row content that keeps the original header order and values."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the stable, delimiter-safe identity of one CSV row."""

    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty string")  # noqa: TRY003
    identity = f"{IDENTITY_PREFIX}{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("CSV row source identity exceeds the maximum source id length")  # noqa: TRY003
    return identity


def _decode_document(content: bytes) -> str:
    if not isinstance(content, bytes | bytearray):
        raise ValueError("CSV content must be bytes")  # noqa: TRY003
    raw = bytes(content)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV content must be valid UTF-8") from exc


def _validate_header(header: list[str]) -> tuple[str, ...]:
    if not header:
        raise ValueError("CSV document must start with a header record")  # noqa: TRY003
    seen: set[str] = set()
    for column in header:
        if not column.strip():
            raise ValueError("CSV header fields must not be blank")  # noqa: TRY003
        if column in seen:
            raise ValueError("CSV header fields must be unique")  # noqa: TRY003
        seen.add(column)
    return tuple(header)


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse one offline CSV document into row inputs, or fail without partial rows."""

    if not isinstance(value, CSVDocumentInput):
        raise TypeError("parse_csv_document expects a CSVDocumentInput")  # noqa: TRY003
    document_id = value.document_id
    primary_key = value.primary_key
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003
    if not isinstance(primary_key, str) or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    text = _decode_document(value.content)
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("CSV document must contain a header record") from exc
    except csv.Error as exc:
        raise ValueError(f"malformed CSV header: {exc}") from exc

    columns = _validate_header(header)
    if primary_key not in columns:
        raise ValueError(f"primary_key {primary_key!r} must match one header field exactly")  # noqa: TRY003
    key_index = columns.index(primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    try:
        for record in reader:
            if not record:
                continue
            if len(record) != len(columns):
                raise ValueError(  # noqa: TRY003
                    f"data record width {len(record)} does not match header width {len(columns)}"
                )
            key_value = record[key_index]
            if not key_value.strip():
                raise ValueError("primary key values must not be blank")  # noqa: TRY003
            if key_value in seen_keys:
                raise ValueError(f"duplicate primary key value {key_value!r}")  # noqa: TRY003
            seen_keys.add(key_value)
            rows.append(
                CSVRowInput(
                    document_id=document_id,
                    primary_key=primary_key,
                    primary_key_value=key_value,
                    columns=columns,
                    values=tuple(record),
                )
            )
    except csv.Error as exc:
        raise ValueError(f"malformed CSV document: {exc}") from exc

    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read one captured CSV record."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        if not isinstance(value, CSVRowInput):
            raise TypeError("CSV row adapter cannot resolve this input")  # noqa: TRY003
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        if not isinstance(source, CSVRowSource):
            raise TypeError("CSV row adapter cannot read this source")  # noqa: TRY003
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as canonical text evidence keyed by its own identity."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        if not isinstance(source, CSVRowSource):
            raise TypeError("CSV row projection expects a CSVRowSource")  # noqa: TRY003
        if len(source.columns) != len(source.values):
            raise ValueError("CSV row columns and values must have the same width")  # noqa: TRY003
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version="1",
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
