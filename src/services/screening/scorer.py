# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""screen_score calculation."""

import pandas as pd

from src.services.screening.models import ScreeningConfig

_FACTOR_COLUMNS = {
    "value": "factor_value_score",
    "liquidity": "factor_liquidity_score",
    "momentum": "factor_momentum_score",
    "reversal": "factor_reversal_score",
    "activity": "factor_activity_score",
    "stability": "factor_stability_score",
    "size": "factor_size_score",
    "theme_heat": "factor_theme_heat_score",
    "topic_alignment": "factor_topic_alignment_score",
}
_DEFAULT_SCORING_PROFILE = {
    "momentum_base": 60.0,
    "momentum_intraday_slope": 5.0,
    "momentum_chase_start_pct": 5.0,
    "momentum_chase_penalty_slope": 10.0,
    "momentum_downside_start_pct": -2.0,
    "momentum_downside_penalty_slope": 3.0,
    "momentum_60d_base": 55.0,
    "momentum_60d_slope": 0.9,
    "momentum_60d_overheat_pct": 45.0,
    "momentum_60d_overheat_penalty_slope": 0.8,
    "momentum_60d_breakdown_pct": -20.0,
    "momentum_60d_breakdown_penalty_slope": 0.7,
    "macd_bullish_bonus": 6.0,
    "macd_bearish_penalty": 8.0,
    "reversal_ideal_change_pct": -3.0,
    "reversal_distance_penalty_slope": 13.0,
    "reversal_collapse_start_pct": -8.0,
    "reversal_collapse_penalty_slope": 10.0,
    "reversal_chase_start_pct": 1.0,
    "reversal_chase_penalty_slope": 8.0,
    "rsi_oversold_bonus": 10.0,
    "rsi_overbought_penalty": 14.0,
    "activity_ideal_volume_ratio": 2.0,
    "activity_volume_ratio_distance_slope": 15.0,
    "activity_high_volume_ratio": 5.0,
    "activity_high_volume_ratio_penalty_slope": 8.0,
    "activity_ideal_turnover_rate": 4.0,
    "activity_turnover_distance_slope": 8.0,
    "activity_high_turnover_rate": 12.0,
    "activity_high_turnover_penalty_slope": 5.0,
    "stability_base": 78.0,
    "stability_change_abs_penalty_slope": 3.0,
    "stability_hot_change_pct": 7.0,
    "stability_hot_change_penalty_slope": 5.0,
    "stability_high_turnover_rate": 10.0,
    "stability_high_turnover_penalty_slope": 2.0,
    "stability_high_volume_ratio": 5.0,
    "stability_high_volume_ratio_penalty_slope": 4.0,
    "stability_invalid_pe_penalty": 18.0,
    "stability_high_volatility_pct": 45.0,
    "stability_high_volatility_penalty_slope": 0.45,
    "stability_max_drawdown_floor_pct": -12.0,
    "stability_drawdown_penalty_slope": 1.2,
    "stability_high_atr_pct": 6.0,
    "stability_high_atr_penalty_slope": 2.0,
    "stability_low_daily_quality_score": 80.0,
    "stability_low_daily_quality_penalty_slope": 0.35,
    "stability_bad_daily_quality_flag_penalty": 8.0,
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
    "topic_alignment_unknown_score": 50.0,
    "topic_alignment_match_bonus": 25.0,
    "topic_alignment_heat_weight": 0.25,
    "topic_alignment_unmatched_penalty": 12.0,
    # 行业中性化开关（YAML 里写 true，_scoring_profile 会转成 1.0）。
    # 开启后 value / liquidity / size 这三个分位类因子改为**行业内**排名。
    # 默认关闭：dual_low 这类策略要的就是"全市场最便宜"，中性化会改变其语义。
    "industry_neutral": 0.0,
    # 行业内有效样本少于该数时回退到全市场口径。实测 102 个行业里 29 个不足 10 只，
    # 最小的只有 1 只——3 只成员的行业分位只能取 33/67/100，没有区分度。
    "industry_neutral_min_size": 10.0,
}


def compute_screen_scores(df: pd.DataFrame, config: ScreeningConfig) -> pd.DataFrame:
    """Compute screen_score for each candidate row.

    Adds a 'screen_score' column (0-100). Higher is better.
    """
    result = df.copy()
    factors = _compute_factor_scores(result, config)
    for name, series in factors.items():
        result[_FACTOR_COLUMNS[name]] = series.round(4)

    weights = _normalized_factor_weights(config)
    result["screen_score"] = 0.0
    for factor, weight in weights.items():
        if factor in factors:
            result["screen_score"] += factors[factor] * weight

    result["screen_score"] = result["screen_score"].clip(0, 100)

    return result


def factor_score_columns() -> dict[str, str]:
    """Return the stable factor-score column mapping used in Pick output."""
    return dict(_FACTOR_COLUMNS)


def _normalized_factor_weights(config: ScreeningConfig) -> dict[str, float]:
    """Use explicit factor weights, or derive a sane legacy default from tech_weight."""
    raw_weights = config.factor_weights or {
        "value": (1 - config.tech_weight) * 0.50,
        "liquidity": (1 - config.tech_weight) * 0.25,
        "stability": (1 - config.tech_weight) * 0.25,
        "momentum": config.tech_weight * 0.55,
        "activity": config.tech_weight * 0.45,
    }
    weights = {
        factor: max(float(weight), 0.0)
        for factor, weight in raw_weights.items()
        if factor in _FACTOR_COLUMNS
    }
    total = sum(weights.values())
    if total <= 0:
        return {"value": 0.4, "liquidity": 0.2, "momentum": 0.2, "activity": 0.2}
    return {factor: weight / total for factor, weight in weights.items()}


def _compute_factor_scores(df: pd.DataFrame, config: ScreeningConfig | None = None) -> dict[str, pd.Series]:
    config = config or ScreeningConfig()
    profile = _scoring_profile(config)
    return {
        # value / liquidity / size 是仅有的三个分位类因子，因此也是行业中性化
        # 唯一作用的地方；momentum / stability / activity 走绝对公式，不受影响。
        "value": _compute_value_score(df, profile),
        "liquidity": _compute_liquidity_score(df, profile),
        "momentum": _compute_momentum_score(df, profile),
        "reversal": _compute_reversal_score(df, profile),
        "activity": _compute_activity_score(df, profile),
        "stability": _compute_stability_score(df, profile),
        "size": _compute_size_score(df, profile),
        "theme_heat": _compute_theme_heat_score(df, profile),
        "topic_alignment": _compute_topic_alignment_score(df, profile),
    }


def _scoring_profile(config: ScreeningConfig) -> dict[str, float]:
    profile = dict(_DEFAULT_SCORING_PROFILE)
    for key, value in (config.scoring_profile or {}).items():
        if key in profile:
            profile[key] = float(value)
    return profile


def _compute_snapshot_score(df: pd.DataFrame) -> pd.Series:
    """Score based on snapshot fundamentals (0-100).

    Components:
    - PE ratio: lower is better (for value), normalized
    - PB ratio: lower is better, normalized
    - Turnover rate: moderate is best
    - Amount (liquidity): higher is better, log-scaled
    - Change pct: near zero or moderate positive preferred
    """
    factors = _compute_factor_scores(df)
    return (
        factors["value"] * 0.50
        + factors["liquidity"] * 0.25
        + factors["stability"] * 0.25
    ).clip(0, 100)


def _compute_tech_score(df: pd.DataFrame) -> pd.Series:
    """Score based on technical features (0-100).

    Uses available columns like volume_ratio, change_pct patterns.
    Full tech scoring (MA structure, MACD/RSI) needs daily data,
    which is not in the snapshot — scored conservatively here.
    """
    factors = _compute_factor_scores(df)
    return (factors["momentum"] * 0.55 + factors["activity"] * 0.45).clip(0, 100)


def _compute_value_score(df: pd.DataFrame, profile: dict[str, float] | None = None) -> pd.Series:
    profile = profile if profile is not None else dict(_DEFAULT_SCORING_PROFILE)
    groups, min_size = _industry_groups(df, profile)
    score = pd.Series(50.0, index=df.index)

    if "pe_ratio" in df.columns:
        pe = pd.to_numeric(df["pe_ratio"], errors="coerce")
        pe_score = _rank_score(
            pe.where((pe > 0) & (pe < 500)), lower_is_better=True, na_score=25,
            groups=groups, min_group_size=min_size,
        )
        score = score * 0.35 + pe_score * 0.65

    if "pb_ratio" in df.columns:
        pb = pd.to_numeric(df["pb_ratio"], errors="coerce")
        pb_score = _rank_score(
            pb.where((pb > 0) & (pb < 50)), lower_is_better=True, na_score=25,
            groups=groups, min_group_size=min_size,
        )
        score = score * 0.55 + pb_score * 0.45

    return score.clip(0, 100)


def _compute_liquidity_score(df: pd.DataFrame, profile: dict[str, float] | None = None) -> pd.Series:
    if "amount" not in df.columns:
        return pd.Series(50.0, index=df.index)

    import numpy as np

    profile = profile if profile is not None else dict(_DEFAULT_SCORING_PROFILE)
    groups, min_size = _industry_groups(df, profile)
    amount = pd.to_numeric(df["amount"], errors="coerce")
    log_amount = np.log10(amount.clip(lower=1))
    return _rank_score(
        log_amount.where(amount > 0), lower_is_better=False, na_score=20,
        groups=groups, min_group_size=min_size,
    )


def _compute_momentum_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(50.0, index=df.index)

    if "change_pct" in df.columns:
        change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        # Prefer constructive positive moves, but penalize chase-risk near limit-up.
        intraday_score = profile["momentum_base"] + change * profile["momentum_intraday_slope"]
        intraday_score = intraday_score - (
            change - profile["momentum_chase_start_pct"]
        ).clip(lower=0) * profile["momentum_chase_penalty_slope"]
        intraday_score = intraday_score - (
            -change + profile["momentum_downside_start_pct"]
        ).clip(lower=0) * profile["momentum_downside_penalty_slope"]
        score = score * 0.35 + intraday_score.clip(5, 100) * 0.65

    if "change_60d" in df.columns:
        change_60d = pd.to_numeric(df["change_60d"], errors="coerce").fillna(0)
        trend_score = profile["momentum_60d_base"] + change_60d * profile["momentum_60d_slope"]
        trend_score = trend_score - (
            change_60d - profile["momentum_60d_overheat_pct"]
        ).clip(lower=0) * profile["momentum_60d_overheat_penalty_slope"]
        trend_score = trend_score - (
            -change_60d + profile["momentum_60d_breakdown_pct"]
        ).clip(lower=0) * profile["momentum_60d_breakdown_penalty_slope"]
        score = score * 0.60 + trend_score.clip(5, 100) * 0.40

    if "signal_score" in df.columns:
        signal = pd.to_numeric(df["signal_score"], errors="coerce").fillna(50)
        score = score * 0.70 + signal.clip(0, 100) * 0.30

    if "macd_status" in df.columns:
        macd = df["macd_status"].astype(str)
        score = score + macd.map({
            "bullish": profile["macd_bullish_bonus"],
            "bearish": -profile["macd_bearish_penalty"],
        }).fillna(0)

    return score.clip(5, 100)


def _compute_reversal_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    if "change_pct" not in df.columns:
        return pd.Series(50.0, index=df.index)

    change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
    # Reversal setups prefer controlled weakness, not collapse.
    score = 100 - (
        change - profile["reversal_ideal_change_pct"]
    ).abs() * profile["reversal_distance_penalty_slope"]
    score = score - (
        -change + profile["reversal_collapse_start_pct"]
    ).clip(lower=0) * profile["reversal_collapse_penalty_slope"]
    score = score - (
        change - profile["reversal_chase_start_pct"]
    ).clip(lower=0) * profile["reversal_chase_penalty_slope"]

    if "rsi_status" in df.columns:
        rsi = df["rsi_status"].astype(str)
        score = score + rsi.map({
            "oversold": profile["rsi_oversold_bonus"],
            "overbought": -profile["rsi_overbought_penalty"],
        }).fillna(0)
    if "change_60d" in df.columns:
        change_60d = pd.to_numeric(df["change_60d"], errors="coerce").fillna(0)
        score = score - (change_60d - 35).clip(lower=0) * 0.5
        score = score - (-change_60d - 35).clip(lower=0) * 0.8
    return score.clip(5, 100)


def _compute_activity_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(50.0, index=df.index)

    if "volume_ratio" in df.columns:
        volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
        vr_score = 100 - (
            volume_ratio - profile["activity_ideal_volume_ratio"]
        ).abs() * profile["activity_volume_ratio_distance_slope"]
        vr_score = vr_score - (
            volume_ratio - profile["activity_high_volume_ratio"]
        ).clip(lower=0) * profile["activity_high_volume_ratio_penalty_slope"]
        score = score * 0.45 + vr_score.clip(5, 100) * 0.55

    if "turnover_rate" in df.columns:
        turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0)
        turnover_score = 100 - (
            turnover - profile["activity_ideal_turnover_rate"]
        ).abs() * profile["activity_turnover_distance_slope"]
        turnover_score = turnover_score - (
            turnover - profile["activity_high_turnover_rate"]
        ).clip(lower=0) * profile["activity_high_turnover_penalty_slope"]
        turnover_score = turnover_score.where(turnover > 0, 40)
        score = score * 0.55 + turnover_score.clip(5, 100) * 0.45

    return score.clip(0, 100)


def _compute_stability_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(profile["stability_base"], index=df.index)

    if "change_pct" in df.columns:
        change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        score -= change.abs().clip(upper=10) * profile["stability_change_abs_penalty_slope"]
        score -= (
            change - profile["stability_hot_change_pct"]
        ).clip(lower=0) * profile["stability_hot_change_penalty_slope"]

    if "turnover_rate" in df.columns:
        turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0)
        score -= (
            turnover - profile["stability_high_turnover_rate"]
        ).clip(lower=0) * profile["stability_high_turnover_penalty_slope"]

    if "volume_ratio" in df.columns:
        volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1)
        score -= (
            volume_ratio - profile["stability_high_volume_ratio"]
        ).clip(lower=0) * profile["stability_high_volume_ratio_penalty_slope"]

    if "pe_ratio" in df.columns:
        pe = pd.to_numeric(df["pe_ratio"], errors="coerce")
        score = score.where((pe.isna()) | (pe > 0), score - profile["stability_invalid_pe_penalty"])

    if "signal_score" in df.columns:
        signal = pd.to_numeric(df["signal_score"], errors="coerce").fillna(50)
        score = score + (signal - 50) * 0.12

    if "volatility_20d_pct" in df.columns:
        volatility = pd.to_numeric(df["volatility_20d_pct"], errors="coerce")
        score -= (
            volatility - profile["stability_high_volatility_pct"]
        ).clip(lower=0).fillna(0) * profile["stability_high_volatility_penalty_slope"]

    if "max_drawdown_20d_pct" in df.columns:
        drawdown = pd.to_numeric(df["max_drawdown_20d_pct"], errors="coerce")
        score -= (
            profile["stability_max_drawdown_floor_pct"] - drawdown
        ).clip(lower=0).fillna(0) * profile["stability_drawdown_penalty_slope"]

    if "atr_20_pct" in df.columns:
        atr = pd.to_numeric(df["atr_20_pct"], errors="coerce")
        score -= (
            atr - profile["stability_high_atr_pct"]
        ).clip(lower=0).fillna(0) * profile["stability_high_atr_penalty_slope"]

    if "daily_quality_score" in df.columns:
        quality = pd.to_numeric(df["daily_quality_score"], errors="coerce")
        score -= (
            profile["stability_low_daily_quality_score"] - quality
        ).clip(lower=0).fillna(0) * profile["stability_low_daily_quality_penalty_slope"]

    if "daily_quality_flags" in df.columns:
        flags = df["daily_quality_flags"].fillna("").astype(str)
        severe_flags = flags.str.contains("invalid_ohlc|non_positive_price|negative_volume|stale_cache")
        score -= severe_flags.astype(float) * profile["stability_bad_daily_quality_flag_penalty"]

    return score.clip(0, 100)


def _compute_size_score(df: pd.DataFrame, profile: dict[str, float] | None = None) -> pd.Series:
    if "total_mv" not in df.columns:
        return pd.Series(50.0, index=df.index)

    import numpy as np

    profile = profile if profile is not None else dict(_DEFAULT_SCORING_PROFILE)
    groups, min_size = _industry_groups(df, profile)
    mv = pd.to_numeric(df["total_mv"], errors="coerce")
    log_mv = np.log10(mv.clip(lower=1))
    return _rank_score(
        log_mv.where(mv > 0), lower_is_better=False, na_score=35,
        groups=groups, min_group_size=min_size,
    )


def _has_any_value(df: pd.DataFrame, column: str) -> bool:
    """列存在**且至少有一个非空值**。

    只判断列是否存在是不够的：`enrich_industry_concepts()` 会把所有热度列预建成
    全 NA，于是"板块接口挂了"和"板块接口没配"在列结构上无法区分。此前第一个
    分支只看 `in df.columns`，全 NA 的 `board_heat_score` 会直接吃掉后面所有
    回退路径，`fillna(base)` 再把结果抹成兜底值——无论下游填了多少行业热度，
    theme_heat 都恒为 50。
    """
    if column not in df.columns:
        return False
    return bool(pd.to_numeric(df[column], errors="coerce").notna().any())


def _compute_theme_heat_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    base = pd.Series(profile["theme_heat_unknown_score"], index=df.index)
    if _has_any_value(df, "board_heat_score"):
        score = pd.to_numeric(df["board_heat_score"], errors="coerce").fillna(base)
    elif _has_any_value(df, "industry_heat_score") or _has_any_value(df, "concept_heat_score"):
        industry = _numeric_column(df, "industry_heat_score")
        concept = _numeric_column(df, "concept_heat_score")
        score = pd.concat([industry, concept], axis=1).max(axis=1).fillna(base)
    elif _has_any_value(df, "industry_change_pct"):
        change = pd.to_numeric(df["industry_change_pct"], errors="coerce").fillna(0)
        score = base + change * profile["theme_heat_change_slope"]
        if "industry_rank" in df.columns:
            rank = pd.to_numeric(df["industry_rank"], errors="coerce")
            score += (
                (profile["theme_heat_rank_bonus"] - rank.clip(lower=1, upper=10))
                .clip(lower=0)
                .fillna(0)
            )
    else:
        return base.clip(0, 100)

    if _has_any_value(df, "board_heat_trend_score"):
        trend = pd.to_numeric(df["board_heat_trend_score"], errors="coerce").fillna(0)
        if "board_heat_observations" in df.columns:
            observations = pd.to_numeric(df["board_heat_observations"], errors="coerce").fillna(0)
        else:
            observations = pd.Series(profile["theme_heat_trend_min_observations"], index=df.index)
        trend_is_reliable = observations >= profile["theme_heat_trend_min_observations"]
        trend_bonus = (trend.clip(lower=0) * profile["theme_heat_trend_slope"]).clip(
            upper=profile["theme_heat_trend_bonus_cap"]
        )
        cooling_penalty = ((-trend).clip(lower=0) * profile["theme_heat_cooling_penalty_slope"]).clip(
            upper=profile["theme_heat_cooling_penalty_cap"]
        )
        score = score + (trend_bonus - cooling_penalty).where(trend_is_reliable, 0)

    if _has_any_value(df, "board_heat_persistence_score"):
        persistence = pd.to_numeric(df["board_heat_persistence_score"], errors="coerce").fillna(0)
        persistence_bonus = (
            (persistence - profile["theme_heat_persistence_min_score"]).clip(lower=0)
            * profile["theme_heat_persistence_slope"]
        ).clip(upper=profile["theme_heat_persistence_bonus_cap"])
        score = score + persistence_bonus

    if _has_any_value(df, "board_heat_cooling_score"):
        cooling = pd.to_numeric(df["board_heat_cooling_score"], errors="coerce").fillna(0)
        cooling_penalty = (cooling * profile["theme_heat_cooling_score_penalty_slope"]).clip(
            upper=profile["theme_heat_cooling_score_penalty_cap"]
        )
        score = score - cooling_penalty

    overheat = (score - profile["theme_heat_overheat_score"]).clip(lower=0)
    score = score - overheat * profile["theme_heat_overheat_penalty_slope"]
    return score.clip(0, 100)


def _compute_topic_alignment_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    """Score whether industry/concept labels align with hotspot route summaries."""
    base = pd.Series(profile["topic_alignment_unknown_score"], index=df.index)
    if not {"industry", "concepts", "board_heat_summary"} & set(df.columns):
        return base

    scores = []
    for _, row in df.iterrows():
        candidate_topics = _topic_tokens(row.get("industry")) | _topic_tokens(row.get("concepts"))
        route_topics = _topic_tokens(row.get("board_heat_summary"))
        if not candidate_topics or not route_topics:
            scores.append(float(profile["topic_alignment_unknown_score"]))
            continue
        overlap = candidate_topics & route_topics
        score = float(profile["topic_alignment_unknown_score"])
        if overlap:
            score += float(profile["topic_alignment_match_bonus"])
            heat = pd.to_numeric(row.get("board_heat_score"), errors="coerce")
            if pd.notna(heat):
                score += max(float(heat) - 50.0, 0.0) * float(profile["topic_alignment_heat_weight"])
        else:
            score -= float(profile["topic_alignment_unmatched_penalty"])
        scores.append(score)
    return pd.Series(scores, index=df.index).clip(0, 100)


def _topic_tokens(value: object) -> set[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return set()
    normalized = text
    for sep in ["|", ",", "，", ";", "；", "/"]:
        normalized = normalized.replace(sep, " ")
    tokens = set()
    for raw in normalized.split():
        token = raw.split(":", 1)[0].strip()
        if token and token.lower() not in {"rank", "nan", "none", "<na>"}:
            tokens.add(token)
    return tokens


def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NA, index=df.index)
    return pd.to_numeric(df[column], errors="coerce")


def _rank_score(
    series: pd.Series,
    *,
    lower_is_better: bool,
    na_score: float = 50.0,
    groups: pd.Series | None = None,
    min_group_size: int = 0,
) -> pd.Series:
    """把一列数值转成 0-100 的分位得分。

    传入 ``groups`` 时改为**组内**分位（行业中性化）：全市场口径下银行凭借行业属性
    垄断 value 因子高分（39 只银行的 value 均值 89.1，行业内重排后回到 51.0），
    分不出"银行里便宜的"和"银行里贵的"。有效样本少于 ``min_group_size`` 的组
    回退到全市场口径，避免 3 只成员的行业只能给出 33/67/100 这种无区分度的分位。
    """
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == 0:
        return pd.Series(float(na_score), index=series.index)

    ranks = numeric.rank(
        ascending=not lower_is_better,
        na_option="keep",
        pct=True,
    ) * 100

    if groups is not None:
        labels = groups.reindex(numeric.index).astype("string").fillna("").str.strip()
        # 组大小按**有效值**计数：一个 20 只的行业里只有 2 只有 PE，同样是噪声。
        valid_per_group = numeric.notna().groupby(labels).transform("sum")
        within = numeric.groupby(labels).rank(
            ascending=not lower_is_better,
            na_option="keep",
            pct=True,
        ) * 100
        use_within = (labels != "") & (valid_per_group >= max(int(min_group_size), 0))
        ranks = ranks.where(~use_within, within)

    return ranks.fillna(float(na_score)).clip(0, 100)


def _industry_groups(
    df: pd.DataFrame,
    profile: dict[str, float],
) -> tuple[pd.Series | None, int]:
    """按 scoring_profile 决定是否做行业中性化，以及小行业回退阈值。"""
    if float(profile.get("industry_neutral", 0.0)) < 1:
        return None, 0
    if "industry" not in df.columns:
        return None, 0
    return df["industry"], int(profile.get("industry_neutral_min_size", 10))
