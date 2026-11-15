# SPEC：薄弱点雷达（WeakRadar）— 基于 PowerContext 记忆的复习教练

## 一句话

学生粘贴课程笔记/错题，系统切成知识点卡片；每次自评后按 SM-2 计算下次复习时间；
所有卡片与复习记录写进 PowerContext Memory，换一天、换会话打开时仍能召回「你上次卡在哪」。

## 真实痛点

华工大二计科学生：数据结构/计组/概率论三科笔记散在 Notion、纸质本、聊天记录里；
复习时不知道「哪些是真不会」，每次从头看，时间全花在已经会的内容上。

## 技术结合（PowerContext 为必选核心依赖）

- 存储：`POST /v1/scopes` 建工作区（每个学生/科目一个 scope）
- 写记忆：`POST /v1/memory/remember`，kind 取值：
  - `review_card`：一行结构化卡片文本（见下）
  - `review_log`：一次自评记录
  - `preference`：学生偏好（例如「每天最多 20 张卡」）
- 召回：`POST /v1/memory/search`（FTS/混合检索），用于「搜索我卡过的知识点」
- 列表：`POST /v1/memory/entries/list`，用于计算今日到期卡片
- 改记忆：`POST /v1/memory/entries/revise`，自评后更新卡片调度状态
- 上下文装配：`POST /v1/context/prepare`，复习前给出一段「今日上下文」

不引入额外数据库：调度状态的唯一事实来源就是 PowerContext Memory。

## 卡片文本格式（存进 memory 的 text，解析用）

```
card|subject=数据结构|topic=红黑树插入|due=2026-11-16|ease=2.5|interval=3|reps=2|last=4|note=<原句摘要>
```

- `due` 为 ISO 日期；`ease` 为 SM-2 难度因子（1.3–2.8）；`interval` 天；`reps` 次数；`last` 上次评分 0–5
- `note` 不能包含 `|` 与换行，超长截断到 120 字

## SM-2 简化规则

- score >= 4：reps+1；interval = 1（reps=1）/ 6（reps=2）/ round(interval*ease)（reps>2）
- score == 3：interval = max(1, round(interval*0.6))
- score <= 2：reps=0，interval=1，ease = max(1.3, ease-0.2)
- ease 更新：ease += (0.1 - (5-score)*0.08)，并夹在 [1.3, 2.8]
- due = today + interval

## 后端 API（FastAPI，端口 8080）

- `GET /` 静态首页（web/）
- `GET /api/health` → `{ok, powercontext:{ready, scope_id}}`
- `POST /api/scope` `{subject}` → 创建/复用 scope，返回 scope_id；scope 名 `薄弱点雷达 · {subject}`，幂等键 `weakradar-{subject}`
- `POST /api/notes` `{subject, text}` → 切卡片并写入 memory，返回新建卡片列表
- `GET /api/cards?subject=` → 返回全部卡片（解析 memory entries）
- `GET /api/today?subject=` → 到期卡片 + `context` 字段（调用 context/prepare 的摘要文本）
- `POST /api/review` `{subject, entry_id, entry_version_id, score}` → 更新卡片（revise）+ 写一条 review_log，返回新卡片状态
- `POST /api/recall` `{subject, query, limit}` → 透传 memory/search，返回命中条目与分数（评审核对记忆召回用）
- `GET /api/memory/raw?subject=` → 原样返回 memory entries（评审核对存储用）

## 前端（web/，纯静态，无构建）

单页：科目切换、粘贴笔记、今日复习（卡片翻转自评 0–5）、搜索召回、记忆原文面板。
深色简洁风，中文界面，移动端可用。

## 约束

- Python 3.12+，依赖 `fastapi`、`uvicorn`、`httpx`、`pydantic`；用 `uv run --with ...` 启动，不额外建 venv
- 不硬编码 PowerContext 地址，读 `POWERCONTEXT_SERVER_URL`，默认 `http://127.0.0.1:8000`
- 所有对 PowerContext 的调用集中在 `server/powercontext_client.py`
- 失败要显式报错（评审能看懂），不静默吞错
