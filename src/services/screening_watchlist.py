# -*- coding: utf-8 -*-
"""全市场扫描观察名单：策略分层、名单合并/淘汰与报告渲染。

本模块只负责观察名单的纯逻辑，不触发任何网络调用，也不依赖选股引擎的
dataclass：`merge_run()` 接收 `dataclasses.asdict(Pick)` 之后的普通 dict，
因此可以在没有 pandas / akshare 的环境下单独测试。

编排入口见 `scripts/market_scan.py`，调度见
`.github/workflows/10-market-scan.yml`。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
SUPPORTED_CADENCES = (CADENCE_DAILY, CADENCE_WEEKLY)

# 策略 YAML 的 style.holding_period → 扫描频率。
# short_term 的信号衰减最快，值得每个交易日重扫；swing / watchlist 每周一次即可。
DEFAULT_CADENCE_MAP: Dict[str, str] = {
    "short_term": CADENCE_DAILY,
    "swing": CADENCE_WEEKLY,
    "watchlist": CADENCE_WEEKLY,
}
# holding_period 缺失或未知时的保守取值：宁可少跑，不要把未知策略拉成每日高频。
FALLBACK_CADENCE = CADENCE_WEEKLY

# 名单分桶：取自策略 YAML 的 style.risk_profile。
#
# 为什么必须分桶：`latest_score` 来自 `_rank_score(..., pct=True)`，是**该策略硬筛
# 存活池内的分位排名**。dual_low 池子里 239 只票的 84 分，和 theme_momentum 池子里
# 几十只票的 84 分不是一回事。全局排序会让两把不同的尺子争同一批名额，
# 因此 TTL、行业配额和容量都按桶独立执行。
BUCKET_DEFENSIVE = "defensive"
BUCKET_BALANCED = "balanced"
BUCKET_AGGRESSIVE = "aggressive"
SUPPORTED_BUCKETS = (BUCKET_DEFENSIVE, BUCKET_BALANCED, BUCKET_AGGRESSIVE)
# 未知 risk_profile 落到中间桶：既不会被当成可以长期留存的防守票，
# 也不会拿到进攻桶更宽松的行业配额。
FALLBACK_BUCKET = BUCKET_BALANCED
# 主桶优先级：一只票同时符合多个桶时，用哪个桶结算 TTL/配额/容量。
# 理由见 WatchlistEntry.bucket 的 docstring——让衰减最快的桶治理时效。
BUCKET_PRIORITY = (BUCKET_AGGRESSIVE, BUCKET_BALANCED, BUCKET_DEFENSIVE)

# 标量默认值，同时作为未知桶的兜底。
DEFAULT_TTL_DAYS = 30
DEFAULT_MAX_SIZE = 60
# 同一行业在名单中最多保留几只。选股引擎的组合分散只在单个策略内生效，
# 跨策略的集中度（7 个策略各选 1 只银行）只能在这一层管。0 表示不限制。
DEFAULT_MAX_PER_INDUSTRY = 2
# 同一行业在**整份名单**里的总数上限，跨桶结算。0 表示不限制。
# 桶内配额管不住跨桶叠加：9 只银行分散在均衡桶和防守桶、每桶各留 2 只，
# 整份名单仍有 4 只。默认 3 而不是 2，是为了让一个行业还能在两个桶里各留一个
# 代表（"银行既是防守也是均衡"本身有信息量）；要严格每行业 2 只就设成 2。
DEFAULT_MAX_PER_INDUSTRY_TOTAL = 3

# 各桶的默认限额。三者的差异是有依据的：
# - TTL：aggressive 是 short_term 信号，两周后基本失效；defensive 可以长期观察。
# - 容量：三桶合计 60，与拆桶前的全局上限一致。
# - 行业配额：热点扩散天然是同板块多只，进攻桶卡 2 只反而砍掉了信号本身。
DEFAULT_TTL_DAYS_BY_BUCKET: Dict[str, int] = {
    BUCKET_DEFENSIVE: 45,
    BUCKET_BALANCED: 30,
    BUCKET_AGGRESSIVE: 14,
}
DEFAULT_MAX_SIZE_BY_BUCKET: Dict[str, int] = {
    BUCKET_DEFENSIVE: 25,
    BUCKET_BALANCED: 20,
    BUCKET_AGGRESSIVE: 15,
}
DEFAULT_MAX_PER_INDUSTRY_BY_BUCKET: Dict[str, int] = {
    BUCKET_DEFENSIVE: 2,
    BUCKET_BALANCED: 2,
    BUCKET_AGGRESSIVE: 4,
}

BUCKET_LABELS: Dict[str, str] = {
    BUCKET_DEFENSIVE: "防守",
    BUCKET_BALANCED: "均衡",
    BUCKET_AGGRESSIVE: "进攻",
}

# 名单排序权重：以最近一次得分为主，命中多个策略/多次入选加分，长时间没再被选中扣分。
_HIT_BONUS_PER_EXTRA_HIT = 2.0
_MAX_BONUS_HITS = 5
_STALENESS_PENALTY_PER_DAY = 0.5

_DATE_FMT = "%Y-%m-%d"

# 2: strategies 从 {名: 分数} 改为 {名: {score,last_seen,bucket}}，bucket 变成派生值。
WATCHLIST_SCHEMA_VERSION = 2


def parse_date(value: Any) -> Optional[date]:
    """把 YYYY-MM-DD 文本解析成 date，解析失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], _DATE_FMT).date()
    except ValueError:
        return None


def format_date(value: date) -> str:
    return value.strftime(_DATE_FMT)


@dataclass
class StrategyHit:
    """某个策略对某只票的一次背书。

    逐策略保存 `bucket` 和 `last_seen`，是为了让 `WatchlistEntry.bucket` 变成
    **派生值**而不是某一瞬间的快照。此前 bucket 是存储字段，跨运行会被最后一轮
    无条件覆盖：周一 daily 跑成 aggressive、周五 weekly 跑成 defensive，
    连带 TTL（14↔45 天）和行业配额（4↔2）一起漂移。
    """

    score: float = 0.0
    last_seen: str = ""
    bucket: str = FALLBACK_BUCKET

    def to_dict(self) -> Dict[str, Any]:
        return {"score": self.score, "last_seen": self.last_seen, "bucket": self.bucket}

    @classmethod
    def from_any(
        cls,
        value: Any,
        *,
        default_bucket: str,
        default_last_seen: str,
    ) -> "StrategyHit":
        """兼容三种历史格式：名字列表、`{名: 分数}`、以及当前的 `{名: {...}}`。

        迁移旧快照时逐策略信息并不存在，只能回填成条目级的值——因此升级后的
        头一两轮 `buckets` 会偏保守，等各策略重新背书后才准确。
        """
        if isinstance(value, Mapping):
            return cls(
                score=_as_float(value.get("score"), 0.0) or 0.0,
                last_seen=str(value.get("last_seen") or default_last_seen),
                bucket=resolve_bucket(value.get("bucket") or default_bucket),
            )
        return cls(
            score=_as_float(value, 0.0) or 0.0,
            last_seen=default_last_seen,
            bucket=resolve_bucket(default_bucket),
        )


@dataclass
class WatchlistEntry:
    """观察名单中的一只股票。

    `bucket` / `buckets` 都是从 `strategies` 派生的，不存储：同一份数据永远得出
    同一个分桶，与策略跑动顺序和运行先后无关。
    """

    code: str
    name: str = ""
    holding_period: str = ""
    cadence: str = FALLBACK_CADENCE
    industry: str = ""
    first_seen: str = ""
    last_seen: str = ""
    hit_count: int = 0
    latest_score: float = 0.0
    best_score: float = 0.0
    latest_rank: Optional[int] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    risk_level: str = ""
    risk_flags: List[str] = field(default_factory=list)
    strategies: Dict[str, StrategyHit] = field(default_factory=dict)
    pinned: bool = False

    @property
    def buckets(self) -> List[str]:
        """该票当前仍然有效的全部分桶，按优先级排序；可能同时属于多个。

        过期背书由 `expire_entries` 剪掉，因此这里读到的都是有效的。
        """
        present = {hit.bucket for hit in self.strategies.values()}
        return [bucket for bucket in BUCKET_PRIORITY if bucket in present]

    @property
    def bucket(self) -> str:
        """主桶：用于 TTL、行业配额和容量结算。

        优先级 aggressive > balanced > defensive，取的不是"哪个更重要"，而是
        **让衰减最快的那个来治理时效**：进攻属性是当下正在发生的事，14 天不再
        被确认就该出局；防守属性是长期底色，掉出名单也随时能被 dual_low 选回来。
        反过来配会让已经退潮的进攻票靠"它也便宜"赖在名单里 45 天。
        """
        buckets = self.buckets
        return buckets[0] if buckets else FALLBACK_BUCKET

    @property
    def secondary_buckets(self) -> List[str]:
        """主桶之外还符合的桶，用于报告里的「兼 X」标注。"""
        return self.buckets[1:]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "holding_period": self.holding_period,
            "cadence": self.cadence,
            # bucket / buckets 是派生值，写出来只为让产物可直接阅读；
            # from_dict 不读它们，一律从 strategies 重新推导。
            "bucket": self.bucket,
            "buckets": self.buckets,
            "industry": self.industry,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "hit_count": self.hit_count,
            "latest_score": self.latest_score,
            "best_score": self.best_score,
            "latest_rank": self.latest_rank,
            "price": self.price,
            "change_pct": self.change_pct,
            "risk_level": self.risk_level,
            "risk_flags": list(self.risk_flags),
            "strategies": {name: hit.to_dict() for name, hit in self.strategies.items()},
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WatchlistEntry":
        code = str(raw.get("code") or "").strip()
        if not code:
            raise ValueError("watchlist entry missing code")
        last_seen = str(raw.get("last_seen") or "")
        legacy_bucket = resolve_bucket(raw.get("bucket"))

        strategies_raw = raw.get("strategies") or {}
        strategies: Dict[str, StrategyHit] = {}
        if isinstance(strategies_raw, Mapping):
            items = strategies_raw.items()
        elif isinstance(strategies_raw, (list, tuple)):
            items = ((name, 0.0) for name in strategies_raw)
        else:
            items = ()
        for key, value in items:
            strategies[str(key)] = StrategyHit.from_any(
                value, default_bucket=legacy_bucket, default_last_seen=last_seen
            )

        return cls(
            code=code,
            name=str(raw.get("name") or ""),
            holding_period=str(raw.get("holding_period") or ""),
            cadence=str(raw.get("cadence") or FALLBACK_CADENCE),
            industry=str(raw.get("industry") or ""),
            first_seen=str(raw.get("first_seen") or ""),
            last_seen=last_seen,
            hit_count=int(_as_float(raw.get("hit_count"), 0.0) or 0.0),
            latest_score=_as_float(raw.get("latest_score"), 0.0) or 0.0,
            best_score=_as_float(raw.get("best_score"), 0.0) or 0.0,
            latest_rank=_as_optional_int(raw.get("latest_rank")),
            price=_as_float(raw.get("price"), None),
            change_pct=_as_float(raw.get("change_pct"), None),
            risk_level=str(raw.get("risk_level") or ""),
            risk_flags=[str(item) for item in (raw.get("risk_flags") or [])],
            strategies=strategies,
            pinned=bool(raw.get("pinned")),
        )


@dataclass
class RunSummary:
    """单个策略一次扫描的执行结果，用于报告和耗时统计。"""

    strategy: str
    cadence: str
    holding_period: str = ""
    elapsed_sec: float = 0.0
    snapshot_count: int = 0
    after_filter_count: int = 0
    pick_count: int = 0
    daily_enriched: bool = False
    snapshot_source: str = ""
    run_id: str = ""
    error: str = ""
    degradation: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "cadence": self.cadence,
            "holding_period": self.holding_period,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "snapshot_count": self.snapshot_count,
            "after_filter_count": self.after_filter_count,
            "pick_count": self.pick_count,
            "daily_enriched": self.daily_enriched,
            "snapshot_source": self.snapshot_source,
            "run_id": self.run_id,
            "error": self.error,
            "degradation": list(self.degradation),
        }


def _as_float(value: Any, default: Optional[float]) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return parsed


def _as_optional_int(value: Any) -> Optional[int]:
    parsed = _as_float(value, None)
    return None if parsed is None else int(parsed)


# ---------------------------------------------------------------------------
# 分层：holding_period → cadence
# ---------------------------------------------------------------------------
def parse_cadence_map(text: str) -> Dict[str, str]:
    """解析 `short_term:daily,swing:weekly` 形式的分层配置。

    非法项会被跳过并记录 warning，保证配置写错时退回默认分层而不是直接崩。
    """
    mapping = dict(DEFAULT_CADENCE_MAP)
    for item in str(text or "").replace("；", ",").replace(";", ",").split(","):
        token = item.strip()
        if not token:
            continue
        if ":" not in token:
            logger.warning("忽略非法的观察名单分层配置项（缺少冒号）: %s", token)
            continue
        holding_period, _, cadence = token.partition(":")
        holding_period = holding_period.strip().lower()
        cadence = cadence.strip().lower()
        if not holding_period:
            logger.warning("忽略非法的观察名单分层配置项（holding_period 为空）: %s", token)
            continue
        if cadence not in SUPPORTED_CADENCES:
            logger.warning(
                "忽略非法的观察名单分层配置项（cadence 只支持 %s）: %s",
                "/".join(SUPPORTED_CADENCES),
                token,
            )
            continue
        mapping[holding_period] = cadence
    return mapping


def resolve_bucket(risk_profile: Any) -> str:
    """策略 YAML 的 `style.risk_profile` → 名单分桶，未知取值落到 FALLBACK_BUCKET。"""
    value = str(risk_profile or "").strip().lower()
    return value if value in SUPPORTED_BUCKETS else FALLBACK_BUCKET


def parse_bucket_limits(
    text: Any,
    *,
    defaults: Mapping[str, int],
    scalar_default: int,
) -> Dict[str, int]:
    """解析按桶配置的限额。

    支持三种写法，可混用：

    - 空 → 全部使用 `defaults`
    - ``"30"`` → 三个桶统一 30
    - ``"30,aggressive:10"`` → 默认 30，进攻桶覆盖为 10
    - ``"defensive:45,balanced:30,aggressive:14"`` → 逐桶指定

    非法项跳过并记录 warning，保证写错配置时退回默认值而不是直接崩。
    """
    limits = {bucket: int(defaults.get(bucket, scalar_default)) for bucket in SUPPORTED_BUCKETS}
    raw = str(text or "").replace("；", ",").replace(";", ",").strip()
    if not raw:
        return limits

    tokens = [item.strip() for item in raw.split(",") if item.strip()]

    # 两趟：先套用不带桶名的统一默认值，再让逐桶配置覆盖它。
    # 单趟顺序处理会让 "aggressive:10,30" 里的 30 反过来把 10 冲掉。
    for token in tokens:
        if ":" in token:
            continue
        parsed = _as_float(token, None)
        if parsed is None or parsed < 0:
            logger.warning("忽略非法的观察名单分桶限额（数值非法）: %s", token)
            continue
        for bucket in SUPPORTED_BUCKETS:
            limits[bucket] = int(parsed)

    for token in tokens:
        if ":" not in token:
            continue
        bucket, _, value = token.partition(":")
        bucket = bucket.strip().lower()
        if bucket not in SUPPORTED_BUCKETS:
            logger.warning(
                "忽略非法的观察名单分桶限额（未知桶名，只支持 %s）: %s",
                "/".join(SUPPORTED_BUCKETS),
                token,
            )
            continue
        parsed = _as_float(value.strip(), None)
        if parsed is None or parsed < 0:
            logger.warning("忽略非法的观察名单分桶限额（数值非法）: %s", token)
            continue
        limits[bucket] = int(parsed)

    return limits


def limit_for(limits: Any, bucket: str, *, default: int) -> int:
    """从「标量或按桶映射」里取出某个桶的限额。"""
    if isinstance(limits, Mapping):
        return int(limits.get(bucket, limits.get(FALLBACK_BUCKET, default)))
    if limits is None:
        return default
    return int(limits)


def resolve_cadence(holding_period: str, cadence_map: Mapping[str, str]) -> str:
    """按 holding_period 决定扫描频率，未知取值退回 FALLBACK_CADENCE。"""
    key = str(holding_period or "").strip().lower()
    return cadence_map.get(key, FALLBACK_CADENCE)


def select_strategies(
    strategy_infos: Sequence[Any],
    *,
    cadence: str,
    cadence_map: Mapping[str, str],
    market: str = "cn",
) -> List[Tuple[str, str]]:
    """挑出属于该频率、且支持目标市场的策略。

    Args:
        strategy_infos: `screening.strategy.list_strategies()` 的返回值，
            或任何带 `name` / `style` / `market_scope` 属性的对象。
        cadence: `daily` / `weekly` / `all`。
        cadence_map: holding_period → cadence 映射。
        market: 目标市场，过滤 `market_scope`。

    Returns:
        `[(strategy_name, holding_period), ...]`，按策略名排序保证运行顺序稳定。
    """
    wanted = str(cadence or "").strip().lower()
    selected: List[Tuple[str, str]] = []
    for info in strategy_infos:
        name = str(getattr(info, "name", "") or "")
        if not name:
            continue
        scope = getattr(info, "market_scope", None) or []
        if market and scope and market not in scope:
            continue
        style = getattr(info, "style", None) or {}
        holding_period = ""
        if isinstance(style, Mapping):
            holding_period = str(style.get("holding_period") or "")
        else:
            holding_period = str(getattr(style, "holding_period", "") or "")
        if wanted not in ("", "all") and resolve_cadence(holding_period, cadence_map) != wanted:
            continue
        selected.append((name, holding_period))
    selected.sort(key=lambda item: item[0])
    return selected


# ---------------------------------------------------------------------------
# 名单读写
# ---------------------------------------------------------------------------
def load_watchlist(path: Path) -> Tuple[Dict[str, WatchlistEntry], Dict[str, Any]]:
    """读取 current.json；文件缺失或损坏时返回空名单（fail-open）。"""
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("观察名单读取失败，按空名单处理: path=%s err=%s", path, exc)
        return {}, {}
    if not isinstance(payload, Mapping):
        logger.warning("观察名单格式非法，按空名单处理: path=%s", path)
        return {}, {}
    entries: Dict[str, WatchlistEntry] = {}
    for raw in payload.get("entries") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            entry = WatchlistEntry.from_dict(raw)
        except ValueError:
            continue
        entries[entry.code] = entry
    meta = dict(payload.get("meta") or {})
    schema_version = int(_as_float(payload.get("schema_version"), 0) or 0)
    if entries and schema_version < WATCHLIST_SCHEMA_VERSION:
        logger.info(
            "观察名单已从 schema v%s 迁移到 v%s：逐策略的 last_seen/bucket 在旧快照里不存在，"
            "已回填成条目级取值；等各策略重新背书后 buckets 才准确。",
            schema_version or "?",
            WATCHLIST_SCHEMA_VERSION,
        )
        meta["migrated_from_schema_version"] = schema_version
    return entries, meta


def save_watchlist(path: Path, entries: Mapping[str, WatchlistEntry], meta: Mapping[str, Any]) -> None:
    """原子写出 current.json。"""
    payload = {
        "schema_version": WATCHLIST_SCHEMA_VERSION,
        "meta": dict(meta),
        "entries": [entry.to_dict() for entry in sort_entries(entries.values(), run_date=None)],
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def save_watchlist_csv(path: Path, entries: Mapping[str, WatchlistEntry]) -> None:
    """导出一份便于肉眼扫的扁平表。"""
    columns = [
        "code", "name", "industry", "bucket", "buckets", "cadence", "holding_period",
        "first_seen", "last_seen", "hit_count", "latest_score", "best_score",
        "risk_level", "strategies", "pinned",
    ]
    lines: List[List[str]] = [columns]
    ordered: List[WatchlistEntry] = []
    grouped = group_by_bucket(entries.values())
    for bucket in SUPPORTED_BUCKETS:
        ordered.extend(sort_entries(grouped[bucket].values(), run_date=None))
    for entry in ordered:
        lines.append([
            entry.code,
            entry.name,
            entry.industry,
            entry.bucket,
            "|".join(entry.buckets),
            entry.cadence,
            entry.holding_period,
            entry.first_seen,
            entry.last_seen,
            str(entry.hit_count),
            f"{entry.latest_score:.2f}",
            f"{entry.best_score:.2f}",
            entry.risk_level,
            "|".join(sorted(entry.strategies)),
            "1" if entry.pinned else "0",
        ])
    buffer = _render_csv(lines)
    _atomic_write_text(path, buffer)


def load_pinned_codes(path: Path) -> List[str]:
    """读取手工固定的代码（每行一个，`#` 起始为注释）。"""
    if not path.exists():
        return []
    codes: List[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("固定名单读取失败，按空处理: path=%s err=%s", path, exc)
        return []
    for line in text.splitlines():
        token = line.split("#", 1)[0].strip()
        if token:
            codes.append(token)
    return codes


def to_stock_list(entries: Mapping[str, WatchlistEntry], *, run_date: Optional[date] = None) -> List[str]:
    """按名单排序输出逗号分隔清单用的代码序列（供 --write-stock-list 手工取用）。"""
    return [entry.code for entry in sort_entries(entries.values(), run_date=run_date)]


# ---------------------------------------------------------------------------
# 合并 / 淘汰
# ---------------------------------------------------------------------------
def rank_score(entry: WatchlistEntry, *, run_date: Optional[date] = None) -> float:
    """名单内排序用的综合分：最近得分 + 命中加分 - 陈旧扣分。"""
    score = entry.latest_score
    score += _HIT_BONUS_PER_EXTRA_HIT * min(max(entry.hit_count - 1, 0), _MAX_BONUS_HITS)
    if run_date is not None:
        last_seen = parse_date(entry.last_seen)
        if last_seen is not None:
            staleness = max((run_date - last_seen).days, 0)
            score -= _STALENESS_PENALTY_PER_DAY * staleness
    return score


def sort_entries(
    entries: Iterable[WatchlistEntry],
    *,
    run_date: Optional[date] = None,
) -> List[WatchlistEntry]:
    """固定的名单排序：pinned 优先，其次综合分、命中数、代码。"""
    return sorted(
        entries,
        key=lambda entry: (
            not entry.pinned,
            -rank_score(entry, run_date=run_date),
            -entry.hit_count,
            entry.code,
        ),
    )


def merge_run(
    entries: Mapping[str, WatchlistEntry],
    picks_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    run_date: date,
    holding_periods: Mapping[str, str],
    cadence_map: Mapping[str, str],
    risk_profiles: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, WatchlistEntry], List[str]]:
    """把一次扫描的候选并入名单。

    同一只票被多个策略选中时按最高分记录 `latest_score`，`latest_rank` /
    `cadence` / `holding_period` / `bucket` 一并取自那个最高分策略，而不是最后跑完的那个
    —— 否则名单里的扫描频率会变成"策略名字母序最后一个"，与实际代表性无关。
    `hit_count` 计的是**被选中的扫描日数**：同一次扫描里被多个策略命中只算一次，
    同一天重复运行也只算一次。它不是 `merge_run` 的调用次数——否则手动重跑几轮
    就能靠 `rank_score` 的命中加分把一只票顶进名单。`last_seen` 同理只前进不后退。

    Returns:
        `(合并后的名单, 本次新进入的代码)`
    """
    merged: Dict[str, WatchlistEntry] = {code: entry for code, entry in entries.items()}
    run_date_text = format_date(run_date)
    added: List[str] = []
    seen_this_run: set = set()
    # 每个代码在本轮开始前的 last_seen，用于按扫描日去重计数。
    previous_seen: Dict[str, Optional[date]] = {}
    # 每 (代码, 策略) 的 (rank, cadence, holding_period)，供合并结束后按主桶
    # 结算 latest_score 时回填。rank 是每只票在该策略内的名次，因此必须按
    # (代码, 策略) 存——只按策略存会把最后一只票的名次张冠李戴。
    run_meta: Dict[Tuple[str, str], Tuple[Optional[int], str, str]] = {}

    for strategy, picks in picks_by_strategy.items():
        holding_period = str(holding_periods.get(strategy, "") or "")
        cadence = resolve_cadence(holding_period, cadence_map)
        bucket = resolve_bucket((risk_profiles or {}).get(strategy))
        for pick in picks or []:
            code = str(pick.get("code") or "").strip()
            if not code:
                continue
            score = _as_float(pick.get("final_score"), 0.0) or 0.0
            run_meta[(code, strategy)] = (
                _as_optional_int(pick.get("rank")), cadence, holding_period
            )
            entry = merged.get(code)
            if entry is None:
                entry = WatchlistEntry(
                    code=code,
                    first_seen=run_date_text,
                    latest_score=score,
                    best_score=score,
                )
                merged[code] = entry
                added.append(code)

            if code not in seen_this_run:
                seen_this_run.add(code)
                # hit_count 计的是**被选中的扫描日数**，不是 merge_run 的调用次数。
                # 同一天重复手动跑 weekly 不该重复计数：hit_count 会通过
                # rank_score 的命中加分（最多 +10 分）影响行业配额和容量裁剪，
                # 手动重跑几次就能把一只票顶上去，那是运行次数在选股而不是策略。
                previous_seen[code] = parse_date(entry.last_seen)
                seen_before = previous_seen[code]
                if seen_before is None or seen_before < run_date:
                    entry.hit_count += 1
                # 同一次扫描内先归零，后续策略再按最高分覆盖。
                entry.latest_score = score
                entry.latest_rank = _as_optional_int(pick.get("rank"))
                entry.cadence = cadence
                entry.holding_period = holding_period
            elif score > entry.latest_score:
                entry.latest_score = score
                entry.latest_rank = _as_optional_int(pick.get("rank"))
                entry.cadence = cadence
                entry.holding_period = holding_period

            # last_seen 只前进不后退：用更早的日期补跑一轮，不应该把名单的时间
            # 基准拉回去，否则 TTL 会凭空延长。
            seen_before = previous_seen.get(code)
            if seen_before is None or seen_before <= run_date:
                entry.last_seen = run_date_text
            entry.best_score = max(entry.best_score, score)
            # 逐策略背书：entry.bucket 由这些记录派生，不再由本轮直接写死。
            entry.strategies[strategy] = StrategyHit(
                score=score, last_seen=run_date_text, bucket=bucket
            )
            entry.name = str(pick.get("name") or "") or entry.name
            entry.industry = str(pick.get("industry") or "") or entry.industry
            entry.price = _as_float(pick.get("price"), entry.price)
            entry.change_pct = _as_float(pick.get("change_pct"), entry.change_pct)
            entry.risk_level = str(pick.get("risk_level") or "") or entry.risk_level
            risk_flags = pick.get("risk_flags") or []
            if risk_flags:
                entry.risk_flags = [str(item) for item in risk_flags]

    # 主桶内结算 latest_score：必须等全部策略合并完再算。
    # `entry.bucket` 是从 strategies 派生的，合并途中每加一条背书都可能改变主桶，
    # 循环里边走边定会用到还没成形的桶。
    # 对**全部**条目结算，不只是本轮碰到的：结算只依赖已存储的 strategies，
    # 是确定性的。只结算本轮碰到的会让历史遗留的跨桶分数一直挂着，直到那只票
    # 碰巧再次被选中才自愈。
    for entry in merged.values():
        _settle_bucket_scoped_score(entry, run_meta)

    return merged, added


def _settle_bucket_scoped_score(
    entry: WatchlistEntry,
    run_meta: Mapping[Tuple[str, str], Tuple[Optional[int], str, str]],
) -> None:
    """把 `latest_score` 收敛到**主桶内**的最高分。

    跨桶命中的票（如同时被 balanced 与 defensive 策略选中）此前取的是全局最高分，
    于是主桶取 balanced、分数却来自 defensive——拿着另一把尺子的读数在 balanced
    桶里排序。实测中远海控/上海银行/中国平安三只都是这种情况，虚高 4~6 分。

    分桶的前提就是"分数只在同桶内可比"（`latest_score` 是该策略硬筛存活池内的
    分位排名），跨桶取分会让报告里"分数不可跨桶比较"的标注对这些行失效。

    `best_score` 不受影响：它的语义是"历史最好成绩"，跨桶取最大值是合理的。
    """
    bucket = entry.bucket
    in_bucket = {
        name: hit for name, hit in entry.strategies.items() if hit.bucket == bucket
    }
    if not in_bucket:
        return
    winner = max(in_bucket.items(), key=lambda kv: (kv[1].score, kv[0]))
    entry.latest_score = winner[1].score
    meta = run_meta.get((entry.code, winner[0]))
    if meta is not None:
        rank, cadence, holding_period = meta
        entry.latest_rank = rank
        entry.cadence = cadence
        entry.holding_period = holding_period


@dataclass
class IndustryQuotaDiagnostic:
    """行业配额是否真的起了作用。

    这一层是**静默失效**的重灾区：`industry` 为空时条目一律放行（无法分组，
    强行淘汰会误伤），于是配置在、逻辑在、数据不在时，报告看起来完全正常，
    只是名单里挤满同一个行业。首次实跑就踩了这个坑——19 条候选行业全空，
    9 只银行原样进入名单，而 `max_per_industry` 早已配好。
    """

    effective: bool
    total: int
    missing_industry: int
    trimmed: int

    @property
    def text(self) -> str:
        if not self.total:
            return "行业配额：名单为空，未参与判定"
        if self.missing_industry == self.total:
            return (
                f"⚠️ 行业配额未生效：{self.total} 条候选全部缺少行业数据，"
                "配额无从分组（检查 INDUSTRY_PROVIDER 与板块缓存）"
            )
        note = f"行业配额：已生效，本次裁掉 {self.trimmed} 条"
        if self.missing_industry:
            note += f"；另有 {self.missing_industry} 条缺少行业数据未参与分组"
        return note


def industry_quota_diagnostic(
    entries: Mapping[str, WatchlistEntry],
    removed: Sequence[Tuple[str, str]],
) -> IndustryQuotaDiagnostic:
    """判断行业配额这一层是生效了还是因为缺数据而静默放行。"""
    total = len(entries)
    missing = sum(1 for entry in entries.values() if not normalize_industry(entry.industry))
    trimmed = sum(
        1 for _code, reason in removed
        if reason in ("industry_quota", "industry_quota_global")
    )
    return IndustryQuotaDiagnostic(
        effective=bool(total) and missing < total,
        total=total,
        missing_industry=missing,
        trimmed=trimmed,
    )


def normalize_industry(value: str) -> str:
    """行业名归一化，仅用于配额分组。空值返回空串（不参与配额）。"""
    return str(value or "").strip().lower()


def apply_industry_quota(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    max_per_industry: int,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """限制同一行业在名单中的数量，超出的按排序靠后者淘汰。

    这是**跨策略**的集中度控制。选股引擎的 `apply_portfolio_overlay` 只在单个
    策略内部生效：7 个策略各自留 1 只银行，名单里仍然会有 7 只银行。这里补上
    这一层。

    行业为空的条目不参与配额（无法分组，强行淘汰会误伤），`pinned` 条目豁免且
    不占用名额。
    """
    if max_per_industry <= 0:
        return dict(entries), []

    kept: Dict[str, WatchlistEntry] = {}
    removed: List[Tuple[str, str]] = []
    industry_counts: Dict[str, int] = {}

    for entry in sort_entries(entries.values(), run_date=run_date):
        industry = normalize_industry(entry.industry)
        if entry.pinned or not industry:
            kept[entry.code] = entry
            continue
        used = industry_counts.get(industry, 0)
        if used < max_per_industry:
            industry_counts[industry] = used + 1
            kept[entry.code] = entry
        else:
            removed.append((entry.code, "industry_quota"))

    return kept, removed


def group_by_bucket(
    entries: Iterable[WatchlistEntry],
) -> Dict[str, Dict[str, WatchlistEntry]]:
    """按 bucket 分组，保证三个桶的 key 都存在（可能为空）。"""
    grouped: Dict[str, Dict[str, WatchlistEntry]] = {bucket: {} for bucket in SUPPORTED_BUCKETS}
    for entry in entries:
        grouped.setdefault(resolve_bucket(entry.bucket), {})[entry.code] = entry
    return grouped


def expire_entries(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    ttl_days: Any = None,
    max_size: Any = None,
    max_per_industry: Any = None,
    max_per_industry_total: Optional[int] = None,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """按 TTL、行业配额和容量上限淘汰名单，**每个 bucket 独立执行**。

    三个限额参数都接受标量（三桶统一）或 `{bucket: 值}` 映射；传 None 使用
    各桶的默认值。

    为什么按桶独立：`latest_score` 是策略硬筛存活池内的分位排名，防守票的 84 分
    和进攻票的 84 分不可比。全局裁剪会让两把不同的尺子争同一批名额——而防守策略
    在数量上占优（11 个策略里 4 个 defensive、4 个 balanced），进攻票会被系统性挤掉。

    桶内的三步顺序仍然是：TTL → 行业配额 → 容量，这样被行业配额腾出来的名额
    可以让给同桶其他行业的候选，而不是白白浪费。`pinned` 条目永不淘汰且不占名额。

    Returns:
        `(保留下来的名单, [(被淘汰代码, 原因)])`，原因取 `ttl`、`industry_quota`
        或 `capacity`。
    """
    kept: Dict[str, WatchlistEntry] = {}
    removed: List[Tuple[str, str]] = []

    # 先按**每条背书自己所属桶的 TTL** 剪枝：进攻策略的背书 14 天失效，
    # 防守策略的 45 天。条目只要还剩任一条有效背书就留下，全部失效才算 ttl 出局。
    # 剪枝必须在分桶之前，否则一条早已失效的进攻背书会继续把条目拉进进攻桶。
    surviving: Dict[str, WatchlistEntry] = {}
    for code, entry in entries.items():
        live: Dict[str, StrategyHit] = {}
        for name, hit in entry.strategies.items():
            hit_ttl = limit_for(
                ttl_days, hit.bucket,
                default=DEFAULT_TTL_DAYS_BY_BUCKET.get(hit.bucket, DEFAULT_TTL_DAYS),
            )
            hit_seen = parse_date(hit.last_seen) or parse_date(entry.last_seen)
            if hit_ttl > 0 and hit_seen is not None and (run_date - hit_seen).days > hit_ttl:
                continue
            live[name] = hit
        if len(live) != len(entry.strategies):
            entry.strategies = live
        if not live and not entry.pinned:
            removed.append((code, "ttl"))
            continue
        surviving[code] = entry

    for bucket, bucket_entries in group_by_bucket(surviving.values()).items():
        if not bucket_entries:
            continue
        bucket_kept, bucket_removed = _expire_one_bucket(
            bucket_entries,
            run_date=run_date,
            max_size=limit_for(
                max_size, bucket,
                default=DEFAULT_MAX_SIZE_BY_BUCKET.get(bucket, DEFAULT_MAX_SIZE),
            ),
            max_per_industry=limit_for(
                max_per_industry, bucket,
                default=DEFAULT_MAX_PER_INDUSTRY_BY_BUCKET.get(bucket, DEFAULT_MAX_PER_INDUSTRY),
            ),
        )
        kept.update(bucket_kept)
        removed.extend(bucket_removed)

    # 最后一道：全局行业上限。桶内配额只保证"每个清单不被一个行业占满"，
    # 但 9 只银行分散在两个桶里、每桶各留 2 只，整份名单仍会有 4 只银行。
    kept, global_removed = apply_global_industry_cap(
        kept,
        run_date=run_date,
        max_total=(
            DEFAULT_MAX_PER_INDUSTRY_TOTAL
            if max_per_industry_total is None
            else int(max_per_industry_total)
        ),
    )
    removed.extend(global_removed)

    return kept, removed


def apply_global_industry_cap(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    max_total: int,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """限制同一行业在**整份名单**里的总数，跨桶结算。

    难点在于名额不够时该淘汰谁：分数是各策略硬筛存活池内的分位排名，跨桶不可比，
    直接按分数全局排序正是分桶要避免的事。因此改成**按桶优先级轮流取**——
    每一轮从每个桶里取该行业排名最高的一只，取满为止。这样：

    - 每个含该行业的桶都能先保住自己最好的那只
    - 全程不做跨桶分数比较
    - 结果确定，与字典序和运行顺序无关

    `pinned` 与行业为空的条目豁免，理由同 `apply_industry_quota`。
    """
    if max_total <= 0:
        return dict(entries), []

    kept: Dict[str, WatchlistEntry] = {}
    removed: List[Tuple[str, str]] = []
    by_industry: Dict[str, Dict[str, List[WatchlistEntry]]] = {}

    for entry in entries.values():
        industry = normalize_industry(entry.industry)
        if entry.pinned or not industry:
            kept[entry.code] = entry
            continue
        by_industry.setdefault(industry, {}).setdefault(entry.bucket, []).append(entry)

    for buckets in by_industry.values():
        ordered = {
            bucket: sort_entries(rows, run_date=run_date)
            for bucket, rows in buckets.items()
        }
        selected: List[WatchlistEntry] = []
        depth = 0
        while len(selected) < max_total:
            progressed = False
            for bucket in BUCKET_PRIORITY:
                rows = ordered.get(bucket) or []
                if depth >= len(rows):
                    continue
                selected.append(rows[depth])
                progressed = True
                if len(selected) >= max_total:
                    break
            if not progressed:
                break
            depth += 1

        selected_codes = {entry.code for entry in selected}
        for rows in ordered.values():
            for entry in rows:
                if entry.code in selected_codes:
                    kept[entry.code] = entry
                else:
                    removed.append((entry.code, "industry_quota_global"))

    return kept, removed


def _expire_one_bucket(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    max_size: int,
    max_per_industry: int,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """单个 bucket 内的行业配额 → 容量两步淘汰。

    TTL 不在这里做：它按**每条策略背书自己的桶**结算，已在 `expire_entries`
    分桶之前完成，否则一条早已失效的进攻背书会继续把条目拉进进攻桶。
    """
    kept, removed = apply_industry_quota(
        dict(entries),
        run_date=run_date,
        max_per_industry=max_per_industry,
    )

    if max_size > 0:
        kept, capacity_removed = _trim_to_capacity_round_robin(
            kept, run_date=run_date, max_size=max_size
        )
        removed.extend(capacity_removed)

    return kept, removed


def _trim_to_capacity_round_robin(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    max_size: int,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """按策略轮流取票填满容量，而不是按 `latest_score` 全桶排序。

    为什么不能直接按分数排：`latest_score` 是**该策略硬筛存活池内的分位排名**，
    池子大小差一个数量级时分数完全不可比。实测同一桶内——

        volume_breakout  存活池  19 只  分数 70.13 ~ 82.22
        capital_heat     存活池  93 只  分数 68.93 ~ 73.84
        theme_momentum   存活池 128 只  分数 67.98 ~ 69.40

    `theme_momentum` 的**最高分低于 volume_breakout 的最低分**，按分数排序时
    它在 15 个名额里一个独立席位都拿不到（实测 13/15 是 volume_breakout）。
    这不是它选的票差，是 19 只池子里排第 3 和 128 只池子里排第 3 拿到的分位
    本就不同。分桶解决了跨桶不可比，这里解决桶内跨策略不可比。

    轮转规则：各策略按自己的分数排好队，然后轮流取第 1、第 2……直到填满。
    等价于均分席位，但策略数变化时自动适配，且某策略候选不足时名额自然流向
    其他策略而不是空置。**全程不跨策略比较分数**，只比较组内次序。

    多策略共同命中的票优先入选：三个独立策略同时背书是名单里最强的信号，
    不该因为在某一策略内排名靠后而被轮空。它只占一个名额，记在主策略名下。

    `pinned` 条目永不淘汰且不占名额。
    """
    survivors: Dict[str, WatchlistEntry] = {}
    pool: List[WatchlistEntry] = []
    for entry in sort_entries(entries.values(), run_date=run_date):
        if entry.pinned:
            survivors[entry.code] = entry
        else:
            pool.append(entry)

    # 多策略命中的先落座，再轮转分配剩下的名额。
    chosen: List[str] = []
    multi = [e for e in pool if len(e.strategies) > 1]
    for entry in sorted(multi, key=lambda e: (-len(e.strategies), -e.latest_score, e.code)):
        if len(chosen) >= max_size:
            break
        survivors[entry.code] = entry
        chosen.append(entry.code)

    # 每个策略一条按自身分数排序的队列；同一只票只会出现在它主策略的队列里，
    # 避免一只多策略票占掉多个策略的轮次。
    queues: Dict[str, List[WatchlistEntry]] = {}
    for entry in pool:
        if entry.code in survivors:
            continue
        primary = _primary_strategy(entry)
        queues.setdefault(primary, []).append(entry)
    for name, queue in queues.items():
        queue.sort(key=lambda e: (-_score_in_strategy(e, name), e.code))

    # 轮转顺序按策略名固定，保证同一份输入得到同一份名单。
    order = sorted(queues)
    cursor = {name: 0 for name in order}
    while len(chosen) < max_size:
        progressed = False
        for name in order:
            if len(chosen) >= max_size:
                break
            index = cursor[name]
            queue = queues[name]
            if index >= len(queue):
                continue
            entry = queue[index]
            cursor[name] = index + 1
            survivors[entry.code] = entry
            chosen.append(entry.code)
            progressed = True
        if not progressed:
            break

    removed = [
        (entry.code, "capacity") for entry in pool if entry.code not in survivors
    ]
    return survivors, removed


def _primary_strategy(entry: WatchlistEntry) -> str:
    """条目归属的策略：取分数最高的那条背书，平手按名字定序。"""
    if not entry.strategies:
        return ""
    return min(entry.strategies.items(), key=lambda kv: (-kv[1].score, kv[0]))[0]


def _score_in_strategy(entry: WatchlistEntry, strategy: str) -> float:
    hit = entry.strategies.get(strategy)
    return hit.score if hit is not None else entry.latest_score


def apply_pinned(entries: Mapping[str, WatchlistEntry], pinned_codes: Sequence[str]) -> Dict[str, WatchlistEntry]:
    """把手工固定的代码标记为 pinned，缺失的补一条占位条目。

    这样每周重扫时，手工盯的票不会因为某次没被任何策略选中就掉出名单。
    """
    result: Dict[str, WatchlistEntry] = {code: entry for code, entry in entries.items()}
    wanted = {str(code).strip() for code in pinned_codes if str(code).strip()}
    for code, entry in result.items():
        entry.pinned = code in wanted
    for code in wanted:
        if code not in result:
            result[code] = WatchlistEntry(code=code, pinned=True)
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def render_report(
    *,
    run_date: date,
    cadence: str,
    entries: Mapping[str, WatchlistEntry],
    added: Sequence[str],
    removed: Sequence[Tuple[str, str]],
    summaries: Sequence[RunSummary],
    max_rows: int = 30,
    known_entries: Mapping[str, WatchlistEntry] | None = None,
) -> str:
    """渲染 Markdown 周报/日报正文。

    Args:
        entries: 淘汰之后**留下**的名单。
        known_entries: 淘汰**之前**的名单，用来给已移出的条目查名称/行业——
            它们已经不在 `entries` 里了，只报代码看不出移走的是什么。
            省略时移出段落只显示代码。
    """
    lines: List[str] = []
    lines.append(f"# 全市场扫描观察名单（{cadence}）")
    lines.append("")
    lines.append(f"- 扫描日期：{format_date(run_date)}")
    grouped = group_by_bucket(entries.values())
    distribution = "｜".join(
        f"{BUCKET_LABELS.get(bucket, bucket)} {len(grouped[bucket])}"
        for bucket in SUPPORTED_BUCKETS
    )
    lines.append(f"- 名单规模：{len(entries)}（{distribution}）")
    lines.append(f"- 本次新进：{len(added)}｜本次移出：{len(removed)}")
    total_elapsed = sum(item.elapsed_sec for item in summaries)
    lines.append(f"- 策略数：{len(summaries)}｜总耗时：{total_elapsed:.1f}s")
    lines.append(f"- {industry_quota_diagnostic(entries, removed).text}")
    lines.append("")

    failed = [item for item in summaries if item.error]
    if failed:
        lines.append("## 失败策略")
        lines.append("")
        for item in failed:
            lines.append(f"- `{item.strategy}`：{item.error}")
        lines.append("")

    if added:
        lines.append("## 新进入观察名单")
        lines.append("")
        lines.append("| 桶 | 代码 | 名称 | 行业 | 分数 | 策略 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for code in added:
            entry = entries.get(code)
            if entry is None:
                continue
            lines.append(
                f"| {BUCKET_LABELS.get(entry.bucket, entry.bucket)} | {entry.code} | "
                f"{entry.name} | {entry.industry} | "
                f"{entry.latest_score:.2f} | {', '.join(sorted(entry.strategies))} |"
            )
        lines.append("")

    if removed:
        lines.append("## 移出观察名单")
        lines.append("")
        reason_text = {
            "ttl": "超过留存期",
            "industry_quota": "同行业在该桶已满额",
            "industry_quota_global": "同行业已达全局上限",
            "capacity": "超出名单容量",
        }
        lookup: Mapping[str, WatchlistEntry] = known_entries or entries
        for code, reason in removed:
            entry = lookup.get(code)
            label = f"{code} {entry.name}".strip() if entry and entry.name else code
            industry = f"，{entry.industry}" if entry and entry.industry else ""
            lines.append(f"- {label}{industry}（{reason_text.get(reason, reason)}）")
        lines.append("")

    # 按桶分段：分数是各策略池内的分位排名，跨桶排在一张表里会误导读者
    # 以为「进攻票 78 分」不如「防守票 84 分」。
    for bucket in SUPPORTED_BUCKETS:
        bucket_entries = grouped.get(bucket) or {}
        if not bucket_entries:
            continue
        label = BUCKET_LABELS.get(bucket, bucket)
        lines.append(f"## 当前名单 · {label}（{len(bucket_entries)}）")
        lines.append("")
        lines.append("| # | 代码 | 名称 | 行业 | 最近分 | 命中日数 | 首次入选 | 最近入选 | 策略 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        ordered = sort_entries(bucket_entries.values(), run_date=run_date)
        for index, entry in enumerate(ordered[:max_rows], start=1):
            flag = "📌 " if entry.pinned else ""
            # 同时符合多个桶的票只在主桶列一行、只占一个名额，在这里标注它的其余属性。
            also = entry.secondary_buckets
            also_text = (
                "（兼 " + "、".join(BUCKET_LABELS.get(b, b) for b in also) + "）" if also else ""
            )
            lines.append(
                f"| {index} | {flag}{entry.code}{also_text} | {entry.name} | {entry.industry} | "
                f"{entry.latest_score:.2f} | {entry.hit_count} | {entry.first_seen} | "
                f"{entry.last_seen} | {', '.join(sorted(entry.strategies))} |"
            )
        if len(ordered) > max_rows:
            lines.append("")
            lines.append(f"> 该桶仅显示前 {max_rows} 条，完整名单见 `data/watchlist/current.csv`。")
        lines.append("")

    if not entries:
        lines.append("## 当前名单")
        lines.append("")
        lines.append("_本次名单为空。_")
        lines.append("")
    else:
        lines.append(
            "> 分数是各策略硬筛存活池内的分位排名，**不可跨桶比较**。"
            "标「兼 X」的票同时符合多个桶，只在主桶计一个名额。"
        )
        lines.append("")

    lines.append("## 策略耗时")
    lines.append("")
    lines.append("| 策略 | 持有周期 | 耗时(s) | 快照 | 硬筛后 | 入选 | 日线补齐 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in summaries:
        lines.append(
            f"| {item.strategy} | {item.holding_period} | {item.elapsed_sec:.1f} | "
            f"{item.snapshot_count} | {item.after_filter_count} | {item.pick_count} | "
            f"{'是' if item.daily_enriched else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


def update_timing_log(
    path: Path,
    summaries: Sequence[RunSummary],
    *,
    run_date: date,
    cadence: str,
    keep_runs: int = 20,
) -> Dict[str, Any]:
    """把本次各策略耗时追加到 timing.json，保留最近 keep_runs 次。

    这份数据是"按实测耗时调频"的依据：某个策略持续逼近 workflow 超时，
    就把它从 daily 降到 weekly，或调小 `DAILY_ENRICH_MAX_CANDIDATES`。
    """
    payload: Dict[str, Any] = {"runs": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping) and isinstance(loaded.get("runs"), list):
                payload = {"runs": list(loaded["runs"])}
        except (OSError, ValueError) as exc:
            logger.warning("耗时记录读取失败，按空记录重建: path=%s err=%s", path, exc)

    payload["runs"].append({
        "run_date": format_date(run_date),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "cadence": cadence,
        "total_elapsed_sec": round(sum(item.elapsed_sec for item in summaries), 2),
        "strategies": [item.to_dict() for item in summaries],
    })
    if keep_runs > 0:
        payload["runs"] = payload["runs"][-keep_runs:]
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _render_csv(rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再替换，避免 workflow 被取消时留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
