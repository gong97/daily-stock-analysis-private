# -*- coding: utf-8 -*-
"""按 history/ 重算 hit_count 的修复脚本。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import repair_watchlist_hit_count as repair  # noqa: E402


def _history(path: Path, run_date: str, picks: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": run_date,
        "cadence": "daily",
        "summaries": [],
        "picks": {
            strategy: [{"code": code, "final_score": 70.0} for code in codes]
            for strategy, codes in picks.items()
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _watchlist(path: Path, entries: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "meta": {},
        "entries": [
            {
                "code": code,
                "hit_count": hits,
                "last_seen": "2026-09-02",
                "first_seen": "2026-08-30",
                "strategies": {
                    "s": {"score": 70.0, "last_seen": "2026-09-02", "bucket": "balanced"}
                },
            }
            for code, hits in entries.items()
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_hits(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {e["code"]: e["hit_count"] for e in payload["entries"]}


def test_counts_distinct_scan_days_not_files(tmp_path: Path) -> None:
    """同一天的 daily 与 weekly 是两个文件，但只算一个扫描日。"""
    hist = tmp_path / "history"
    _history(hist / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})
    _history(hist / "2026-08-31-weekly.json", "2026-08-31", {"b": ["000001"]})
    _history(hist / "2026-09-01-daily.json", "2026-09-01", {"a": ["000001"]})

    assert repair.count_hits_from_history(hist) == {"000001": 2}


def test_repairs_inflated_counts(tmp_path: Path) -> None:
    """修掉 2026-08-30 之前同一天重复运行灌出来的虚高计数。"""
    _history(tmp_path / "history" / "2026-08-30-weekly.json", "2026-08-30", {"a": ["000001"]})
    _history(tmp_path / "history" / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})
    current = tmp_path / "current.json"
    _watchlist(current, {"000001": 7})

    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 0
    assert _load_hits(current) == {"000001": 2}


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    _history(tmp_path / "history" / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})
    current = tmp_path / "current.json"
    _watchlist(current, {"000001": 7})

    assert repair.main(["--watchlist-dir", str(tmp_path), "--dry-run"]) == 0
    assert _load_hits(current) == {"000001": 7}


def test_keeps_entries_absent_from_history(tmp_path: Path) -> None:
    """history 里查不到的条目保持原值——缺记录不等于没命中过，清零会误伤。"""
    _history(tmp_path / "history" / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})
    current = tmp_path / "current.json"
    _watchlist(current, {"000001": 5, "600519": 4})

    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 0
    hits = _load_hits(current)
    assert hits["000001"] == 1
    assert hits["600519"] == 4, "pinned 占位或早于留档的条目不该被清零"


def test_also_fixes_undercounted_entries(tmp_path: Path) -> None:
    """TTL 剪枝后重建的条目会低于真实命中数，同样要修正。"""
    for day in ("2026-08-31", "2026-09-01", "2026-09-02"):
        _history(tmp_path / "history" / f"{day}-daily.json", day, {"a": ["000425"]})
    current = tmp_path / "current.json"
    _watchlist(current, {"000425": 1})

    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 0
    assert _load_hits(current) == {"000425": 3}


def test_is_idempotent(tmp_path: Path) -> None:
    _history(tmp_path / "history" / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})
    current = tmp_path / "current.json"
    _watchlist(current, {"000001": 7})

    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 0
    first = _load_hits(current)
    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 0
    assert _load_hits(current) == first


def test_refuses_to_zero_everything_when_history_is_empty(tmp_path: Path) -> None:
    """history 为空时放弃重算，而不是把所有计数清零。"""
    (tmp_path / "history").mkdir(parents=True)
    current = tmp_path / "current.json"
    _watchlist(current, {"000001": 6})

    assert repair.main(["--watchlist-dir", str(tmp_path)]) == 1
    assert _load_hits(current) == {"000001": 6}


def test_skips_history_files_without_run_date(tmp_path: Path) -> None:
    hist = tmp_path / "history"
    hist.mkdir(parents=True)
    (hist / "broken.json").write_text(json.dumps({"picks": {"a": [{"code": "000001"}]}}), encoding="utf-8")
    _history(hist / "2026-08-31-daily.json", "2026-08-31", {"a": ["000001"]})

    assert repair.count_hits_from_history(hist) == {"000001": 1}
