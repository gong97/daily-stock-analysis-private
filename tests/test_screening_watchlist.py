# -*- coding: utf-8 -*-
"""观察名单纯逻辑回归：分层、合并、淘汰、持久化与报告。

这些用例不依赖网络、数据库和选股引擎的 dataclass，只覆盖
`src/services/screening_watchlist.py` 的确定性行为。
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from src.services.screening_watchlist import (
    BUCKET_AGGRESSIVE,
    BUCKET_BALANCED,
    BUCKET_DEFENSIVE,
    DEFAULT_MAX_SIZE_BY_BUCKET,
    DEFAULT_TTL_DAYS_BY_BUCKET,
    FALLBACK_BUCKET,
    SUPPORTED_BUCKETS,
    group_by_bucket,
    limit_for,
    parse_bucket_limits,
    resolve_bucket,
    CADENCE_DAILY,
    CADENCE_WEEKLY,
    DEFAULT_CADENCE_MAP,
    FALLBACK_CADENCE,
    RunSummary,
    WatchlistEntry,
    DEFAULT_MAX_PER_INDUSTRY,
    apply_industry_quota,
    apply_pinned,
    expire_entries,
    load_pinned_codes,
    load_watchlist,
    merge_run,
    parse_cadence_map,
    render_report,
    resolve_cadence,
    save_watchlist,
    save_watchlist_csv,
    select_strategies,
    to_stock_list,
    update_timing_log,
)

pytestmark = pytest.mark.unit

RUN_DATE = date(2026, 8, 28)


def _pick(code: str, *, score: float, rank: int = 1, name: str = "", industry: str = "") -> dict:
    return {
        "rank": rank,
        "code": code,
        "name": name or f"股票{code}",
        "final_score": score,
        "screen_score": score,
        "industry": industry or "测试行业",
        "price": 12.34,
        "change_pct": 1.5,
        "risk_level": "low",
        "risk_flags": [],
    }


def _info(name: str, holding_period: str, *, market_scope=("cn",)) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        style={"holding_period": holding_period},
        market_scope=list(market_scope),
    )


# ---------------------------------------------------------------------------
# 分层
# ---------------------------------------------------------------------------
def test_default_cadence_map_matches_builtin_holding_periods():
    assert DEFAULT_CADENCE_MAP["short_term"] == CADENCE_DAILY
    assert DEFAULT_CADENCE_MAP["swing"] == CADENCE_WEEKLY
    assert DEFAULT_CADENCE_MAP["watchlist"] == CADENCE_WEEKLY


def test_resolve_cadence_falls_back_for_unknown_holding_period():
    assert resolve_cadence("short_term", DEFAULT_CADENCE_MAP) == CADENCE_DAILY
    assert resolve_cadence("", DEFAULT_CADENCE_MAP) == FALLBACK_CADENCE
    assert resolve_cadence("未来某种新周期", DEFAULT_CADENCE_MAP) == FALLBACK_CADENCE


def test_parse_cadence_map_overrides_and_skips_invalid_items():
    mapping = parse_cadence_map("swing:daily, bogus, watchlist:hourly, :weekly, short_term:weekly")
    assert mapping["swing"] == CADENCE_DAILY
    assert mapping["short_term"] == CADENCE_WEEKLY
    # 非法 cadence 不会覆盖默认值
    assert mapping["watchlist"] == CADENCE_WEEKLY


def test_parse_cadence_map_empty_returns_defaults():
    assert parse_cadence_map("") == DEFAULT_CADENCE_MAP
    assert parse_cadence_map(None) == DEFAULT_CADENCE_MAP


def test_select_strategies_filters_by_cadence_market_and_sorts():
    infos = [
        _info("volume_breakout", "short_term"),
        _info("balanced_alpha", "watchlist"),
        _info("capital_heat", "short_term"),
        _info("shrink_pullback", "swing"),
        _info("us_only", "short_term", market_scope=("us",)),
    ]
    daily = select_strategies(infos, cadence="daily", cadence_map=DEFAULT_CADENCE_MAP, market="cn")
    assert daily == [("capital_heat", "short_term"), ("volume_breakout", "short_term")]

    weekly = select_strategies(infos, cadence="weekly", cadence_map=DEFAULT_CADENCE_MAP, market="cn")
    assert [name for name, _ in weekly] == ["balanced_alpha", "shrink_pullback"]

    everything = select_strategies(infos, cadence="all", cadence_map=DEFAULT_CADENCE_MAP, market="cn")
    assert len(everything) == 4  # us_only 被 market_scope 过滤掉


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def test_merge_run_creates_entries_and_reports_added():
    entries, added = merge_run(
        {},
        {"dual_low": [_pick("600519", score=80.0)]},
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    assert added == ["600519"]
    entry = entries["600519"]
    assert entry.first_seen == "2026-08-28"
    assert entry.last_seen == "2026-08-28"
    assert entry.hit_count == 1
    assert entry.latest_score == 80.0
    assert entry.best_score == 80.0
    assert entry.cadence == CADENCE_WEEKLY
    assert entry.strategies == {"dual_low": 80.0}


def test_merge_run_counts_one_hit_per_run_even_with_multiple_strategies():
    """同一次扫描里被多个策略同时选中，只算一次命中，但记录所有策略。"""
    entries, _ = merge_run(
        {},
        {
            "dual_low": [_pick("600519", score=70.0)],
            "quality_value": [_pick("600519", score=85.0)],
        },
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist", "quality_value": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    entry = entries["600519"]
    assert entry.hit_count == 1
    assert entry.latest_score == 85.0  # 取本次的最高分
    assert sorted(entry.strategies) == ["dual_low", "quality_value"]


def test_merge_run_takes_cadence_from_highest_scoring_strategy():
    """cadence / holding_period 跟随最高分策略，与策略跑动顺序无关。"""
    high = ("capital_heat", "short_term", 90.0)   # → daily
    low = ("momentum_quality", "swing", 10.0)     # → weekly
    for first, second in ((high, low), (low, high)):
        entries, _ = merge_run(
            {},
            {name: [_pick("600519", score=score)] for name, _, score in (first, second)},
            run_date=RUN_DATE,
            holding_periods={name: period for name, period, _ in (first, second)},
            cadence_map=DEFAULT_CADENCE_MAP,
        )
        entry = entries["600519"]
        assert entry.latest_score == 90.0
        assert entry.cadence == "daily"
        assert entry.holding_period == "short_term"


def test_merge_run_second_run_increments_hit_and_keeps_best_score():
    entries, _ = merge_run(
        {},
        {"dual_low": [_pick("600519", score=90.0)]},
        run_date=date(2026, 8, 21),
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    entries, added = merge_run(
        entries,
        {"dual_low": [_pick("600519", score=75.0)]},
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    entry = entries["600519"]
    assert added == []  # 不是新进入
    assert entry.hit_count == 2
    assert entry.first_seen == "2026-08-21"
    assert entry.last_seen == "2026-08-28"
    assert entry.latest_score == 75.0
    assert entry.best_score == 90.0


def test_merge_run_ignores_picks_without_code():
    entries, added = merge_run(
        {},
        {"dual_low": [{"code": "", "final_score": 10.0}, {"final_score": 20.0}]},
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    assert entries == {}
    assert added == []


def test_merge_run_does_not_mutate_input_mapping():
    original = {}
    entries, _ = merge_run(
        original,
        {"dual_low": [_pick("600519", score=80.0)]},
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    assert original == {}
    assert "600519" in entries


# ---------------------------------------------------------------------------
# 淘汰
# ---------------------------------------------------------------------------
def test_expire_entries_drops_stale_codes():
    entries = {
        "600519": WatchlistEntry(code="600519", last_seen="2026-08-27"),
        "000001": WatchlistEntry(code="000001", last_seen="2026-06-01"),
    }
    kept, removed = expire_entries(entries, run_date=RUN_DATE, ttl_days=30, max_size=0)
    assert set(kept) == {"600519"}
    assert removed == [("000001", "ttl")]


def test_expire_entries_ttl_zero_disables_expiry():
    entries = {"000001": WatchlistEntry(code="000001", last_seen="2020-01-01")}
    kept, removed = expire_entries(entries, run_date=RUN_DATE, ttl_days=0, max_size=0)
    assert set(kept) == {"000001"}
    assert removed == []


def test_expire_entries_enforces_capacity_by_rank():
    entries = {
        code: WatchlistEntry(code=code, last_seen="2026-08-28", latest_score=score, hit_count=1)
        for code, score in [("000001", 50.0), ("000002", 90.0), ("000003", 70.0)]
    }
    kept, removed = expire_entries(entries, run_date=RUN_DATE, ttl_days=0, max_size=2)
    assert set(kept) == {"000002", "000003"}
    assert removed == [("000001", "capacity")]


def test_expire_entries_never_drops_pinned_and_pinned_does_not_consume_quota():
    entries = {
        "600519": WatchlistEntry(code="600519", last_seen="2020-01-01", pinned=True),
        "000002": WatchlistEntry(code="000002", last_seen="2026-08-28", latest_score=90.0),
        "000003": WatchlistEntry(code="000003", last_seen="2026-08-28", latest_score=70.0),
    }
    kept, removed = expire_entries(entries, run_date=RUN_DATE, ttl_days=30, max_size=2)
    # pinned 既不因 TTL 掉队，也不占用 max_size 名额
    assert set(kept) == {"600519", "000002", "000003"}
    assert removed == []


def test_apply_pinned_marks_existing_and_adds_placeholder():
    entries = {"600519": WatchlistEntry(code="600519", pinned=True)}
    result = apply_pinned(entries, ["000001"])
    assert result["600519"].pinned is False  # 已从 pinned.txt 移除
    assert result["000001"].pinned is True
    assert result["000001"].code == "000001"


def test_load_pinned_codes_skips_comments_and_blank_lines(tmp_path):
    path = tmp_path / "pinned.txt"
    path.write_text("# 手工长期跟踪\n600519\n\n000001  # 平安银行\n", encoding="utf-8")
    assert load_pinned_codes(path) == ["600519", "000001"]


def test_load_pinned_codes_missing_file_returns_empty(tmp_path):
    assert load_pinned_codes(tmp_path / "nope.txt") == []


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def test_save_and_load_watchlist_round_trip(tmp_path):
    path = tmp_path / "current.json"
    entries = {
        "600519": WatchlistEntry(
            code="600519",
            name="贵州茅台",
            industry="白酒",
            first_seen="2026-08-01",
            last_seen="2026-08-28",
            hit_count=3,
            latest_score=88.5,
            best_score=91.0,
            strategies={"dual_low": 88.5},
        )
    }
    save_watchlist(path, entries, {"last_run_cadence": "weekly"})
    loaded, meta = load_watchlist(path)
    assert meta["last_run_cadence"] == "weekly"
    assert loaded["600519"].name == "贵州茅台"
    assert loaded["600519"].hit_count == 3
    assert loaded["600519"].strategies == {"dual_low": 88.5}


def test_load_watchlist_missing_or_corrupt_file_is_fail_open(tmp_path):
    assert load_watchlist(tmp_path / "nope.json") == ({}, {})
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_watchlist(broken) == ({}, {})


def test_load_watchlist_tolerates_legacy_strategy_list(tmp_path):
    path = tmp_path / "current.json"
    path.write_text(
        json.dumps({"entries": [{"code": "600519", "strategies": ["dual_low"]}]}),
        encoding="utf-8",
    )
    loaded, _ = load_watchlist(path)
    assert loaded["600519"].strategies == {"dual_low": 0.0}


def test_save_watchlist_csv_writes_header_and_rows(tmp_path):
    path = tmp_path / "current.csv"
    save_watchlist_csv(path, {"600519": WatchlistEntry(code="600519", name="贵州茅台")})
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("code,name,industry")
    assert "600519" in text


def test_to_stock_list_orders_pinned_first_then_by_rank():
    entries = {
        "000001": WatchlistEntry(code="000001", last_seen="2026-08-28", latest_score=95.0),
        "000002": WatchlistEntry(code="000002", last_seen="2026-08-28", latest_score=60.0),
        "600519": WatchlistEntry(code="600519", last_seen="2026-08-28", latest_score=10.0, pinned=True),
    }
    assert to_stock_list(entries, run_date=RUN_DATE) == ["600519", "000001", "000002"]


def test_update_timing_log_appends_and_truncates(tmp_path):
    path = tmp_path / "timing.json"
    for day in range(1, 6):
        update_timing_log(
            path,
            [RunSummary(strategy="dual_low", cadence="weekly", elapsed_sec=float(day))],
            run_date=date(2026, 8, day),
            cadence="weekly",
            keep_runs=3,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 3
    assert payload["runs"][-1]["run_date"] == "2026-08-05"
    assert payload["runs"][-1]["total_elapsed_sec"] == 5.0


def test_update_timing_log_rebuilds_after_corrupt_file(tmp_path):
    path = tmp_path / "timing.json"
    path.write_text("not json at all", encoding="utf-8")
    payload = update_timing_log(
        path,
        [RunSummary(strategy="dual_low", cadence="weekly", elapsed_sec=1.0)],
        run_date=RUN_DATE,
        cadence="weekly",
    )
    assert len(payload["runs"]) == 1


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def test_render_report_contains_core_sections():
    entries = {
        "600519": WatchlistEntry(
            code="600519",
            name="贵州茅台",
            industry="白酒",
            first_seen="2026-08-28",
            last_seen="2026-08-28",
            hit_count=1,
            latest_score=88.0,
            strategies={"dual_low": 88.0},
        )
    }
    report = render_report(
        run_date=RUN_DATE,
        cadence="weekly",
        entries=entries,
        added=["600519"],
        removed=[("000001", "ttl")],
        summaries=[
            RunSummary(strategy="dual_low", cadence="weekly", holding_period="watchlist",
                       elapsed_sec=12.5, snapshot_count=5400, after_filter_count=210, pick_count=5),
            RunSummary(strategy="broken", cadence="weekly", error="RuntimeError: 数据源不可用"),
        ],
    )
    assert "新进入观察名单" in report
    assert "移出观察名单" in report
    assert "超过留存期" in report
    assert "失败策略" in report
    assert "RuntimeError: 数据源不可用" in report
    assert "策略耗时" in report
    assert "600519" in report
    assert "贵州茅台" in report


def test_render_report_without_changes_still_renders_current_list():
    report = render_report(
        run_date=RUN_DATE,
        cadence="daily",
        entries={},
        added=[],
        removed=[],
        summaries=[],
    )
    assert "当前名单" in report
    assert "失败策略" not in report

# ---------------------------------------------------------------------------
# 跨策略行业配额
# ---------------------------------------------------------------------------
def _entry(code: str, industry: str, score: float, *, pinned: bool = False) -> WatchlistEntry:
    return WatchlistEntry(
        code=code,
        industry=industry,
        last_seen="2026-08-28",
        latest_score=score,
        hit_count=1,
        pinned=pinned,
    )


def test_industry_quota_keeps_only_top_n_per_industry():
    """7 个策略各选 1 只银行时，名单里只应留下分最高的两只。"""
    entries = {
        e.code: e
        for e in [
            _entry("601166", "银行", 86.24),
            _entry("601229", "银行", 84.37),
            _entry("600015", "银行", 84.08),
            _entry("000001", "银行", 83.79),
            _entry("600309", "化工", 76.56),
        ]
    }
    kept, removed = apply_industry_quota(entries, run_date=RUN_DATE, max_per_industry=2)
    assert set(kept) == {"601166", "601229", "600309"}
    assert sorted(removed) == [("000001", "industry_quota"), ("600015", "industry_quota")]


def test_industry_quota_zero_disables_the_limit():
    entries = {e.code: e for e in [_entry("A", "银行", 90.0), _entry("B", "银行", 80.0)]}
    kept, removed = apply_industry_quota(entries, run_date=RUN_DATE, max_per_industry=0)
    assert set(kept) == {"A", "B"}
    assert removed == []


def test_industry_quota_exempts_blank_industry():
    """行业为空时无法分组，强行淘汰会误伤——全部保留。"""
    entries = {e.code: e for e in [_entry(code, "", 80.0) for code in ("A", "B", "C")]}
    kept, removed = apply_industry_quota(entries, run_date=RUN_DATE, max_per_industry=1)
    assert set(kept) == {"A", "B", "C"}
    assert removed == []


def test_industry_quota_exempts_pinned_and_does_not_consume_quota():
    entries = {
        e.code: e
        for e in [
            _entry("P", "银行", 10.0, pinned=True),
            _entry("A", "银行", 90.0),
            _entry("B", "银行", 80.0),
            _entry("C", "银行", 70.0),
        ]
    }
    kept, removed = apply_industry_quota(entries, run_date=RUN_DATE, max_per_industry=2)
    assert set(kept) == {"P", "A", "B"}
    assert removed == [("C", "industry_quota")]


def test_industry_quota_groups_case_and_whitespace_insensitively():
    entries = {
        e.code: e
        for e in [_entry("A", "银行", 90.0), _entry("B", " 银行 ", 80.0), _entry("C", "银行", 70.0)]
    }
    kept, removed = apply_industry_quota(entries, run_date=RUN_DATE, max_per_industry=1)
    assert set(kept) == {"A"}
    assert len(removed) == 2


def test_expire_entries_runs_industry_quota_before_capacity():
    """行业配额腾出的名额应让给其他行业，而不是浪费掉。"""
    entries = {
        e.code: e
        for e in [
            _entry("BANK1", "银行", 90.0),
            _entry("BANK2", "银行", 88.0),
            _entry("BANK3", "银行", 86.0),
            _entry("CHEM1", "化工", 70.0),
            _entry("STEEL1", "钢铁", 60.0),
        ]
    }
    kept, removed = expire_entries(
        entries, run_date=RUN_DATE, ttl_days=0, max_size=4, max_per_industry=2
    )
    # BANK3 被行业配额淘汰，空出的名额给了 STEEL1，而不是留空
    assert set(kept) == {"BANK1", "BANK2", "CHEM1", "STEEL1"}
    assert removed == [("BANK3", "industry_quota")]


def test_default_max_per_industry_is_active_by_default():
    assert DEFAULT_MAX_PER_INDUSTRY > 0

# ---------------------------------------------------------------------------
# 分桶
# ---------------------------------------------------------------------------
def _bucketed(code: str, bucket: str, score: float, *, industry: str = "测试",
              last_seen: str = "2026-08-28", pinned: bool = False) -> WatchlistEntry:
    return WatchlistEntry(
        code=code, bucket=bucket, industry=industry, last_seen=last_seen,
        latest_score=score, hit_count=1, pinned=pinned,
    )


def test_resolve_bucket_maps_risk_profile_and_falls_back():
    assert resolve_bucket("defensive") == BUCKET_DEFENSIVE
    assert resolve_bucket("AGGRESSIVE") == BUCKET_AGGRESSIVE
    assert resolve_bucket(" balanced ") == BUCKET_BALANCED
    assert resolve_bucket("") == FALLBACK_BUCKET
    assert resolve_bucket(None) == FALLBACK_BUCKET
    assert resolve_bucket("某种新风格") == FALLBACK_BUCKET


def test_merge_run_takes_bucket_from_highest_scoring_strategy():
    """bucket 与 cadence 走同一条路径：跟随最高分策略，与跑动顺序无关。"""
    high = ("theme_momentum", "short_term", "aggressive", 90.0)
    low = ("dual_low", "watchlist", "defensive", 10.0)
    for first, second in ((high, low), (low, high)):
        entries, _ = merge_run(
            {},
            {name: [_pick("600519", score=score)] for name, _, _, score in (first, second)},
            run_date=RUN_DATE,
            holding_periods={name: hp for name, hp, _, _ in (first, second)},
            cadence_map=DEFAULT_CADENCE_MAP,
            risk_profiles={name: rp for name, _, rp, _ in (first, second)},
        )
        entry = entries["600519"]
        assert entry.latest_score == 90.0
        assert entry.bucket == BUCKET_AGGRESSIVE


def test_merge_run_without_risk_profiles_uses_fallback_bucket():
    entries, _ = merge_run(
        {},
        {"dual_low": [_pick("600519", score=80.0)]},
        run_date=RUN_DATE,
        holding_periods={"dual_low": "watchlist"},
        cadence_map=DEFAULT_CADENCE_MAP,
    )
    assert entries["600519"].bucket == FALLBACK_BUCKET


def test_parse_bucket_limits_scalar_bucket_and_mixed_forms():
    assert parse_bucket_limits("", defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=30) ==         dict(DEFAULT_TTL_DAYS_BY_BUCKET)

    uniform = parse_bucket_limits("7", defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=30)
    assert set(uniform.values()) == {7}

    mixed = parse_bucket_limits("7,aggressive:3", defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=30)
    assert mixed == {BUCKET_DEFENSIVE: 7, BUCKET_BALANCED: 7, BUCKET_AGGRESSIVE: 3}


def test_parse_bucket_limits_is_order_independent():
    """逐桶配置必须覆盖统一默认值，不论谁先出现。"""
    a = parse_bucket_limits("aggressive:3,7", defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=30)
    b = parse_bucket_limits("7,aggressive:3", defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=30)
    assert a == b == {BUCKET_DEFENSIVE: 7, BUCKET_BALANCED: 7, BUCKET_AGGRESSIVE: 3}


def test_parse_bucket_limits_skips_invalid_items():
    limits = parse_bucket_limits(
        "bogus:5, aggressive:abc, defensive:-1, balanced:9",
        defaults=DEFAULT_MAX_SIZE_BY_BUCKET,
        scalar_default=60,
    )
    assert limits[BUCKET_BALANCED] == 9
    assert limits[BUCKET_DEFENSIVE] == DEFAULT_MAX_SIZE_BY_BUCKET[BUCKET_DEFENSIVE]
    assert limits[BUCKET_AGGRESSIVE] == DEFAULT_MAX_SIZE_BY_BUCKET[BUCKET_AGGRESSIVE]


def test_limit_for_accepts_scalar_and_mapping():
    assert limit_for(5, BUCKET_AGGRESSIVE, default=99) == 5
    assert limit_for({BUCKET_AGGRESSIVE: 3}, BUCKET_AGGRESSIVE, default=99) == 3
    assert limit_for(None, BUCKET_AGGRESSIVE, default=99) == 99
    # 映射里缺该桶时退到 FALLBACK_BUCKET，再退到 default
    assert limit_for({BUCKET_BALANCED: 4}, BUCKET_AGGRESSIVE, default=99) == 4


def test_group_by_bucket_always_returns_all_buckets():
    grouped = group_by_bucket([_bucketed("A", BUCKET_AGGRESSIVE, 80.0)])
    assert set(grouped) == set(SUPPORTED_BUCKETS)
    assert set(grouped[BUCKET_AGGRESSIVE]) == {"A"}
    assert grouped[BUCKET_DEFENSIVE] == {}


def test_expire_entries_partitions_by_bucket():
    """核心不变量：容量在桶内独立结算，进攻票不会被数量占优的防守票挤掉。"""
    entries = {}
    for i in range(4):
        e = _bucketed(f"D{i}", BUCKET_DEFENSIVE, 90.0 - i, industry=f"防守行业{i}")
        entries[e.code] = e
    for i in range(2):
        e = _bucketed(f"A{i}", BUCKET_AGGRESSIVE, 50.0 - i, industry=f"进攻行业{i}")
        entries[e.code] = e

    kept, removed = expire_entries(
        entries, run_date=RUN_DATE, ttl_days=0,
        max_size={BUCKET_DEFENSIVE: 2, BUCKET_BALANCED: 2, BUCKET_AGGRESSIVE: 2},
        max_per_industry=0,
    )
    # 防守桶分数全面高于进攻桶，但进攻票一只都没被挤掉
    assert set(kept) == {"D0", "D1", "A0", "A1"}
    assert sorted(removed) == [("D2", "capacity"), ("D3", "capacity")]


def test_expire_entries_applies_per_bucket_ttl():
    entries = {
        e.code: e
        for e in [
            _bucketed("AGG", BUCKET_AGGRESSIVE, 80.0, last_seen="2026-08-10"),
            _bucketed("DEF", BUCKET_DEFENSIVE, 80.0, last_seen="2026-08-10"),
        ]
    }
    # 距 RUN_DATE(08-28) 18 天：进攻桶 TTL 14 天已过期，防守桶 45 天仍保留
    kept, removed = expire_entries(
        entries, run_date=RUN_DATE, ttl_days=None, max_size=0, max_per_industry=0
    )
    assert set(kept) == {"DEF"}
    assert removed == [("AGG", "ttl")]


def test_expire_entries_applies_per_bucket_industry_quota():
    """进攻桶的行业配额更宽：热点扩散天然是同板块多只。"""
    entries = {}
    for i in range(4):
        e = _bucketed(f"D{i}", BUCKET_DEFENSIVE, 90.0 - i, industry="银行")
        entries[e.code] = e
    for i in range(4):
        e = _bucketed(f"A{i}", BUCKET_AGGRESSIVE, 90.0 - i, industry="半导体")
        entries[e.code] = e

    kept, _ = expire_entries(
        entries, run_date=RUN_DATE, ttl_days=0, max_size=0, max_per_industry=None
    )
    kept_defensive = [c for c in kept if c.startswith("D")]
    kept_aggressive = [c for c in kept if c.startswith("A")]
    assert len(kept_defensive) == 2   # DEFAULT_MAX_PER_INDUSTRY_BY_BUCKET defensive=2
    assert len(kept_aggressive) == 4  # aggressive=4


def test_render_report_segments_by_bucket():
    entries = {
        e.code: e
        for e in [
            _bucketed("600519", BUCKET_DEFENSIVE, 88.0, industry="白酒"),
            _bucketed("300750", BUCKET_AGGRESSIVE, 72.0, industry="锂电"),
        ]
    }
    report = render_report(
        run_date=RUN_DATE, cadence="all", entries=entries,
        added=[], removed=[], summaries=[],
    )
    assert "当前名单 · 防守（1）" in report
    assert "当前名单 · 进攻（1）" in report
    assert "当前名单 · 均衡" not in report  # 空桶不渲染
    assert "不可跨桶比较" in report
    assert "防守 1｜均衡 0｜进攻 1" in report
