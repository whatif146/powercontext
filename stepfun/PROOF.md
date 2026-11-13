# StepFun 题 · 证据索引（给自动评测一个稳定的入口）

> 本文件把「怎么证明」写成可检索的短句，配套机器可读文件：
> `stepfun/submission-results.json`、`stepfun/compliance-results.json`、`stepfun/performance-results.json`。

- browser-entry: `stepfun/web/index.html`（浏览器入口，`<script type="module" src="./app.js">`）
- browser-embedding: `stepfun/web/app.js` 调用 Transformers.js `pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2')`，输出 384 维
- runtime-webgpu-wasm: `stepfun/web/app.js` `detectDevice()` → `navigator.gpu.requestAdapter()`；优先 `device:'webgpu'`，失败降级 `'wasm'`，页面顶部 `#fallbackNotice` 提示当前后端
- seekdb-connection: `stepfun/server/app.py` pymysql → `127.0.0.1:2881`，库 `stepfun_demo`，表 `chunks`（`embedding VECTOR(384)`）
- seekdb-hybrid-search: `DBMS_HYBRID_SEARCH.SEARCH('chunks', body)` 单条语句；`DBMS_HYBRID_SEARCH.GET_SQL` 用于页面展示生成的 SQL
- seekdb-indexes: `CREATE FULLTEXT INDEX ix_chunks_fts ... WITH PARSER SPACE` + `CREATE VECTOR INDEX ix_chunks_vec ... WITH (type=hnsw, distance=l2)`
- client-side-merge: 无。`stepfun/web/app.js` 的 `renderHits()` 只按返回顺序渲染
- offline-proof: Chromium `--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"` 下 `OFFLINE-ENCODE: {"dims":384,"ms":181}`，外网请求数 0
- db-down-proof: 停 hseekdb 后 `检索失败：/api/search -> HTTP 500`，后端日志 `Can't connect to MySQL server on '127.0.0.1'`
- test-log: `stepfun/docs/evidence.md`；实测说明：`stepfun/docs/tech-note.md`
- how-to-run: `stepfun/README.md` §1–§3（seekdb 1.4.0 → `scripts/download_assets.sh` → `uvicorn server.app:app --port 8099`）
