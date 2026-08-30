# -*- coding: utf-8 -*-
"""`scripts/market_scan.py` 编排回归。

选股引擎的 `screen()` 被替换成确定性的假实现，因此这些用例不触网、不落库，
只验证编排本身：开关校验、策略选择、失败降级、产物落盘和退出码。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.screening.models import Pick, ScreenResult

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def market_scan():
    """按路径加载 CLI 脚本（scripts/ 不是包，不能按模块名导入）。"""
    spec = importlib.util.spec_from_file_location(
        "market_scan_cli", REPO_ROOT / "scripts" / "market_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _info(name: str, holding_period: str, risk_profile: str = "balanced") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        style={"holding_period": holding_period, "risk_profile": risk_profile},
        market_scope=["cn"],
    )


# 每个代码一个独立行业，避免默认的跨策略行业配额干扰其它用例；
# 配额本身由 test_industry_quota_trims_crowded_industry 单独覆盖。
_INDUSTRY_BY_CODE = {
    "000001": "银行",
    "000002": "地产",
    "000003": "化工",
    "600519": "白酒",
    "600036": "钢铁",
}


def _result(strategy: str, codes, *, industry: str | None = None) -> ScreenResult:
    return ScreenResult(
        strategy=strategy,
        market="cn",
        snapshot_count=5400,
        after_filter_count=210,
        run_id=f"run-{strategy}",
        snapshot_source="sina",
        picks=[
            Pick(
                rank=index,
                code=code,
                name=f"股票{code}",
                final_score=90.0 - index,
                screen_score=80.0,
                industry=industry or _INDUSTRY_BY_CODE.get(code, f"行业{code}"),
                price=10.0,
            )
            for index, code in enumerate(codes, start=1)
        ],
    )


@pytest.fixture
def stub_engine(monkeypatch):
    """把策略清单和 screen() 换成确定性假实现，返回被调用过的策略名。"""
    import src.services.screening.pipeline as pipeline_module
    import src.services.screening.strategy as strategy_module

    infos = [
        _info("capital_heat", "short_term", "aggressive"),
        _info("volume_breakout", "short_term", "aggressive"),
        _info("dual_low", "watchlist", "defensive"),
        _info("shrink_pullback", "swing", "balanced"),
    ]
    picks_by_strategy = {
        "capital_heat": ["000001", "000002"],
        "volume_breakout": ["000002", "000003"],
        "dual_low": ["600519"],
        "shrink_pullback": ["600036"],
    }
    called: list = []

    def _fake_list_strategies(_strategies_dir=None):
        return infos

    def _fake_screen(strategy, **_kwargs):
        called.append(strategy)
        return _result(strategy, picks_by_strategy.get(strategy, []))

    monkeypatch.setattr(strategy_module, "list_strategies", _fake_list_strategies)
    monkeypatch.setattr(pipeline_module, "screen", _fake_screen)
    monkeypatch.setenv("SCREENING_ENABLED", "true")
    # 让用例只受 CLI 参数影响：清掉可能从 CI/本地环境泄漏进来的观察名单配置，
    # 并避免把测试报告追加进 GitHub Actions 的 Job Summary。
    for name in (
        "WATCHLIST_DIR",
        "WATCHLIST_CADENCE_MAP",
        "WATCHLIST_TTL_DAYS",
        "WATCHLIST_MAX_SIZE",
        "WATCHLIST_MAX_PER_INDUSTRY",
        "WATCHLIST_MAX_PER_INDUSTRY_TOTAL",
        "WATCHLIST_NOTIFY",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)
    return called


def _args(out_dir: Path, *extra: str) -> list:
    return ["--force-run", "--out-dir", str(out_dir), *extra]


def test_refuses_to_run_when_screening_disabled(market_scan, monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_ENABLED", "false")
    assert market_scan.main(_args(tmp_path)) == 2
    assert not (tmp_path / "current.json").exists()


def test_rejects_unknown_explicit_strategy(market_scan, stub_engine, tmp_path):
    assert market_scan.main(_args(tmp_path, "--strategies", "no_such_strategy")) == 2
    assert stub_engine == []


def test_daily_cadence_only_runs_short_term_strategies(market_scan, stub_engine, tmp_path):
    assert market_scan.main(_args(tmp_path, "--cadence", "daily")) == 0
    assert sorted(stub_engine) == ["capital_heat", "volume_breakout"]


def test_weekly_cadence_only_runs_swing_and_watchlist_strategies(market_scan, stub_engine, tmp_path):
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly")) == 0
    assert sorted(stub_engine) == ["dual_low", "shrink_pullback"]


def test_full_run_writes_every_artifact(market_scan, stub_engine, tmp_path):
    exit_code = market_scan.main(_args(tmp_path, "--cadence", "all", "--write-stock-list"))
    assert exit_code == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    codes = {entry["code"] for entry in current["entries"]}
    assert codes == {"000001", "000002", "000003", "600519", "600036"}
    assert current["meta"]["last_run_cadence"] == "all"
    assert current["meta"]["llm_ranked"] is False

    # 000002 被两个策略同时选中，仍然只算一次命中
    entry_000002 = next(item for item in current["entries"] if item["code"] == "000002")
    assert entry_000002["hit_count"] == 1
    assert sorted(entry_000002["strategies"]) == ["capital_heat", "volume_breakout"]

    stock_list = (tmp_path / "STOCK_LIST.txt").read_text(encoding="utf-8").strip()
    assert set(stock_list.split(",")) == codes

    assert (tmp_path / "current.csv").exists()
    assert (tmp_path / "latest_report.md").exists()
    timing = json.loads((tmp_path / "timing.json").read_text(encoding="utf-8"))
    assert len(timing["runs"]) == 1
    assert len(timing["runs"][0]["strategies"]) == 4
    assert len(list((tmp_path / "history").glob("*.json"))) == 1


def test_dry_run_writes_nothing(market_scan, stub_engine, tmp_path):
    assert market_scan.main(_args(tmp_path, "--cadence", "all", "--dry-run", "--write-stock-list")) == 0
    assert stub_engine  # 扫描照常执行
    assert list(tmp_path.iterdir()) == []


def test_all_strategies_failing_returns_error_and_keeps_watchlist(
    market_scan, stub_engine, monkeypatch, tmp_path
):
    import src.services.screening.pipeline as pipeline_module

    # 先跑一轮建立名单
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly")) == 0
    before = (tmp_path / "current.json").read_text(encoding="utf-8")

    def _always_fail(_strategy, **_kwargs):
        raise RuntimeError("快照数据源全部不可用")

    monkeypatch.setattr(pipeline_module, "screen", _always_fail)
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly")) == 1
    assert (tmp_path / "current.json").read_text(encoding="utf-8") == before


def test_partial_failure_still_updates_watchlist(market_scan, stub_engine, monkeypatch, tmp_path):
    import src.services.screening.pipeline as pipeline_module

    def _half_broken(strategy, **_kwargs):
        if strategy == "dual_low":
            raise RuntimeError("该策略数据源超时")
        return _result(strategy, ["600036"])

    monkeypatch.setattr(pipeline_module, "screen", _half_broken)
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly")) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert {entry["code"] for entry in current["entries"]} == {"600036"}
    assert current["meta"]["last_run_failed_strategies"] == ["dual_low"]


def test_pinned_codes_survive_and_lead_the_stock_list(market_scan, stub_engine, tmp_path):
    (tmp_path / "pinned.txt").write_text("# 手工长期跟踪\n688111\n", encoding="utf-8")
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly", "--write-stock-list")) == 0

    stock_list = (tmp_path / "STOCK_LIST.txt").read_text(encoding="utf-8").strip().split(",")
    assert stock_list[0] == "688111"
    assert set(stock_list) == {"688111", "600519", "600036"}


def test_max_size_trims_each_bucket_independently(market_scan, stub_engine, tmp_path):
    """--max-size 是**每个桶**的上限，不是名单总量。"""
    assert market_scan.main(_args(tmp_path, "--cadence", "all", "--max-size", "2")) == 0
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))

    buckets = [entry["bucket"] for entry in current["entries"]]
    # aggressive 有 3 只（000001/000002/000003）被裁到 2；另外两桶各 1 只不受影响
    assert buckets.count("aggressive") == 2
    assert buckets.count("defensive") == 1
    assert buckets.count("balanced") == 1
    assert len(current["entries"]) == 4


def _banks_only(strategy, **_kwargs):
    return _result(strategy, ["000001", "000002", "000003"], industry="银行")


def test_industry_quota_trims_crowded_industry(market_scan, stub_engine, monkeypatch, tmp_path):
    """跨策略集中度：多个策略选出同一行业的票时，名单只保留配额内的。"""
    import src.services.screening.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "screen", _banks_only)
    assert market_scan.main(_args(tmp_path, "--cadence", "all", "--max-per-industry", "2")) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert len(current["entries"]) == 2
    assert {entry["industry"] for entry in current["entries"]} == {"银行"}
    # 限额按桶存储；命令行给标量时三个桶取同一个值
    assert current["meta"]["max_per_industry"] == {
        "defensive": 2,
        "balanced": 2,
        "aggressive": 2,
    }
    assert "同行业在该桶已满额" in (tmp_path / "latest_report.md").read_text(encoding="utf-8")


def test_max_per_industry_zero_keeps_everything(market_scan, stub_engine, monkeypatch, tmp_path):
    import src.services.screening.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "screen", _banks_only)
    assert market_scan.main(_args(tmp_path, "--cadence", "all", "--max-per-industry", "0")) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert len(current["entries"]) == 3


def test_bucket_is_derived_from_strategy_risk_profile(market_scan, stub_engine, tmp_path):
    """bucket 取自 style.risk_profile，与 cadence 是两根不同的轴。"""
    assert market_scan.main(_args(tmp_path, "--cadence", "all")) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    by_code = {e["code"]: e for e in current["entries"]}
    # 600519 只被 dual_low(defensive) 选中；600036 只被 shrink_pullback(balanced) 选中
    assert by_code["600519"]["bucket"] == "defensive"
    assert by_code["600036"]["bucket"] == "balanced"
    # 000001/000003 来自 capital_heat / volume_breakout（都是 aggressive）
    assert by_code["000001"]["bucket"] == "aggressive"
    assert by_code["000003"]["bucket"] == "aggressive"

    report = (tmp_path / "latest_report.md").read_text(encoding="utf-8")
    assert "当前名单 · 防守" in report
    assert "当前名单 · 进攻" in report
    assert "不可跨桶比较" in report

    csv_text = (tmp_path / "current.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].split(",")[3] == "bucket"


def test_per_bucket_limits_accept_bucket_syntax(market_scan, stub_engine, tmp_path):
    """--max-size 支持 'bucket:N' 写法，且只影响对应桶。"""
    assert market_scan.main(
        _args(tmp_path, "--cadence", "all", "--max-size", "aggressive:1", "--max-per-industry", "0")
    ) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    buckets = [e["bucket"] for e in current["entries"]]
    assert buckets.count("aggressive") == 1          # 被裁到 1
    assert buckets.count("defensive") == 1           # 未受影响
    assert buckets.count("balanced") == 1
    assert current["meta"]["max_size"]["aggressive"] == 1
    assert current["meta"]["max_size"]["defensive"] == 25


def test_bucket_does_not_drift_between_runs(market_scan, stub_engine, monkeypatch, tmp_path):
    """跨运行回归：周一进攻组、周五防守组，同一只票不应被后一轮改写分桶。"""
    import src.services.screening.pipeline as pipeline_module

    def _only(strategy, **_kwargs):
        return _result(strategy, ["600036"]) if strategy in _wanted[0] else _result(strategy, [])

    _wanted = [{"capital_heat", "volume_breakout"}]
    monkeypatch.setattr(pipeline_module, "screen", _only)
    assert market_scan.main(_args(tmp_path, "--cadence", "daily")) == 0
    first = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))["entries"][0]
    assert first["bucket"] == "aggressive"

    _wanted[0] = {"dual_low"}
    assert market_scan.main(_args(tmp_path, "--cadence", "weekly")) == 0
    second = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))["entries"][0]

    # 防守背书追加进来，进攻属性仍在，主桶不变
    assert second["buckets"] == ["aggressive", "defensive"]
    assert second["bucket"] == "aggressive"
    assert sorted(second["strategies"]) == ["capital_heat", "dual_low", "volume_breakout"]
    assert second["strategies"]["dual_low"]["bucket"] == "defensive"
    assert second["strategies"]["capital_heat"]["bucket"] == "aggressive"

    report = (tmp_path / "latest_report.md").read_text(encoding="utf-8")
    assert "（兼 防守）" in report


def test_global_industry_cap_trims_across_buckets(market_scan, stub_engine, monkeypatch, tmp_path):
    """桶内配额放行后，全局上限仍能把跨桶叠加的同行业压下来。"""
    import src.services.screening.pipeline as pipeline_module

    def _banks_everywhere(strategy, **_kwargs):
        # 每个策略选不同的票，都是银行；桶内配额（各桶 ≤2）因此不会触发
        by_strategy = {
            "capital_heat": ["000001"],       # aggressive
            "volume_breakout": ["000002"],    # aggressive
            "shrink_pullback": ["000003"],    # balanced
            "dual_low": ["600519"],           # defensive
        }
        return _result(strategy, by_strategy.get(strategy, []), industry="银行")

    monkeypatch.setattr(pipeline_module, "screen", _banks_everywhere)
    assert market_scan.main(
        _args(tmp_path, "--cadence", "all", "--max-per-industry", "2",
              "--max-per-industry-total", "2")
    ) == 0

    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert len(current["entries"]) == 2
    assert current["meta"]["max_per_industry_total"] == 2
    assert "同行业已达全局上限" in (tmp_path / "latest_report.md").read_text(encoding="utf-8")


def test_global_industry_cap_zero_disables_it(market_scan, stub_engine, monkeypatch, tmp_path):
    import src.services.screening.pipeline as pipeline_module

    def _banks_everywhere(strategy, **_kwargs):
        by_strategy = {
            "capital_heat": ["000001"], "volume_breakout": ["000002"],
            "shrink_pullback": ["000003"], "dual_low": ["600519"],
        }
        return _result(strategy, by_strategy.get(strategy, []), industry="银行")

    monkeypatch.setattr(pipeline_module, "screen", _banks_everywhere)
    assert market_scan.main(
        _args(tmp_path, "--cadence", "all", "--max-per-industry", "0",
              "--max-per-industry-total", "0")
    ) == 0
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert len(current["entries"]) == 4
