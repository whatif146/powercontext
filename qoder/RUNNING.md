# 运行说明（评审 10 分钟内可跑通）

## 0. 环境要求

- Python 3.11+（推荐 3.12），Node 不需要（前端是纯静态文件）
- 已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（`uv --version` 有输出即可）
- 端口占用：PowerContext 用 `8000`，本项目用 `8080`

本文命令以 Windows PowerShell 为例；macOS / Linux 把 `C:\...` 换成本地路径、`$env:VAR` 换成 `export VAR=` 即可。

## 1. 安装并启动 PowerContext（记忆服务）

```powershell
uv tool install "powercontext[cli,server]==1.2.0"
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

在本项目目录下创建 `.env`（仓库里已带一份基础记忆版 `.env`，可直接用）：

```powershell
cd <项目目录>/qoder
powercontext server run --env-file .env
```

另开一个终端确认服务就绪：

```powershell
powercontext ready            # 期望: Status: ready
powercontext capabilities      # 期望能看到 memory:enabled
```

> `.env` 使用 SQLite 存储（`POWERCONTEXT_SERVER_DATABASE_KIND=sqlite`），无需 seekdb、无需模型 API；
> 基础记忆能力（显式写入 / 检索 / 列表 / 修订）全部可用。

## 2. 启动本项目

```powershell
cd <项目目录>/qoder
python -m uv run --with fastapi --with uvicorn --with httpx --with pydantic `
  -- python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
```

首次运行会下载 4 个依赖（约 20 秒）。

## 3. 打开并使用

浏览器访问 http://127.0.0.1:8080

1. 顶部确认「PowerContext 已就绪」为绿色，右侧显示 scope id；
2. 在「粘贴笔记导入」里贴入几行笔记（每行一个知识点），点「导入」；
3. 回到「今日复习」，点卡片翻面看原句，按 0–5 自评；
4. 自评后卡片消失、统计里的「今日到期 / 待复习」减少，同时出现绿色提示（含下次复习日期与难度因子）；
5. 在「记忆召回」里搜一个知识点词（例如「红黑树」），能看到命中条目与分数——这就是评审要核对的「记忆实际起作用」。

## 4. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 页面顶部显示「PowerContext 未就绪」 | `8000` 端口没有服务；先执行第 1 步，再点页面「刷新」 |
| `error while attempting to bind ... 10048` | `8080` 已被占用，换 `--port 8081` 或结束占用进程 |
| 提示 `无法连接 PowerContext` | 检查 `.env` 里的 `POWERCONTEXT_SERVER_HTTP_PORT` 是否与 `POWERCONTEXT_SERVER_URL` 一致 |
| 卡片进入「无法解析」计数 | Memory 里存在不符合 `card|...` 格式的旧条目，页面会给出样本，不影响其它卡片 |

## 5. 停止

- 应用：在运行 uvicorn 的终端 `Ctrl+C`
- PowerContext：`Ctrl+C`
- 数据位置：`.env` 中 `POWERCONTEXT_HOME` 未设置时，SQLite 落在 PowerContext 的用户数据目录；
  想重置演示数据，删掉该目录下的 `powercontext.db` 再启动即可。
