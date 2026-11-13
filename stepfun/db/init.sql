-- StepFun × seekdb demo schema (reference DDL).
-- `POST /api/init` applies the same statements idempotently at runtime.
--
-- NOTE: DBMS_HYBRID_SEARCH addresses rows through the hidden __pk_increment column,
-- so the table must NOT declare a user primary key.
CREATE DATABASE IF NOT EXISTS stepfun_demo;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   VARCHAR(64)  NOT NULL,
    doc_id     VARCHAR(64)  NOT NULL,
    title      VARCHAR(200),
    source     VARCHAR(160),
    batch      INT          NOT NULL DEFAULT 1,
    content    TEXT         NOT NULL,
    created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    embedding  VECTOR(384)
);

CREATE FULLTEXT INDEX ix_chunks_fts ON chunks (content) WITH PARSER SPACE;

CREATE VECTOR INDEX ix_chunks_vec ON chunks (embedding) WITH (type=hnsw, distance=l2);
