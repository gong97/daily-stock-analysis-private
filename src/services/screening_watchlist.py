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

DEFAULT_TTL_DAYS = 30
DEFAULT_MAX_SIZE = 60

# 名单排序权重：以最近一次得分为主，命中多个策略/多次入选加分，长时间没再被选中扣分。
_HIT_BONUS_PER_EXTRA_HIT = 2.0
_MAX_BONUS_HITS = 5
_STALENESS_PENALTY_PER_DAY = 0.5

_DATE_FMT = "%Y-%m-%d"

WATCHLIST_SCHEMA_VERSION = 1


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
class WatchlistEntry:
    """观察名单中的一只股票。"""

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
    strategies: Dict[str, float] = field(default_factory=dict)
    pinned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "holding_period": self.holding_period,
            "cadence": self.cadence,
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
            "strategies": dict(self.strategies),
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WatchlistEntry":
        code = str(raw.get("code") or "").strip()
        if not code:
            raise ValueError("watchlist entry missing code")
        strategies_raw = raw.get("strategies") or {}
        strategies: Dict[str, float] = {}
        if isinstance(strategies_raw, Mapping):
            for key, value in strategies_raw.items():
                strategies[str(key)] = _as_float(value, 0.0) or 0.0
        elif isinstance(strategies_raw, (list, tuple)):
            # 兼容早期把 strategies 存成名字列表的快照。
            for key in strategies_raw:
                strategies[str(key)] = 0.0
        return cls(
            code=code,
            name=str(raw.get("name") or ""),
            holding_period=str(raw.get("holding_period") or ""),
            cadence=str(raw.get("cadence") or FALLBACK_CADENCE),
            industry=str(raw.get("industry") or ""),
            first_seen=str(raw.get("first_seen") or ""),
            last_seen=str(raw.get("last_seen") or ""),
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
        "code", "name", "industry", "cadence", "holding_period",
        "first_seen", "last_seen", "hit_count", "latest_score", "best_score",
        "risk_level", "strategies", "pinned",
    ]
    lines: List[List[str]] = [columns]
    for entry in sort_entries(entries.values(), run_date=None):
        lines.append([
            entry.code,
            entry.name,
            entry.industry,
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
    """按名单排序输出 STOCK_LIST 用的代码序列。"""
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
) -> Tuple[Dict[str, WatchlistEntry], List[str]]:
    """把一次扫描的候选并入名单。

    同一只票被多个策略选中时按最高分记录 `latest_score`，`hit_count` 每次扫描
    最多 +1（不按策略数重复累加），避免多策略同时命中把命中次数灌水。

    Returns:
        `(合并后的名单, 本次新进入的代码)`
    """
    merged: Dict[str, WatchlistEntry] = {code: entry for code, entry in entries.items()}
    run_date_text = format_date(run_date)
    added: List[str] = []
    seen_this_run: set = set()

    for strategy, picks in picks_by_strategy.items():
        holding_period = str(holding_periods.get(strategy, "") or "")
        cadence = resolve_cadence(holding_period, cadence_map)
        for pick in picks or []:
            code = str(pick.get("code") or "").strip()
            if not code:
                continue
            score = _as_float(pick.get("final_score"), 0.0) or 0.0
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
                entry.hit_count += 1
                # 同一次扫描内先归零，后续策略再按最高分覆盖。
                entry.latest_score = score
                entry.latest_rank = _as_optional_int(pick.get("rank"))
            elif score > entry.latest_score:
                entry.latest_score = score
                entry.latest_rank = _as_optional_int(pick.get("rank"))

            entry.last_seen = run_date_text
            entry.best_score = max(entry.best_score, score)
            entry.strategies[strategy] = score
            entry.cadence = cadence
            entry.holding_period = holding_period
            entry.name = str(pick.get("name") or "") or entry.name
            entry.industry = str(pick.get("industry") or "") or entry.industry
            entry.price = _as_float(pick.get("price"), entry.price)
            entry.change_pct = _as_float(pick.get("change_pct"), entry.change_pct)
            entry.risk_level = str(pick.get("risk_level") or "") or entry.risk_level
            risk_flags = pick.get("risk_flags") or []
            if risk_flags:
                entry.risk_flags = [str(item) for item in risk_flags]

    return merged, added


def expire_entries(
    entries: Mapping[str, WatchlistEntry],
    *,
    run_date: date,
    ttl_days: int = DEFAULT_TTL_DAYS,
    max_size: int = DEFAULT_MAX_SIZE,
) -> Tuple[Dict[str, WatchlistEntry], List[Tuple[str, str]]]:
    """按 TTL 和容量上限淘汰名单，`pinned` 条目永不淘汰。

    Returns:
        `(保留下来的名单, [(被淘汰代码, 原因)])`，原因取 `ttl` 或 `capacity`。
    """
    kept: Dict[str, WatchlistEntry] = {}
    removed: List[Tuple[str, str]] = []

    for code, entry in entries.items():
        if entry.pinned:
            kept[code] = entry
            continue
        last_seen = parse_date(entry.last_seen)
        if ttl_days > 0 and last_seen is not None and (run_date - last_seen).days > ttl_days:
            removed.append((code, "ttl"))
            continue
        kept[code] = entry

    if max_size > 0:
        survivors: Dict[str, WatchlistEntry] = {}
        non_pinned_kept = 0
        for entry in sort_entries(kept.values(), run_date=run_date):
            if entry.pinned:
                survivors[entry.code] = entry
                continue
            if non_pinned_kept < max_size:
                survivors[entry.code] = entry
                non_pinned_kept += 1
            else:
                removed.append((entry.code, "capacity"))
        kept = survivors

    return kept, removed


def apply_pinned(entries: Mapping[str, WatchlistEntry], pinned_codes: Sequence[str]) -> Dict[str, WatchlistEntry]:
    """把手工固定的代码标记为 pinned，缺失的补一条占位条目。

    这样"扫描结果写回 STOCK_LIST"不会把手工加的票冲掉。
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
) -> str:
    """渲染 Markdown 周报/日报正文。"""
    lines: List[str] = []
    lines.append(f"# 全市场扫描观察名单（{cadence}）")
    lines.append("")
    lines.append(f"- 扫描日期：{format_date(run_date)}")
    lines.append(f"- 名单规模：{len(entries)}")
    lines.append(f"- 本次新进：{len(added)}｜本次移出：{len(removed)}")
    total_elapsed = sum(item.elapsed_sec for item in summaries)
    lines.append(f"- 策略数：{len(summaries)}｜总耗时：{total_elapsed:.1f}s")
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
        lines.append("| 代码 | 名称 | 行业 | 分数 | 策略 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for code in added:
            entry = entries.get(code)
            if entry is None:
                continue
            lines.append(
                f"| {entry.code} | {entry.name} | {entry.industry} | "
                f"{entry.latest_score:.2f} | {', '.join(sorted(entry.strategies))} |"
            )
        lines.append("")

    if removed:
        lines.append("## 移出观察名单")
        lines.append("")
        reason_text = {"ttl": "超过留存期", "capacity": "超出名单容量"}
        for code, reason in removed:
            lines.append(f"- {code}（{reason_text.get(reason, reason)}）")
        lines.append("")

    lines.append("## 当前名单")
    lines.append("")
    lines.append("| # | 代码 | 名称 | 行业 | 最近分 | 命中 | 首次入选 | 最近入选 | 策略 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, entry in enumerate(sort_entries(entries.values(), run_date=run_date)[:max_rows], start=1):
        flag = "📌 " if entry.pinned else ""
        lines.append(
            f"| {index} | {flag}{entry.code} | {entry.name} | {entry.industry} | "
            f"{entry.latest_score:.2f} | {entry.hit_count} | {entry.first_seen} | "
            f"{entry.last_seen} | {', '.join(sorted(entry.strategies))} |"
        )
    if len(entries) > max_rows:
        lines.append("")
        lines.append(f"> 仅显示前 {max_rows} 条，完整名单见 `data/watchlist/current.csv`。")
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
