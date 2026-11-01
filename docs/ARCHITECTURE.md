# 架构

## 模块

| 模块 | 职责 |
| --- | --- |
| `PriceEntry` / `PricingSection` / `PeakSection` / `PluginSection` | 配置模型：价格条目、价格库设置、峰谷规则、插件开关 |
| `LedgerPlugin` | 插件主体：取数、聚合、计价、缓存、首页卡片、页面 API |
| `webui.json` | 页面声明：`ledger`（声明式）、`prices`（自定义 HTML） |
| `webui/prices.html` | 价格库编辑页（自包含 HTML/CSS/JS，通过 `api.query` / `api.action` 与插件通信） |

## 数据流

```
llm_usage (ModelUsage) ─┐
                        ├─ 窗口过滤 → 按 (provider, model) 聚合 → 别名归一 → 价格库匹配
chat_sessions (ChatSession) ┘                                                 │
                                                                              ▼
                                             峰谷判定（请求时间 + [peak] 规则）→ 逐条重算费用
                                                                              │
                                        ┌───────────────┬───────────────┬─────┴──────┐
                                        ▼               ▼               ▼            ▼
                                     总览           趋势序列        按群明细      按模型明细
                                        │
                                        ▼
                          报告缓存 data_dir/report_cache.json ＋ 首页卡片缓存 card_cache.json
```

## 能力（capabilities）

- `database.get`：只读 `ModelUsage`、`ChatSession`。
- `config.get`：读取宿主机器人昵称等展示信息（可选）。

不声明发送、写库、联网、文件读取类能力。

## 页面 API

| API | 用途 |
| --- | --- |
| `ledger_overview` / `ledger_overview_range` | 总览（7 天 / 指定范围） |
| `ledger_series` | 花费与 Token 趋势（按天分桶） |
| `ledger_groups` | 按群明细 |
| `ledger_models` | 按模型明细 |
| `ledger_prices` | 价格库列表（旧接口，保留兼容） |
| `prices_rows` | 价格库页面行数据（窗口内用过的模型 + 自定义条目） |
| `price_save` / `price_add` / `price_verify` | 保存 / 新增 / 确认价格 |
| `tool_prices` / `tool_price_save` | 工具调用价 |
| `peak_rule` | 当前峰谷规则摘要 |

页面通过 `queries`（只读）与 `actions`（需 `confirmation`）调用上述 API；参数在 `webui.json` 中显式声明，未声明参数会被宿主拒绝。

## 缓存与性能

- 报告缓存：`report_cache.json`，按范围（24h/7d/30d）与口径（全量/仅聊天）分别缓存。
- 命中缓存立即返回；过期时返回旧值并在后台重算；无缓存时返回占位并显示「正在计算」。
- 会话映射缓存 10 分钟；价格库条目缓存 60 秒；价格保存后失效。
- 定时把首页卡片文案写入 `card_cache.json`，注册组件时读取。

## 数据与隐私

- 只读数据库；不改任何业务表。
- 不读宿主安装目录下的配置文件；不联网。
- 价格库、缓存、报告都写在插件自己的数据目录。

## 相关文档

- [价格库设计](PRICING.md)
- [路线图](ROADMAP.md)
