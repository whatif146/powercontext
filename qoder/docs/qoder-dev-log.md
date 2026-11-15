# Qoder 开发记录

Qoder 账号：`aliyun0402031328` · 阿里云账号 ID（UID）：**1614412229476632**
使用形态：**Qoder CN CLI 1.1.88 无头模式（`qodercn -p --permission-mode bypass_permissions`）**
开发时间：2026-11-15（初赛截止前 3 天）
项目目录：`qoder/`（本仓库 `2026-competition-ecosystem` 分支）

## 1. 立项与规格（人做）

作者先把真实痛点与接口契约写成 `SPEC.md`：卡片存储格式、SM-2 规则、8 个 API 路由、
「调度状态唯一事实来源是 PowerContext」这条硬约束，以及「所有上游调用集中在 powercontext_client.py」。

规格先行是刻意的：Qoder 的产出要能被逐条核对，而不是"看起来像那么回事"。

## 2. Qoder 承担的工作（逐条可核对）

| 轮次 | 提示词要点 | Qoder 产出 | 日志 |
|---|---|---|---|
| 第 1 轮 | 阅读 `SPEC.md`，实现 `server/` 全部后端 | `server/app.py`（13.4 KB，9 个路由 + 错误处理器）、`server/powercontext_client.py`（8.6 KB，6 个上游端点封装）、`server/cards.py`（8.5 KB，切卡片与编解码）、`server/scheduler.py`（3.2 KB，SM-2） | `qoder-log-1-server.txt` |
| 第 2 轮 | 实现 `web/` 前端（深色、翻面自评、召回、记忆原文） | `web/index.html`、`web/app.js`（28.3 KB）、`web/style.css`（11.9 KB） | 与第 1 轮同一 CLI 会话，产出直接落盘为 `web/` 三个文件，会话日志未单独留存 |
| 第 3 轮 | 做 Qoder Sites 静态展示站 `site/` | `site/index.html`（16 KB）、`site/style.css`（11.8 KB） | `qoder-log-3-site.txt` |

Qoder 在第 1 轮里做的一件关键事：它不只按 SPEC 抄字段，还**去本地安装的 `powercontext` 包里读真实契约**，
自己发现并写进了实现：

- `POST /v1/scopes` 需要 `title / summary / idempotency_key`，且按幂等键天然幂等，直接 create 即可；
- `entries/list` **没有 kind 过滤**，所以卡片必须按 `card|` 前缀在本地筛；
- `entries/revise` 需要的 `citation` 含完整 `memory_ref{family,artifact_id,revision}`，
  而 SPEC 只传 `entry_id + entry_version_id` → 它自己改成"先 list 拿 citation 再 revise"，并校验版本没过期（不一致返回 409）；
- `remember` 对 `(kind, text)` 完全相同的条目会去重（返回 200 但 `entry: null`）→ 不能当错误，要作为 `duplicates` 正常返回；
- `remember` 的 `text` 有 8192 UTF-8 字节硬上限 → 加了写前校验，超限给出人类可读报错。

这几条正是后来联调没返工的原因（否则每次自评都会 400 / 重复粘贴会 502）。

## 3. 作者评审与修正（也是 Qoder 参与的部分）

| 问题 | 现象 | 处理 |
|---|---|---|
| 卡片翻面文字镜像 | `rotateY(180deg)` + `backface-visibility` 在目标渲染环境把正面文字镜像化 | 改为 2D 显示切换（`.card.is-flipped .card-front{display:none}`），保留"点击翻面"语义；由作者在 Qoder 产出上直接修正 |
| 卡片背面直接糊原始 `card|` 文本 | 背面像调试输出，评审观感差 | 改为只显示原句 + `已存入 PowerContext · mem_ent_…` 一行 |
| 「今日上下文」整段塞原始 JSON | 面板被 3.5 KB 装配原文刷屏 | 改为知识点 chips + 「查看装配原文」折叠按钮（默认收起） |

## 4. 复现 Qoder 开发过程

```powershell
# 安装（一次）
npm install -g @qodercn-ai/qoderclicn
$env:QODERCN_PERSONAL_ACCESS_TOKEN = "<个人访问令牌>"   # 见 qoder.cn/account/integrations

# 第 1 轮：后端
cd qoder
qodercn -p --permission-mode bypass_permissions "阅读 SPEC.md，实现 server/ 目录下全部后端代码：app.py、powercontext_client.py、cards.py、scheduler.py、__init__.py。按 SPEC 的 API 契约实现，完成后用 python -m compileall 自检语法。不要修改 SPEC.md。"

# 第 2 轮：前端
qodercn -p --permission-mode bypass_permissions "实现 web/ 前端（index.html、app.js、style.css）：纯静态、无构建、深色简洁中文界面……"

# 第 3 轮：展示站
qodercn -p --permission-mode bypass_permissions "新建 site/ 目录，做 Qoder Sites 静态展示站……"
```

> 令牌等价于账号凭据，本记录不保存真实值；评审如需核对归属，用页脚 UID `1614412229476632` 在 Qoder 后台比对站点归属与用量。

## 5. 站点归属

- Qoder Sites 站点由同一账号（`aliyun0402031328` / UID `1614412229476632`）创建并发布，可见范围「任何人都可访问」。
- 站点访问统计（Qoder Sites Analytics，24h PV）：`3`（coverage=partial，data_as_of 2026-11-15T03:41:32Z）。

## 6. Qoder Sites 发布记录（可核对）

| 项 | 值 |
|---|---|
| 发布工具 | `qodercn sites prepare_site / publish_site / update_access_policy`（Qoder CN CLI 1.1.88，本地登录态） |
| Project（displayName / slug） | `WeakRadar 薄弱点雷达` / `weakradar` |
| projectId | `01a1dd48-7199-7e88-af91-f7ce2c86204c` |
| siteId | `01a1dd48-719d-7ae9-9a86-4091cb6ff317` |
| 草稿准备 actionId | `8f3c2b1e-4d5a-4c7b-9e21-6a0b1c2d3e4f`（artifactReady=true） |
| 发布 operationId | `01a1dd49-8670-7253-b80f-110c0fa78a82`（state=succeeded，committed=true，readiness=publication_completed） |
| 站点后端 | backend.status=disabled（纯静态部署，符合 Free 版 Sites 能力） |
| 可见范围 | `access_mode=public`，`auth_integration_status=not_required`（未登录浏览器 GET 返回 200） |
| 线上地址 | https://weakradar-py06kl6a46t.qoder.zone |
| 线上截图 | `docs/evidence/site-live.png` |
