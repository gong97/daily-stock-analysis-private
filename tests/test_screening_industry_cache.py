# -*- coding: utf-8 -*-
"""行业板块缓存回归：成分/热度两级 TTL、created_at 过期与热度历史。

akshare 被替换成确定性假实现，用例不触网。核心不变量是请求次数：
成分缓存命中时，一次刷新只应发出 2 个板块列表请求，而不是 2 + 2*max_boards。
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.services.screening import industry as industry_module

pytestmark = pytest.mark.unit


class _FakeAkshare:
    """记录调用次数的假 akshare，行业/概念各 2 个板块。"""

    def __init__(self, *, industry_change=(6.0, 1.0), concept_change=(8.0, 2.0)):
        self.list_calls = 0
        self.cons_calls = 0
        self._industry_change = industry_change
        self._concept_change = concept_change

    def _boards(self, names, changes):
        self.list_calls += 1
        return pd.DataFrame(
            [{"板块名称": n, "排名": i + 1, "涨跌幅": c}
             for i, (n, c) in enumerate(zip(names, changes))]
        )

    def stock_board_industry_name_em(self):
        return self._boards(["半导体", "银行"], self._industry_change)

    def stock_board_concept_name_em(self):
        return self._boards(["AI算力", "高股息"], self._concept_change)

    def _cons(self, symbol):
        self.cons_calls += 1
        members = {
            "半导体": ["688111"], "银行": ["601166"],
            "AI算力": ["688111"], "高股息": ["601166"],
        }
        return pd.DataFrame([{"代码": code} for code in members.get(symbol, [])])

    stock_board_industry_cons_em = _cons
    stock_board_concept_cons_em = _cons


@pytest.fixture
def fake_akshare(monkeypatch):
    fake = _FakeAkshare()
    module = types.ModuleType("akshare")
    for name in (
        "stock_board_industry_name_em", "stock_board_concept_name_em",
        "stock_board_industry_cons_em", "stock_board_concept_cons_em",
    ):
        setattr(module, name, getattr(fake, name))
    monkeypatch.setitem(sys.modules, "akshare", module)
    monkeypatch.delenv("SCREENING_INDUSTRY_CONSTITUENTS_CACHE_TTL_HOURS", raising=False)
    monkeypatch.delenv("SCREENING_INDUSTRY_PROVIDER_CACHE_TTL_HOURS", raising=False)
    monkeypatch.delenv("INDUSTRY_PROVIDER_CACHE_TTL_HOURS", raising=False)
    return fake


def _fetch(tmp_path, fake, **kwargs):
    return industry_module.fetch_akshare_board_map(
        max_boards=10, cache_dir=tmp_path, **kwargs
    )


def test_first_run_fetches_both_and_populates_membership_and_heat(tmp_path, fake_akshare):
    mapping, notes = _fetch(tmp_path, fake_akshare)

    assert fake_akshare.list_calls == 2
    assert fake_akshare.cons_calls == 4  # 2 个行业板块 + 2 个概念板块
    assert mapping["688111"]["industry"] == "半导体"
    assert "AI算力" in str(mapping["688111"]["concepts"])
    assert mapping["688111"]["industry_heat_score"] > 0
    assert mapping["688111"]["board_heat_score"] > 0
    assert (tmp_path / "akshare_board_constituents_v1_max_boards_10.json").is_file()
    assert (tmp_path / "akshare_board_heat_v1_max_boards_10.json").is_file()


def test_warm_constituents_cache_makes_heat_refresh_cost_two_requests(tmp_path, fake_akshare):
    """核心收益：成分命中缓存后，日更热度只发 2 个请求而不是 2+2*max_boards。"""
    _fetch(tmp_path, fake_akshare)
    baseline_cons = fake_akshare.cons_calls

    # 热度 TTL 设 0 会禁用热度缓存，强制重取；成分 TTL 保持默认 720 小时
    mapping, notes = _fetch(tmp_path, fake_akshare, cache_ttl_hours=0)

    assert fake_akshare.list_calls == 4          # 又发了 2 个列表请求
    assert fake_akshare.cons_calls == baseline_cons  # 成分一个都没重拉
    assert mapping["688111"]["industry"] == "半导体"
    assert mapping["688111"]["board_heat_score"] > 0


def test_both_caches_warm_issues_no_requests(tmp_path, fake_akshare):
    _fetch(tmp_path, fake_akshare)
    fake_akshare.list_calls = 0
    fake_akshare.cons_calls = 0

    mapping, _ = _fetch(tmp_path, fake_akshare)
    assert fake_akshare.list_calls == 0
    assert fake_akshare.cons_calls == 0
    assert mapping["688111"]["industry"] == "半导体"


def test_expiry_uses_created_at_not_file_mtime(tmp_path, fake_akshare):
    """CI 上 actions/checkout 会重置 mtime，必须按 payload 里的 created_at 判过期。"""
    _fetch(tmp_path, fake_akshare)
    heat_path = tmp_path / "akshare_board_heat_v1_max_boards_10.json"

    payload = json.loads(heat_path.read_text(encoding="utf-8"))
    payload["created_at"] = (datetime.now() - timedelta(hours=48)).isoformat()
    heat_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 重写文件让 mtime 变成"刚刚"——正是 checkout 之后的状态
    assert industry_module._cache_age_seconds(heat_path, payload) > 47 * 3600

    baseline_list = fake_akshare.list_calls
    _fetch(tmp_path, fake_akshare, cache_ttl_hours=24)
    assert fake_akshare.list_calls == baseline_list + 2  # 按 created_at 判定为过期，重取


def test_missing_created_at_falls_back_to_mtime(tmp_path, fake_akshare):
    _fetch(tmp_path, fake_akshare)
    heat_path = tmp_path / "akshare_board_heat_v1_max_boards_10.json"
    payload = json.loads(heat_path.read_text(encoding="utf-8"))
    payload.pop("created_at")
    heat_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    age = industry_module._cache_age_seconds(heat_path, payload)
    assert age < 3600  # 刚写的文件，mtime 兜底给出很小的年龄


def test_heat_history_is_appended_once_per_day_and_feeds_trends(tmp_path, fake_akshare):
    history = tmp_path / "akshare_board_heat_v1_max_boards_10.json.history.jsonl"

    _fetch(tmp_path, fake_akshare)
    assert history.is_file()
    first_rows = history.read_text(encoding="utf-8").strip().splitlines()
    assert len(first_rows) == 4  # 2 行业 + 2 概念
    assert {json.loads(r)["board"] for r in first_rows} == {"半导体", "银行", "AI算力", "高股息"}

    # 同一天再刷一次热度，不应重复追加
    _fetch(tmp_path, fake_akshare, cache_ttl_hours=0)
    assert len(history.read_text(encoding="utf-8").strip().splitlines()) == 4


def test_trends_from_history_are_applied_to_mapping(tmp_path, fake_akshare):
    """有多天历史时，board_heat_trend_score 等字段才会出现在映射里。"""
    history = tmp_path / "akshare_board_heat_v1_max_boards_10.json.history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for day, score in enumerate([40.0, 55.0, 70.0, 85.0], start=1):
        stamp = f"2026-08-2{day}T15:00:00"
        rows.append(json.dumps({"generated_at": stamp, "board": "半导体",
                                "max_board_heat_score": score}, ensure_ascii=False))
    history.write_text("\n".join(rows) + "\n", encoding="utf-8")

    mapping, notes = _fetch(tmp_path, fake_akshare)
    entry = mapping["688111"]
    assert entry["board_heat_trend_score"] > 0        # 持续升温
    assert entry["board_heat_observations"] >= 4
    assert any("board heat trends applied" in note for note in notes)


def test_partial_constituent_failure_is_not_cached(tmp_path, fake_akshare, monkeypatch):
    """部分板块拉取失败时本轮仍可用，但不能把残缺映射固化成"新鲜"缓存。"""
    calls = {"n": 0}
    original = fake_akshare.stock_board_industry_cons_em

    def _flaky(symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("RemoteDisconnected")
        return original(symbol)

    sys.modules["akshare"].stock_board_industry_cons_em = _flaky

    mapping, notes = _fetch(tmp_path, fake_akshare)
    assert mapping  # 本轮仍返回可用数据
    assert any("cache not written" in note for note in notes)
    assert not (tmp_path / "akshare_board_constituents_v1_max_boards_10.json").is_file()
