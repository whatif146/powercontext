# 证据目录说明

| 文件 | 内容 |
|---|---|
| `memory-entries.json` | `POST /v1/memory/entries/list` 的真实返回（含 `card|` 与 `review_log|` 文本、version） |
| `context-prepare.json` | `POST /v1/context/prepare` 的真实返回（status=ready，content_bytes=3510） |
| `ui-home.png` / `ui-flipped.png` / `ui-after-review.png` | 应用界面截图（首页 / 卡面翻面自评 / 自评后状态） |
| `site-live.png` | Qoder Sites 线上站点首屏截图 |
| `app.out.log` / `powercontext-server.out.log` | 运行时日志（应用与记忆服务） |

说明：通过 GitHub 连接器提交时二进制图片未入库；三张界面截图同时存在于本地交付包
`docs/evidence/` 与线上站点（https://weakradar-py06kl6a46t.qoder.zone）中，接口返回
（JSON）与日志（文本）已在仓库内可直接核对。
