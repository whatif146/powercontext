# 离谱宇宙 · 接龙冒险（E5 · Dify × PowerContext）

> 赛题：Just for Fun——用 Dify 做一个好玩的智能应用（20 分）
> 技术选型：**Dify（必选）+ PowerContext（至少一种）**。本文档不含 seekdb 用法，原因见文末「为什么不用 seekdb 作 Dify 数据库」。

## 一句话

你说三个完全不搭界的词（电梯、榴莲、前任），AI 把它们写进**同一个宇宙**的一章短篇；这一章的世界观、人物和伏笔会写进 PowerContext，所以**下一轮、甚至换一个会话**再给三个新词，剧情还是接着上一章往下走——不是重新开天辟地。

好玩在「词越离谱越好」，记得住在「越聊越连贯」。

## 目录

```
dify/
├── app.dsl.yml        # Dify Chatflow DSL（version 0.6.0）
├── README.md          # 本文件：运行说明 + 验证样例 + 技术分工
└── screenshots/       # 可选：实机截图（导入成功、跨会话续写、记忆命中）
```

## 运行环境

| 组件 | 版本/要求 | 说明 |
|---|---|---|
| Dify | ≥ 0.6.0（对应 DSL `version: 0.6.0`） | 社区版即可；推荐自部署（见下方「为什么推荐自部署」） |
| PowerContext Server | 官方 CLI 安装的最新版（本次实测 1.5.1） | 默认监听 `http://127.0.0.1:8000`，loopback 安装默认不鉴权 |
| 模型 | 你在 Dify 里已配置的任意 Chat 模型 | DSL 里占位为 `langgenius/openai/openai` + `gpt-4o-mini`，导入后**必须重选**成你自己可用的模型 |
| 浏览器 | Chrome / Edge 最新版 | 评审体验入口就是 Dify 的应用页 |

## 一、启动 PowerContext（已实测，照抄可跑）

> 下列命令在 PowerContext **1.5.1** 上逐条验证过（Linux，Python 3.12）。

1. 装 CLI + Server + 内置存储（三个 extras 缺一不可，只装 base 会缺 `typer`/`fastapi`）：

```bash
uv pip install "powercontext[cli,server,builtin]"
```

2. 起服务（1.5.1 **没有** `powercontext server run` 子命令，用模块入口）：

```bash
python -c "from powercontext.server.cli import app; app(['run'])"
```

若报 `PermissionError: /home/xxx/.local/share/powercontext`，说明默认数据目录不可写，指定一个可写目录：

```bash
POWERCONTEXT_HOME=/tmp/pchome python -c "from powercontext.server.cli import app; app(['run'])"
```

3. 确认就绪：

```bash
curl http://127.0.0.1:8000/health/ready
```

实测返回：`{"status":"ready","checks":{...}}`。日志里出现的「未配置推理模型」提示属正常——本作品**不需要**生成/embedding 模型。

4. **建 scope 拿 scope_id（必须做，不能手写字符串）**：scope_id 是服务端生成的不透明 ID（形如 `scp_68v7s2p2935rfttycdk2qs6fjr`），随手写 `universe:lipu` 是查不到东西的。

```bash
powercontext --server-url http://127.0.0.1:8000 --allow-insecure-http --json \
  scope create --title "离谱宇宙" --summary "E5 接龙冒险的记忆 scope" \
  --idempotency-key "universe-lipu-v1"
```

返回 JSON 里的 `scope_id` 就是要填进环境变量的值。换一个 scope 就等于换一个宇宙（对照实验要用到这一点）。

### 鉴权：默认不要发 Authorization 头

loopback 安装默认关闭鉴权，此时发 `Authorization: Bearer `（token 为空）是**非法 HTTP Header**，请求会直接失败——实测会抛 `Illegal header value`。所以本 DSL 的两个 HTTP 节点**只发 `Content-Type`**。

只有在你显式开启了鉴权时，才需要在两个 HTTP 节点的 `Headers` 里各加一行：

```
Authorization: Bearer <你的 token>
```

### 为什么检索用固定 query「离谱宇宙 宇宙档案」

没配 embedding 模型时，`/v1/memory/search` 走纯文本检索（返回 `"mode": "text"`），查询词必须与记忆正文有词面重叠才会命中。实测「世界观 人物 伏笔 上一章结尾」这类抽象 query **命中 0 条**。因此每章写入时都带固定锚点前缀 `【离谱宇宙·宇宙档案】`，检索固定用 `离谱宇宙 宇宙档案`，实测能取回该 scope 下全部档案，且**最新的一章排在最前**（正好当“上一章结尾”用）。

## 二、导入 DSL（约 5 分钟）

1. Dify → 工作室 → 导入 DSL → 选择 `dify/app.dsl.yml`。
2. 导入后打开应用，检查并补齐三处：
   - **LLM 节点「续写本章」**：重选模型与 provider（用你已配好 key 的模型）。
   - **环境变量**（应用设置 → 环境变量）：
     - `POWERCONTEXT_BASE_URL` = 你的 PowerContext 地址
     - `POWERCONTEXT_SCOPE_ID` = 上一步 `scope create` 返回的 `scp_...`
     - `POWERCONTEXT_TOKEN` = 只有开启鉴权时才用；默认留空，且**不要**因此去加 Authorization 头（见第一节）
   - **两个 HTTP 节点**：`读取宇宙记忆` 与 `写回宇宙记忆`，确认 Header 只有 `Content-Type: application/json`，body 是 raw-text JSON。
3. 点「运行」试一句：`电梯、榴莲、前任`。

> **Dify 版本兼容**：若导入报 schema 错误，不要硬改 `version` 值。做法是本地新建一个空白 Chatflow → 导出 DSL → 用那份骨架，把本文件的 7 个节点按 `data` 内容逐节点粘回去（节点类型与字段名见「节点表」）。

### 为什么推荐自部署

Dify 云端版跑在厂商网络里，**访问不到你机器上的 `127.0.0.1:8000`**。本作品的两个 HTTP 节点都要连 PowerContext，所以运行环境推荐「Dify 与 PowerContext 都在同一台机器/同一内网」。若必须用云端 Dify，需要把 PowerContext 暴露成公网 HTTPS 地址，再改 `POWERCONTEXT_BASE_URL`。

## 三、节点表（7 个节点，全部由 Dify 编排）

| # | 节点 | 类型 | 作用 | 关键字段 |
|---|---|---|---|---|
| 1 | 开始 | start | 收三个词 | `words`（text-input，必填，≤120 字） |
| 2 | 读取宇宙记忆 | http-request | `POST /v1/memory/search` 取回该宇宙最近的设定 | body 为 raw-text JSON：`scope_id / query="离谱宇宙 宇宙档案" / limit=5 / mode=auto` |
| 3 | 整理记忆 | code | 把返回的 JSON 压成纯文本；解析失败/空结果**明确说“没有既有设定”** | 输出 `memory_text`、`has_memory`、`status` |
| 4 | 续写本章 | llm | 三个词 + 既有设定 → 一章 + 「本章新增设定」 | system 规则 + user 模板 |
| 5 | 打包记忆请求体 | code | `json.dumps` 转义正文，避免换行/引号导致 422 | 输出 `chapter_json` |
| 6 | 写回宇宙记忆 | http-request | `POST /v1/memory/remember` 存本章 | body 为 raw-text JSON：`scope_id / kind=story-chapter / text` |
| 7 | 直接回复 | answer | 输出本章 + 引导语 | 引用 `n_llm.text` |

数据流：`开始 → 读取记忆 → 整理 → 续写 → 打包 → 写回 → 回复`。

## 四、验证样例（评审照着点就行）

### 样例 1 · 首轮（冷启动）

- **初始条件**：一个**没有历史记忆**的 scope（新 scope，或先清空该 scope 的记忆）。
- **输入**：`电梯、榴莲、前任`
- **操作步骤**：打开应用 → 粘贴三个词 → 运行。
- **预期表现**：
  1. 返回 300–600 字的一章，三个词都作为情节要素出现；
  2. 正文后有一行「本章新增设定：」，列出 1–3 条（人物/地点/规则/伏笔）；
  3. 文末有「这个宇宙已经记住了这一章……」引导语；
  4. **不会出现**“我记得你之前……”这类无依据的话（首轮不该有记忆）。

### 样例 2 · 同一会话续写

- **初始条件**：样例 1 刚跑完，仍用同一会话、同一 scope。
- **输入**：`泡面、月亮、广场舞`
- **操作步骤**：直接再发一句话（不要新建会话）。
- **预期表现**：沿用样例 1 的人物/地名/伏笔（例如第一章出现过的角色再次出场），结尾悬念承接上一章；「本章新增设定」只写新增的，不重复旧设定。

### 样例 3 · 跨会话续写（这条是技术结合的硬证据）

- **初始条件**：样例 1、2 已跑完；**新建一个会话**（同一个应用、同一 scope）。
- **输入**：`扫地机器人、寺庙、炒股`
- **操作步骤**：新会话里直接发三个词。
- **预期表现**：仍然接得上前两章的世界观（能说出前章人物或伏笔），**证明记忆不在对话窗口里，而在 PowerContext**。

### 样例 4 · 异常（服务不可用，验证“不编造”）

- **初始条件**：把 PowerContext 停掉（或把 `POWERCONTEXT_BASE_URL` 改错）。
- **输入**：`冰箱、风筝、班主任`
- **操作步骤**：照常运行。
- **预期表现**：应用仍返回一章，但按「第一章」处理，明确说明没有既有设定；**不假装记得**。写回记忆会失败，不影响本次回复。

### 对照实验（证明技术真的在起作用，不是“装了个组件”）

同一组三个词（`电梯、榴莲、前任`）跑两次：

| 组 | scope | 观察点 |
|---|---|---|
| A | 已累积 3 章的 scope | 人物/伏笔延续，正文出现前章设定 |
| B | 全新 scope | 每次都当第一章，人物全新 |

两组的差别只能来自 PowerContext；再附一条数据证据：`POST /v1/memory/search` 的返回里能看到本章写入的 `hits`（可截图）。

## 五、技术分工（一页说清）

| 组件 | 承担什么 | 证据 |
|---|---|---|
| **Dify（必选）** | 核心流程编排：输入处理（开始节点）→ 外部检索（HTTP 节点）→ 模型调用（LLM）→ 结果输出（回复节点）；DSL 即流程 | `app.dsl.yml` 的 7 节点 6 边 |
| **PowerContext（至少一种）** | 记忆的保存与复用：每章作为一条 Atomic Memory 写入，下一轮按语义检索回来注入提示词；**跨会话生效** | 样例 3 + 对照实验 |
| seekdb | 未使用 | 见下 |

## 六、为什么不用 seekdb 作 Dify 数据库

官方允许「seekdb 作为自行部署 Dify 时的数据库」，但要求同时提供**适配的 Dify 版本/分支、seekdb 版本、部署配置与读写验证**。本题只要求 seekdb 与 PowerContext **二选一**；把 Dify 的元数据库换成 seekdb 属于「换库 + 出适配证据」的额外工程，收益（本题不加分）与风险（跑不起来就 0 分）不成比例。因此本作品只用 PowerContext，并把 Dify 的编排价值做足。

## 七、已知限制（如实写）

1. **Dify 版本差异**：DSL 按 0.6.0 版式生成；不同版本的节点字段可能微调，导入失败按「空白 Chatflow 骨架 + 粘节点」处理。
2. **模型未锁定**：DSL 里是占位模型，评审需换成自己的 provider；这是官方允许的「按说明配置模型」。
3. **没配 embedding 时只能靠词面命中**：本作品用固定锚点 query 绕开了这个限制；若你的 PowerContext 配了 embedding，`mode: auto` 会自动升级成向量/混合检索，行为不变。
4. **未做** IF/ELSE 首轮/续写分流：当前用「整理记忆」节点统一降级，行为等价且更少导入风险；若想加分可后补（见下）。

## 八、接口实测记录（可当证据用）

在 PowerContext 1.5.1 上按本 DSL 的报文逐条打过：

| 动作 | 报文 | 实测结果 |
|---|---|---|
| 建 scope | `scope create --title ... --summary ... --idempotency-key ...` | 200，返回 `scope_id=scp_...`（不透明 ID） |
| 写记忆 | `POST /v1/memory/remember`，`text` 含多行 + 中文引号 + emoji 级特殊字符 | **200**，`changed:true`，返回 `artifact_id / revision / state:active` |
| 查记忆（抽象 query） | `query="世界观 人物 伏笔 上一章结尾"` | 200，`mode:"text"`，`hits: []`（**词面无交集，命中 0**） |
| 查记忆（锚点 query） | `query="离谱宇宙 宇宙档案"` | 200，4 条档案全返回，**最新的一章排第一** |
| 空 token 鉴权头 | `Authorization: Bearer ` | **失败**：`Illegal header value`（故 DSL 不发该头） |

这些正是“技术真的在起作用”的直接证据，评审时可直接截图 `/docs` 或上面这张表。

## 九、可选的增强（导入成功后再做，不要第一次就改）

加一个 IF/ELSE 节点到「整理记忆 → 续写本章」之间，按 `has_memory` 分流成两个 LLM（「开天辟地·第一章」与「承接前文·续写」），再用 Variable Aggregator 合并回「续写本章」的入参。收益：流程上多一处显式条件判断；代价：多 3 个节点，导入与调试成本上升。**建议先提交当前版本拿到分，再用剩余时间迭代。**
