# StepFun × seekdb：浏览器端推理 + 库内混合检索

文档在**浏览器里**被编码成向量（Transformers.js / ONNX Runtime Web，WebGPU 优先、WASM 自动降级），
向量、原文、元数据写入 **seekdb**；检索时查询也在浏览器里编码，随后由 seekdb 的
`DBMS_HYBRID_SEARCH.SEARCH` **一条语句在库内**完成「全文 + 向量」混合打分与排序，前端不做任何两路拼合。

## 目录

```
stepfun/
├── server/app.py              # FastAPI 后端：建库建索引 / 写入 / 单语句混合检索 / 状态
├── db/init.sql                # 参考 DDL（后端启动时会幂等创建）
├── web/
│   ├── index.html             # 主界面：导入 / 检索 / 后端状态条
│   ├── app.js                 # 浏览器端 embedding、分块、导入、检索
│   ├── bench.html / bench.js  # 量化与延迟实测页
│   ├── vendor/                # 固定的 Transformers.js 与 ORT-WASM（由脚本下载）
│   └── models/                # 固定的 ONNX 权重（由脚本下载）
├── scripts/download_assets.sh # 一次性下载全部资源并生成 sha256 清单
├── scripts/e2e_check.js       # 端到端自检脚本（puppeteer 驱动）
├── assets/manifests/          # 下载产物的体积 + sha256 清单
├── docs/tech-note.md          # 一页技术说明（含实测数字）
└── docs/evidence.md           # 自测与取证记录
```

## 1. 启动 seekdb（二选一）

**A. 官方发行包（推荐，无容器）**

```bash
# 以 seekdb 1.4.0 的 ubuntu22.04 amd64 包为例
sudo apt-get install -y libaio1
sudo dpkg -i seekdb_1.4.0-*ubuntu22.04_amd64.deb
seekdb --base-dir /tmp/seekdb --port 2881      # 默认参数即可，监听 127.0.0.1:2881
```

**B. 容器**

```bash
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:1.4.0
```

连接信息默认 `127.0.0.1:2881`，用户 `root`，空密码；可用环境变量覆盖：
`SEEKDB_HOST` / `SEEKDB_PORT` / `SEEKDB_USER` / `SEEKDB_PASSWORD` / `SEEKDB_DB`（默认库名 `stepfun_demo`）。

## 2. 一次性准备资源（锁版本）

```bash
bash scripts/download_assets.sh
```

它会下载并校验：

| 资源 | 版本 | 说明 |
|---|---|---|
| Transformers.js | `@huggingface/transformers@3.7.2` | `dist/transformers.min.js` + ORT-WASM jsep 文件 |
| 模型权重 | `Xenova/all-MiniLM-L6-v2 @ 751bff37…` | `onnx/model_quantized.onnx`（INT8）与 `onnx/model.onnx`（FP32 基线） |

下载后运行时 `env.allowRemoteModels = false`，页面只读本地文件，断网仍可编码新文本。

## 3. 启动应用

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt
uvicorn server.app:app --host 127.0.0.1 --port 8099
```

首次访问 `POST /api/init` 会幂等建表建索引（也可直接用 `db/init.sql`）。

打开 `http://127.0.0.1:8099/`：

1. 选批次（batch 1 / batch 2）与精度（INT8 / FP32），点「加载 / 切换模型」，状态条显示当前后端与加载耗时；
2. 点「载入示例语料」→「导入到 seekdb」，状态条会显示库内 `chunks / batch` 数；
3. 输入查询 → 点「检索」，结果按库内融合分数排序；展开「证明」可看到 `DBMS_HYBRID_SEARCH.GET_SQL` 生成的真实 SQL；
4. 换批次再导入一批 → 刷新页面（或点「刷新索引状态」）→ 新内容可直接检索到。

量化对比与延迟：打开 `http://127.0.0.1:8099/bench.html`，点「开始测量」得到 JSON 报告（可直接粘进技术说明）。

## 4. 环境要求

- Chrome / Edge 最新版（WebGPU 可用时走 GPU，不可用时自动降级 WASM 并在页面顶部提示）
- Python 3.10+
- seekdb 1.4.0（表使用 `VECTOR(384)` 列、`FULLTEXT` 与 `VECTOR HNSW` 索引；**不要声明主键**，`DBMS_HYBRID_SEARCH` 需要库内隐藏列 `__pk_increment`）

## 5. 自检（对应验收点）

| 验收点 | 怎么验证 |
|---|---|
| 完整流程 | 导入 → 写库 → 查询 → 排序结果（主界面即可完成） |
| 本地推理 | 封锁外网后编码仍产出 384 维向量；DevTools Network 只有本机 `127.0.0.1` 请求 |
| 模型量化 | `bench.html` 输出 INT8 / FP32 的体积、加载、编码延迟与 Top-5 一致率 |
| 后端与降级 | 页面顶部状态条显示 WebGPU / WASM；下拉框可强制 `wasm`，自动模式在无 WebGPU 时降级并提示 |
| 库内混合检索 | 检索结果下方展示 `DBMS_HYBRID_SEARCH.GET_SQL` 生成的单条 SQL（两路打分在库内相加） |
| 状态与刷新 | 导入 batch 2 后刷新页面，`/api/stats` 显示两批段数，新内容可检索 |
| 可复现 | 本文第 1–3 步 + `download_assets.sh` 的 sha256 清单 |

## 6. 明确没有做的事

- 不调用任何云端推理 API；模型权重与运行库均来自本地文件；
- 不在服务端做预计算回放：每次检索的向量都来自当前浏览器标签页的实时编码；
- 不在前端拼合两路结果：只有一次 `/api/search` 请求，排序由 seekdb 单条语句产生。
