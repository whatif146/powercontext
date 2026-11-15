# 薄弱点雷达 · WeakRadar

> 基于 PowerContext 记忆的复习教练：只复习你真正不会的那几句。

第六届 OceanBase 数据库大赛 · Qoder 赛题（600078）参赛作品
队伍：星火辽源（单人）· 赛号 AntTech_FG3WBV · 作者：许耀元（华南理工大学 计算机科学与技术 2024 级）

- 项目网站（Qoder Sites，公开可访问）：https://weakradar-py06kl6a46t.qoder.zone
- 代码仓库：https://github.com/whatif146/powercontext/tree/2026-competition-ecosystem/qoder
- Qoder 账号 UID：1614412229476632

## 1. 解决什么问题

大二学生学习资料散落在纸质笔记、Notion、微信群和 PPT 里。复习时的真实困境不是"没资料"，而是：

- 不知道哪些内容是**真不会**，每次从头看，时间花在已经掌握的部分；
- 上周明明卡在"红黑树 case2/case3 旋转判断"，这周打开资料已经忘了自己卡在哪；
- 换设备、换会话后，之前的学习进度和薄弱点全部丢失。

WeakRadar 把「知识点 → 自评 → 下次复习时间」这条链路交给 **PowerContext 记忆**：
每次自评都写进 PowerContext，第二天打开（甚至换一台机器、换一个 Agent）时，系统仍能召回
"你上次在哪些点上打了低分"，并据此生成今日复习清单。

## 2. 核心功能

| 功能 | 说明 |
|---|---|
| 笔记切卡片 | 粘贴一段笔记，按编号 / 分号 / 句号切成知识点卡片，重复内容自动跳过 |
| 今日复习 | 按 SM-2 计算到期卡片，卡片正面只给知识点，翻面才看原句，避免"看着答案觉得会" |
| 0–5 自评 | 六档自评（完全没印象 → 轻松掌握），写回卡片调度状态并追加一条复习日志 |
| 记忆召回 | 关键词召回历史薄弱点（PowerContext `/v1/memory/search`），评审核对记忆是否真的在用 |
| 今日上下文 | 复习前调用 `/v1/context/prepare` 装配当前 Scope 的上下文，证明"记忆被复用" |

## 3. 技术结合：PowerContext 如何真正起作用

调度状态的**唯一事实来源是 PowerContext Memory**，项目自身不建数据库，只做解析与编排。

| 环节 | 调用的 PowerContext 接口 | 作用 |
|---|---|---|
| 建工作区 | `POST /v1/scopes` | 每个科目一个 Scope，幂等键 `weakradar-<科目>` |
| 记卡片 | `POST /v1/memory/remember`（kind=`review_card`） | 卡片文本 `card|subject=…|topic=…|due=…|ease=…|interval=…|reps=…|last=…|note=…` |
| 算今日 | `POST /v1/memory/entries/list` | 列出全部条目，本地按 `card|` 前缀解析出到期卡片 |
| 自评回写 | `POST /v1/memory/entries/revise` | 用上游返回的完整 `citation` 改卡片文本，版本号推进 |
| 复习日志 | `POST /v1/memory/remember`（kind=`review_log`） | 每次自评追加一条 `log|…|score=…|due=…` |
| 记忆召回 | `POST /v1/memory/search` | 关键词召回薄弱点，返回 score / matched_by 供评审核对 |
| 上下文装配 | `POST /v1/context/prepare` | 复习前装配上下文，页面展示装配结果与条目引用 |

可核对点：页面上每张卡都显示 `mem_ent_…` 记忆条目号；`/api/memory/raw` 原样返回 Memory 条目；
自评后 `due` / `ease` / `reps` 的变化在卡片与 Memory 原文里同时可见。

## 4. 目录结构

```
qoder/
├── README.md                  # 本文件
├── RUNNING.md                 # 运行说明（评审从这里开始）
├── SPEC.md                    # 开发前写的接口与调度规格（Qoder 按此实现）
├── .env                       # PowerContext 基础记忆模式配置（SQLite，无模型依赖）
├── server/                    # FastAPI 后端
│   ├── app.py                 # 路由与编排
│   ├── powercontext_client.py # 全部 PowerContext HTTP 调用集中于此
│   ├── cards.py               # 笔记切卡片 / card| 文本编解码
│   └── scheduler.py           # SM-2 调度
├── web/                       # 前端（纯静态，无构建）
├── site/                      # Qoder Sites 发布的展示站（已上线）
└── docs/
    ├── verification.md        # 验证样例（初始数据 / 步骤 / 预期结果）
    ├── qoder-dev-log.md       # Qoder 开发记录（对话与产出、发布记录）
    └── evidence/              # 接口返回值与截图
```

## 5. 快速开始

见 [RUNNING.md](RUNNING.md)。三步：启动 PowerContext 服务 → 启动应用 → 打开 http://127.0.0.1:8080 。

## 6. 复用与来源说明

- 记忆服务使用赛事官方开源项目 PowerContext（Apache-2.0），本项目**未修改其源码**，仅通过其 HTTP API 接入。
- 数据来源：作者本人《数据结构》《计算机组成原理》《概率论》课程笔记片段与错题，无第三方版权内容。
- 本项目代码由 Qoder（CLI 无头模式）生成初版并由作者评审修正，过程见 `docs/qoder-dev-log.md`。
- 展示站 `site/` 里的三张界面截图（`site/img/ui-*.png`）在本地完整交付包中存在；通过 GitHub 连接器提交时二进制文件未入库，线上站点已包含这些图片（https://weakradar-py06kl6a46t.qoder.zone）。
