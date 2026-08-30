# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Industry and concept enrichment for candidate snapshots."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.services.screening.normalize import (
    normalize_code as _normalize_code,
    safe_float as _safe_float,
    safe_text as _safe_text,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AKSHARE_BOARD_CACHE_SCHEMA = "v1"
_CACHE_DIR_UNSET = object()
_NUMERIC_FIELDS = (
    "industry_rank",
    "industry_change_pct",
    "industry_heat_score",
    "concept_heat_score",
    "board_heat_score",
    "board_heat_latest_score",
    "board_heat_trend_score",
    "board_heat_persistence_score",
    "board_heat_cooling_score",
    "board_heat_observations",
)
_TEXT_FIELDS = ("board_heat_summary", "board_heat_state")
_HEAT_FIELDS = (*_NUMERIC_FIELDS, *_TEXT_FIELDS)
_FIELD_ALIASES = {
    "industry_rank": ["industry_rank", "行业排名", "板块排名", "排名"],
    "industry_change_pct": ["industry_change_pct", "行业涨跌幅", "板块涨跌幅", "涨跌幅"],
    "industry_heat_score": ["industry_heat_score", "行业热度分"],
    "concept_heat_score": ["concept_heat_score", "概念热度分"],
    "board_heat_score": ["board_heat_score", "theme_heat_score", "板块热度分", "主题热度分"],
    "board_heat_latest_score": ["board_heat_latest_score", "板块最新热度分", "主题最新热度分"],
    "board_heat_trend_score": ["board_heat_trend_score", "板块热度趋势分", "主题热度趋势分"],
    "board_heat_persistence_score": ["board_heat_persistence_score", "板块热度持续分", "主题热度持续分"],
    "board_heat_cooling_score": ["board_heat_cooling_score", "板块降温分", "主题降温分"],
    "board_heat_observations": ["board_heat_observations", "板块热度观测数", "主题热度观测数"],
    "board_heat_summary": ["board_heat_summary", "theme_heat_summary", "板块热度", "主题热度"],
    "board_heat_state": ["board_heat_state", "板块热度状态", "主题热度状态"],
}


def enrich_industry_concepts(
    df: pd.DataFrame,
    *,
    map_files: list[str | Path] | None = None,
    provider: str = "none",
    max_boards: int = 80,
    provider_cache_dir: str | Path | None | object = _CACHE_DIR_UNSET,
    provider_cache_ttl_hours: float | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Attach industry/concepts columns from stable files and optional providers."""
    result = df.copy()
    notes: list[str] = []
    if result.empty or "code" not in result.columns:
        return result, notes

    if "industry" not in result.columns:
        result["industry"] = ""
    if "concepts" not in result.columns:
        result["concepts"] = ""
    for field in _NUMERIC_FIELDS:
        if field not in result.columns:
            result[field] = pd.NA
    for field in _TEXT_FIELDS:
        if field not in result.columns:
            result[field] = ""

    mapping: dict[str, dict[str, object]] = {}
    for path_like in map_files or []:
        file_mapping = load_industry_map(path_like)
        trend_mapping, trend_note = _load_companion_board_heat_trends(path_like)
        if trend_mapping:
            _apply_board_heat_trends(file_mapping, trend_mapping)
        _merge_mapping(mapping, file_mapping)
        notes.append(f"industry map loaded: {path_like} rows={len(file_mapping)}")
        if trend_note:
            notes.append(trend_note)

    if provider and provider.lower() not in {"", "none", "off", "false"}:
        if provider.lower() == "akshare":
            provider_mapping, provider_notes = fetch_akshare_board_map(
                max_boards=max_boards,
                cache_dir=provider_cache_dir,
                cache_ttl_hours=provider_cache_ttl_hours,
            )
            _merge_mapping(mapping, provider_mapping)
            notes.extend(provider_notes)
        else:
            notes.append(f"industry provider skipped: unsupported provider={provider}")

    if not mapping:
        return result, notes

    result, filled_industry, filled_concepts, filled_heat = _apply_mapping_to_snapshot(
        result,
        mapping,
    )

    notes.append(
        "industry/concepts enrichment applied: "
        f"industry={filled_industry}, concepts={filled_concepts}, heat={filled_heat}"
    )
    return result, notes


def load_industry_map(path_like: str | Path) -> dict[str, dict[str, object]]:
    """Load code -> industry/concepts mapping from CSV, JSON or JSONL."""
    path = Path(path_like)
    if not path.is_file():
        raise FileNotFoundError(f"Industry map file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = pd.read_csv(path, dtype=str).fillna("").to_dict(orient="records")
    elif suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            rows = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            rows = []
            for code, value in data.items():
                if isinstance(value, dict):
                    rows.append({"code": code, **value})
                elif isinstance(value, str):
                    rows.append({"code": code, "industry": value})
        else:
            rows = []
    else:
        raise ValueError(f"Unsupported industry map format: {path}")

    mapping: dict[str, dict[str, object]] = {}
    for row in rows:
        code = _normalize_code(row.get("code") or row.get("代码"))
        if not code or code == "000000":
            continue
        industry = _safe_text(row.get("industry") or row.get("行业") or row.get("所属行业"))
        concepts = _safe_text(row.get("concepts") or row.get("概念") or row.get("概念题材"))
        item: dict[str, object] = {
            "industry": industry,
            "concepts": concepts,
        }
        for field in _HEAT_FIELDS:
            value = _first_row_value(row, _FIELD_ALIASES.get(field, [field]))
            if field in _NUMERIC_FIELDS:
                parsed = _safe_float(value)
                if parsed is not None:
                    item[field] = int(parsed) if field in {"industry_rank", "board_heat_observations"} else parsed
            else:
                text = _safe_text(value)
                if text:
                    item[field] = text
        mapping[code] = item
    return mapping


def fetch_akshare_board_map(
    *,
    max_boards: int = 80,
    cache_dir: str | Path | None | object = _CACHE_DIR_UNSET,
    cache_ttl_seconds: float | None = None,
    cache_ttl_hours: float | None = None,
    constituents_cache_ttl_hours: float | None = None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Build a code mapping from AkShare industry/concept board constituents.

    The two halves of this data change at very different rates, and the
    expensive half is the slow one:

    - Board **constituents** (``cons_func`` per board) cost up to
      ``2 * max_boards`` requests and only produce ``industry`` / ``concepts``,
      which change on the order of months.
    - Board **heat** (``list_func``) costs 2 requests and produces every
      ``*_heat_score`` / ``*_rank`` / ``*_change_pct`` field, which change daily.

    They are therefore cached separately: constituents under
    ``constituents_cache_ttl_hours`` (long) and heat under ``cache_ttl_hours``
    (short). A daily refresh costs 2 requests instead of ~162.

    Each heat refresh also appends one row per board to a companion
    ``.history.jsonl``, which feeds :func:`load_board_heat_trends` and thereby
    the ``board_heat_trend_score`` / ``persistence`` / ``cooling`` fields.
    Without that history those fields never exist and any strategy tuned on
    them is silently scoring on nothing.
    """
    board_limit = max(int(max_boards), 1)
    notes: list[str] = []
    resolved_cache_dir = _resolve_akshare_board_cache_dir(cache_dir)

    constituents_path = heat_path = history_path = None
    if resolved_cache_dir is not None:
        constituents_path = _board_cache_path(resolved_cache_dir, "constituents", max_boards=board_limit)
        heat_path = _board_cache_path(resolved_cache_dir, "heat", max_boards=board_limit)
        history_path = _board_heat_history_path(resolved_cache_dir, max_boards=board_limit)

    heat_ttl = _resolve_cache_ttl_seconds(
        cache_ttl_seconds=cache_ttl_seconds,
        cache_ttl_hours=cache_ttl_hours,
    )
    constituents_ttl = _resolve_constituents_ttl_seconds(constituents_cache_ttl_hours)

    constituents, note = _read_board_cache(
        constituents_path, kind="constituents", max_boards=board_limit, ttl_seconds=constituents_ttl,
    )
    if note:
        notes.append(note)
    heat, note = _read_board_cache(
        heat_path, kind="heat", max_boards=board_limit, ttl_seconds=heat_ttl,
    )
    if note:
        notes.append(note)

    heat_refreshed = False
    if constituents is None or heat is None:
        import akshare as ak

        board_specs = [
            ("industry", ak.stock_board_industry_name_em, ak.stock_board_industry_cons_em),
            ("concepts", ak.stock_board_concept_name_em, ak.stock_board_concept_cons_em),
        ]
        fetched_heat: dict[str, object] = {}
        fetched_constituents: dict[str, dict[str, object]] = {}
        constituents_ok = True

        for field, list_func, cons_func in board_specs:
            try:
                boards = list_func()
            except Exception as exc:
                notes.append(f"akshare {field} board list failed: {exc}")
                constituents_ok = False
                continue
            board_items = _board_items(boards)[:board_limit]
            fetched_heat[field] = {
                str(item["name"]): {
                    "rank": _safe_float(item.get("rank")),
                    "change_pct": _safe_float(item.get("change_pct")),
                }
                for item in board_items
                if _safe_text(item.get("name"))
            }

            if constituents is not None:
                # 成分缓存还新鲜，本轮只刷热度：这就是从 162 次降到 2 次的地方。
                continue

            loaded = 0
            for board_item in board_items:
                board = str(board_item["name"])
                try:
                    members = cons_func(symbol=board)
                except Exception as exc:
                    notes.append(f"akshare {field} board skipped {board}: {exc}")
                    constituents_ok = False
                    continue
                for _, row in members.iterrows():
                    code = _normalize_code(row.get("代码") or row.get("code"))
                    if not code or code == "000000":
                        continue
                    item = fetched_constituents.setdefault(code, {"industry": "", "concepts": ""})
                    if field == "industry" and not item["industry"]:
                        item["industry"] = board
                    elif field == "concepts":
                        item["concepts"] = _merge_label_text(item.get("concepts", ""), board)
                loaded += 1
            notes.append(f"akshare {field} boards loaded: {loaded}/{len(board_items)}")

        if any(fetched_heat.values()):
            heat = fetched_heat
            heat_refreshed = True
            if heat_path is not None:
                note = _write_board_cache(heat_path, "heat", heat, max_boards=board_limit)
                if note:
                    notes.append(note)
        if constituents is None and fetched_constituents and constituents_ok:
            constituents = fetched_constituents
            if constituents_path is not None:
                note = _write_board_cache(
                    constituents_path, "constituents", fetched_constituents, max_boards=board_limit,
                )
                if note:
                    notes.append(note)
        elif constituents is None and fetched_constituents:
            # 部分板块拉取失败：本轮可用，但不写缓存，避免把残缺映射固化成"新鲜"结果。
            constituents = fetched_constituents
            notes.append("akshare constituents partially failed; cache not written")

    if not constituents:
        return {}, notes

    mapping = _combine_constituents_and_heat(constituents, heat or {})

    if heat_refreshed and history_path is not None and heat:
        note = _append_board_heat_history(history_path, heat)
        if note:
            notes.append(note)

    if history_path is not None and history_path.is_file():
        try:
            trends = load_board_heat_trends(history_path)
        except Exception as exc:
            notes.append(f"board heat trends skipped: {history_path} error={exc}")
        else:
            if trends:
                _apply_board_heat_trends(mapping, trends)
                notes.append(f"board heat trends applied: boards={len(trends)}")

    return mapping, notes


def _combine_constituents_and_heat(
    constituents: dict[str, dict[str, object]],
    heat: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Attach per-board heat fields onto the (separately cached) membership map.

    Reproduces the original single-pass semantics: ``industry`` keeps the first
    board it was assigned, concepts accumulate, and ``board_heat_score`` /
    ``board_heat_summary`` take the max / merged value across every board the
    code belongs to.
    """
    industry_heat = heat.get("industry") if isinstance(heat.get("industry"), dict) else {}
    concept_heat = heat.get("concepts") if isinstance(heat.get("concepts"), dict) else {}

    mapping: dict[str, dict[str, object]] = {}
    for code, source in constituents.items():
        item: dict[str, object] = {
            "industry": _safe_text(source.get("industry")),
            "concepts": _safe_text(source.get("concepts")),
        }

        industry_board = str(item["industry"])
        stats = industry_heat.get(industry_board) if industry_board else None
        if isinstance(stats, dict):
            rank = _safe_float(stats.get("rank"))
            change_pct = _safe_float(stats.get("change_pct"))
            score = _board_heat_score(change_pct=change_pct, rank=rank)
            if rank is not None:
                item["industry_rank"] = int(rank)
            if change_pct is not None:
                item["industry_change_pct"] = change_pct
            item["industry_heat_score"] = score
            item["board_heat_score"] = _max_numeric(item.get("board_heat_score"), score)
            item["board_heat_summary"] = _merge_summary_text(
                _safe_text(item.get("board_heat_summary")),
                _board_heat_summary(industry_board, change_pct=change_pct, rank=rank),
            )

        for concept_board in _split_label_text(item["concepts"]):
            stats = concept_heat.get(concept_board)
            if not isinstance(stats, dict):
                continue
            rank = _safe_float(stats.get("rank"))
            change_pct = _safe_float(stats.get("change_pct"))
            score = _board_heat_score(change_pct=change_pct, rank=rank)
            item["concept_heat_score"] = _max_numeric(item.get("concept_heat_score"), score)
            item["board_heat_score"] = _max_numeric(item.get("board_heat_score"), score)
            item["board_heat_summary"] = _merge_summary_text(
                _safe_text(item.get("board_heat_summary")),
                _board_heat_summary(concept_board, change_pct=change_pct, rank=rank),
            )

        mapping[code] = item
    return mapping


def _split_label_text(value: object) -> list[str]:
    text = _safe_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def save_industry_map(mapping: dict[str, dict[str, object]], path_like: str | Path) -> Path:
    """Persist a code->industry/concepts mapping as CSV or JSON."""
    path = Path(path_like)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "code": code,
            "industry": item.get("industry", ""),
            "concepts": item.get("concepts", ""),
            "industry_rank": item.get("industry_rank", ""),
            "industry_change_pct": item.get("industry_change_pct", ""),
            "industry_heat_score": item.get("industry_heat_score", ""),
            "concept_heat_score": item.get("concept_heat_score", ""),
            "board_heat_score": item.get("board_heat_score", ""),
            "board_heat_latest_score": item.get("board_heat_latest_score", ""),
            "board_heat_trend_score": item.get("board_heat_trend_score", ""),
            "board_heat_persistence_score": item.get("board_heat_persistence_score", ""),
            "board_heat_cooling_score": item.get("board_heat_cooling_score", ""),
            "board_heat_observations": item.get("board_heat_observations", ""),
            "board_heat_summary": item.get("board_heat_summary", ""),
            "board_heat_state": item.get("board_heat_state", ""),
        }
        for code, item in sorted(mapping.items())
    ]
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def _apply_mapping_to_snapshot(
    result: pd.DataFrame,
    mapping: dict[str, dict[str, object]],
) -> tuple[pd.DataFrame, int, int, int]:
    map_df = _mapping_dataframe(mapping)
    if map_df.empty:
        return result, 0, 0, 0

    output = result.copy()
    work = output.copy()
    work["__industry_row"] = range(len(work))
    work["__industry_code"] = work["code"].map(_normalize_code)
    merged = work.merge(map_df, on="__industry_code", how="left", sort=False)
    merged = merged.sort_values("__industry_row", kind="stable")
    merged.index = output.index

    filled_industry = _apply_industry_column(output, merged)
    filled_concepts = _apply_concepts_column(output, merged)
    filled_heat = 0
    for field in _NUMERIC_FIELDS:
        filled_heat += _apply_numeric_column(output, merged, field)
    for field in _TEXT_FIELDS:
        filled_heat += _apply_text_column(output, merged, field)
    return output, filled_industry, filled_concepts, filled_heat


def _mapping_dataframe(mapping: dict[str, dict[str, object]]) -> pd.DataFrame:
    fields = ("industry", "concepts", *_HEAT_FIELDS)
    rows: list[dict[str, object]] = []
    for code, item in mapping.items():
        normalized = _normalize_code(code)
        if not normalized or normalized == "000000" or not isinstance(item, dict):
            continue
        row = {"__industry_code": normalized}
        for field in fields:
            row[f"__map_{field}"] = item.get(field, pd.NA)
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["__industry_code", *(f"__map_{field}" for field in fields)])
    frame = pd.DataFrame(rows)
    return frame.drop_duplicates(subset=["__industry_code"], keep="last")


def _apply_industry_column(output: pd.DataFrame, merged: pd.DataFrame) -> int:
    current = output["industry"].map(_safe_text)
    incoming = merged["__map_industry"].map(_safe_text)
    mask = current.eq("") & incoming.ne("")
    if mask.any():
        output.loc[mask, "industry"] = incoming[mask].to_numpy()
    return int(mask.sum())


def _apply_concepts_column(output: pd.DataFrame, merged: pd.DataFrame) -> int:
    current = output["concepts"].map(_safe_text)
    incoming = merged["__map_concepts"].map(_safe_text)
    candidate_mask = incoming.ne("")
    if not candidate_mask.any():
        return 0
    merged_values = pd.Series(
        [
            _merge_label_text(left, right) if right else left
            for left, right in zip(current.tolist(), incoming.tolist(), strict=False)
        ],
        index=output.index,
    )
    mask = candidate_mask & merged_values.ne(current)
    if mask.any():
        output.loc[mask, "concepts"] = merged_values[mask].to_numpy()
    return int(mask.sum())


def _apply_numeric_column(output: pd.DataFrame, merged: pd.DataFrame, field: str) -> int:
    incoming = merged[f"__map_{field}"].map(_safe_float)
    current = output[field].map(_safe_float)
    mask = _numeric_replacement_mask(field, incoming, current)
    if not mask.any():
        return 0
    values = incoming[mask]
    if field in {"industry_rank", "board_heat_observations"}:
        values = values.map(int)
    output.loc[mask, field] = values.to_numpy()
    return int(mask.sum())


def _apply_text_column(output: pd.DataFrame, merged: pd.DataFrame, field: str) -> int:
    current = output[field].map(_safe_text)
    incoming = merged[f"__map_{field}"].map(_safe_text)
    candidate_mask = incoming.ne("")
    if not candidate_mask.any():
        return 0
    if field == "board_heat_summary":
        merged_values = pd.Series(
            [
                _merge_summary_text(left, right) if right else left
                for left, right in zip(current.tolist(), incoming.tolist(), strict=False)
            ],
            index=output.index,
        )
    else:
        merged_values = pd.Series(
            [left or right for left, right in zip(current.tolist(), incoming.tolist(), strict=False)],
            index=output.index,
        )
    mask = candidate_mask & merged_values.ne(current)
    if mask.any():
        output.loc[mask, field] = merged_values[mask].to_numpy()
    return int(mask.sum())


def _numeric_replacement_mask(field: str, incoming: pd.Series, current: pd.Series) -> pd.Series:
    candidate_mask = incoming.notna()
    missing_mask = current.isna()
    comparable_mask = candidate_mask & ~missing_mask
    wins = pd.Series(False, index=incoming.index)
    if comparable_mask.any():
        new_values = incoming[comparable_mask].astype(float)
        current_values = current[comparable_mask].astype(float)
        if field == "industry_rank":
            wins.loc[comparable_mask] = new_values < current_values
        elif field == "board_heat_observations":
            wins.loc[comparable_mask] = new_values > current_values
        elif field in {"board_heat_latest_score", "board_heat_persistence_score", "board_heat_cooling_score"}:
            wins.loc[comparable_mask] = new_values > current_values
        elif field == "board_heat_trend_score":
            wins.loc[comparable_mask] = new_values.abs() > current_values.abs()
        elif field.endswith("heat_score"):
            wins.loc[comparable_mask] = new_values > current_values
    return candidate_mask & (missing_mask | wins)


def _resolve_akshare_board_cache_dir(cache_dir: str | Path | None | object) -> Path | None:
    if cache_dir is _CACHE_DIR_UNSET:
        return _default_akshare_board_cache_dir()
    if cache_dir is None:
        return None
    return Path(cache_dir)


def _default_akshare_board_cache_dir() -> Path:
    explicit = (
        os.getenv("SCREENING_INDUSTRY_PROVIDER_CACHE_DIR", "").strip()
        or os.getenv("INDUSTRY_PROVIDER_CACHE_DIR", "").strip()
    )
    if explicit:
        return Path(explicit)
    data_dir = Path(os.getenv("SCREENING_DATA_DIR", str(_PROJECT_ROOT / "data")))
    return data_dir / "industry_provider_cache"


def _resolve_cache_ttl_seconds(
    *,
    cache_ttl_seconds: float | None,
    cache_ttl_hours: float | None,
) -> float:
    if cache_ttl_seconds is not None:
        return float(cache_ttl_seconds)
    if cache_ttl_hours is not None:
        return float(cache_ttl_hours) * 3600
    raw_hours = (
        os.getenv("SCREENING_INDUSTRY_PROVIDER_CACHE_TTL_HOURS", "").strip()
        or os.getenv("INDUSTRY_PROVIDER_CACHE_TTL_HOURS", "").strip()
        or "24"
    )
    return max(0.0, float(raw_hours)) * 3600


def _resolve_constituents_ttl_seconds(cache_ttl_hours: float | None) -> float:
    """成分映射的 TTL，默认 30 天；它只随成分调整变化，不需要跟着热度天天重拉。"""
    if cache_ttl_hours is not None:
        return max(0.0, float(cache_ttl_hours)) * 3600
    raw_hours = os.getenv("SCREENING_INDUSTRY_CONSTITUENTS_CACHE_TTL_HOURS", "").strip() or "720"
    try:
        return max(0.0, float(raw_hours)) * 3600
    except ValueError:
        return 720 * 3600


def _board_cache_path(cache_dir: Path, kind: str, *, max_boards: int) -> Path:
    return cache_dir / f"akshare_board_{kind}_{_AKSHARE_BOARD_CACHE_SCHEMA}_max_boards_{int(max_boards)}.json"


def _board_heat_history_path(cache_dir: Path, *, max_boards: int) -> Path:
    return _board_cache_path(cache_dir, "heat", max_boards=max_boards).with_suffix(".json.history.jsonl")


def _cache_age_seconds(path: Path, payload: object) -> float:
    """缓存年龄优先按 payload 里的 created_at 算，取不到才退回文件 mtime。

    这一点在 CI 上是决定性的：`actions/checkout` 会把每个文件的 mtime 重置成
    检出时刻，因此随仓库版本化的缓存用 mtime 判断永远"新鲜"，TTL 完全失效。
    """
    if isinstance(payload, dict):
        created_at = _safe_text(payload.get("created_at"))
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
            except ValueError:
                created = None
            if created is not None:
                if created.tzinfo is not None:
                    created = created.astimezone().replace(tzinfo=None)
                return max((datetime.now() - created).total_seconds(), 0.0)
    try:
        return max(time.time() - path.stat().st_mtime, 0.0)
    except OSError:
        return float("inf")


def _read_board_cache(
    path: Path | None,
    *,
    kind: str,
    max_boards: int,
    ttl_seconds: float,
) -> tuple[dict | None, str]:
    if path is None:
        return None, ""
    if ttl_seconds <= 0:
        return None, f"industry {kind} cache disabled: ttl=0"
    if not path.is_file():
        return None, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"industry {kind} cache skipped: {path} error={exc}"
    if not isinstance(payload, dict):
        return None, f"industry {kind} cache skipped: {path} invalid payload"
    if (
        payload.get("schema") != _AKSHARE_BOARD_CACHE_SCHEMA
        or payload.get("provider") != "akshare"
        or payload.get("kind") != kind
        or int(payload.get("max_boards", 0) or 0) != int(max_boards)
    ):
        return None, f"industry {kind} cache skipped: {path} schema mismatch"

    age_seconds = _cache_age_seconds(path, payload)
    if age_seconds > ttl_seconds:
        return None, (
            f"industry {kind} cache expired: {path} "
            f"age_hours={age_seconds / 3600:.1f} ttl_hours={ttl_seconds / 3600:.1f}"
        )

    data = payload.get("data")
    if kind == "constituents":
        normalized = _normalize_cached_mapping(data)
        if normalized is None:
            return None, f"industry {kind} cache skipped: {path} invalid mapping"
        return normalized, (
            f"industry {kind} cache hit: {path} rows={len(normalized)} "
            f"age_hours={age_seconds / 3600:.1f}"
        )

    if not isinstance(data, dict):
        return None, f"industry {kind} cache skipped: {path} invalid data"
    boards = sum(len(v) for v in data.values() if isinstance(v, dict))
    return data, (
        f"industry {kind} cache hit: {path} boards={boards} "
        f"age_hours={age_seconds / 3600:.1f}"
    )


def _write_board_cache(path: Path, kind: str, data: object, *, max_boards: int) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _AKSHARE_BOARD_CACHE_SCHEMA,
            "provider": "akshare",
            "kind": kind,
            "max_boards": int(max_boards),
            "created_at": datetime.now().isoformat(),
            "data": _json_safe_mapping(data) if kind == "constituents" else data,
        }
        tmp_path = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        return f"industry {kind} cache saved: {path}"
    except Exception as exc:
        return f"industry {kind} cache write failed: {path} error={exc}"


def _append_board_heat_history(path: Path, heat: dict[str, object]) -> str:
    """把本次热度按板块追加一行，供 load_board_heat_trends 计算趋势。

    同一天只记一次：一天内重复运行不应该把 5 次观测的滚动窗口灌满同一天的值。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if f'"generated_at": "{today}' in line or f'"generated_at":"{today}' in line:
                    return ""
    except OSError as exc:
        return f"board heat history read failed: {path} error={exc}"

    rows: list[str] = []
    generated_at = datetime.now().isoformat(timespec="seconds")
    for field_boards in heat.values():
        if not isinstance(field_boards, dict):
            continue
        for board, stats in field_boards.items():
            if not isinstance(stats, dict):
                continue
            score = _board_heat_score(
                change_pct=_safe_float(stats.get("change_pct")),
                rank=_safe_float(stats.get("rank")),
            )
            rows.append(json.dumps(
                {"generated_at": generated_at, "board": board, "max_board_heat_score": score},
                ensure_ascii=False,
            ))
    if not rows:
        return ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
    except OSError as exc:
        return f"board heat history write failed: {path} error={exc}"
    return f"board heat history appended: {path} boards={len(rows)}"


def _normalize_cached_mapping(value: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(value, dict):
        return None
    mapping: dict[str, dict[str, object]] = {}
    for code, raw_item in value.items():
        normalized = _normalize_code(code)
        if not normalized or normalized == "000000" or not isinstance(raw_item, dict):
            continue
        item: dict[str, object] = {
            "industry": _safe_text(raw_item.get("industry")),
            "concepts": _safe_text(raw_item.get("concepts")),
        }
        for field in _NUMERIC_FIELDS:
            parsed = _safe_float(raw_item.get(field))
            if parsed is not None:
                item[field] = int(parsed) if field in {"industry_rank", "board_heat_observations"} else parsed
        for field in _TEXT_FIELDS:
            text = _safe_text(raw_item.get(field))
            if text:
                item[field] = text
        mapping[normalized] = item
    return mapping


def _json_safe_mapping(mapping: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        code: _json_safe_item(item)
        for code, item in sorted(mapping.items())
        if isinstance(item, dict)
    }


def _json_safe_item(item: dict[str, object]) -> dict[str, object]:
    cleaned: dict[str, object] = {
        "industry": _safe_text(item.get("industry")),
        "concepts": _safe_text(item.get("concepts")),
    }
    for field in _NUMERIC_FIELDS:
        value = _safe_float(item.get(field))
        if value is not None:
            cleaned[field] = int(value) if field in {"industry_rank", "board_heat_observations"} else value
    for field in _TEXT_FIELDS:
        text = _safe_text(item.get(field))
        if text:
            cleaned[field] = text
    return cleaned


def _board_names(df: pd.DataFrame) -> list[str]:
    for column in ("板块名称", "名称", "name"):
        if column in df.columns:
            return [_safe_text(item) for item in df[column].tolist() if _safe_text(item)]
    return []


def _board_items(df: pd.DataFrame) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for idx, row in df.iterrows():
        name = _first_row_value(row, ["板块名称", "名称", "name"])
        if not _safe_text(name):
            continue
        rank = _safe_float(_first_row_value(row, ["排名", "序号", "rank"]))
        if rank is None:
            rank = float(idx + 1)
        change_pct = _safe_float(_first_row_value(row, ["涨跌幅", "涨幅", "change_pct"]))
        items.append({
            "name": _safe_text(name),
            "rank": rank,
            "change_pct": change_pct,
        })
    return items


def _merge_mapping(target: dict[str, dict[str, object]], source: dict[str, dict[str, object]]) -> None:
    for code, item in source.items():
        existing = target.setdefault(code, {"industry": "", "concepts": ""})
        if item.get("industry") and not existing.get("industry"):
            existing["industry"] = item["industry"]
        if item.get("concepts"):
            existing["concepts"] = _merge_label_text(existing.get("concepts", ""), item["concepts"])
        if item.get("board_heat_summary"):
            existing["board_heat_summary"] = _merge_summary_text(
                _safe_text(existing.get("board_heat_summary")),
                item.get("board_heat_summary", ""),
            )
        if item.get("board_heat_state") and not existing.get("board_heat_state"):
            existing["board_heat_state"] = item["board_heat_state"]
        for field in _NUMERIC_FIELDS:
            value = _safe_float(item.get(field))
            if value is None:
                continue
            current = _safe_float(existing.get(field))
            if current is None or _should_replace_numeric(field, value, current):
                existing[field] = int(value) if field in {"industry_rank", "board_heat_observations"} else value


def _load_companion_board_heat_trends(path_like: str | Path) -> tuple[dict[str, dict[str, object]], str]:
    path = Path(path_like)
    history_path = path.with_suffix(path.suffix + ".history.jsonl")
    if not history_path.is_file():
        return {}, ""
    try:
        trends = load_board_heat_trends(history_path)
    except Exception as exc:
        return {}, f"board heat trends skipped: {history_path} error={exc}"
    return trends, f"board heat trends loaded: {history_path} boards={len(trends)}"


def load_board_heat_trends(
    path_like: str | Path,
    *,
    window_size: int = 5,
    hot_score: float = 60.0,
    cooling_threshold: float = 5.0,
) -> dict[str, dict[str, object]]:
    """Load board heat trend stats from an industry-cache history JSONL file."""
    path = Path(path_like)
    if not path.is_file():
        raise FileNotFoundError(f"Board heat history file not found: {path}")
    grouped: dict[str, list[dict[str, object]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        board = _safe_text(item.get("board"))
        heat = _safe_float(item.get("max_board_heat_score"))
        if not board or heat is None:
            continue
        if heat < 0 or heat > 100:
            continue
        grouped.setdefault(board, []).append({
            "generated_at": _safe_text(item.get("generated_at")),
            "heat": heat,
        })

    trends: dict[str, dict[str, object]] = {}
    for board, rows in grouped.items():
        ordered = sorted(rows, key=lambda item: str(item.get("generated_at", "")))
        recent = ordered[-max(int(window_size), 1):]
        heat_values = [
            heat
            for heat in (_safe_float(item.get("heat")) for item in recent)
            if heat is not None
        ]
        if not heat_values:
            continue
        first = heat_values[0]
        last = heat_values[-1]
        if first is None or last is None:
            continue
        previous = heat_values[-2] if len(heat_values) >= 2 else last
        trend_score = last - first
        cooling_score = max(previous - last, 0.0)
        persistence_score = sum(1 for heat in heat_values if heat >= hot_score) / len(heat_values) * 100
        trends[board] = {
            "board_heat_latest_score": round(last, 4),
            "board_heat_trend_score": round(trend_score, 4),
            "board_heat_persistence_score": round(persistence_score, 4),
            "board_heat_cooling_score": round(cooling_score, 4),
            "board_heat_observations": len(heat_values),
            "board_heat_state": _board_heat_state(
                trend_score=trend_score,
                cooling_score=cooling_score,
                persistence_score=persistence_score,
                hot_score=hot_score,
                cooling_threshold=cooling_threshold,
            ),
        }
    return trends


def _apply_board_heat_trends(
    mapping: dict[str, dict[str, object]],
    trends: dict[str, dict[str, object]],
) -> None:
    for item in mapping.values():
        boards = _summary_boards(item.get("board_heat_summary", ""))
        matches = [trends[board] for board in boards if board in trends]
        if not matches:
            continue
        best = max(
            matches,
            key=lambda trend: (
                int(trend.get("board_heat_observations", 0) or 0),
                _safe_float(trend.get("board_heat_latest_score")) or 0.0,
                abs(_safe_float(trend.get("board_heat_trend_score")) or 0.0),
            ),
        )
        for field in (
            "board_heat_latest_score",
            "board_heat_trend_score",
            "board_heat_persistence_score",
            "board_heat_cooling_score",
            "board_heat_observations",
            "board_heat_state",
        ):
            if field in best:
                item[field] = best.get(field)


def _summary_boards(value: object) -> list[str]:
    boards = []
    for summary in _merge_summary_text("", value).split("|"):
        board = summary.strip().split(":", 1)[0].strip()
        if board:
            boards.append(board)
    return boards


def _merge_label_text(left: str, right: str) -> str:
    labels: list[str] = []
    seen = set()
    for raw in (left, right):
        for item in str(raw or "").replace("，", ",").replace("、", ",").split(","):
            label = item.strip()
            if label and label.lower() not in {"nan", "none", "<na>"} and label not in seen:
                seen.add(label)
                labels.append(label)
    return ",".join(labels)


def _merge_summary_text(left: object, right: object, *, limit: int = 8) -> str:
    labels: list[str] = []
    seen = set()
    for raw in (left, right):
        for item in str(raw or "").replace("\n", " | ").split("|"):
            label = item.strip()
            if label and label.lower() not in {"nan", "none", "<na>"} and label not in seen:
                seen.add(label)
                labels.append(label)
    return " | ".join(labels[:limit])


def _first_row_value(row: dict | pd.Series, columns: list[str]) -> object:
    for column in columns:
        if column in row:
            return row.get(column)
    return None


def _max_numeric(left: object, right: object) -> float | None:
    left_num = _safe_float(left)
    right_num = _safe_float(right)
    if left_num is None:
        return right_num
    if right_num is None:
        return left_num
    return max(left_num, right_num)


def _should_replace_numeric(field: str, new_value: float, current_value: float) -> bool:
    if field == "industry_rank":
        return new_value < current_value
    if field == "board_heat_observations":
        return new_value > current_value
    if field in {"board_heat_latest_score", "board_heat_persistence_score", "board_heat_cooling_score"}:
        return new_value > current_value
    if field == "board_heat_trend_score":
        return abs(new_value) > abs(current_value)
    if field.endswith("heat_score"):
        return new_value > current_value
    return False


def _board_heat_state(
    *,
    trend_score: float,
    cooling_score: float,
    persistence_score: float,
    hot_score: float,
    cooling_threshold: float,
) -> str:
    if cooling_score >= cooling_threshold:
        return "cooling"
    if trend_score >= cooling_threshold:
        return "warming"
    if persistence_score >= 66.6667 and hot_score > 0:
        return "persistent_hot"
    if trend_score <= -cooling_threshold:
        return "weakening"
    return "flat"


def _board_heat_score(*, change_pct: float | None, rank: float | None) -> float:
    score = 50.0
    if change_pct is not None:
        score += change_pct * 6.0
    if rank is not None and rank > 0:
        score += max(0.0, 12.0 - min(rank, 12.0))
    return round(max(0.0, min(score, 100.0)), 4)


def _board_heat_summary(board: str, *, change_pct: float | None, rank: float | None) -> str:
    parts = [board]
    if change_pct is not None:
        parts.append(f"{change_pct:+.2f}%")
    if rank is not None:
        parts.append(f"rank={int(rank)}")
    return ":".join(parts)
