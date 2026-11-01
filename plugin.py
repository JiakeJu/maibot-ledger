"""麦麦账本（Token Ledger）——把宿主的 token 用量换算成可核对的费用。

- 只读宿主 ``llm_usage``（``ModelUsage``）与 ``ChatSession``，不写库、不发消息；
- 按自维护价格库（支持峰谷价、星期与节假日规则）重算每次请求费用；
- 首页卡片给近 7 天摘要，工作区页面给总览、趋势与按群/按模型明细；
- 价格库按实际用过的模型生成，未识别模型不猜价，留给你手动补。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from maibot_sdk import API, Field, HomeCard, MaiBotPlugin, PluginConfigBase

PLUGIN_ID = "github.jiakeju.token-ledger"
USAGE_MODEL = "ModelUsage"
SESSION_MODEL = "ChatSession"
BEIJING = timezone(timedelta(hours=8))

USAGE_LIMIT = 50000
REPORT_CACHE_FILE = "report_cache.json"
SESSION_TTL_SECONDS = 600
RANGE_DAYS = {"24h": 1, "7d": 7, "30d": 30}
CARD_CACHE_FILE = "card_cache.json"

# 中国 2026 年法定节假日（近似维护；插件只用于判峰谷，不涉及结算规则）

# 内置的官方价目（用于「扫描到模型后自动适配」）：只在条目还没有峰谷价、且未标记 auto_adapted 时填充，
# 用户手工改过（或清除）后不会反复覆盖。金额单位：元 / 百万 token。
OFFICIAL_PRICE_CATALOG: List[Dict[str, Any]] = [
    {
        "provider_keys": ("deepseek",),
        "aliases": (
            "deepseek-flash", "deepseek-v4.1", "deepseek-v4-flash", "deepseek-v4.1-flash",
            "deepseek-chat", "deepseek-v3", "deepseek-v3.2",
        ),
        "input_price": 1.0,
        "cache_input_price": 0.02,
        "output_price": 4.0,
        "peak_input_price": 2.0,
        "peak_cache_input_price": 0.04,
        "peak_output_price": 8.0,
        "peak_weekdays": "1-5",
        "peak_windows": "09:00-12:00,14:00-18:00",
        "peak_holiday_mode": "china",
        "source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "fetched_at": "2026-11-01",
    },
    {
        "provider_keys": ("deepseek",),
        "aliases": ("deepseek-v4-pro", "deepseek-pro", "deepseek-reasoner", "deepseek-v4-reasoner"),
        "input_price": 4.5,
        "cache_input_price": 0.15,
        "output_price": 13.5,
        "peak_input_price": 9.0,
        "peak_cache_input_price": 0.3,
        "peak_output_price": 27.0,
        "peak_weekdays": "1-5",
        "peak_windows": "09:00-12:00,14:00-18:00",
        "peak_holiday_mode": "china",
        "source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        "fetched_at": "2026-11-01",
    },
]
CN_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07", "2026-10-08",
}

DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
TOOL_PRICE_DEFAULTS: List[Dict[str, Any]] = []
# 默认不预置任何工具单价：未配置的工具按 0 计，由用户在价格库中按实际服务商填写。


# ────────────────────────────── 通用工具 ──────────────────────────────


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 ORM 对象与 dict 的取值。"""

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_ts(value: Any) -> Optional[float]:
    """把数据库里的 timestamp 统一成 epoch 秒。"""

    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _parse_weekdays(text: str) -> set[int]:
    """解析星期掩码（1=周一…7=周日），支持 ``1-5``、``1,3,5``。"""

    days: set[int] = set()
    for part in str(text or "").split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            left, _, right = token.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                continue
            if start <= end:
                days.update(range(start, end + 1))
            else:
                days.update(range(start, 8))
                days.update(range(1, end + 1))
        else:
            try:
                days.add(int(token))
            except ValueError:
                continue
    return {day for day in days if 1 <= day <= 7}


def _parse_windows(text: str) -> List[Tuple[int, int, int, int]]:
    """解析每日时段，例如 ``09:00-12:00,14:00-18:00``。"""

    windows: List[Tuple[int, int, int, int]] = []
    for part in str(text or "").split(","):
        token = part.strip()
        if "-" not in token:
            continue
        left, _, right = token.partition("-")
        try:
            sh, sm = (int(item) for item in left.strip().split(":"))
            eh, em = (int(item) for item in right.strip().split(":"))
        except ValueError:
            continue
        windows.append((sh, sm, eh, em))
    return windows


def _split_aliases(text: str) -> List[str]:
    return [item.strip().lower() for item in str(text or "").split(",") if item.strip()]


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _round_ratio(value: float) -> float:
    return round(float(value) * 100, 1)


def _is_peak(peak: Any, ts: float, entry: Any = None) -> bool:
    """判断某一时刻是否处于高价时段。

    价格条目上的「峰谷星期 / 峰谷时段 / 节假日模式」非空时优先于全局「峰谷时段」设置，
    便于不同厂商的模型各自使用不同峰谷规则；留空（默认）表示跟随全局。
    """

    dt = datetime.fromtimestamp(ts, BEIJING)
    today = dt.strftime("%Y-%m-%d")
    entry_weekdays = str(getattr(entry, "peak_weekdays", "") or "").strip() if entry is not None else ""
    entry_windows = str(getattr(entry, "peak_windows", "") or "").strip() if entry is not None else ""
    entry_holiday = str(getattr(entry, "peak_holiday_mode", "") or "").strip() if entry is not None else ""
    extra: set[str] = set()
    if peak is not None:
        extra = {item.strip() for item in str(getattr(peak, "extra_holidays", "") or "").split(",") if item.strip()}
        weekdays_text = entry_weekdays or str(getattr(peak, "weekdays", "") or "")
        windows_text = entry_windows or str(getattr(peak, "windows", "") or "")
        holiday_mode = entry_holiday or str(getattr(peak, "holiday_mode", "china") or "china")
    else:
        weekdays_text = entry_weekdays
        windows_text = entry_windows
        holiday_mode = entry_holiday or "china"
    if holiday_mode == "china" and (today in CN_HOLIDAYS_2026 or today in extra):
        return False
    weekdays = _parse_weekdays(weekdays_text)
    if weekdays and dt.isoweekday() not in weekdays:
        return False
    minutes = dt.hour * 60 + dt.minute
    for start_h, start_m, end_h, end_m in _parse_windows(windows_text):
        if start_h * 60 + start_m <= minutes < end_h * 60 + end_m:
            return True
    return False


def _default_prices() -> List["PriceEntry"]:
    """内置价格库：官方 CNY 价（2026-11-01 抓取），其余模型请自行补录。"""

    return [
        PriceEntry(
            provider="deepseek",
            model="deepseek-flash",
            aliases="deepseek-v4.1,deepseek-v4-flash,deepseek-v4.1-flash",
            currency="CNY",
            input_price=1.0,
            cache_input_price=0.02,
            output_price=4.0,
            peak_input_price=2.0,
            peak_cache_input_price=0.04,
            peak_output_price=8.0,
            source_url=DEEPSEEK_SOURCE,
            fetched_at="2026-11-01",
            verified=True,
        ),
        PriceEntry(
            provider="deepseek",
            model="deepseek-v4-pro",
            aliases="deepseek-pro",
            currency="CNY",
            input_price=4.5,
            cache_input_price=0.15,
            output_price=13.5,
            peak_input_price=9.0,
            peak_cache_input_price=0.30,
            peak_output_price=27.0,
            source_url=DEEPSEEK_SOURCE,
            fetched_at="2026-11-01",
            verified=True,
        ),
        PriceEntry(
            provider="zhipuai",
            model="glm-4.6v-flash",
            aliases="glm-4.6v-flash,glm-4v-flash",
            currency="CNY",
            input_price=0.0,
            cache_input_price=0.0,
            output_price=0.0,
            source_url="https://open.bigmodel.cn/pricing",
            fetched_at="2026-11-01",
            verified=True,
        ),
        PriceEntry(
            provider="siliconflow",
            model="bge-m3",
            aliases="baai/bge-m3",
            currency="CNY",
            input_price=0.0,
            cache_input_price=0.0,
            output_price=0.0,
            source_url="https://siliconflow.cn/pricing",
            fetched_at="2026-11-01",
            verified=True,
        ),
        PriceEntry(
            provider="siliconflow",
            model="qwen3-vl-embedding-8b",
            aliases="qwen/qwen3-vl-embedding-8b",
            currency="CNY",
            input_price=0.0,
            cache_input_price=0.0,
            output_price=0.0,
            source_url="https://siliconflow.cn/pricing",
            fetched_at="2026-11-01",
            verified=True,
        ),
    ]


# ────────────────────────────── 配置模型 ──────────────────────────────


class PluginSection(PluginConfigBase):
    """插件基础设置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "puzzle"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用账本插件（默认 true；关闭后页面不刷新、首页卡片不更新）。",
    )
    config_version: str = Field(
        default="0.1.0",
        description="配置结构版本（默认 0.1.0；插件升级时由插件自行迁移，一般无需手动修改）。",
    )


class PeakSection(PluginConfigBase):
    """峰谷时段（全局共享，独立面板，避免每个价格条目重复填写）。"""

    __ui_label__ = "峰谷时段"
    __ui_icon__ = "clock"
    __ui_order__ = 2

    weekdays: str = Field(
        default="1-5",
        description="高峰适用的星期（1=周一…7=周日；默认 1-5，支持 1,3,5；留空=每天都是高峰规则）。",
    )
    windows: str = Field(
        default="09:00-12:00,14:00-18:00",
        description="高峰时段（北京时间，默认 09:00-12:00,14:00-18:00；留空=不区分峰谷，全按空闲价）。",
    )
    holiday_mode: Literal["none", "china"] = Field(
        default="china",
        description="法定节假日规则（china=中国法定节假日不算高峰；none=不区分节假日）。",
    )
    extra_holidays: str = Field(
        default="",
        description="额外节假日（YYYY-MM-DD，逗号分隔，默认空；用于补录调休或临时放假）。",
    )


class PriceEntry(PluginConfigBase):
    """价格库中的一条模型价格。"""

    __ui_label__ = "价格条目"

    provider: str = Field(
        default="deepseek",
        description="厂商标识（对应宿主模型配置里的 API 提供方，如 deepseek / siliconflow / zhipuai；留空表示通用）。",
    )
    model: str = Field(
        default="deepseek-flash",
        description="模型标识（宿主 model_config 的 identifier，或 llm_usage 里 model_assign_name 的值）。",
    )
    aliases: str = Field(
        default="",
        description="模型别名，逗号分隔（例如 deepseek-v4.1,deepseek-v4-flash；用于匹配历史模型名）。",
    )
    unit: Literal["token", "call"] = Field(
        default="token",
        description="计价单位：token=按 token 量计费（元/百万 token）；call=按调用次数计费（元/次）。",
    )
    unit_price: float = Field(
        default=0.0,
        description="unit=call 时使用的单价（元/次，默认 0）。unit=token 时忽略。",
    )
    currency: Literal["CNY", "USD"] = Field(
        default="CNY",
        description="价格币种（CNY=人民币，USD=美元；USD 会按账本设置的汇率折算）。",
    )
    input_price: float = Field(
        default=0.0,
        description="空闲时段：缓存未命中的输入价（元 / 百万 token，默认 0）。",
    )
    cache_input_price: float = Field(
        default=0.0,
        description="空闲时段：缓存命中的输入价（元 / 百万 token，默认 0）。",
    )
    output_price: float = Field(
        default=0.0,
        description="空闲时段：输出价（元 / 百万 token，默认 0）。",
    )
    peak_input_price: float = Field(
        default=-1.0,
        description="高峰时段：缓存未命中的输入价（默认 -1 = 沿用空闲价；无峰谷价的模型保持 -1）。",
    )
    peak_cache_input_price: float = Field(
        default=-1.0,
        description="高峰时段：缓存命中的输入价（默认 -1 = 沿用空闲价）。",
    )
    peak_output_price: float = Field(
        default=-1.0,
        description="高峰时段：输出价（默认 -1 = 沿用空闲价）。",
    )
    peak_weekdays: str = Field(
        default="",
        description="单个模型的高峰星期（默认空=跟随「峰谷时段」全局设置；格式如 1-5，留空表示跟随）。",
    )
    peak_windows: str = Field(
        default="",
        description="单个模型的高峰时段（北京时间，默认空=跟随全局；如 09:00-12:00,14:00-18:00）。",
    )
    peak_holiday_mode: str = Field(
        default="",
        description="单个模型的节假日规则（默认空=跟随全局；none=不区分节假日；china=中国法定节假日不算高峰）。",
    )
    source_url: str = Field(
        default="",
        description="官方价目来源链接（用于核对与展示，默认空）。",
    )
    fetched_at: str = Field(
        default="",
        description="价格抓取日期（YYYY-MM-DD，用于展示数据年龄，默认空）。",
    )
    verified: bool = Field(
        default=False,
        description="是否已人工确认（默认 false；确认后不再提示待核对）。",
    )


class PricingSection(PluginConfigBase):
    """账本与价格库设置。"""

    __ui_label__ = "账本"
    __ui_icon__ = "database"
    __ui_order__ = 1

    pricing_source: Literal["config", "page"] = Field(
        default="config",
        description="价格来源：config=使用下方 prices 列表（1A 方案，推荐）；page=改用「价格库」页面编辑器写入的数据（1B 方案，预留）。",
    )
    usd_cny_rate: float = Field(
        default=7.2,
        description="美元折算人民币汇率（默认 7.2；仅对 currency=USD 的价格生效）。",
    )
    include_background: bool = Field(
        default=True,
        description="总览默认口径：是否把无会话的后台请求也计入（默认 true=全量，更贴近 API 平台账单；false=只看聊天流）。",
    )
    tool_fee_per_1000: float = Field(
        default=0.0,
        description="未单独配置时的兜底单价（元/1000 次，默认 0=不估算）。通常保持 0，改在「价格库 → 工具调用价」里按实际服务商逐项填写。",
    )
    usage_window_days: int = Field(
        default=30,
        description="价格库只收录最近多少天内实际跑过 token 的模型（默认 30；范围 1–365；0=不限时间）。",
    )
    exclude_test_tasks: bool = Field(
        default=True,
        description="是否排除 WebUI「模型测试」调用（task_name=webui_model_test，默认上；排除后测试模型不会占用价格库）。",
    )
    refresh_seconds: int = Field(
        default=300,
        description="后台重算间隔（秒，默认 300；范围 30–3600）。页面会先显示上次结果，到间隔后在后台重算，不阻塞页面。",
    )
    prices: List[PriceEntry] = Field(
        default_factory=_default_prices,
        description="价格库（对象数组）：每行一个模型，可增删改；输入/缓存/输出价按「元/百万 token」填写，峰谷价用 peak_* 字段覆盖。",
    )


class LedgerPluginConfig(PluginConfigBase):
    """插件配置根。"""

    plugin: PluginSection = Field(default_factory=PluginSection, description="插件基础设置")
    pricing: PricingSection = Field(default_factory=PricingSection, description="账本与价格库")
    peak: PeakSection = Field(default_factory=PeakSection, description="峰谷时段（所有价格条目共用）")


# ────────────────────────────── 插件实现 ──────────────────────────────


class LedgerPlugin(MaiBotPlugin):
    """麦麦账本：只读 llm_usage，按价格库重算费用。"""

    config_model = LedgerPluginConfig
    config: LedgerPluginConfig

    @classmethod
    def build_config_schema(cls, **kwargs: Any) -> Dict[str, Any]:
        """生成配置 schema 时把 description 回填为 WebUI 输入框下方的说明。"""

        schema = super().build_config_schema(**kwargs)
        sections = schema.get("sections") if isinstance(schema, dict) else None
        if isinstance(sections, dict):
            for section in sections.values():
                fields = section.get("fields") if isinstance(section, dict) else None
                if not isinstance(fields, dict):
                    continue
                for field in fields.values():
                    if isinstance(field, dict) and not field.get("hint") and field.get("description"):
                        field["hint"] = field["description"]
        return schema

    # ── 首页卡片 ──

    @HomeCard(
        "ledger_card",
        title="麦麦账本",
        content="统计中…",
        description="近 7 天 token 用量与费用（只读）",
        link_url="/statistics",
        link_label="查看详细统计",
        icon="chart",
        width="large",
        order=50,
    )
    async def ledger_card(self, **kwargs: Any) -> Dict[str, Any]:
        """首页卡片占位处理器；卡片内容由 ``get_components`` 动态填充。"""

        return {"success": True}

    def get_components(self) -> List[Dict[str, Any]]:
        """注册组件时把缓存里的数据块填进首页卡片。"""

        components = super().get_components()
        blocks = self._load_card_blocks()
        if blocks:
            for component in components:
                metadata = component.get("metadata") or {}
                if str(component.get("type", "")).upper() == "HOME_CARD":
                    metadata["content"] = blocks
        return components

    def _card_cache_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / CARD_CACHE_FILE

    def _load_card_blocks(self) -> List[Dict[str, Any]]:
        try:
            path = self._card_cache_path()
            if not path.exists():
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
            blocks = data.get("blocks")
            return blocks if isinstance(blocks, list) else []
        except Exception:  # noqa: BLE001
            return []

    async def _write_card_cache(self) -> None:
        """按近 7 天数据生成首页卡片内容并写入缓存文件。"""

        try:
            report = await self._report(7)
        except Exception:  # noqa: BLE001
            return
        overview = report.get("overview") or {}
        groups = report.get("groups") or []

        def money(value: Any) -> str:
            try:
                return f"¥{float(value or 0):,.2f}"
            except (TypeError, ValueError):
                return "¥0.00"

        def wan(value: Any) -> str:
            try:
                return f"{float(value or 0) / 10000:,.1f} 万"
            except (TypeError, ValueError):
                return "0.0 万"

        cache_value = float(overview.get("cache_hit_ratio") or 0)
        top = "、".join(f"{g.get('group')} {money(g.get('cost'))}" for g in groups[:3])
        lines = [
            f"**近 7 天** {money(overview.get('cost'))} · {wan(overview.get('tokens'))} tokens · {int(overview.get('requests') or 0)} 次",
            f"缓存命中 {cache_value:.0f}% · 高峰 {float(overview.get('peak_ratio') or 0):.0f}% · {int(overview.get('groups_count') or 0)} 个群",
            f"**花费 Top 群** {top}" if top else "",
            str(overview.get("unknown_text") or ""),
        ]
        blocks = [{"type": "markdown", "content": "  \n".join(line for line in lines if line)}]
        try:
            path = self._card_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"blocks": blocks, "updated_at": datetime.now().isoformat(timespec="seconds")},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def _pricing(self) -> PricingSection:
        config = getattr(self, "config", None)
        pricing = getattr(config, "pricing", None)
        if isinstance(pricing, PricingSection):
            return pricing
        return PricingSection()

    def _peak(self) -> PeakSection:
        config = getattr(self, "config", None)
        peak = getattr(config, "peak", None)
        if isinstance(peak, PeakSection):
            return peak
        return PeakSection()

    # ── 价格库存储（页面编辑器写入 data_dir/prices.json；配置数组作初始种子）──

    def _prices_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "prices.json"

    @staticmethod
    def _entry_to_dict(entry: PriceEntry) -> Dict[str, Any]:
        return {
            "provider": str(entry.provider or ""),
            "model": str(entry.model or ""),
            "aliases": str(entry.aliases or ""),
            "unit": str(getattr(entry, "unit", "token") or "token"),
            "unit_price": float(getattr(entry, "unit_price", 0.0) or 0.0),
            "currency": str(entry.currency or "CNY"),
            "input_price": float(entry.input_price or 0.0),
            "cache_input_price": float(entry.cache_input_price or 0.0),
            "output_price": float(entry.output_price or 0.0),
            "peak_input_price": float(entry.peak_input_price if entry.peak_input_price >= 0 else -1.0),
            "peak_cache_input_price": float(
                entry.peak_cache_input_price if entry.peak_cache_input_price >= 0 else -1.0
            ),
            "peak_output_price": float(entry.peak_output_price if entry.peak_output_price >= 0 else -1.0),
            "peak_weekdays": str(getattr(entry, "peak_weekdays", "") or ""),
            "peak_windows": str(getattr(entry, "peak_windows", "") or ""),
            "peak_holiday_mode": str(getattr(entry, "peak_holiday_mode", "") or ""),
            "source_url": str(entry.source_url or ""),
            "fetched_at": str(entry.fetched_at or ""),
            "verified": bool(entry.verified),
        }

    @staticmethod
    def _entry_from_dict(data: Dict[str, Any]) -> Optional[PriceEntry]:
        try:
            model = str(data.get("model") or "").strip()
            if not model:
                return None
            return PriceEntry(
                provider=str(data.get("provider") or ""),
                model=model,
                aliases=str(data.get("aliases") or ""),
                unit=str(data.get("unit") or "token"),
                unit_price=float(data.get("unit_price") or 0.0),
                currency=str(data.get("currency") or "CNY"),
                input_price=float(data.get("input_price") or 0.0),
                cache_input_price=float(data.get("cache_input_price") or 0.0),
                output_price=float(data.get("output_price") or 0.0),
                peak_input_price=float(data.get("peak_input_price", -1.0)),
                peak_cache_input_price=float(data.get("peak_cache_input_price", -1.0)),
                peak_output_price=float(data.get("peak_output_price", -1.0)),
                peak_weekdays=str(data.get("peak_weekdays") or ""),
                peak_windows=str(data.get("peak_windows") or ""),
                peak_holiday_mode=str(data.get("peak_holiday_mode") or ""),
                source_url=str(data.get("source_url") or ""),
                fetched_at=str(data.get("fetched_at") or ""),
                verified=bool(data.get("verified", False)),
            )
        except Exception:  # noqa: BLE001
            return None

    def _load_price_store(self) -> List[Dict[str, Any]]:
        path = self._prices_path()
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw.get("prices") if isinstance(raw, dict) else raw
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        except Exception:  # noqa: BLE001
            pass
        return [self._entry_to_dict(entry) for entry in (self._pricing().prices or [])]

    def _save_price_store(self, items: List[Dict[str, Any]]) -> None:
        path = self._prices_path()
        existing: Dict[str, Any] = {}
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = raw
        except Exception:  # noqa: BLE001
            existing = {}
        existing["prices"] = items
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_tool_store(self) -> List[Dict[str, Any]]:
        path = self._prices_path()
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw.get("tools") if isinstance(raw, dict) else None
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        except Exception:  # noqa: BLE001
            pass
        return [dict(item) for item in TOOL_PRICE_DEFAULTS]

    def _save_tool_store(self, items: List[Dict[str, Any]]) -> None:
        path = self._prices_path()
        existing: Dict[str, Any] = {}
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = raw
        except Exception:  # noqa: BLE001
            existing = {}
        existing.pop("tools", None)
        existing["tools"] = items
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_tool_store(self) -> List[Dict[str, Any]]:
        items = self._load_tool_store()
        path = self._prices_path()
        try:
            has = False
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                has = isinstance(raw, dict) and isinstance(raw.get("tools"), list)
            if not has:
                self._save_tool_store(items)
        except Exception:  # noqa: BLE001
            pass
        return items

    def _tool_price_map(self) -> Dict[str, float]:
        pricing = self._pricing()
        fallback = max(0.0, float(getattr(pricing, "tool_fee_per_1000", 0.0) or 0.0))
        rate = float(getattr(pricing, "usd_cny_rate", 7.2) or 7.2)
        mapping: Dict[str, float] = {}
        for item in self._load_tool_store():
            name = str(item.get("tool") or "").strip().lower()
            if not name:
                continue
            try:
                value = max(0.0, float(item.get("price_per_1000") or 0.0))
            except (TypeError, ValueError):
                continue
            if str(item.get("currency") or "CNY").upper() == "USD":
                value *= rate
            mapping[name] = value
        if fallback > 0:
            for name in ("web_search", "web_fetch"):
                mapping.setdefault(name, fallback)
        return mapping

    def _tool_cost_total(
        self, tools: Optional[List[Dict[str, Any]]]
    ) -> Tuple[int, float, Dict[str, int], int]:
        """按工具名单价计算工具费，返回（调用次数、费用、按工具拆解、未定价次数）。"""
        price_map = self._tool_price_map()
        total_calls = 0
        total_cost = 0.0
        unpriced = 0
        breakdown: Dict[str, int] = {}
        for row in tools or []:
            name = str(row.get("name") or "").strip().lower()
            count = int(row.get("count") or 0)
            if not name or count <= 0 or not name.startswith("web_"):
                continue
            total_calls += count
            price = price_map.get(name)
            if price is None or price <= 0:
                unpriced += count
                continue
            breakdown[name] = breakdown.get(name, 0) + count
            total_cost += count * price / 1000.0
        return total_calls, total_cost, breakdown, unpriced

    def _ensure_price_store(self) -> List[Dict[str, Any]]:
        path = self._prices_path()
        items = self._load_price_store()
        if not path.is_file():
            try:
                self._save_price_store(items)
            except Exception:  # noqa: BLE001
                pass
        return items

    def _effective_entries(self) -> List[PriceEntry]:
        cache = getattr(self, "_entries_cache", None)
        now = time.time()
        if isinstance(cache, tuple) and now - cache[0] < 60:
            return cache[1]
        entries: List[PriceEntry] = []
        for item in self._load_price_store():
            entry = self._entry_from_dict(item)
            if entry is not None:
                entries.append(entry)
        if not entries:
            entries = [entry for entry in (self._pricing().prices or []) if entry.model]
        self._entries_cache = (now, entries)
        return entries

    def _invalidate_entries(self) -> None:
        self._entries_cache = None
        self._price_memo = {}

    @staticmethod
    def _store_key(provider: str, model: str) -> str:
        return f"{str(provider or '').strip().lower()}::{str(model or '').strip().lower()}"

    def _peak_rule_text(self) -> str:
        peak = self._peak()
        return (
            f"高峰：星期 {peak.weekdays}，时段 {peak.windows}（北京时间）；"
            f"节假日模式 {peak.holiday_mode}，额外节假日 {peak.extra_holidays or '无'}。"
            "（下方每个模型卡片可单独覆盖峰谷时段，留空=跟随这里。）"
        )

    # ── 数据访问 ──

    async def _query(self, model_name: str, limit: int, order_by: Optional[List[str]] = None) -> List[Any]:
        order = list(order_by or ["-timestamp"])
        try:
            rows = await self.ctx.call_capability(
                "database.get",
                model_name=model_name,
                filters={},
                order_by=order,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_query_error = f"{model_name}: {exc!r}"
            return []
        return rows if isinstance(rows, list) else []

    async def _load_usage(self, days: int) -> List[Dict[str, Any]]:
        cutoff = time.time() - days * 86400
        rows = await self._query(USAGE_MODEL, limit=USAGE_LIMIT)
        usage: List[Dict[str, Any]] = []
        for row in rows:
            ts = _to_ts(_get(row, "timestamp"))
            if ts is None or ts < cutoff:
                continue
            usage.append(
                {
                    "ts": ts,
                    "session_id": str(_get(row, "session_id") or "").strip(),
                    "model": str(_get(row, "model_assign_name") or _get(row, "model_name") or "").strip(),
                    "model_id": str(_get(row, "model_name") or "").strip(),
                    "provider": str(_get(row, "model_api_provider_name") or "").strip(),
                    "task": str(_get(row, "task_name") or "").strip(),
                    "cost": float(_get(row, "cost") or 0.0),
                    "prompt": int(_get(row, "prompt_tokens") or 0),
                    "hit": int(_get(row, "prompt_cache_hit_tokens") or 0),
                    "miss": int(_get(row, "prompt_cache_miss_tokens") or 0),
                    "out": int(_get(row, "completion_tokens") or 0),
                    "total": int(_get(row, "total_tokens") or 0),
                }
            )
        return usage

    async def _load_tools(self, days: int) -> List[Dict[str, Any]]:
        """按小时聚合表统计工具调用次数（ToolRecord 行含大字段，整表查询会超 RPC 帧上限）。"""
        cutoff = time.time() - days * 86400
        rows = await self._query("StatisticsToolHourly", limit=5000, order_by=["-bucket_time"])
        tools: List[Dict[str, Any]] = []
        for row in rows:
            ts = _to_ts(_get(row, "bucket_time"))
            if ts is None or ts < cutoff:
                continue
            name = str(_get(row, "tool_name") or "").strip()
            try:
                count = max(0, int(float(_get(row, "call_count") or 0)))
            except (TypeError, ValueError):
                count = 0
            tools.append({"ts": ts, "name": name, "count": count})
        return tools

    async def _session_map(self) -> Dict[str, Dict[str, str]]:
        cache = getattr(self, "_sessions_cache", None)
        if isinstance(cache, tuple) and time.time() - cache[0] < SESSION_TTL_SECONDS:
            return cache[1]
        rows = await self._query(SESSION_MODEL, limit=5000, order_by=["-last_active_timestamp"])
        mapping: Dict[str, Dict[str, str]] = {}
        for row in rows:
            session_id = str(_get(row, "session_id") or "").strip()
            if not session_id:
                continue
            group_name = str(_get(row, "group_name") or "").strip()
            group_id = str(_get(row, "group_id") or "").strip()
            scope = str(_get(row, "scope") or "").strip()
            label = group_name or (f"群 {group_id}" if group_id else f"私聊 {session_id[:8]}" if scope == "private" else "私聊")
            mapping[session_id] = {"group": label, "scope": scope, "group_id": group_id}
        self._sessions_cache = (time.time(), mapping)
        return mapping

    # ── 价格计算 ──

    def _resolve_entry(self, model: str, provider: str) -> Optional[PriceEntry]:
        memo = getattr(self, "_price_memo", None)
        if not isinstance(memo, dict):
            memo = {}
            self._price_memo = memo
        memo_key = ((model or "").strip().lower(), (provider or "").strip().lower())
        if memo_key in memo:
            return memo[memo_key]
        result = self._resolve_entry_uncached(model, provider)
        memo[memo_key] = result
        return result

    def _resolve_entry_uncached(self, model: str, provider: str) -> Optional[PriceEntry]:
        entries = [entry for entry in self._effective_entries() if str(entry.model or "").strip()]
        if not entries:
            return None
        model_key = (model or "").strip().lower()
        if not model_key:
            return None
        tail = model_key.split("/")[-1]
        provider_key = (provider or "").strip().lower()
        candidates: List[PriceEntry] = []
        for entry in entries:
            keys = [str(entry.model).strip().lower()] + _split_aliases(entry.aliases)
            if model_key in keys or tail in keys:
                candidates.append(entry)
                continue
            for key in keys:
                if key and (key in model_key or key in tail):
                    candidates.append(entry)
                    break
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        for entry in candidates:
            entry_provider = str(entry.provider or "").strip().lower()
            if entry_provider and (entry_provider in provider_key or provider_key in entry_provider):
                return entry
        return candidates[0]

    def _unit_price(self, entry: PriceEntry, peak: bool, field: str) -> float:
        ledger = self._pricing()
        rate = float(getattr(ledger, "usd_cny_rate", 7.2) or 7.2)
        base = float(getattr(entry, field, 0.0) or 0.0)
        peak_field = f"peak_{field}"
        peak_value = float(getattr(entry, peak_field, -1.0) if hasattr(entry, peak_field) else -1.0)
        value = base
        if peak and peak_value > 0:
            value = peak_value
        if str(entry.currency or "CNY").upper() == "USD":
            value *= rate
        return value

    def _recompute(
        self,
        entry: PriceEntry,
        ts: float,
        hit: int,
        miss: int,
        out: int,
        peak: bool = False,
        requests: int = 1,
    ) -> float:
        unit = str(getattr(entry, "unit", "token") or "token").strip().lower()
        if unit == "call":
            ledger = self._pricing()
            rate = float(getattr(ledger, "usd_cny_rate", 7.2) or 7.2)
            price = float(getattr(entry, "unit_price", 0.0) or 0.0)
            if str(entry.currency or "CNY").upper() == "USD":
                price *= rate
            return price * max(0, int(requests or 0))
        prompt = hit + miss
        total = (
            (self._unit_price(entry, peak, "input_price") * miss)
            + (self._unit_price(entry, peak, "cache_input_price") * hit)
            + (self._unit_price(entry, peak, "output_price") * out)
        )
        return total / 1_000_000.0

    def _build_report(
        self,
        days: int,
        usage: List[Dict[str, Any]],
        sessions: Dict[str, Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        scope: str = "",
    ) -> Dict[str, Any]:
        pricing = self._pricing()
        peak_cfg = self._peak()
        include_background = bool(getattr(pricing, "include_background", True))
        scope = (scope or "").strip().lower()
        if scope not in ("all", "chat"):
            scope = "all" if include_background else "chat"

        overview = {
            "cost": 0.0,
            "requests": 0,
            "tokens": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "cache_hit_ratio": 0.0,
            "peak_ratio": 0.0,
            "groups_count": 0,
            "unknown_count": 0,
            "unknown_text": "全部已识别",
            "scope": scope,
            "chat_cost": 0.0,
            "chat_requests": 0,
            "chat_tokens": 0,
            "background_cost": 0.0,
            "background_requests": 0,
            "background_tokens": 0,
            "tool_requests": 0,
            "tool_cost": 0.0,
            "tool_note": "",
        }
        hit_total = 0
        prompt_total = 0
        peak_requests = 0
        priced_requests = 0
        unknown_models: Dict[str, int] = {}
        series: Dict[str, Dict[str, Any]] = {}
        groups: Dict[str, Dict[str, Any]] = {}
        models: Dict[str, Dict[str, Any]] = {}

        for row in usage:
            ts = float(row["ts"])
            if scope == "chat" and not row["session_id"]:
                continue
            session = sessions.get(row["session_id"]) or {}
            group_label = session.get("group") or ("后台（无会话）" if not row["session_id"] else f"会话 {row['session_id'][:8]}")
            model_key = row["model"] or "(未知模型)"
            entry = self._resolve_entry(row["model"], row["provider"]) if row["model"] else None

            hit = int(row["hit"])
            miss = int(row["miss"])
            prompt = int(row["prompt"])
            if not hit and not miss:
                miss = max(0, prompt)
            if hit and not miss and prompt > hit:
                miss = prompt - hit
            out = int(row["out"])
            total = int(row["total"] or 0) or (hit + miss + out)

            cost: Optional[float] = None
            peak = False
            if entry is not None:
                peak = _is_peak(peak_cfg, ts, entry)
                cost = self._recompute(entry, ts, hit, miss, out, peak, requests=1)
                priced_requests += 1
                if peak:
                    peak_requests += 1
            else:
                unknown_models[model_key] = unknown_models.get(model_key, 0) + 1

            overview["cost"] += cost or 0.0
            overview["requests"] += 1
            overview["tokens"] += total
            overview["prompt_tokens"] += max(0, prompt)
            overview["output_tokens"] += max(0, out)
            hit_total += max(0, hit)
            prompt_total += max(0, prompt)
            if row["session_id"]:
                overview["chat_cost"] += cost or 0.0
                overview["chat_requests"] += 1
                overview["chat_tokens"] += total
            else:
                overview["background_cost"] += cost or 0.0
                overview["background_requests"] += 1
                overview["background_tokens"] += total

            if days <= 1:
                label = datetime.fromtimestamp(ts, BEIJING).strftime("%m-%d %H:00")
            else:
                label = datetime.fromtimestamp(ts, BEIJING).strftime("%m-%d")
            bucket = series.setdefault(label, {"bucket": label, "cost": 0.0, "tokens": 0, "requests": 0, "first_ts": ts})
            bucket["cost"] += cost or 0.0
            bucket["tokens"] += total
            bucket["requests"] += 1
            bucket["first_ts"] = min(bucket["first_ts"], ts)

            group = groups.setdefault(
                group_label,
                {
                    "group": group_label,
                    "session_id": row["session_id"],
                    "requests": 0,
                    "tokens": 0,
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "cost": 0.0,
                    "hit": 0,
                    "prompt": 0,
                    "peak": 0,
                    "priced": 0,
                },
            )
            group["requests"] += 1
            group["tokens"] += total
            group["prompt_tokens"] += max(0, prompt)
            group["output_tokens"] += max(0, out)
            group["cost"] += cost or 0.0
            group["hit"] += max(0, hit)
            group["prompt"] += max(0, prompt)
            if entry is not None:
                group["priced"] += 1
                group["peak"] += 1 if peak else 0

            model_row = models.setdefault(
                model_key,
                {"model": model_key, "provider": row["provider"] or "—", "requests": 0, "tokens": 0, "cost": 0.0, "peak": 0, "priced": 0},
            )
            model_row["requests"] += 1
            model_row["tokens"] += total
            model_row["cost"] += cost or 0.0
            if entry is not None:
                model_row["priced"] += 1
                model_row["peak"] += 1 if peak else 0

        overview["cost"] = _round_money(overview["cost"])
        overview["chat_cost"] = _round_money(overview["chat_cost"])
        overview["background_cost"] = _round_money(overview["background_cost"])
        overview["cache_hit_ratio"] = _round_ratio(hit_total / prompt_total) if prompt_total else 0.0
        overview["peak_ratio"] = _round_ratio(peak_requests / priced_requests) if priced_requests else 0.0
        overview["groups_count"] = len(groups)
        overview["unknown_count"] = sum(unknown_models.values())
        if unknown_models:
            names = "、".join(f"{name}×{count}" for name, count in sorted(unknown_models.items(), key=lambda item: -item[1])[:3])
            overview["unknown_text"] = f"未识别 {len(unknown_models)} 个模型（费用按 0 计）：{names}"

        tool_calls, tool_cost_total, tool_breakdown, tool_unpriced = self._tool_cost_total(tools)
        overview["tool_requests"] = tool_calls
        overview["tool_cost"] = _round_money(tool_cost_total)
        overview["token_cost"] = overview["cost"]
        overview["total_cost"] = _round_money(overview["cost"] + overview["tool_cost"])
        if tool_calls > 0:
            price_map = self._tool_price_map()
            detail = "、".join(
                f"{name} {count} 次 × ¥{price_map.get(name, 0.0):g}/1000"
                for name, count in sorted(tool_breakdown.items())
            )
            suffix = f"；其中 {tool_unpriced} 次未配置单价、暂按 0 计" if tool_unpriced else ""
            overview["tool_note"] = (
                f"工具调用 {tool_calls} 次（{detail}），估算工具费 ¥{overview['tool_cost']:.2f}，已计入总花费{suffix}"
            )
        else:
            overview["tool_note"] = (
                f"工具调用 {tool_calls} 次，均未配置单价（暂按 0 计），请在价格库填写工具价"
                if tool_calls
                else "窗口内没有联网检索工具调用"
            )

        series_rows = []
        for row in sorted(series.values(), key=lambda item: item["first_ts"]):
            series_rows.append(
                {
                    "bucket": row["bucket"],
                    "cost": _round_money(row["cost"]),
                    "tokens": row["tokens"],
                    "requests": row["requests"],
                }
            )

        group_rows = []
        for row in sorted(groups.values(), key=lambda item: -item["cost"]):
            row["cost"] = _round_money(row["cost"])
            row["cache_hit_ratio"] = _round_ratio(row["hit"] / row["prompt"]) if row["prompt"] else 0.0
            row["peak_ratio"] = _round_ratio(row["peak"] / row["priced"]) if row["priced"] else 0.0
            for key in ("hit", "prompt", "peak", "priced"):
                row.pop(key, None)
            group_rows.append(row)

        model_rows = []
        for row in sorted(models.values(), key=lambda item: -item["cost"]):
            row["cost"] = _round_money(row["cost"])
            row["peak_ratio"] = _round_ratio(row["peak"] / row["priced"]) if row["priced"] else 0.0
            row.pop("peak", None)
            row.pop("priced", None)
            model_rows.append(row)

        report = {
            "overview": overview,
            "series": series_rows,
            "groups": group_rows,
            "models": model_rows,
            "days": days,
        }
        return report

    def _report_cache_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / REPORT_CACHE_FILE

    def _load_report_cache(self) -> None:
        """从 data_dir 恢复上次计算结果（重启后页面先显示旧数据）。"""
        cache: Dict[str, Any] = {}
        try:
            path = self._report_cache_path()
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                reports = raw.get("reports") if isinstance(raw, dict) else None
                if isinstance(reports, dict):
                    for key, item in reports.items():
                        if isinstance(item, dict) and isinstance(item.get("report"), dict):
                            cache[str(key)] = (float(item.get("ts") or 0.0), item["report"])
        except Exception:  # noqa: BLE001
            cache = {}
        self._report_cache = cache

    def _save_report_cache(self) -> None:
        try:
            cache = getattr(self, "_report_cache", None)
            if not isinstance(cache, dict):
                return
            payload = {
                "reports": {
                    key: {"ts": ts, "report": report}
                    for key, (ts, report) in cache.items()
                    if isinstance(ts, (int, float)) and isinstance(report, dict)
                }
            }
            path = self._report_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001
            pass

    def _schedule_refresh(self, days: int) -> None:
        tasks = getattr(self, "_refresh_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._refresh_tasks = tasks
        task = tasks.get(days)
        if task is not None and not task.done():
            return
        try:
            tasks[days] = asyncio.create_task(self._compute_reports(days))
        except RuntimeError:
            pass

    async def _compute_reports(self, days: int) -> None:
        """后台重算：一次取数，同时算出「全部」与「仅聊天」两份结果。"""
        try:
            usage = await self._load_usage(days)
            sessions = await self._session_map()
            tools = await self._load_tools(days)
        except Exception:  # noqa: BLE001
            return
        stamp = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        now = time.time()
        cache = getattr(self, "_report_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._report_cache = cache
        for scope in ("all", "chat"):
            report = self._build_report(days, usage, sessions, tools, scope)
            report["updated_at"] = stamp
            cache[f"{days}:{scope}"] = (now, report)
        self._save_report_cache()
        try:
            await self._write_card_cache()
        except Exception:  # noqa: BLE001
            pass

    def _report_meta(self, report: Dict[str, Any], days: int) -> Dict[str, Any]:
        tasks = getattr(self, "_refresh_tasks", None) or {}
        task = tasks.get(days) if isinstance(tasks, dict) else None
        computing = bool(task is not None and not task.done())
        pricing = self._pricing()
        interval = max(30, min(3600, int(getattr(pricing, "refresh_seconds", 300) or 300)))
        updated = str(report.get("updated_at") or "")
        if computing:
            text = f"正在计算新的结果…（上次更新 {updated or '暂无'}）"
        else:
            text = f"数据更新于 {updated or '暂无'} · 每 {interval} 秒后台刷新"
        return {"computing": computing, "status_text": text}

    def _placeholder_report(self, days: int, scope: str) -> Dict[str, Any]:
        report = self._build_report(days, [], {}, [], scope)
        report["updated_at"] = ""
        return report

    async def _report(self, days: int, scope: str = "") -> Dict[str, Any]:
        pricing = self._pricing()
        scope_norm = (scope or "").strip().lower()
        if scope_norm not in ("all", "chat"):
            scope_norm = "all" if bool(getattr(pricing, "include_background", True)) else "chat"
        cache = getattr(self, "_report_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._report_cache = cache
        key = f"{days}:{scope_norm}"
        cached = cache.get(key)
        interval = max(30, min(3600, int(getattr(pricing, "refresh_seconds", 300) or 300)))
        if isinstance(cached, tuple) and time.time() - float(cached[0]) < interval:
            return cached[1]
        self._schedule_refresh(days)
        if isinstance(cached, tuple):
            return cached[1]
        return self._placeholder_report(days, scope_norm)

    def _days(self, range_key: str) -> int:
        return RANGE_DAYS.get(str(range_key or "").strip().lower(), 7)

    # ── WebUI 查询 API ──

    @API(name="ledger_overview", description="账本总览", version="1")
    async def ledger_overview(self, scope: str = "") -> Dict[str, Any]:
        """返回总览指标（按 token 与价格库演算的花费、请求数、缓存/高峰占比、群数量）。"""

        report = await self._report(7, scope)
        overview = dict(report["overview"])
        overview.update(self._report_meta(report, 7))
        overview["updated_at"] = report.get("updated_at", "")
        overview["range"] = "7d"
        return overview

    @API(name="ledger_overview_range", description="账本总览（指定时间范围）", version="1")
    async def ledger_overview_range(self, range_key: str = "7d", scope: str = "") -> Dict[str, Any]:
        """返回指定时间范围的总览指标。"""

        days = self._days(range_key)
        report = await self._report(days, scope)
        overview = dict(report["overview"])
        overview.update(self._report_meta(report, days))
        overview["updated_at"] = report.get("updated_at", "")
        overview["range"] = str(range_key)
        return overview

    @API(name="ledger_series", description="花费趋势", version="1")
    async def ledger_series(self, range_key: str = "7d", scope: str = "") -> List[Dict[str, Any]]:
        """返回按小时/按天聚合的趋势数据（x=bucket，y=cost/tokens/requests）。"""

        report = await self._report(self._days(range_key), scope)
        return report["series"]

    @API(name="ledger_groups", description="按群花费明细", version="1")
    async def ledger_groups(self, range_key: str = "7d") -> List[Dict[str, Any]]:
        """返回按群聚合的明细行（核心数据：token 与费用开销）。"""

        report = await self._report(self._days(range_key))
        return report["groups"]

    @API(name="ledger_models", description="按模型花费明细", version="1")
    async def ledger_models(self, range_key: str = "7d") -> List[Dict[str, Any]]:
        """返回按模型聚合的明细行（含匹配/未匹配价格状态）。"""

        report = await self._report(self._days(range_key))
        return report["models"]

    @API(name="ledger_prices", description="价格库列表", version="1")
    async def ledger_prices(self) -> List[Dict[str, Any]]:
        """返回当前配置中的价格库条目（供价格库页面展示）。"""

        ledger = self._pricing()
        rows: List[Dict[str, Any]] = []
        for entry in ledger.prices or []:
            rows.append(
                {
                    "provider": str(entry.provider or ""),
                    "model": str(entry.model or ""),
                    "currency": str(entry.currency or "CNY"),
                    "input": float(entry.input_price or 0.0),
                    "cache_input": float(entry.cache_input_price or 0.0),
                    "output": float(entry.output_price or 0.0),
                    "peak_input": float(entry.peak_input_price if entry.peak_input_price > 0 else entry.input_price),
                    "peak_output": float(entry.peak_output_price if entry.peak_output_price > 0 else entry.output_price),
                    "source": str(entry.source_url or ""),
                    "fetched_at": str(entry.fetched_at or ""),
                    "verified": bool(entry.verified),
                }
            )
        return rows

    @staticmethod
    def _provider_key(provider: str) -> str:
        """供应商归一化键：去尾部 Coding 等后缀、压缩空白、转小写。"""

        text = re.sub(r"\s+", " ", str(provider or "").strip())
        text = re.sub(r"\s+(?:coding|openai-compatible)\s*$", "", text, flags=re.IGNORECASE).strip()
        return text.lower() or "unknown"

    @staticmethod
    def _display_provider(provider: str) -> str:
        """供应商显示名：保留原始大小写，去掉「 Coding」等后缀。"""

        text = re.sub(r"\s+", " ", str(provider or "").strip())
        text = re.sub(r"\s+(?:coding|openai-compatible)\s*$", "", text, flags=re.IGNORECASE).strip()
        return text or "未知"

    @staticmethod
    def _entry_tokens(entry: Dict[str, Any]) -> List[str]:
        """条目可用于匹配的模型标识（模型名 + 标识 + 别名）。"""

        tokens = [str(entry.get("model") or ""), str(entry.get("model_id") or "")]
        tokens.extend(str(entry.get("aliases") or "").split(","))
        return [token.strip().lower() for token in tokens if token and token.strip()]

    async def _usage_buckets(self) -> List[Dict[str, Any]]:
        """按「供应商 + 模型标识」聚合窗口内的实际调用（可排除测试调用）。"""

        cfg = self.config.pricing
        days = max(1, min(365, int(cfg.usage_window_days or 30)))
        exclude_tests = bool(cfg.exclude_test_tasks)
        buckets: Dict[str, Dict[str, Any]] = {}
        for item in await self._load_usage(days):
            if exclude_tests and str(item.get("task") or "") == "webui_model_test":
                continue
            provider_key = self._provider_key(item.get("provider"))
            model_id = str(item.get("model_id") or "").strip()
            assign = str(item.get("model") or "").strip()
            model_key = (model_id or assign).lower()
            if not model_key:
                continue
            key = provider_key + "::" + model_key
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "provider": self._display_provider(item.get("provider")),
                    "model_id": model_id,
                    "assign_counts": {},
                    "requests": 0,
                    "tokens": 0,
                }
                buckets[key] = bucket
            bucket["requests"] += 1
            bucket["tokens"] += int(item.get("total") or 0)
            if assign:
                counts = bucket["assign_counts"]
                counts[assign] = counts.get(assign, 0) + 1
        result: List[Dict[str, Any]] = []
        for bucket in buckets.values():
            counts = bucket.pop("assign_counts", {})
            bucket["model_display"] = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
            if not bucket["model_display"]:
                bucket["model_display"] = str(bucket.get("model_id") or "")
            result.append(bucket)
        result.sort(key=lambda item: (-int(item.get("requests") or 0), str(item.get("model_display") or "").lower()))
        return result

    def _find_entry(self, items: List[Dict[str, Any]], provider: str, model: str, model_id: str = "") -> Optional[Dict[str, Any]]:
        """在价格库里找条目：先按模型名/标识/别名匹配，再优先同供应商。"""

        wanted = [token.strip().lower() for token in (model, model_id) if token and token.strip()]
        if not wanted:
            return None
        provider_key = self._provider_key(provider)
        candidates = [entry for entry in items if any(token in self._entry_tokens(entry) for token in wanted)]
        if not candidates:
            return None
        for entry in candidates:
            if self._provider_key(entry.get("provider")) == provider_key:
                return entry
        return candidates[0]

    def _official_price_for(self, provider: str, model: str, model_id: str) -> Optional[Dict[str, Any]]:
        """按「服务商 + 模型标识/别名」匹配内置官方价目；匹配不到返回 None。"""

        provider_key = self._provider_key(provider)
        names = {str(model or "").strip().lower(), str(model_id or "").strip().lower()}
        names.discard("")
        for item in OFFICIAL_PRICE_CATALOG:
            if not any(key in provider_key for key in item["provider_keys"]):
                continue
            aliases = {str(alias).strip().lower() for alias in item["aliases"]}
            if names & aliases:
                return dict(item)
        return None

    def _adapt_official_peak(self, entry: Dict[str, Any], catalog: Dict[str, Any]) -> bool:
        """给已有条目补峰谷价（只补缺失项）；返回是否发生了修改。"""

        if bool(entry.get("auto_adapted")):
            return False
        peak_values = (
            float(entry.get("peak_input_price") or 0.0),
            float(entry.get("peak_cache_input_price") or 0.0),
            float(entry.get("peak_output_price") or 0.0),
        )
        has_peak = any(value > 0 for value in peak_values)
        has_window = bool(str(entry.get("peak_windows") or "").strip()) or bool(str(entry.get("peak_weekdays") or "").strip())
        if has_peak and has_window:
            return False
        changed = False
        for field in ("peak_input_price", "peak_cache_input_price", "peak_output_price",
                      "peak_weekdays", "peak_windows", "peak_holiday_mode"):
            current = entry.get(field)
            if field.endswith("_price"):
                if float(current or 0.0) <= 0:
                    entry[field] = float(catalog[field])
                    changed = True
            elif not str(current or "").strip():
                entry[field] = catalog[field]
                changed = True
        if not str(entry.get("source_url") or "").strip():
            entry["source_url"] = catalog["source_url"]
            changed = True
        if not str(entry.get("fetched_at") or "").strip():
            entry["fetched_at"] = catalog["fetched_at"]
            changed = True
        entry["auto_adapted"] = True
        entry.setdefault("note", "峰谷价按官方价目自动适配，请核对")
        return changed

    def _auto_adapt_prices(self, items: List[Dict[str, Any]], buckets: List[Dict[str, Any]]) -> bool:
        """扫描到用量模型后，命中内置官方价目的条目自动补充价格/峰谷价；返回是否持久化改动。"""

        changed = False
        for bucket in buckets:
            catalog = self._official_price_for(
                str(bucket.get("provider") or ""),
                str(bucket.get("model_display") or ""),
                str(bucket.get("model_id") or ""),
            )
            if catalog is None:
                continue
            entry = self._find_entry(items, str(bucket.get("provider") or ""), str(bucket.get("model_display") or ""), str(bucket.get("model_id") or ""))
            if entry is None:
                entry = {"provider": str(bucket.get("provider") or ""), "model": str(bucket.get("model_display") or ""),
                         "model_id": str(bucket.get("model_id") or "")}
                for field in ("input_price", "cache_input_price", "output_price", "peak_input_price",
                              "peak_cache_input_price", "peak_output_price", "peak_weekdays", "peak_windows",
                              "peak_holiday_mode", "source_url", "fetched_at"):
                    entry[field] = catalog[field]
                entry["currency"] = "CNY"
                entry["verified"] = False
                entry["auto_adapted"] = True
                items.append(entry)
                changed = True
                continue
            if self._adapt_official_peak(entry, catalog):
                changed = True
        return changed

    @API(name="prices_rows", description="价格库页面数据（只含窗口内实际用过的模型）", version="1")
    async def prices_rows(self) -> List[Dict[str, Any]]:
        """按实际用量返回价格行：模型标识去重、命名用宿主显示名。"""

        items = self._ensure_price_store()
        buckets = await self._usage_buckets()
        if self._auto_adapt_prices(items, buckets):
            self._save_price_store(items)
            self._invalidate_entries()
            self._report_cache = {}
        rows: List[Dict[str, Any]] = []
        matched_ids: set[int] = set()
        for bucket in buckets:
            entry = self._find_entry(items, str(bucket.get("provider") or ""), str(bucket.get("model_display") or ""), str(bucket.get("model_id") or ""))
            if entry is None:
                status = "— 待补价"
                prices = {
                    "currency": "CNY",
                    "input": 0.0,
                    "cache_input": 0.0,
                    "output": 0.0,
                    "peak_input": 0.0,
                    "peak_cache_input": 0.0,
                    "peak_output": 0.0,
                    "peak_weekdays": "",
                    "peak_windows": "",
                    "peak_holiday_mode": "",
                    "source": "",
                    "fetched_at": "",
                    "verified": False,
                }
            else:
                status = "✓ 已确认" if bool(entry.get("verified")) else "待确认"
                prices = {
                    "currency": str(entry.get("currency") or "CNY"),
                    "input": float(entry.get("input_price") or 0.0),
                    "cache_input": float(entry.get("cache_input_price") or 0.0),
                    "output": float(entry.get("output_price") or 0.0),
                    "peak_input": max(0.0, float(entry.get("peak_input_price") or 0.0)),
                    "peak_cache_input": max(0.0, float(entry.get("peak_cache_input_price") or 0.0)),
                    "peak_output": max(0.0, float(entry.get("peak_output_price") or 0.0)),
                    "peak_weekdays": str(entry.get("peak_weekdays") or ""),
                    "peak_windows": str(entry.get("peak_windows") or ""),
                    "peak_holiday_mode": str(entry.get("peak_holiday_mode") or ""),
                    "source": str(entry.get("source_url") or ""),
                    "fetched_at": str(entry.get("fetched_at") or ""),
                    "verified": bool(entry.get("verified")),
                }
            if entry is not None:
                matched_ids.add(id(entry))
            row = {
                "provider": bucket.get("provider") or "",
                "model": bucket.get("model_display") or "",
                "model_id": bucket.get("model_id") or "",
                "requests": int(bucket.get("requests") or 0),
                "tokens": int(bucket.get("tokens") or 0),
                "status": status,
                "unit": str((entry or {}).get("unit") or "token"),
                "unit_price": float((entry or {}).get("unit_price") or 0.0),
            }
            row.update(prices)
            rows.append(row)
        for entry in items:
            if id(entry) in matched_ids:
                continue
            name = str(entry.get("model") or "").strip()
            if not name:
                continue
            rows.append(
                {
                    "provider": self._display_provider(str(entry.get("provider") or "")),
                    "model": name,
                    "model_id": str(entry.get("model_id") or ""),
                    "requests": 0,
                    "tokens": 0,
                    "status": "自定义 · 暂无调用",
                    "unit": str(entry.get("unit") or "token"),
                    "unit_price": float(entry.get("unit_price") or 0.0),
                    "currency": str(entry.get("currency") or "CNY"),
                    "input": float(entry.get("input_price") or 0.0),
                    "cache_input": float(entry.get("cache_input_price") or 0.0),
                    "output": float(entry.get("output_price") or 0.0),
                    "peak_input": max(0.0, float(entry.get("peak_input_price") or 0.0)),
                    "peak_cache_input": max(0.0, float(entry.get("peak_cache_input_price") or 0.0)),
                    "peak_output": max(0.0, float(entry.get("peak_output_price") or 0.0)),
                    "peak_weekdays": str(entry.get("peak_weekdays") or ""),
                    "peak_windows": str(entry.get("peak_windows") or ""),
                    "peak_holiday_mode": str(entry.get("peak_holiday_mode") or ""),
                    "source": str(entry.get("source_url") or ""),
                    "fetched_at": str(entry.get("fetched_at") or ""),
                    "verified": bool(entry.get("verified")),
                    "note": str(entry.get("note") or ""),
                }
            )
        return rows

    @API(name="price_save", description="保存单个模型价格（空字符串=保持不变）", version="1")
    async def price_save(
        self,
        provider: str = "",
        model: str = "",
        model_id: str = "",
        input_price: str = "",
        cache_input_price: str = "",
        output_price: str = "",
        peak_input_price: str = "",
        peak_cache_input_price: str = "",
        peak_output_price: str = "",
        peak_weekdays: str = "",
        peak_windows: str = "",
        peak_holiday_mode: str = "",
        source_url: str = "",
        currency: str = "",
        aliases: str = "",
        verified: str = "",
        unit: str = "",
        unit_price: str = "",
        fetched_at: str = "",
    ) -> Dict[str, Any]:
        """按字段更新价格库；仅修改传入的非空值。"""

        provider = str(provider or "").strip()
        model = str(model or "").strip()
        if not model:
            raise ValueError("缺少模型名")
        items = self._ensure_price_store()
        target = self._find_entry(items, provider, model, model_id)
        if target is None:
            target = {
                "provider": provider,
                "model": model,
                "model_id": model_id,
                "aliases": "",
                "unit": "token",
                "unit_price": 0.0,
                "currency": "CNY",
                "input_price": 0.0,
                "cache_input_price": 0.0,
                "output_price": 0.0,
                "peak_input_price": 0.0,
                "peak_cache_input_price": 0.0,
                "peak_output_price": 0.0,
                "peak_weekdays": "",
                "peak_windows": "",
                "peak_holiday_mode": "",
                "source_url": "",
                "fetched_at": "",
                "verified": False,
            }
            items.append(target)
        if str(provider or "").strip():
            target["provider"] = str(provider).strip()
        if str(model or "").strip():
            target["model"] = str(model).strip()
        if str(model_id or "").strip():
            target["model_id"] = str(model_id).strip()
        numeric = {
            "input_price": input_price,
            "cache_input_price": cache_input_price,
            "output_price": output_price,
            "peak_input_price": peak_input_price,
            "peak_cache_input_price": peak_cache_input_price,
            "peak_output_price": peak_output_price,
        }
        for field, value in numeric.items():
            text = str(value or "").strip()
            if text:
                target[field] = max(-1.0, float(text)) if field.startswith("peak_") else max(0.0, float(text))
        unit_text = str(unit or "").strip().lower()
        if unit_text:
            if unit_text not in ("token", "call"):
                raise ValueError("计价单位只能是 token / call")
            target["unit"] = unit_text
        unit_price_text = str(unit_price or "").strip()
        if unit_price_text:
            target["unit_price"] = max(0.0, float(unit_price_text))
        for field, value in {
            "source_url": source_url,
            "currency": currency,
            "aliases": aliases,
            "fetched_at": fetched_at,
        }.items():
            text = str(value or "").strip()
            if text:
                target[field] = text
        for field, value in {
            "peak_weekdays": peak_weekdays,
            "peak_windows": peak_windows,
            "peak_holiday_mode": peak_holiday_mode,
        }.items():
            text = str(value or "").strip()
            if text == "-":
                target[field] = ""
            elif text:
                target[field] = text
        if str(verified or "").strip():
            target["verified"] = str(verified).strip().lower() in {"1", "true", "yes", "on"}
        self._save_price_store(items)
        self._invalidate_entries()
        self._report_cache = {}
        await self._write_card_cache()
        return {
            "ok": True,
            "provider": target.get("provider", ""),
            "model": target.get("model", ""),
            "model_id": target.get("model_id", ""),
        }

    @API(name="price_add", description="手动新增一条模型价格（或合并已有条目）", version="1")
    async def price_add(
        self,
        provider: str = "",
        model: str = "",
        model_id: str = "",
        aliases: str = "",
        unit: str = "token",
        unit_price: str = "",
        currency: str = "CNY",
        input_price: str = "",
        cache_input_price: str = "",
        output_price: str = "",
        peak_input_price: str = "",
        peak_cache_input_price: str = "",
        peak_output_price: str = "",
        peak_weekdays: str = "",
        peak_windows: str = "",
        peak_holiday_mode: str = "",
        source_url: str = "",
        fetched_at: str = "",
        verified: str = "false",
    ) -> Dict[str, Any]:
        """手动新增：没有调用记录的模型也能提前建价。"""

        saved = await self.price_save(
            provider=provider,
            model=model,
            model_id=model_id,
            aliases=aliases,
            unit=unit,
            unit_price=unit_price,
            currency=currency,
            input_price=input_price,
            cache_input_price=cache_input_price,
            output_price=output_price,
            peak_input_price=peak_input_price,
            peak_cache_input_price=peak_cache_input_price,
            peak_output_price=peak_output_price,
            peak_weekdays=peak_weekdays,
            peak_windows=peak_windows,
            peak_holiday_mode=peak_holiday_mode,
            source_url=source_url,
            fetched_at=fetched_at,
            verified=verified,
        )
        saved["added"] = True
        return saved

    @API(name="price_verify", description="设置某个模型价格是否已确认", version="1")
    async def price_verify(self, provider: str = "", model: str = "", verified: str = "true", model_id: str = "") -> Dict[str, Any]:
        """切换确认状态。"""

        items = self._ensure_price_store()
        target = self._find_entry(items, provider, model, model_id)
        if target is None:
            # 待补价行也能直接切换确认状态：先按当前命名建空条目，再写确认位
            target = {
                "provider": str(provider or ""),
                "model": str(model or provider or ""),
                "model_id": str(model_id or ""),
                "aliases": [],
                "unit": "token",
                "unit_price": 0.0,
                "currency": "CNY",
                "input_price": 0.0,
                "cache_input_price": 0.0,
                "output_price": 0.0,
                "peak_input_price": 0.0,
                "peak_cache_input_price": 0.0,
                "peak_output_price": 0.0,
                "peak_weekdays": "",
                "peak_windows": "",
                "peak_holiday_mode": "",
                "source_url": "",
                "fetched_at": "",
                "verified": False,
            }
            items.append(target)
        target["verified"] = str(verified).strip().lower() in {"1", "true", "yes", "on"}
        self._save_price_store(items)
        self._invalidate_entries()
        self._report_cache = {}
        await self._write_card_cache()
        return {"ok": True, "model": target.get("model", ""), "verified": target["verified"]}

    @API(name="tool_prices", description="工具调用价列表（含窗口内调用次数）", version="1")
    async def tool_prices(self) -> List[Dict[str, Any]]:
        """返回工具价条目：工具名、单价（元/1000 次）、来源、确认状态与窗口内调用次数。"""

        days = max(1, min(365, int(getattr(self._pricing(), "usage_window_days", 30) or 30)))
        counts: Dict[str, int] = {}
        try:
            for row in await self._load_tools(days):
                name = str(row.get("name") or "").strip().lower()
                if name:
                    counts[name] = counts.get(name, 0) + int(row.get("count") or 0)
        except Exception:  # noqa: BLE001
            counts = {}
        rows: List[Dict[str, Any]] = []
        known = set()
        for item in self._ensure_tool_store():
            name = str(item.get("tool") or "").strip().lower()
            if not name:
                continue
            known.add(name)
            price = float(item.get("price_per_1000") or 0.0)
            rows.append(
                {
                    "provider": str(item.get("provider") or ""),
                    "tool": name,
                    "price_per_1000": price,
                    "currency": str(item.get("currency") or "CNY"),
                    "source_url": str(item.get("source_url") or ""),
                    "fetched_at": str(item.get("fetched_at") or ""),
                    "verified": bool(item.get("verified")),
                    "note": str(item.get("note") or ""),
                    "calls": counts.get(name, 0),
                    "cost": round(counts.get(name, 0) * price / 1000.0, 4),
                    "status": ("未确认" if price > 0 else "待补价") if not item.get("verified") else "已确认",
                    "window_days": days,
                }
            )
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if name in known or not name.startswith("web_"):
                continue
            rows.append(
                {
                    "provider": "",
                    "tool": name,
                    "price_per_1000": 0.0,
                    "currency": "CNY",
                    "source_url": "",
                    "fetched_at": "",
                    "verified": False,
                    "note": "",
                    "calls": count,
                    "cost": 0.0,
                    "status": "待补价",
                    "window_days": days,
                }
            )
        return rows

    @API(name="tool_price_save", description="保存工具调用价", version="1")
    async def tool_price_save(
        self,
        tool: str = "",
        provider: str = "",
        price_per_1000: str = "",
        currency: str = "",
        source_url: str = "",
        fetched_at: str = "",
        verified: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """保存某工具的单价（留空字段不修改；单价单位：元/1000 次）。"""

        name = str(tool or "").strip().lower()
        if not name:
            raise ValueError("缺少工具名")
        items = self._ensure_tool_store()
        target: Optional[Dict[str, Any]] = None
        for item in items:
            if str(item.get("tool") or "").strip().lower() == name:
                target = item
                break
        if target is None:
            target = {
                "provider": "",
                "tool": name,
                "price_per_1000": 0.0,
                "currency": "CNY",
                "source_url": "",
                "fetched_at": "",
                "verified": False,
                "note": "",
            }
            items.append(target)
        if str(provider).strip():
            target["provider"] = str(provider).strip()
        if str(price_per_1000).strip() != "":
            target["price_per_1000"] = max(0.0, float(price_per_1000))
        if str(currency).strip():
            target["currency"] = str(currency).strip().upper()
        if str(source_url).strip():
            target["source_url"] = str(source_url).strip()
        if str(fetched_at).strip():
            target["fetched_at"] = str(fetched_at).strip()
        if str(verified).strip() != "":
            target["verified"] = str(verified).strip().lower() in ("1", "true", "yes", "on")
        if str(note).strip():
            target["note"] = str(note).strip()
        self._save_tool_store(items)
        self._report_cache = {}
        self._schedule_refresh(7)
        self._schedule_refresh(30)
        return {"saved": True, "tool": name}

    @API(name="peak_rule", description="当前峰谷时段规则摘要", version="1")
    async def peak_rule(self) -> Dict[str, Any]:
        """返回峰谷规则文本。"""

        return {"text": self._peak_rule_text()}

    # ── 生命周期 ──

    async def on_load(self) -> None:
        """加载时恢复上次计算结果（页面先显示旧数据），并在后台预热。"""

        data_dir = Path(self.ctx.paths.data_dir)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_price_store()
            self._ensure_tool_store()
            self._load_report_cache()
            self._schedule_refresh(7)
            self._schedule_refresh(30)
        except Exception:  # noqa: BLE001
            pass

    async def on_unload(self) -> None:
        """卸载时无需额外清理（无后台任务）。"""

        return None

    async def on_config_update(self, scope: str = "", config_data: dict[str, Any] | None = None, version: str = "") -> None:
        """配置变化（如价格库修改）后使缓存失效并在后台重算。"""

        self._report_cache = {}
        self._sessions_cache = None
        self._invalidate_entries()
        self._schedule_refresh(7)
        self._schedule_refresh(30)


def create_plugin() -> LedgerPlugin:
    """插件入口：Runner 通过该工厂函数创建实例。"""

    return LedgerPlugin()
