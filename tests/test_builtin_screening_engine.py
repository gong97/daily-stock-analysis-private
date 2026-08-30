# -*- coding: utf-8 -*-
"""Regression contracts for the DSA-owned screening implementation."""

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import call, patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastAPITestClient

from api.v1.router import router
from src.services.screening import REFERENCE_REVISION
from src.services.screening.dsa_provider import apply_dsa_provider_context
from src.services.screening import pipeline as screening_pipeline
from src.services.screening import post_analysis as screening_post_analysis
from src.services.screening.filter import apply_hard_filters
from src.services.screening.config import Config as ScreeningRuntimeConfig
from src.services.screening.models import HardFilterConfig, Pick, ScreeningConfig, Strategy
from src.services.screening import scorer as screening_scorer
from src.services.screening.scorer import compute_screen_scores
from src.services.screening import snapshot as screening_snapshot
from src.services.screening.strategy import list_strategies, load_all_strategies


REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENING_ROOT = REPO_ROOT / "src" / "services" / "screening"


def test_screening_engine_is_collected_from_the_internal_package() -> None:
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "alphasift.git" not in requirements
    assert "#egg=alphasift" not in requirements
    assert "import src.services.screening.pipeline" in dockerfile


def test_screening_routes_have_a_primary_prefix_and_no_install_endpoint() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    schema_paths = app.openapi()["paths"]
    assert "/api/v1/screening/status" in schema_paths
    assert "/api/v1/alphasift/status" not in schema_paths

    client = FastAPITestClient(app)
    assert client.request("OPTIONS", "/api/v1/screening/status").status_code != 404
    assert client.request("OPTIONS", "/api/v1/alphasift/status").status_code == 404
    assert client.request("POST", "/api/v1/screening/install").status_code == 404


def test_bundled_engine_keeps_source_and_license_notices() -> None:
    notice = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    license_text = (SCREENING_ROOT / "LICENSE").read_text(encoding="utf-8")

    assert REFERENCE_REVISION in notice
    assert "Apache License" in license_text
    # DSA 自己新增的策略不是 AlphaSift 衍生物，不能挂来源头（那是错误归因）。
    # 白名单显式列出，保证新增 DSA 原生文件必须在这里登记，衍生文件的归因约束不被削弱。
    dsa_native_files = {"theme_momentum.yaml"}
    derived_files = [
        path
        for path in (
            *SCREENING_ROOT.glob("*.py"),
            *(SCREENING_ROOT / "strategies").glob("*.yaml"),
        )
        if path.name not in dsa_native_files
    ]
    assert derived_files
    for path in derived_files:
        source = path.read_text(encoding="utf-8")
        assert f"Derived from AlphaSift revision {REFERENCE_REVISION}." in source

    for name in dsa_native_files:
        source = (SCREENING_ROOT / "strategies" / name).read_text(encoding="utf-8")
        assert "Derived from AlphaSift" not in source, f"{name} 是 DSA 原生文件，不应声明 AlphaSift 归因"


def test_bundled_strategies_are_loaded_from_the_internal_package() -> None:
    strategies = load_all_strategies(SCREENING_ROOT / "strategies")

    assert set(strategies) == {
        "balanced_alpha",
        "blue_chip_income",
        "capital_heat",
        "dual_low",
        "low_volatility_quality",
        "momentum_quality",
        "oversold_reversal",
        "quality_value",
        "shrink_pullback",
        "theme_momentum",
        "volume_breakout",
    }
    assert strategies["dual_low"].screening.factor_weights["value"] < 0.40

    # theme_momentum 是显式的进攻侧补充：不看估值、限定中小市值、依赖板块热度。
    offensive = strategies["theme_momentum"].screening
    assert "value" not in offensive.factor_weights
    assert offensive.factor_weights["theme_heat"] >= 0.25
    assert offensive.hard_filters.market_cap_max is not None
    assert offensive.hard_filters.pe_ttm_min is None
    assert offensive.hard_filters.pe_ttm_max is None


def test_list_strategies_preserves_legacy_strategies_dir_override() -> None:
    strategy_yaml = """
name: custom_demo
display_name: 自定义策略
description: custom strategy
screening:
  enabled: true
  market_scope: [cn]
  hard_filters: {}
  factor_weights:
    value: 1.0
  max_output: 3
""".strip()

    with tempfile.TemporaryDirectory() as temp_dir:
        strategy_path = Path(temp_dir) / "custom_demo.yaml"
        strategy_path.write_text(strategy_yaml, encoding="utf-8")

        with patch.dict(os.environ, {"STRATEGIES_DIR": temp_dir}, clear=False):
            config = ScreeningRuntimeConfig.from_env()
            strategies = list_strategies()

    assert config.strategies_dir == Path(temp_dir)
    assert [item.name for item in strategies] == ["custom_demo"]


def test_screening_config_reads_snapshot_cache_ttl() -> None:
    with patch.dict(
        os.environ,
        {"SCREENING_SNAPSHOT_CACHE_TTL_SEC": "120"},
        clear=False,
    ):
        config = ScreeningRuntimeConfig.from_env()

    assert config.snapshot_cache_ttl_seconds == 120.0


def test_pipeline_passes_daily_history_cache_settings_to_enrichment(monkeypatch) -> None:
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "Ping An",
                "price": 10.0,
                "change_pct": 1.2,
                "amount": 200_000_000.0,
            }
        ]
    )
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=ScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=3,
        ),
    )
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        daily_enrich_enabled=True,
        daily_history_cache_dir=Path("/tmp/daily-history-cache"),
        daily_history_cache_ttl_hours=6,
        post_analyzers=[],
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )
    captured: dict[str, object] = {}

    def fake_enrich_daily_features(df: pd.DataFrame, **kwargs):
        captured.update(kwargs)
        enriched = df.copy()
        enriched["daily_source"] = "cache"
        enriched.attrs["daily_errors"] = []
        enriched.attrs["daily_success_count"] = len(enriched)
        enriched.attrs["daily_source_counts"] = {"cache": len(enriched)}
        enriched.attrs["daily_quality_flag_counts"] = {}
        enriched.attrs["daily_source_order_notes"] = []
        enriched.attrs["daily_source_health"] = {}
        return enriched

    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "enrich_daily_features", fake_enrich_daily_features)
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))

    history_fetcher = lambda *_args, **_kwargs: pd.DataFrame()
    result = screening_pipeline.screen(
        "demo",
        use_llm=False,
        config=config,
        daily_history_fetcher=history_fetcher,
    )

    assert captured["cache_dir"] == Path("/tmp/daily-history-cache")
    assert captured["cache_ttl_seconds"] == 6 * 60 * 60
    assert captured["history_fetcher"] is history_fetcher
    assert result.daily_enriched is True


def test_dsa_post_analyzer_records_attempted_and_capped_statuses(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=90.0 - index,
            screen_score=90.0 - index,
        )
        for index in range(1, 5)
    ]
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        dsa_api_url="https://dsa.example",
    )

    def fake_analyze(candidates, **_kwargs):
        candidates[0].deep_analysis_status = "completed"
        candidates[0].deep_analysis_summary = "completed"
        candidates[1].deep_analysis_status = "failed"
        candidates[1].deep_analysis_summary = "failed"
        return candidates, ["one failure"]

    monkeypatch.setattr(screening_post_analysis, "analyze_picks_with_dsa", fake_analyze)
    # A completed remote overlay can move an attempted pick below an
    # unattempted one. Status ownership must follow the original attempted
    # objects, not the post-overlay list positions.
    monkeypatch.setattr(
        screening_post_analysis,
        "apply_dsa_overlay",
        lambda candidates: [candidates[2], candidates[0], candidates[3], candidates[1]],
    )

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["dsa"],
        run_id="run-1",
        config=config,
        max_picks=2,
    )

    status_by_code = {
        pick.code: pick.post_analysis_status.get("dsa")
        for pick in analyzed
    }
    assert status_by_code == {
        "000001": "completed",
        "000002": "failed",
        "000003": "skipped",
        "000004": "skipped",
    }
    assert degradation == ["one failure"]


def test_scorecard_reranks_before_capped_dsa_analyzer(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=91.0 - index,
            screen_score=91.0 - index,
            factor_scores={"value": 50.0, "stability": 50.0},
        )
        for index in range(1, 5)
    ]
    # The original fourth candidate crosses the remote Top-3 cutoff after the
    # full-pool scorecard applies its value/quality bonus.
    picks[3].factor_scores = {"value": 100.0, "stability": 100.0}
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        dsa_api_url="https://dsa.example",
    )
    attempted_codes: list[str] = []

    def fake_analyze(candidates, *, max_picks, **_kwargs):
        attempted = candidates[:max_picks]
        attempted_codes.extend(pick.code for pick in attempted)
        for pick in attempted:
            pick.deep_analysis_status = "completed"
        return candidates, []

    monkeypatch.setattr(screening_post_analysis, "analyze_picks_with_dsa", fake_analyze)
    monkeypatch.setattr(screening_post_analysis, "apply_dsa_overlay", lambda candidates: candidates)

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["scorecard", "dsa"],
        run_id="run-1",
        config=config,
        max_picks=3,
    )

    assert attempted_codes == ["000001", "000004", "000002"]
    assert {
        pick.code: pick.post_analysis_status.get("dsa")
        for pick in analyzed
    } == {
        "000004": "completed",
        "000001": "completed",
        "000002": "completed",
        "000003": "skipped",
    }
    assert degradation == []


def test_external_post_analyzer_rejects_results_beyond_remote_cap(monkeypatch) -> None:
    picks = [
        Pick(
            rank=index,
            code=f"00000{index}",
            name=f"Stock {index}",
            final_score=90.0 - index,
            screen_score=90.0 - index,
        )
        for index in range(1, 4)
    ]
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        post_analyzer_url="https://analyzer.example/rank",
    )
    captured = {}

    def fake_post(_url, *, json, timeout):
        captured["payload"] = json
        captured["timeout"] = timeout
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [
                {"code": "000001", "score_delta": 1.0, "risk_flags": ["submitted"]},
                {"code": "000001", "score_delta": 50.0, "risk_flags": ["duplicate"]},
                {"code": "000003", "score_delta": 50.0, "risk_flags": ["not-submitted"]},
            ],
        )

    monkeypatch.setattr(screening_post_analysis.requests, "post", fake_post)

    analyzed, degradation = screening_post_analysis.run_post_analyzers(
        picks,
        analyzer_names=["external_http"],
        run_id="run-1",
        config=config,
        max_picks=2,
    )

    by_code = {pick.code: pick for pick in analyzed}
    assert [item["code"] for item in captured["payload"]["candidates"]] == ["000001", "000002"]
    assert by_code["000001"].final_score == 90.0
    assert by_code["000001"].risk_flags == ["submitted"]
    assert by_code["000001"].post_analysis_status["external_http"] == "completed"
    assert by_code["000002"].post_analysis_status["external_http"] == "failed"
    assert by_code["000003"].final_score == 87.0
    assert by_code["000003"].risk_flags == []
    assert by_code["000003"].post_analysis_status["external_http"] == "skipped"
    assert degradation == []


def test_pipeline_uses_ranker_success_flag_instead_of_partial_llm_scores(monkeypatch) -> None:
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "Ping An",
                "price": 10.0,
                "change_pct": 1.2,
                "amount": 200_000_000.0,
            }
        ]
    )
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=ScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=3,
        ),
    )
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        llm_model="openai/gpt-5-mini",
        llm_api_key="test-key",
        post_analyzers=[],
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )

    def fake_ranker(picks, *_args, **_kwargs):
        leaked_pick = picks[0]
        leaked_pick.llm_score = 99.0
        return SimpleNamespace(
            picks=picks,
            ranked=False,
            market_view="",
            selection_logic="",
            portfolio_risk="",
            coverage=0.5,
            errors=["coverage below threshold"],
        )

    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=88.0),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "rank_candidates_with_metadata", fake_ranker)
    monkeypatch.setattr(screening_pipeline, "apply_risk_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "apply_portfolio_overlay", lambda picks, **kwargs: (picks, []))
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **kwargs: (picks, []))

    result = screening_pipeline.screen("demo", use_llm=True, config=config)

    assert result.llm_ranked is False
    assert result.picks[0].llm_score is None
    assert result.picks[0].final_score == result.picks[0].screen_score
    assert "LLM ranking failed: fell back to screen_score" in result.degradation


def test_default_scorecard_scores_full_pool_before_seeded_rotation(monkeypatch) -> None:
    snapshot_df = pd.DataFrame([
        {
            "code": f"00000{index}",
            "name": f"Stock {index}",
            "price": 10.0,
            "change_pct": 1.0,
            "amount": 200_000_000.0,
            "raw_score": score,
        }
        for index, score in enumerate([84.2, 84.1, 84.0, 83.9, 83.8], start=1)
    ])
    snapshot_df.attrs.update({
        "snapshot_source": "sina",
        "source_errors": [],
        "fallback_used": False,
    })
    strategy = Strategy(
        name="demo",
        display_name="Demo",
        description="demo",
        screening=ScreeningConfig(
            enabled=True,
            market_scope=["cn"],
            hard_filters=HardFilterConfig(),
            factor_weights={"value": 1.0},
            max_output=3,
        ),
    )
    config = ScreeningRuntimeConfig(
        strategies_dir=SCREENING_ROOT / "strategies",
        post_analyzers=["scorecard"],
        post_analysis_max_picks=3,
        llm_candidate_multiplier=2,
        llm_max_candidates=10,
        risk_enabled=False,
        portfolio_diversity_enabled=False,
    )
    observed_statuses: list[list[str | None]] = []
    original_variant = screening_pipeline.apply_seeded_selection_variant

    def capture_variant(picks, **kwargs):
        observed_statuses.append([
            pick.post_analysis_status.get("scorecard") for pick in picks
        ])
        return original_variant(picks, **kwargs)

    run_ids = iter(f"{index:012d}" for index in range(20))
    monkeypatch.setattr(screening_pipeline, "load_all_strategies", lambda _path: {"demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *args, **kwargs: snapshot_df.copy())
    monkeypatch.setattr(screening_pipeline, "apply_hard_filters", lambda df, _filters: df.copy())
    monkeypatch.setattr(
        screening_pipeline,
        "compute_screen_scores",
        lambda df, _screening: df.assign(screen_score=df["raw_score"]),
    )
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _context: [])
    monkeypatch.setattr(screening_pipeline, "apply_seeded_selection_variant", capture_variant)
    monkeypatch.setattr(screening_pipeline.uuid, "uuid4", lambda: SimpleNamespace(hex=next(run_ids)))

    variants = {
        tuple(
            pick.code
            for pick in screening_pipeline.screen(
                "demo",
                max_output=3,
                use_llm=False,
                selection_seed="browser-a",
                config=config,
            ).picks
        )
        for _ in range(20)
    }

    assert all(statuses == ["completed"] * 5 for statuses in observed_statuses)
    assert len(variants) >= 2


def test_dsa_provider_context_respects_host_max_candidates_setting() -> None:
    picks = [
        Pick(rank=index + 1, code=f"00000{index + 1}", name=f"Stock {index + 1}", final_score=90.0, screen_score=90.0)
        for index in range(5)
    ]
    requested_codes: list[str] = []

    def get_candidate_context(code: str, _name: str) -> dict[str, object]:
        requested_codes.append(code)
        return {"enriched": True, "quote": {"price": 10.0}}

    notes = apply_dsa_provider_context(
        picks,
        {"dsa": {"max_candidates": 3, "get_candidate_context": get_candidate_context}},
    )

    assert requested_codes == ["000001", "000002", "000003"]
    assert all(pick.dsa_context.get("enriched") is True for pick in picks[:3])
    assert all(pick.dsa_context == {} for pick in picks[3:])
    assert notes == ["DSA provider context applied 3 of 3 candidates"]


def test_hard_filter_and_factor_scoring_keep_core_semantics() -> None:
    frame = pd.DataFrame(
        [
            {
                "code": "low_value",
                "name": "Low Value",
                "price": 10.0,
                "amount": 200_000_000,
                "pe_ratio": 5.0,
                "pb_ratio": 0.6,
                "turnover_rate": 2.0,
                "volume_ratio": 1.2,
                "change_pct": 0.0,
            },
            {
                "code": "high_value",
                "name": "High Value",
                "price": 10.0,
                "amount": 20_000_000,
                "pe_ratio": 15.0,
                "pb_ratio": 2.0,
                "turnover_rate": 2.0,
                "volume_ratio": 1.2,
                "change_pct": 0.0,
            },
        ]
    )

    filtered = apply_hard_filters(frame, HardFilterConfig(amount_min=100_000_000))
    assert filtered["code"].tolist() == ["low_value"]

    scored = compute_screen_scores(
        frame,
        ScreeningConfig(factor_weights={"value": 1.0}),
    ).set_index("code")
    assert scored.loc["low_value", "screen_score"] > scored.loc["high_value", "screen_score"]


def test_snapshot_schema_mismatch_counts_toward_source_circuit_breaker(
    monkeypatch,
) -> None:
    bad_snapshot = pd.DataFrame([{"code": "000001", "name": "Ping An", "price": 10.0}])
    good_snapshot = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    source_health: dict[str, dict[str, object]] = {}
    calls: list[str] = []

    def fake_fetch(source: str) -> pd.DataFrame:
        calls.append(source)
        if source == "sina":
            return bad_snapshot
        if source == "efinance":
            return good_snapshot.copy()
        raise AssertionError(f"unexpected source {source}")

    monkeypatch.setattr(screening_snapshot, "_SOURCE_HEALTH", source_health)
    monkeypatch.setattr(screening_snapshot, "fetch_cn_snapshot", fake_fetch)

    for _ in range(3):
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["sina", "efinance"],
            required_columns=["volume_ratio"],
        )
        assert result.attrs["snapshot_source"] == "efinance"

    health = screening_snapshot.snapshot_source_health_snapshot(["sina"])
    assert health["sina"]["failures"] == 3
    assert health["sina"]["disabled"] is True

    calls.clear()
    result = screening_snapshot.fetch_snapshot_with_fallback(
        ["sina", "efinance"],
        required_columns=["volume_ratio"],
    )

    assert calls == ["efinance"]
    assert result.attrs["snapshot_source"] == "efinance"
    assert "temporarily disabled" in result.attrs["source_errors"][0]


def test_sina_snapshot_uses_timeout_wrapper(monkeypatch) -> None:
    expected = pd.DataFrame([{"code": "000001"}])
    captured: dict[str, object] = {}

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        captured["fetcher"] = fetcher
        captured["source"] = source
        return expected

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)
    monkeypatch.setattr(screening_snapshot, "_fetch_sina", lambda: expected)

    result = screening_snapshot.fetch_cn_snapshot("sina")

    assert result is expected
    assert captured["source"] == "sina"
    assert captured["fetcher"] is screening_snapshot._fetch_sina


def test_em_datacenter_snapshot_uses_timeout_wrapper(monkeypatch) -> None:
    expected = pd.DataFrame([{"code": "000001"}])
    captured: dict[str, object] = {}

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        captured["fetcher"] = fetcher
        captured["source"] = source
        return expected

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)
    monkeypatch.setattr(screening_snapshot, "_fetch_em_datacenter", lambda: expected)

    result = screening_snapshot.fetch_cn_snapshot("em_datacenter")

    assert result is expected
    assert captured["source"] == "em_datacenter"
    assert captured["fetcher"] is screening_snapshot._fetch_em_datacenter


def test_em_datacenter_timeout_falls_back_to_last_good_snapshot(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "cached:last_good"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    def fake_wrapper(fetcher, *, source: str) -> pd.DataFrame:
        assert source == "em_datacenter"
        assert fetcher is screening_snapshot._fetch_em_datacenter
        raise RuntimeError("snapshot source em_datacenter timed out")

    monkeypatch.setattr(screening_snapshot, "_call_snapshot_wrapper", fake_wrapper)

    result = screening_snapshot.fetch_snapshot_with_fallback(
        ["em_datacenter"],
        required_columns=["volume_ratio"],
        fallback_snapshot_path=cache_path,
    )

    assert result.attrs["fallback_used"] is True
    assert result.attrs["snapshot_source"] == "last_good_cache"
    assert result.loc[0, "code"] == "000001"


def test_fresh_snapshot_cache_skips_live_sources(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        side_effect=AssertionError("fresh cache should avoid live snapshot calls"),
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_not_called()
    assert result.attrs["snapshot_source"] == "last_good_cache"
    assert result.attrs["cache_used"] is True
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False
    assert result.attrs["last_good_snapshot_source"] == "sina"


def test_fresh_snapshot_cache_ignores_mismatched_source(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "tushare"
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        return_value=live,
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_called_once_with("tushare")
    assert result.loc[0, "code"] == "000002"
    assert result.attrs["snapshot_source"] == "tushare"
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False


def test_fresh_snapshot_cache_ignores_non_primary_source(tmp_path, monkeypatch) -> None:
    cached = pd.DataFrame(
        [{"code": "000001", "name": "Ping An", "price": 10.0, "volume_ratio": 1.5}]
    )
    cached.attrs["snapshot_source"] = "sina"
    cache_path = tmp_path / "snapshot-cache.json"
    screening_snapshot._write_last_good_snapshot(cache_path, cached)

    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "tushare"
    live_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        return_value=live,
    )

    with live_fetch as fetch_mock:
        result = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_called_once_with("tushare")
    assert result.loc[0, "code"] == "000002"
    assert result.attrs["snapshot_source"] == "tushare"
    assert result.attrs["fallback_used"] is False
    assert result.attrs["stale"] is False


def test_fresh_snapshot_cache_reuses_fallback_from_same_source_chain(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "snapshot-cache.json"
    live = pd.DataFrame(
        [{"code": "000002", "name": "Vanke", "price": 11.0, "volume_ratio": 2.0}]
    )
    live.attrs["snapshot_source"] = "sina"

    first_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        autospec=True,
        side_effect=[RuntimeError("tushare unavailable"), live],
    )
    with first_fetch as fetch_mock:
        first = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    assert fetch_mock.call_args_list == [call("tushare"), call("sina")]
    assert first.attrs["snapshot_source"] == "sina"

    second_fetch = patch.object(
        screening_snapshot,
        "fetch_cn_snapshot",
        side_effect=AssertionError("same-chain fresh cache should avoid live calls"),
    )
    with second_fetch as fetch_mock:
        second = screening_snapshot.fetch_snapshot_with_fallback(
            ["tushare", "sina"],
            required_columns=["volume_ratio"],
            fallback_snapshot_path=cache_path,
            cache_ttl_seconds=300,
        )

    fetch_mock.assert_not_called()
    assert second.attrs["snapshot_source"] == "last_good_cache"
    assert second.attrs["last_good_snapshot_source"] == "sina"
    assert second.attrs["fallback_used"] is False


def test_em_datacenter_requests_industry_and_concept(monkeypatch) -> None:
    """em_datacenter 必须随快照一并取回行业/概念。

    这两个字段不额外增加请求也不改变返回行数，却是行业配额、策略内
    portfolio_profile 和 theme_heat 的唯一可靠来源：akshare 的板块接口走
    push2.eastmoney.com，在 GitHub Actions 上稳定 502 / RemoteDisconnected。
    """
    captured: dict = {}

    class _Resp:
        @staticmethod
        def json():
            return {
                "success": True,
                "result": {
                    "count": 2,
                    "data": [
                        {"SECURITY_CODE": "000001", "SECURITY_NAME_ABBR": "平安银行",
                         "NEW_PRICE": 11.0, "INDUSTRY": "银行",
                         "CONCEPT": ["深圳特区", "跨境支付"]},
                        {"SECURITY_CODE": "000007", "SECURITY_NAME_ABBR": "全新好",
                         "NEW_PRICE": 5.0, "INDUSTRY": "汽车",
                         "CONCEPT": "深圳特区"},
                    ],
                },
            }

    def _fake_get(url, **kwargs):
        captured["params"] = kwargs.get("params", {})
        return _Resp()

    monkeypatch.setattr(screening_snapshot, "_eastmoney_get", _fake_get)
    df = screening_snapshot._fetch_em_datacenter()

    assert "INDUSTRY" in captured["params"]["sty"]
    assert "CONCEPT" in captured["params"]["sty"]
    assert list(df["industry"]) == ["银行", "汽车"]
    # CONCEPT 单个时是 str、多个时是 list，都要归一成 `a|b` 文本，
    # 否则 list 会原样进 DataFrame 并炸掉下游的字符串处理。
    assert list(df["concepts"]) == ["深圳特区|跨境支付", "深圳特区"]
    assert df["concepts"].map(type).eq(str).all()


def test_join_label_list_normalizes_concept_shapes() -> None:
    join = screening_snapshot._join_label_list
    assert join(["a", "b"]) == "a|b"
    assert join("a") == "a"
    assert join(["a", "", "  ", "b"]) == "a|b"
    assert join(None) == ""
    assert join(float("nan")) == ""


def _valuation_frame() -> pd.DataFrame:
    """两个大行业（各 10 只）+ 一个小行业（2 只）。

    银行整体 PE 远低于科技，全市场口径下会垄断 value 高分。
    """
    rows = []
    for i in range(10):
        rows.append({"code": f"B{i:02d}", "industry": "银行", "pe_ratio": 4.0 + i * 0.3})
    for i in range(10):
        rows.append({"code": f"T{i:02d}", "industry": "半导体", "pe_ratio": 60.0 + i * 3.0})
    for i in range(2):
        rows.append({"code": f"S{i:02d}", "industry": "小行业", "pe_ratio": 20.0 + i})
    return pd.DataFrame(rows)


def test_rank_score_ranks_within_industry_when_groups_given() -> None:
    df = _valuation_frame()
    pe = df["pe_ratio"]

    glob = screening_scorer._rank_score(pe, lower_is_better=True)
    within = screening_scorer._rank_score(
        pe, lower_is_better=True, groups=df["industry"], min_group_size=5
    )

    banks = df["industry"] == "银行"
    tech = df["industry"] == "半导体"

    # 全市场口径：银行整体碾压科技——差距来自行业属性，不是个股优秀
    assert glob[banks].mean() - glob[tech].mean() > 40
    # 行业中性：两个行业的均值收敛到一起，行业不再是打分的来源
    assert abs(within[banks].mean() - within[tech].mean()) < 1.0
    # 但组内的区分度必须完整保留（不是把所有票压成同一个分）
    assert within[banks].max() - within[banks].min() > 50
    assert within[tech].max() - within[tech].min() > 50


def test_rank_score_falls_back_to_global_for_small_industries() -> None:
    df = _valuation_frame()
    within = screening_scorer._rank_score(
        df["pe_ratio"], lower_is_better=True, groups=df["industry"], min_group_size=5
    )
    glob = screening_scorer._rank_score(df["pe_ratio"], lower_is_better=True)

    small = df["industry"] == "小行业"   # 只有 2 只，低于阈值
    assert (within[small] == glob[small]).all()


def test_rank_score_falls_back_when_industry_is_blank() -> None:
    df = _valuation_frame()
    groups = df["industry"].copy()
    groups.iloc[:5] = ""
    within = screening_scorer._rank_score(
        df["pe_ratio"], lower_is_better=True, groups=groups, min_group_size=5
    )
    glob = screening_scorer._rank_score(df["pe_ratio"], lower_is_better=True)
    assert (within.iloc[:5] == glob.iloc[:5]).all()


def test_rank_score_group_size_counts_valid_values_only() -> None:
    """20 只的行业里只有 2 只有 PE，同样是噪声，必须回退。"""
    df = _valuation_frame()
    pe = df["pe_ratio"].copy()
    banks = df.index[df["industry"] == "银行"]
    pe.loc[banks[2:]] = None          # 银行只剩 2 个有效值

    within = screening_scorer._rank_score(
        pe, lower_is_better=True, groups=df["industry"], min_group_size=5
    )
    glob = screening_scorer._rank_score(pe, lower_is_better=True)
    assert (within.loc[banks[:2]] == glob.loc[banks[:2]]).all()


def test_industry_neutral_is_off_by_default() -> None:
    df = _valuation_frame()
    default_profile = dict(screening_scorer._DEFAULT_SCORING_PROFILE)
    assert default_profile["industry_neutral"] == 0.0

    groups, _ = screening_scorer._industry_groups(df, default_profile)
    assert groups is None

    on = {**default_profile, "industry_neutral": 1.0, "industry_neutral_min_size": 5.0}
    groups, min_size = screening_scorer._industry_groups(df, on)
    assert groups is not None and min_size == 5


def test_industry_neutral_needs_an_industry_column() -> None:
    df = _valuation_frame().drop(columns=["industry"])
    on = {**screening_scorer._DEFAULT_SCORING_PROFILE, "industry_neutral": 1.0}
    groups, _ = screening_scorer._industry_groups(df, on)
    assert groups is None


def test_value_score_neutralization_demotes_a_whole_cheap_industry() -> None:
    df = _valuation_frame()
    off = screening_scorer._compute_value_score(df)
    on = screening_scorer._compute_value_score(
        df, {**screening_scorer._DEFAULT_SCORING_PROFILE,
             "industry_neutral": 1.0, "industry_neutral_min_size": 5.0}
    )
    banks = df["industry"] == "银行"
    assert off[banks].mean() > on[banks].mean() + 15


def test_only_the_intended_strategies_opt_into_industry_neutral() -> None:
    strategies = load_all_strategies(SCREENING_ROOT / "strategies")
    enabled = {
        name for name, s in strategies.items()
        if s.screening.scoring_profile.get("industry_neutral")
    }
    # "选好公司"的策略中性化；dual_low 要的就是全市场最便宜，必须保持原口径
    assert enabled == {"quality_value", "momentum_quality", "balanced_alpha"}
    assert not strategies["dual_low"].screening.scoring_profile.get("industry_neutral")
