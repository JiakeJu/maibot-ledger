# 价格库设计

## 存储位置

- 运行期存储在插件数据目录：`data/plugins/<插件 id>/prices.json`。
- 结构：`{"prices": [价格条目...], "tools": [工具单价条目...]}`。
- 配置节 `[pricing].prices` 只是**首次种子**：插件首次加载时若 `prices.json` 不存在，会用它播种；之后以文件为准。

## 价格条目（模型）

| 字段 | 含义 |
| --- | --- |
| provider / model / model_id | 服务商与模型标识；model 作为展示名，model_id 为宿主原始标识 |
| aliases | 别名列表（逗号分隔），用于匹配历史调用记录 |
| currency | 币种（CNY / USD） |
| input_price / cache_input_price / output_price | 常规输入、缓存命中输入、输出单价（元 / 百万 token） |
| peak_input_price / peak_cache_input_price / peak_output_price | 高峰三价；0 表示无峰谷差异 |
| unit / unit_price | 计价单位：`token`（百万 token）或 `call`（按次） |
| source_url / fetched_at / verified | 官方来源、抓取时间、是否已人工确认 |
| note | 备注 |

## 工具单价条目

| 字段 | 含义 |
| --- | --- |
| provider / tool | 服务商与工具名（如 `web_search`、`web_fetch`） |
| price_per_1000 | 单价（元 / 1000 次） |
| currency / source_url / fetched_at / verified | 同上 |

## 官方价目自动适配

- 代码内置 `OFFICIAL_PRICE_CATALOG`（当前含 DeepSeek `deepseek-flash` 与 `deepseek-v4-pro` 两档，含常规价与峰谷价、峰时窗口、官方来源与抓取时间）。
- 生成价格库行时，若某个实际用过的模型命中内置价目：
  - 条目不存在 → 按官方价目新建（`verified=false`，标 `auto_adapted`）；
  - 条目已存在但缺常规价/峰谷价 → 只补空缺字段，不覆盖已有值；
  - 已标记 `auto_adapted` 的条目不会再次改写，用户手工修改/清空后保持用户值。
- 自动适配只会作用于「窗口内实际产生过调用」的模型；没有调用的手动条目不会被改动。
- 新增厂商价目时，只需在 `OFFICIAL_PRICE_CATALOG` 增加一条（provider_keys / aliases / 三价 / 峰三价 / 峰时窗口 / 来源与抓取时间）。

## 匹配规则

1. 用 `(provider, model)` 组合键匹配；provider 做归一（去 `Coding` 等尾缀、小写），模型支持别名与 `vendor/model` 尾段匹配。
2. 未命中任何条目 → 该调用的费用按 0 计，并在页面标「待补价」，不猜测价格。
3. 价格库行由「窗口内实际产生过调用的模型」生成；没有任何条目但有用量的组合也会出现一行待补价。

## 峰谷规则

- 全局节 `[peak]`：`weekdays`（1=周一…7=周日）、`windows`（`09:00-12:00,14:00-18:00`）、`holiday_mode`（`china` 使用内置节假日近似表）、`extra_holidays`。
- 只有 `peak_*` 价格 **大于 0** 才视为「有峰谷价」；否则使用常规价。
- 每个价格条目可单独覆盖峰谷规则：`peak_weekdays`（如 `1-5`）、`peak_windows`（如 `09:00-12:00,14:00-18:00`）、`peak_holiday_mode`（`none` / `china`）。留空=跟随全局；任一非空即对该模型生效。
- 所有时间按北京时间（UTC+8）判断。

## 官方来源清单

| 厂商 | 价格页 |
| --- | --- |
| DeepSeek | https://api-docs.deepseek.com/zh-cn/quick_start/pricing |
| OpenAI | https://openai.com/api/pricing/ |
| Anthropic | https://www.anthropic.com/pricing |
| Google Gemini | https://ai.google.dev/pricing |
| 阿里云百炼 | https://help.aliyun.com/zh/model-studio/models |
| 智谱 | https://open.bigmodel.cn/pricing |
| Moonshot | https://platform.moonshot.cn/docs/pricing/chat |
| 硅基流动 | https://siliconflow.cn/pricing |

价格以官方页面为准，抓取时间记录在条目 `fetched_at`；账号折扣、免费额度无法自动识别，只显示官方标价。

## 时效与人工确认

- 页面提示抓取时间；`verified=false` 表示尚未人工核对。
- 官方调价后需手动更新条目；插件不会自动改价。
