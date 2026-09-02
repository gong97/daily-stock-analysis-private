# -*- coding: utf-8 -*-
"""从全市场快照按行业算 theme_heat，替代长期 502 的 akshare 板块接口。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.screening.industry import (  # noqa: E402
    SNAPSHOT_HEAT_MIN_SIZE,
    apply_snapshot_industry_heat,
    compute_snapshot_industry_heat,
    enrich_industry_concepts,
)
from src.services.screening.scorer import _compute_theme_heat_score  # noqa: E402

_PROFILE = {
    "theme_heat_unknown_score": 50.0,
    "theme_heat_change_slope": 6.0,
    "theme_heat_rank_bonus": 10.0,
    "theme_heat_trend_min_observations": 2.0,
    "theme_heat_trend_slope": 0.8,
    "theme_heat_trend_bonus_cap": 10.0,
    "theme_heat_cooling_penalty_slope": 0.8,
    "theme_heat_cooling_penalty_cap": 12.0,
    "theme_heat_persistence_min_score": 60.0,
    "theme_heat_persistence_slope": 0.08,
    "theme_heat_persistence_bonus_cap": 6.0,
    "theme_heat_cooling_score_penalty_slope": 0.6,
    "theme_heat_cooling_score_penalty_cap": 10.0,
    "theme_heat_overheat_score": 88.0,
    "theme_heat_overheat_penalty_slope": 0.5,
}


def _snapshot(rows: list[tuple[str, float]], turnover: float = 2.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [f"{i:06d}" for i in range(len(rows))],
            "industry": [industry for industry, _ in rows],
            "change_pct": [change for _, change in rows],
            "turnover_rate": [turnover] * len(rows),
        }
    )


def test_hot_industry_scores_above_cold_industry() -> None:
    rows = [("热", 4.0)] * 20 + [("冷", -4.0)] * 20
    heat, _ = compute_snapshot_industry_heat(_snapshot(rows))
    assert heat["热"]["industry_heat_score"] > heat["冷"]["industry_heat_score"]
    assert heat["热"]["industry_rank"] < heat["冷"]["industry_rank"]


def test_breadth_separates_broad_rally_from_single_stock_spike() -> None:
    """涨幅中位数相同时，普涨的行业热度应高于被个别股拉动的行业。

    这正是只看板块涨跌幅看不出来的差别。
    """
    broad = [("普涨", 1.0)] * 20
    # 中位数同为 1.0，但只有一半在涨，另一半在跌
    spike = [("个别拉动", 1.0)] * 10 + [("个别拉动", -3.0)] * 10
    heat, _ = compute_snapshot_industry_heat(_snapshot(broad + spike))
    assert heat["普涨"]["industry_breadth"] > heat["个别拉动"]["industry_breadth"]
    assert heat["普涨"]["industry_heat_score"] > heat["个别拉动"]["industry_heat_score"]


def test_small_industries_are_skipped_rather_than_guessed() -> None:
    """样本不足的行业不给分——2 只票的上涨比例只能取 0/50/100%，是噪声。"""
    rows = [("大", 1.0)] * SNAPSHOT_HEAT_MIN_SIZE + [("小", 9.0)] * 2
    heat, notes = compute_snapshot_industry_heat(_snapshot(rows))
    assert "大" in heat
    assert "小" not in heat
    assert any("skipped_small=1" in note for note in notes)


def test_heat_is_relative_so_a_market_wide_selloff_still_discriminates() -> None:
    """普跌行情下仍要分得出强弱，不能所有行业一起塌到底。"""
    rows = [("抗跌", -0.5)] * 20 + [("重挫", -5.0)] * 20
    heat, _ = compute_snapshot_industry_heat(_snapshot(rows))
    assert heat["抗跌"]["industry_heat_score"] > heat["重挫"]["industry_heat_score"]


def test_skips_cleanly_without_industry_column() -> None:
    df = pd.DataFrame({"code": ["000001"], "change_pct": [1.0]})
    heat, notes = compute_snapshot_industry_heat(df)
    assert heat == {}
    assert notes and "skipped" in notes[0]


def test_apply_does_not_overwrite_existing_board_data() -> None:
    """板块接口通的时候它的数据更权威，快照热度只补空缺。"""
    df = _snapshot([("甲", 3.0)] * 20)
    df["industry_heat_score"] = [99.0] * 10 + [np.nan] * 10
    out, _ = apply_snapshot_industry_heat(df)
    filled = pd.to_numeric(out["industry_heat_score"], errors="coerce")
    assert filled.head(10).eq(99.0).all(), "已有值不应被覆盖"
    assert filled.tail(10).notna().all(), "空缺应被补上"


def test_all_nan_board_heat_column_no_longer_swallows_the_fallback() -> None:
    """全 NA 的 board_heat_score 不能吃掉后面的回退路径。

    `enrich_industry_concepts()` 会把所有热度列预建成全 NA，因此"接口挂了"和
    "没配接口"在列结构上无法区分。此前分支只看列是否存在，全 NA 的
    board_heat_score 会让 theme_heat 恒为兜底值 50——实测 115/115 个候选都是 50。
    """
    df = pd.DataFrame(
        {
            "board_heat_score": [np.nan, np.nan, np.nan],
            "industry_heat_score": [80.0, 20.0, 50.0],
        }
    )
    score = _compute_theme_heat_score(df, _PROFILE)
    assert score.nunique() > 1, "全 NA 的 board_heat 不该把结果抹成单一兜底值"
    assert score.tolist() == [80.0, 20.0, 50.0]


def test_populated_board_heat_still_takes_priority() -> None:
    """板块接口有数据时仍然优先，修复不改变原有优先级。"""
    df = pd.DataFrame(
        {"board_heat_score": [70.0, 30.0], "industry_heat_score": [10.0, 90.0]}
    )
    score = _compute_theme_heat_score(df, _PROFILE)
    assert score.tolist() == [70.0, 30.0]


def test_snapshot_provider_produces_varying_theme_heat_end_to_end() -> None:
    """端到端：provider=snapshot 后 theme_heat 不再是常数 50。"""
    rows = [("热", 4.0)] * 20 + [("冷", -4.0)] * 20
    enriched, notes = enrich_industry_concepts(_snapshot(rows), provider="snapshot")
    score = _compute_theme_heat_score(enriched, _PROFILE)
    assert score.nunique() > 1
    assert any("snapshot industry heat computed" in note for note in notes)


def test_unsupported_provider_still_reports_skipped() -> None:
    enriched, notes = enrich_industry_concepts(_snapshot([("甲", 1.0)] * 12), provider="bogus")
    assert any("unsupported provider" in note for note in notes)
