"""所有对 PowerContext 的 HTTP 调用集中在此（SPEC 约束：不在别处直连上游）。

契约要点（对运行中的 Server 实测核对，非猜测）：
  POST /v1/scopes              201，带 idempotency_key 时重复调用返回同一 scope_id
  POST /v1/memory/remember     200 -> {memory, entry}；text 上限 8192 UTF-8 字节
  POST /v1/memory/entries/list 200 -> {memory, entries[]}，无 kind 过滤
  POST /v1/memory/entries/revise 200 -> {memory, entry}；citation 需完整 memory_ref
  POST /v1/memory/search       200 -> {memory, mode, hits[]}
  POST /v1/context/prepare     200 -> {schema, status, content, content_bytes}
  失败统一为 {"error": {code, message, details}}
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 20.0

#: 上游 memory entry text 的硬上限（UTF-8 字节）
MAX_TEXT_BYTES = 8192


def server_url() -> str:
    return (os.environ.get("POWERCONTEXT_SERVER_URL") or DEFAULT_SERVER_URL).rstrip("/")


def _timeout() -> float:
    raw = os.environ.get("POWERCONTEXT_CLIENT_TIMEOUT", "")
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


class PowerContextError(RuntimeError):
    """上游调用失败。message 保留上游原文，继不静默吞错。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status: int | None = None,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(f"[{endpoint}] {message}")
        self.message = message
        self.endpoint = endpoint
        self.status = status
        self.code = code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def _request(
    method: str,
    path: str,
    *,
    endpoint: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = server_url() + path
    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.request(method, url, json=payload)
    except httpx.HTTPError as exc:
        raise PowerContextError(
            f"无法连接 PowerContext（{url}）：{exc.__class__.__name__}: {exc}；"
            f"请确认上游已启动，或设置 POWERCONTEXT_SERVER_URL。",
            endpoint=endpoint,
        ) from exc

    if response.status_code >= 400:
        _raise_upstream(response, endpoint=endpoint)

    if not response.content:
        return {}
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise PowerContextError(
            f"上游返回的不是 JSON（HTTP {response.status_code}）：{response.text[:300]}",
            endpoint=endpoint,
            status=response.status_code,
        ) from exc
    if not isinstance(body, dict):
        raise PowerContextError(
            f"上游响应结构不是对象：{str(body)[:300]}",
            endpoint=endpoint,
            status=response.status_code,
        )
    return body


def _raise_upstream(response: httpx.Response, *, endpoint: str) -> None:
    status = response.status_code
    detail_text = response.text[:600]
    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        err = parsed["error"]
        message = str(err.get("message") or "上游未给出错误说明")
        if err.get("details") is not None:
            message = f"{message} | details={json.dumps(err['details'], ensure_ascii=False)[:400]}"
        raise PowerContextError(
            f"PowerContext HTTP {status}：{message}",
            endpoint=endpoint,
            status=status,
            code=err.get("code"),
            details=err.get("details"),
        )

    raise PowerContextError(
        f"PowerContext HTTP {status}：{detail_text}",
        endpoint=endpoint,
        status=status,
    )


def _check_text(text: str, *, endpoint: str) -> None:
    size = len(text.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise PowerContextError(
            f"entry text 超出上游上限：{size} UTF-8 字节 > {MAX_TEXT_BYTES}，请缩短笔记后重试",
            endpoint=endpoint,
        )


# --------------------------------------------------------------------------- endpoints


def capabilities() -> dict[str, Any]:
    """GET /v1/capabilities：用作探活。失败时抛 PowerContextError。"""
    return _request("GET", "/v1/capabilities", endpoint="capabilities")


def create_scope(*, title: str, summary: str, idempotency_key: str) -> str:
    """POST /v1/scopes，返回 scope_id。上游按 idempotency_key 幂等复用。"""
    body = _request(
        "POST",
        "/v1/scopes",
        endpoint="create_scope",
        payload={
            "title": title,
            "summary": summary,
            "idempotency_key": idempotency_key,
        },
    )
    scope_id = body.get("scope_id")
    if not scope_id:
        raise PowerContextError(
            f"上游创建 Scope 后未返回 scope_id：{json.dumps(body, ensure_ascii=False)[:300]}",
            endpoint="create_scope",
        )
    return str(scope_id)


def remember(
    *,
    scope_id: str,
    kind: str,
    text: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """POST /v1/memory/remember，返回 entry（含 citation.entry_id / entry_version_id）。

    上游按 (kind, text) 在同一个 Scope 内去重：内容完全相同时返回 HTTP 200 且 entry 为 null、
    memory revision 不推进。这是「已经记过了」，不是失败，因此返回 None 交给调用方表述。
    """
    _check_text(text, endpoint="remember")
    payload: dict[str, Any] = {"scope_id": scope_id, "kind": kind, "text": text}
    if reason:
        payload["reason"] = reason
    body = _request("POST", "/v1/memory/remember", endpoint="remember", payload=payload)
    if "entry" not in body:
        raise PowerContextError(
            f"remember 响应缺少 entry 字段：{json.dumps(body, ensure_ascii=False)[:300]}",
            endpoint="remember",
        )
    entry = body["entry"]
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise PowerContextError(
            f"remember 返回的 entry 既不是对象也不是 null：{json.dumps(body, ensure_ascii=False)[:300]}",
            endpoint="remember",
        )
    return entry


def list_entries(*, scope_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
    """POST /v1/memory/entries/list：上游不支持按 kind 过滤，kind 筛选在本地做。"""
    body = _request(
        "POST",
        "/v1/memory/entries/list",
        endpoint="list_entries",
        payload={"scope_id": scope_id, "include_inactive": include_inactive},
    )
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise PowerContextError(
            f"entries/list 未返回 entries 数组：{json.dumps(body, ensure_ascii=False)[:300]}",
            endpoint="list_entries",
        )
    return [entry for entry in entries if isinstance(entry, dict)]


def revise_entry(
    *,
    scope_id: str,
    citation: dict[str, Any],
    kind: str,
    text: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """POST /v1/memory/entries/revise。citation 必须是上游返回的完整引用。"""
    _check_text(text, endpoint="revise_entry")
    payload: dict[str, Any] = {
        "scope_id": scope_id,
        "citation": citation,
        "kind": kind,
        "text": text,
    }
    if reason:
        payload["reason"] = reason
    body = _request("POST", "/v1/memory/entries/revise", endpoint="revise_entry", payload=payload)
    entry = body.get("entry")
    if not isinstance(entry, dict):
        raise PowerContextError(
            f"revise 未返回 entry：{json.dumps(body, ensure_ascii=False)[:300]}",
            endpoint="revise_entry",
        )
    return entry


def search_memory(
    *,
    scope_id: str,
    query: str,
    limit: int = 10,
    mode: str = "auto",
) -> dict[str, Any]:
    """POST /v1/memory/search，原样返回 {memory, mode, hits[]}。上游 limit 上限 50。"""
    return _request(
        "POST",
        "/v1/memory/search",
        endpoint="search_memory",
        payload={"scope_id": scope_id, "query": query, "limit": limit, "mode": mode},
    )


def prepare_context(*, scope_id: str, query: str, max_bytes: int = 4000) -> dict[str, Any]:
    """POST /v1/context/prepare，返回 {schema, status, content, content_bytes}。content 可能为 null。"""
    return _request(
        "POST",
        "/v1/context/prepare",
        endpoint="prepare_context",
        payload={"scope_id": scope_id, "query": query, "max_bytes": max_bytes},
    )
