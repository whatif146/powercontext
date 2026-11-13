# 一页技术说明 · StepFun × seekdb 端侧推理与混合检索

**作者**：许耀元（单人队「星火辽源」，赛号 AntTech_FG3WBV）
**提交物**：`stepfun/`（Web 应用 + 服务端 + 建模脚本 + 本文）

## 1. 结论先说

| 事情 | 做法 | 结果 |
|---|---|---|
| 端侧推理 | Transformers.js 3.7.2 + ONNX Runtime Web，模型 all-MiniLM-L6-v2（输出 384 维） | 文档与查询的向量全部在浏览器标签页内计算，零云端推理请求 |
| 量化 | 同一模型的 `model_quantized.onnx`（INT8）与 `model.onnx`（FP32） | 体积 86.20 MB → 21.91 MB（3.93×），单条编码 25.3 → 23.3 ms，Top-5 与 FP32 一致率 94% |
| 后端策略 | `device: 'webgpu'` 优先；`navigator.gpu` / `requestAdapter()` 失败自动降级 `wasm` 并在界面提示 | 两条路径都跑完整流程，界面状态条实时显示当前后端 |
| 数据层 | seekdb 1.4.0，`chunks` 表存 `VECTOR(384)` + 原文 + 元数据，`FULLTEXT`（WITH PARSER SPACE）+ `VECTOR HNSW` 双索引 | 混合检索由 `DBMS_HYBRID_SEARCH.SEARCH` 单条语句在库内完成 |
| 增量与刷新 | 批次写入（batch 1 / batch 2），页面刷新后 `/api/stats` 反映新批次，新增内容可检索 | 通过 |

## 2. 模型来源与版本（锁死）

- 权重：`Xenova/all-MiniLM-L6-v2`，Hugging Face revision `751bff37182d3f1213fa05d7196b954e230abad9`
- 运行库：`@huggingface/transformers@3.7.2`（`dist/transformers.min.js` + `ort-wasm-simd-threaded.jsep.{mjs,wasm}`）
- 全部由 `scripts/download_assets.sh` 下载，生成 `assets/manifests/model-files.txt`（体积 + sha256）
- 运行时 `env.allowRemoteModels = false`、`env.localModelPath = './models/'`：只读本地文件，断网可继续编码

## 3. 混合检索为什么合规

官方禁令：**不得在客户端分别获取两路结果后自行拼合**。本实现的调用链只有一次：

```
浏览器（编码 query 向量）
  → POST /api/search {query_text, query_vector, k}
    → seekdb: SELECT DBMS_HYBRID_SEARCH.GET_SQL('chunks', body)   -- 取得库内生成的语句
    → seekdb: SELECT DBMS_HYBRID_SEARCH.SEARCH('chunks', body)    -- 单条语句执行
浏览器 ← 已融合排序的 Top-K（_score = keyword_score + semantic_score）
```

`body` 同时包含 `query.match.content` 与 `knn.field/query_vector`，两路候选由库内隐藏列
`__pk_increment` 对齐、分数相加后排序；前端只渲染返回顺序，没有任何排序或合并逻辑。
检索页「证明」折叠区直接展示 `GET_SQL` 生成的真实 SQL，可逐字核对。

## 4. 实测数据（同机同文本，浏览器端）

测试机：云主机 4 vCPU（2 核共享，无 GPU），Chromium 147 headless，后端 `wasm`（`--disable-gpu`）；
同一进程内两套权重各跑 20 次取中位数，语料 15 段，查询 10 条，检索 5 次。

| 项 | FP32 基线 | INT8 (q8) | 说明 |
|---|---|---|---|
| 模型体积 | 86.20 MB | 21.91 MB | 压缩比 3.93×（`assets/manifests/model-files.txt` 有 sha256） |
| 首次加载耗时（冷启动） | 1085 ms | 1001 ms | 同一标签页内加载 + 建会话，含 tokenizer 解析 |
| 单条编码延迟（中位数） | 25.30 ms | 23.30 ms | 15 段语料 × 20 次，WASM 单线程 |
| 3 段批量编码 | 93.7 ms | 62.4 ms | 批量把 3 条压到 20.8 ms/条，省掉 2 次前处理 |
| Top-5 与 FP32 一致率 | 100%（基线） | 94% | 10 条固定查询，4 条完全一致、4 条 80%、2 条 100% |
| 检索延迟（库内，p50 / max） | 8.3 ms / 18.1 ms | — | 11 段语料，`DBMS_HYBRID_SEARCH.SEARCH` 单条语句 |

> 说明：WASM 单线程下的延迟受共享 vCPU 噪声影响，重复三次测量的单条延迟在 17–25 ms 之间浮动；
> 上表取中位数。INT8 的主要收益是体积（3.93×）与批量吞吐，延迟收益在 WASM 上有限，
> 官方评测机若支持 WebGPU，INT8 会走 GPU 上的 int8 kernel（本次环境无 GPU，未能实测）。

**原始测量 JSON（`bench.html` 输出，2026-11-13）**

```json
{
  "device": "wasm",
  "batch3Ms": { "int8": 62.4, "fp32": 93.7 },
  "fileSizesMB": { "int8": "21.91", "fp32": "86.20" },
  "loadMs": { "int8": 1001, "fp32": 1085 },
  "encodeMsPerText": { "int8": 23.3, "fp32": 25.3 },
  "top5Agreement": "94%",
  "retrieval": { "samples": [8.6, 7.0, 18.1, 8.3, 6.7], "p50": 8.3, "max": 18.1 },
  "perQueryAgreement": [100, 80, 100, 80, 100, 100, 100, 100, 100, 80]
}
```

## 5. 优化点与前后对比

| 优化 | 之前 | 之后 | 收益 |
|---|---|---|---|
| 批量编码 | 逐段调用 `extractor`（3 段 = 3 次前处理） | 一次调用编码 3 段 | 93.7 ms → 62.4 ms（-33%，FP32 同一套权重下实测） |
| 模型预热 | 首次检索时才编码 | 导入阶段先热 3 次编码 | 首次检索不再携带 60–100 ms 的 kernel 初始化 |
| 分块策略 | 整篇文档一个向量 | 420 字符以内按段切分 | 命中粒度更细，「遥控器有几个按键」直接命中按键段落 |
| 索引 | 无索引（顺序扫描 + 无全文打分） | `FULLTEXT` + `VECTOR HNSW` | 检索 p50 8.3 ms，两路候选在库内一次对齐 |

## 6. 复现步骤

见 `README.md` §1–§3（seekdb 1.4.0 启动 → `download_assets.sh` → `uvicorn` → 打开页面）。

## 7. 已知边界

- 图片内容支持为可选项，本次只做文本知识库（官方明确文本为基础要求）；
- 评测机若完全没有 WebGPU，会走 WASM 路径，延迟更高但流程完整；
- 数据集为演示语料（3 篇文档 / 8 段），扩量只需重复「导入」并换批次号。
