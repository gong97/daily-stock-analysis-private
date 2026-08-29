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


def _info(name: str, holding_period: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, style={"holding_period": holding_period}, market_scope=["cn"])


def _result(strategy: str, codes) -> ScreenResult:
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
                industry="测试行业",
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
        _info("capital_heat", "short_term"),
        _info("volume_breakout", "short_term"),
        _info("dual_low", "watchlist"),
        _info("shrink_pullback", "swing"),
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


def test_max_size_trims_watchlist(market_scan, stub_engine, tmp_path):
    assert market_scan.main(_args(tmp_path, "--cadence", "all", "--max-size", "2")) == 0
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    assert len(current["entries"]) == 2
