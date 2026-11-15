# 验证样例（可按步骤复现）

目标：证明「导入笔记 → 生成卡片 → 自评 → 记忆持久化 → 换会话召回」这条链路真实跑通，
且 PowerContext 在其中承担存储与召回职责。

前置：已按 `RUNNING.md` 启动 PowerContext（8000）与 WeakRadar（8080）。

## 样例 A：从零导入三张卡片

**初始数据**：一个全新科目「数据结构-验证」，无任何卡片。

**输入**（粘贴到「粘贴笔记导入」文本框）：

```
1. 红黑树插入：case2/case3 旋转判断总记混
2. 并查集路径压缩复杂度是 O(alpha(n))，写不严谨
3. 拓扑排序 Kahn 算法入度更新漏边
```

**操作**：粘贴 → 点「导入」→ 切到「今日复习」。

**预期结果**：

- 接口 `POST /api/notes` 返回 `created: 3`，每条卡片带 `entry_id`（形如 `mem_ent_…`）；
- 「卡片总数 3 / 今日到期 3 / 待复习 3」；
- 三张卡正面只显示知识点标题，不带原句；点卡片翻面才显示原句与 0–5 自评按钮。

**核对记忆确实写入**：

```powershell
$body = @{ scope_id = "<页面显示的 scope_id>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/memory/entries/list `
  -ContentType application/json -Body $body |
  Select-Object -ExpandProperty entries |
  Select-Object text
```

期望能看到 3 条以 `card|subject=数据结构-验证|topic=…` 开头的条目。

## 样例 B：自评后调度状态写回记忆

**输入**：样例 A 的第一张卡，翻面后点「5 分 · 轻松掌握」。

**预期结果**：

- 页面绿条提示：`已记录 5 分 · … → 下次 <明天>（间隔 1 天，难度 2.6）`；
- 「今日到期」由 3 变 2，「本次已自评」变 1；
- Memory 里同一条 `card|` 文本的 `due` 变成明天、`ease` 变成 `2.6`、`reps` 变 1；
- 同时新增一条 `review_log|…|score=5|due=<明天>` 条目（自评日志）。

**核对**：再执行一次样例 A 的 `entries/list`，对比 `text` 字段；或打开页面「记忆原文」面板查看 `version` 从 1 变成 2。

对应接口：`POST /api/review`（内部先 `entries/list` 取完整 citation，再 `entries/revise` 改卡片，再 `remember` 追加日志）。

## 样例 C：换会话召回（记忆复用）

**操作**：关闭浏览器（或换一个浏览器/无痕窗口），重新打开 http://127.0.0.1:8080 ；
在「记忆召回」里搜 `红黑树 旋转`。

**预期结果**：

- 返回命中卡片，`score` 与 `matched_by`（如 `["fts"]`）可见；
- 顶部「今日上下文」区显示由 `/v1/context/prepare` 装配的条目（含 citation），说明上下文是从记忆里取回的，不是前端缓存。

**反证**：停掉 8080 后重启应用进程（记忆服务不重启），再次打开页面，卡片与到期日不变——
因为状态不在应用进程里，只在 PowerContext Memory 里。

## 样例 D：上游故障显式暴露（可跳过）

停掉 PowerContext（Ctrl+C）后再点「刷新」。

**预期结果**：页面顶部红条显示 `无法连接 PowerContext（http://127.0.0.1:8000）…`，
接口返回 502 且带 `error.message` 原文——不静默吞错，评审可据此判断是环境问题而非功能问题。

## 截图与现场证据

`docs/evidence/` 下：

| 文件 | 内容 |
|---|---|
| `ui-home.png` | 首页：PowerContext 就绪、scope id、今日上下文、待复习卡片 |
| `ui-flipped.png` | 卡片翻面：原句 + 0–5 自评六档 |
| `ui-after-review.png` | 自评 5 分后：到期数减少、绿条提示新到期日与难度 |
| `memory-entries.json` | `entries/list` 的真实返回（含 `card|` 与 `review_log|` 文本） |
| `context-prepare.json` | `context/prepare` 的真实返回（含 citation 与条目内容） |
| `site-live.png` | Qoder Sites 线上站点首页（公开访问） |
