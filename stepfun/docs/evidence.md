# 自测与取证记录（StepFun × seekdb）

测试时间：2026-11-13（UTC+8）
测试机：云主机 4 vCPU（2 核共享）/ 4 GB 内存 / 无 GPU
浏览器：Chromium 147（headless，`--disable-gpu`，因此走 WASM 降级路径）
seekdb：1.4.0 发行包原生部署（`seekdb --base-dir /tmp/sd2 --port 2881`），库名 `stepfun_demo`

## 1. 完整流程（验收点 1）

脚本 `scripts/e2e_check.js`（puppeteer-core 驱动 Chromium）依次执行：加载模型 → 导入示例语料（batch 1）
→ 查询「遥控器有几个按键」→ 导入 batch 2 → 刷新页面 → 查询「HNSW 索引延迟」。实测输出：

```
MODEL: {"ms":1121,"b":{"device":"wasm","dtype":"q8"}}
IMPORT: 已写入 batch 1：+8 段，库内共 8 段（1 批）｜编码 18 ms
SEARCH: 5 条结果｜库内检索 22 ms
IMPORT2: 已写入 batch 2：+2 段，库内共 11 段（2 批）｜编码 18 ms
AFTER-REFRESH: 5 条结果｜库内检索 22 ms
```

查询「遥控器有几个按键」的返回顺序（库内融合排序）：

| 名次 | `_score` | 命中文档 | 命中段落 |
|---|---|---|---|
| 1 | 见截图 | 遥控器原型评审 | 产品经理：遥控器原型目前只有三个按键…（Top-1） |
| 2 | 0.0161 | 遥控器原型评审 | 电池方案：两节 AAA 电池可以撑大约两周… |
| 3 | 0.0159 | 遥控器原型评审 | 硬件工程师：遥控器使用 2.4G 无线模块… |
| 4 | 0.0156 | 视频管线性能优化记录 | 渲染队列积压超过 4 帧时丢弃过期帧… |
| 5 | 0.0154 | seekdb 混合检索备忘 | 表不能自带主键，隐藏列 `__pk_increment`… |

页面截图（`01-search.png` / `02-incremental.png` / `03-bench.png`）在本机留存，未随仓库提交；
按 §1 的脚本与本机 seekdb 可完整复现上述输出。

## 2. 本地推理（验收点 2）

- 页面设置 `env.allowRemoteModels = false`、`env.localModelPath = './models/'`，权重全部来自本站静态文件；
- 用 `--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"` 让浏览器**无法访问任何外网**后，
  在新标签页执行编码：`OFFLINE-ENCODE: {"dims":384,"ms":181}`（仍产出 384 维向量）；
- 同一会话内抓到的外网请求数：`EXTERNAL REQUESTS: 0`（只有 `127.0.0.1:8099` 的静态资源与 `/api/*`）。

## 3. 混合检索在库内完成（验收点 5，最大扣分点）

一次检索只有一个业务请求 `/api/search`，服务端在 seekdb 内做两件事：

```sql
SELECT DBMS_HYBRID_SEARCH.GET_SQL('chunks', :body)   -- 库内生成融合语句（用于取证展示）
SELECT DBMS_HYBRID_SEARCH.SEARCH('chunks', :body)    -- 执行该单条语句
```

`body` 同时带 `query.match.content` 与 `knn.field/query_vector`，生成的语句形态为
`(ifnull(_fts._keyword_score,0) + ifnull(_vs._semantic_score,0)) AS _score`，两路候选在库内对齐后排序。
前端 `renderHits()` 只按返回顺序渲染，`web/app.js` 中没有任何排序或合并逻辑。

## 4. 后端与降级（验收点 4）

| 场景 | 结果 |
|---|---|
| 自动模式（无 GPU 环境，本机实测） | `device: 'wasm'`，页面顶部出现降级提示条，流程完整 |
| 强制 `wasm` | 正常加载与检索 |
| 强制 `gpu`（无 WebGPU） | 明确报错「当前浏览器没有 WebGPU，无法强制启用 GPU 后端」，不静默失败 |

## 5. 增量与刷新（验收点 6）

- `/api/import` 按 `batch` 写入，`/api/stats` 返回 `chunks / batches / per_batch`；
- 导入 batch 2 后刷新整页，状态条显示 `11 chunks / 2 batch`，batch 2 的段落可被检索命中。

## 6. 禁令自检（验收点 8）

| 禁令 | 自检 | 实测 |
|---|---|---|
| 禁云端推理 API | 代码 `allowRemoteModels=false` + 外网封锁下的编码测试 | 384 维照常产出，外网请求 0 |
| 禁服务端预计算回放 | 服务端只存浏览器送来的向量；任意新文本随时可编码 | 通过（`/api/import` 只接收 `vector` 字段） |
| 禁绕过 seekdb | 停掉 seekdb 后点「检索」 | 页面显示「检索失败：/api/search -> HTTP 500」，后端日志为 `Can't connect to MySQL server on '127.0.0.1'`，无客户端兜底 |

## 7. 边界与未覆盖（诚实声明）

- 本机无 GPU：WebGPU 正路径未在真实 GPU 上跑过，只验证了「探测失败 → 自动降级 WASM」；
- 图片内容支持为官方可选扩展，未实现；
- 演示语料 11 段；写入 / 检索代码不随规模变化，扩量只需重复导入并换批次号。
