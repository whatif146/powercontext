"""StepFun × seekdb demo backend.

The browser does all embedding (Transformers.js / ONNX Runtime Web, WebGPU or WASM).
This server only stores vectors / text / metadata in seekdb and runs ONE hybrid
search statement, `DBMS_HYBRID_SEARCH.SEARCH`, so keyword and vector ranking are
fused inside the database (no client-side merging of two result sets).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

DB_CONF = {
    "host": os.environ.get("SEEKDB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("SEEKDB_PORT", "2881")),
    "user": os.environ.get("SEEKDB_USER", "root"),
    "password": os.environ.get("SEEKDB_PASSWORD", ""),
    "database": os.environ.get("SEEKDB_DB", "stepfun_demo"),
    "charset": "utf8mb4",
    "autocommit": True,
}

app = FastAPI(title="StepFun local RAG demo")


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(**DB_CONF)


def vector_literal(values: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(v):.7f}" for v in values) + "]"


@app.post("/api/init")
def init_schema() -> dict[str, Any]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id   VARCHAR(64)  NOT NULL,
                    doc_id     VARCHAR(64)  NOT NULL,
                    title      VARCHAR(200),
                    source     VARCHAR(160),
                    batch      INT          NOT NULL DEFAULT 1,
                    content    TEXT         NOT NULL,
                    created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    embedding  VECTOR(384)
                )
                """
            )
            for name, ddl in (
                (
                    "ix_chunks_fts",
                    "CREATE FULLTEXT INDEX ix_chunks_fts ON chunks (content) WITH PARSER SPACE",
                ),
                (
                    "ix_chunks_vec",
                    "CREATE VECTOR INDEX ix_chunks_vec ON chunks (embedding) "
                    "WITH (type=hnsw, distance=l2)",
                ),
            ):
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = 'chunks'
                      AND index_name = %s
                    """,
                    (name,),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(ddl)
            cur.execute("SELECT COUNT(*) FROM chunks")
            rows = cur.fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "chunks": int(rows)}


class ChunkIn(BaseModel):
    chunk_id: str | None = None
    text: str
    vector: list[float]


class DocIn(BaseModel):
    doc_id: str | None = None
    title: str = ""
    source: str = ""
    chunks: list[ChunkIn]


class ImportRequest(BaseModel):
    batch: int = 1
    docs: list[DocIn]
    replace_batch: bool = False


@app.post("/api/import")
def import_docs(req: ImportRequest) -> dict[str, Any]:
    if not req.docs:
        raise HTTPException(status_code=400, detail="no docs")
    conn = connect()
    inserted = 0
    try:
        with conn.cursor() as cur:
            if req.replace_batch:
                cur.execute("DELETE FROM chunks WHERE batch = %s", (req.batch,))
            for doc_index, doc in enumerate(req.docs):
                doc_id = doc.doc_id or f"doc-{req.batch}-{doc_index}"
                for chunk_index, chunk in enumerate(doc.chunks):
                    if len(chunk.vector) != 384:
                        raise HTTPException(
                            status_code=400,
                            detail=f"dimension mismatch: {len(chunk.vector)} != 384",
                        )
                    chunk_id = chunk.chunk_id or f"{doc_id}-c{chunk_index}"
                    cur.execute(
                        """
                        INSERT INTO chunks
                            (chunk_id, doc_id, title, source, batch, content, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            chunk_id,
                            doc_id,
                            doc.title,
                            doc.source,
                            req.batch,
                            chunk.text,
                            vector_literal(chunk.vector),
                        ),
                    )
                    inserted += 1
    finally:
        conn.close()
    return {"ok": True, "inserted": inserted, "batch": req.batch, **stats_dict()}


class SearchRequest(BaseModel):
    query_text: str
    query_vector: list[float]
    k: int = 5
    num_candidates: int = 50
    rank_constant: int = 60


@app.post("/api/search")
def search(req: SearchRequest) -> dict[str, Any]:
    """Run seekdb's native hybrid search: keyword + vector ranking fused in one statement."""
    if len(req.query_vector) != 384:
        raise HTTPException(status_code=400, detail="query_vector must be 384-dim")
    body = {
        "query": {"match": {"content": req.query_text}},
        "knn": {
            "field": "embedding",
            "query_vector": vector_literal(req.query_vector),
            "k": req.k,
            "num_candidates": req.num_candidates,
        },
        "size": req.k,
        "_source": ["chunk_id", "doc_id", "title", "source", "batch", "content"],
        "rank": {"rrf": {"rank_constant": req.rank_constant}},
    }
    body_str = json.dumps(body, ensure_ascii=False)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DBMS_HYBRID_SEARCH.GET_SQL(%s, %s)", ("chunks", body_str))
            generated = cur.fetchone()[0]
            cur.execute("SELECT DBMS_HYBRID_SEARCH.SEARCH(%s, %s)", ("chunks", body_str))
            payload = cur.fetchone()[0]
            rows = json.loads(payload) if payload else []
    finally:
        conn.close()
    hits = []
    for record in rows:
        hits.append(
            {
                "chunk_id": record.get("chunk_id"),
                "doc_id": record.get("doc_id"),
                "title": record.get("title"),
                "source": record.get("source"),
                "batch": record.get("batch"),
                "content": record.get("content"),
                "score": round(float(record.get("_score") or 0.0), 6),
            }
        )
    return {
        "ok": True,
        "engine": "seekdb DBMS_HYBRID_SEARCH.SEARCH -> one fused statement "
                  "(keyword score + semantic score combined inside the database)",
        "generated_sql": generated,
        "hits": hits,
        "total": len(hits),
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return {"ok": True, **stats_dict()}


def stats_dict() -> dict[str, Any]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT batch), MAX(batch) FROM chunks")
            count, batches, max_batch = cur.fetchone()
            cur.execute(
                "SELECT batch, COUNT(*) FROM chunks GROUP BY batch ORDER BY batch"
            )
            per_batch = [{"batch": row[0], "chunks": row[1]} for row in cur.fetchall()]
    finally:
        conn.close()
    return {"chunks": int(count), "batches": int(batches), "max_batch": int(max_batch or 0),
            "per_batch": per_batch}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, **stats_dict()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
