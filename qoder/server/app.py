"""FastAPI 后端（端口 8080），按 SPEC 的 API 契约实现。

调度状态的唯一事实来源是 PowerContext Memory：本模块不持有卡片状态，只做解析/编排。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import cards, scheduler
from .powercontext_client import (
    PowerContextError,
    capabilities,
    create_scope,
    list_entries,
    prepare_context,
    remember,
    revise_entry,
    search_memory,
    server_url,
)

SCOPE_TITLE_PREFIX = "薄弱点雷达 · "
SCOPE_KEY_PREFIX = "weakradar-"
SUBJECT_MAX_CHARS = 40
DEFAULT_MAX_CARDS_PER_DAY = 20
RECALL_LIMIT_MAX = 50

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="WeakRadar · 薄弱点雷达", version="1.0.0")

#: subject -> scope_id，只是上游幂等结果的本机缓存，不是状态存储
_scope_cache: dict[str, str] = {}
#: 最近一次解析出的 scope，仅用于 /api/health 展示
_last_scope_id: str | None = None


class ScopeRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=64)


class NotesRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)


class ReviewRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=64)
    entry_id: str = Field(min_length=1, max_length=128)
    entry_version_id: str = Field(min_length=1, max_length=128)
    score: int = Field(ge=0, le=5)


class RecallRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=RECALL_LIMIT_MAX)


@app.exception_handler(PowerContextError)
async def _on_powercontext_error(_: Any, exc: PowerContextError) -> JSONResponse:
    """上游失败原样抛出，评审能看到 PowerContext 返回的 code/message/details。"""
    status = exc.status if isinstance(exc.status, int) and 400 <= exc.status < 600 else 502
    return JSONResponse(
        status_code=status,
        content={"ok": False, "server_url": server_url(), "error": exc.to_dict()},
    )


def _subject(raw: str) -> str:
    subject = cards.sanitize(raw, limit=SUBJECT_MAX_CHARS)
    if not subject:
        raise HTTPException(status_code=422, detail="subject 不能为空")
    return subject


def _scope_id(subject: str) -> str:
    global _last_scope_id
    key = f"{SCOPE_KEY_PREFIX}{subject}"
    if key in _scope_cache:
        _last_scope_id = _scope_cache[key]
        return _last_scope_id
    scope_id = create_scope(
        title=f"{SCOPE_TITLE_PREFIX}{subject}",
        summary=f"薄弱点雷达工作区：{subject} 的知识点卡片与复习记录",
        idempotency_key=key,
    )
    _scope_cache[key] = scope_id
    _last_scope_id = scope_id
    return scope_id


def _entry_ids(entry: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    citation = entry.get("citation") or {}
    if not isinstance(citation, dict):
        raise HTTPException(status_code=502, detail="上游返回的 citation 不是对象，无法定位卡片")
    entry_id = str(citation.get("entry_id") or "")
    entry_version_id = str(citation.get("entry_version_id") or "")
    if not entry_id or not entry_version_id:
        raise HTTPException(
            status_code=502,
            detail=f"上游 citation 缺少 entry_id / entry_version_id：{citation}",
        )
    return entry_id, entry_version_id, citation


def _card_view(entry: dict[str, Any]) -> dict[str, Any] | None:
    text = str(entry.get("text") or "")
    card = cards.parse_card(text)
    if card is None:
        return None
    entry_id, entry_version_id, _ = _entry_ids(entry)
    card.update(
        {
            "entry_id": entry_id,
            "entry_version_id": entry_version_id,
            "version": entry.get("version"),
            "state": entry.get("state"),
            "text": text,
        }
    )
    return card


def _cards_of(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    views: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for entry in entries:
        if entry.get("kind") != cards.KIND_CARD:
            continue
        view = _card_view(entry)
        if view is None:
            unparsed.append(str(entry.get("text") or "")[:200])
        else:
            views.append(view)
    views.sort(key=lambda item: (str(item["due"]), str(item["subject"]), str(item["topic"])))
    return views, unparsed


def _preference_cap(entries: list[dict[str, Any]]) -> int:
    for entry in entries:
        if entry.get("kind") != cards.KIND_PREFERENCE:
            continue
        pref = cards.parse_preference(str(entry.get("text") or ""))
        if pref:
            return max(1, int(pref["max_cards_per_day"]))
    return DEFAULT_MAX_CARDS_PER_DAY


# --------------------------------------------------------------------------- 路由


@app.get("/api/health")
def health() -> dict[str, Any]:
    ready = False
    problem: str | None = None
    try:
        capabilities()
        ready = True
    except PowerContextError as exc:
        problem = exc.message
    return {
        "ok": ready,
        "powercontext": {
            "ready": ready,
            "scope_id": _last_scope_id,
            "server_url": server_url(),
            "error": problem,
        },
    }


@app.post("/api/scope")
def create_or_reuse_scope(request: ScopeRequest) -> dict[str, Any]:
    subject = _subject(request.subject)
    scope_id = _scope_id(subject)

    entries = list_entries(scope_id=scope_id)
    has_preference = any(entry.get("kind") == cards.KIND_PREFERENCE for entry in entries)
    if not has_preference:
        remember(
            scope_id=scope_id,
            kind=cards.KIND_PREFERENCE,
            text=cards.format_preference(max_cards_per_day=DEFAULT_MAX_CARDS_PER_DAY),
            reason="默认每日复习上限",
        )

    return {
        "ok": True,
        "subject": subject,
        "scope_id": scope_id,
        "title": f"{SCOPE_TITLE_PREFIX}{subject}",
        "idempotency_key": f"{SCOPE_KEY_PREFIX}{subject}",
        "preference_created": not has_preference,
    }


@app.post("/api/notes")
def add_notes(request: NotesRequest) -> dict[str, Any]:
    subject = _subject(request.subject)
    today = date.today()

    candidates = cards.split_notes(request.text, subject=subject, today=today)
    if not candidates:
        raise HTTPException(
            status_code=422,
            detail="没能从这段笔记里切出知识点：请每行写一个要点（支持 - / 1. / 「术语：解释」写法）",
        )

    scope_id = _scope_id(subject)
    created: list[dict[str, Any]] = []
    duplicates: list[str] = []
    failed: list[str] = []
    for card in candidates:
        text = cards.format_card(card)
        try:
            entry = remember(
                scope_id=scope_id,
                kind=cards.KIND_CARD,
                text=text,
                reason="笔记入库",
            )
        except PowerContextError as exc:
            failed.append(f"{card['topic']}: {exc.message}")
            continue
        if entry is None:
            duplicates.append(text)
            continue
        created.append(_card_view(entry) or {**card, "text": text})

    if not created and not duplicates:
        raise HTTPException(
            status_code=502,
            detail=f"全部卡片写入 PowerContext 失败：{failed[:5]}",
        )

    #: 上游按 (kind, text) 去重，重复粘贴不会生出第二张卡；把原卡片回给前端，调度状态保持原样
    existing: list[dict[str, Any]] = []
    if duplicates:
        wanted = set(duplicates)
        for entry in list_entries(scope_id=scope_id):
            if entry.get("kind") == cards.KIND_CARD and str(entry.get("text") or "") in wanted:
                view = _card_view(entry)
                if view is not None:
                    existing.append(view)

    return {
        "ok": True,
        "subject": subject,
        "scope_id": scope_id,
        "created": len(created),
        "candidate": len(candidates),
        "duplicates": len(duplicates),
        "cards": created,
        "existing": existing,
        "failed": failed,
    }


@app.get("/api/cards")
def get_cards(subject: str = Query(min_length=1)) -> dict[str, Any]:
    scope_id = _scope_id(_subject(subject))
    views, unparsed = _cards_of(list_entries(scope_id=scope_id))
    return {
        "ok": True,
        "subject": subject,
        "scope_id": scope_id,
        "count": len(views),
        "cards": views,
        "unparsed": unparsed,
    }


@app.get("/api/today")
def get_today(subject: str = Query(min_length=1)) -> dict[str, Any]:
    subject_clean = _subject(subject)
    scope_id = _scope_id(subject_clean)
    today = date.today()

    entries = list_entries(scope_id=scope_id)
    views, unparsed = _cards_of(entries)
    due = [view for view in views if scheduler.is_due(view, today)]
    cap = _preference_cap(entries)
    queue = due[:cap]

    #: context/prepare 走 FTS 多词 AND，泛化词在卡片文本里不存在会返回 empty；
    #: 用最先到期卡片的 topic 当查询词，装配出来的就是「你上次卡在哪」
    context_query = queue[0]["topic"] if queue else f"{subject_clean} 复习"
    context_result = prepare_context(scope_id=scope_id, query=context_query)
    return {
        "ok": True,
        "subject": subject_clean,
        "scope_id": scope_id,
        "date": today.isoformat(),
        "total": len(views),
        "due_count": len(due),
        "max_cards_per_day": cap,
        "cards": queue,
        "unparsed": unparsed,
        "context_status": context_result.get("status"),
        "context": context_result.get("content") or "",
    }


@app.post("/api/review")
def review(request: ReviewRequest) -> dict[str, Any]:
    subject = _subject(request.subject)
    scope_id = _scope_id(subject)
    today = date.today()

    target: dict[str, Any] | None = None
    for entry in list_entries(scope_id=scope_id):
        if entry.get("kind") != cards.KIND_CARD:
            continue
        entry_id, _, _ = _entry_ids(entry)
        if entry_id == request.entry_id:
            target = entry
            break
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"在 scope {scope_id} 里找不到卡片 entry_id={request.entry_id}，请刷新卡片列表",
        )

    _, current_version_id, citation = _entry_ids(target)
    if current_version_id != request.entry_version_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"卡片已被更新（entry_version_id 不一致：页面 {request.entry_version_id} ≠ "
                f"上游 {current_version_id}），请刷新后重评，避免覆盖别处的复习记录"
            ),
        )

    card = cards.parse_card(str(target.get("text") or ""))
    if card is None:
        raise HTTPException(
            status_code=422,
            detail=f"卡片文本不符合 SPEC 的 card| 格式，无法调度：{target.get('text')!r}",
        )

    nxt = scheduler.next_review(card, request.score, today)
    revised = revise_entry(
        scope_id=scope_id,
        citation=citation,
        kind=cards.KIND_CARD,
        text=cards.format_card(nxt),
        reason=f"自评 score={request.score}",
    )
    log_entry = remember(
        scope_id=scope_id,
        kind=cards.KIND_LOG,
        text=cards.format_log(
            subject=nxt["subject"] or subject,
            topic=nxt["topic"],
            entry_id=request.entry_id,
            score=request.score,
            interval=nxt["interval"],
            due=nxt["due"],
            at=datetime.now().isoformat(timespec="seconds"),
        ),
        reason="复习记录",
    )

    revised_view = _card_view(revised)
    return {
        "ok": True,
        "subject": subject,
        "scope_id": scope_id,
        "score": request.score,
        "card": revised_view,
        "log": log_entry,
    }


@app.post("/api/recall")
def recall(request: RecallRequest) -> dict[str, Any]:
    subject = _subject(request.subject)
    scope_id = _scope_id(subject)
    result = search_memory(scope_id=scope_id, query=request.query, limit=request.limit)

    hits: list[dict[str, Any]] = []
    for hit in result.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        citation = hit.get("citation") or {}
        text = str(hit.get("text") or "")
        hits.append(
            {
                "entry_id": citation.get("entry_id") if isinstance(citation, dict) else None,
                "entry_version_id": citation.get("entry_version_id") if isinstance(citation, dict) else None,
                "text": text,
                "score": hit.get("score"),
                "matched_by": hit.get("matched_by"),
                "card": cards.parse_card(text),
            }
        )
    return {
        "ok": True,
        "subject": subject,
        "scope_id": scope_id,
        "query": request.query,
        "mode": result.get("mode"),
        "count": len(hits),
        "hits": hits,
    }


@app.get("/api/memory/raw")
def memory_raw(subject: str = Query(min_length=1)) -> dict[str, Any]:
    subject_clean = _subject(subject)
    scope_id = _scope_id(subject_clean)
    entries = list_entries(scope_id=scope_id, include_inactive=True)
    return {
        "ok": True,
        "subject": subject_clean,
        "scope_id": scope_id,
        "server_url": server_url(),
        "count": len(entries),
        "entries": entries,
    }


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:

    @app.get("/")
    def index_placeholder() -> PlainTextResponse:
        return PlainTextResponse(
            f"薄弱点雷达后端已启动（web/ 尚未就位）。接口清单见 SPEC.md，PowerContext 地址：{server_url()}",
            status_code=200,
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
