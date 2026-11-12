#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Macd-Qlib-Analyzer 的持仓账本生成 A 股 DSA 的分析名单（2026-11-12 起，取代手写的 STOCK_LIST 变量）。

为什么
------
STOCK_LIST 变量是 2026-09-03 手写的 24 只，两个多月后与实际持仓差了 15 只（约 16% 仓位，含刚买的）、
多了 3 只已清仓的。名单限在 24 只左右是怕用光 Gemini 免费额度，实测每只只调一次 flash-lite，
真正的成本是 Actions 分钟（每只约 50 秒）。用户选的折中（方案 2）：

    1. 持仓里的股票（不含 ETF / 基金），仓位 >= MIN_WEIGHT（0.5%）的
    2. 仓位再小，最近 RECENT_WEEKDAYS（20）个交易日交易过的（含做 T 相抵的——用户天天做 T 的小仓位也要看）
    3. 卖出后还没买回、波段 T 对账里仍在进行中的（标「待买回」：买回时要用次日区间设限价）

交易日按周一到周五数，不扣节假日（窗口略长几天，宁多勿少）。

数据（只读 token：VALUATION_RED_FLAGS_TOKEN 或 MACD_QLIB_READ_TOKEN，与 src/recent_runup.py 相同）
    portfolio/positions.csv        最新持仓快照（weight）
    portfolio/trade_activity.csv   每天每只的买卖股数，含做 T（2026-11-06 起）
    portfolio/trades.csv           净成交（更早的交易）
    portfolio/swing_review.csv     波段 T 的腿：side=sell、status=进行中、left>0 = 待买回

输出
----
stdout 一行逗号分隔的代码（持仓按仓位从大到小，再接待买回）；--notes-out 写 {code: 说明} 的 JSON，
总览表据此标「待买回」。名单少于 MIN_STOCKS 只或任何一份取不到就退出码 1——工作流沿用 STOCK_LIST 变量，不静默。

    python scripts/holdings_stock_list.py --notes-out data/stock_list_notes.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from typing import Dict, List, Optional

TOKEN_ENVS = ("VALUATION_RED_FLAGS_TOKEN", "MACD_QLIB_READ_TOKEN")
REPO_ENV = "HOLDINGS_REPO"
DEFAULT_REPO = "gong97/Macd-Qlib-Analyzer"
FILES = ("positions.csv", "trade_activity.csv", "trades.csv", "swing_review.csv")
MIN_WEIGHT = 0.005
RECENT_WEEKDAYS = 20
MIN_STOCKS = 5
TIMEOUT_SEC = 20


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def is_a_share_stock(code: str) -> bool:
    """沪深 A 股个股：6 位、0 / 3 / 6 开头。ETF、LOF、基金（1x、5x）与北交所不进名单。"""
    return len(code) == 6 and code.isdigit() and code[0] in "036"


def weekdays_back(today: date, n: int) -> date:
    d = today
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def fetch(repo: str, path: str, token: str) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8-sig")


def rows(text: str) -> List[Dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def build(tables: Dict[str, List[Dict[str, str]]], today: date) -> tuple[List[str], Dict[str, str], List[str]]:
    """返回 (名单, 待买回说明, 日志行)。"""
    since = weekdays_back(today, RECENT_WEEKDAYS).isoformat()
    recent: Dict[str, str] = {}
    for name in ("trade_activity.csv", "trades.csv"):
        for r in tables.get(name, []):
            code = r.get("code", "").zfill(6)
            if r.get("date", "") >= since:
                recent[code] = max(recent.get(code, ""), r["date"])

    pos = [r for r in tables["positions.csv"] if r.get("code") not in ("CASH", "TOTAL")]
    latest = max((r["date"] for r in pos), default="")
    held = []
    for r in pos:
        if r["date"] != latest:
            continue
        code = r["code"].zfill(6)
        try:
            w = float(r.get("weight") or 0)
        except ValueError:
            w = 0.0
        held.append((code, r.get("name", ""), w))
    held.sort(key=lambda x: -x[2])

    out: List[str] = []
    notes: Dict[str, str] = {}
    lines = [f"持仓快照 {latest}，{len(held)} 只；最近交易的窗口从 {since} 起"]
    skipped = []
    for code, name, w in held:
        if not is_a_share_stock(code):
            skipped.append(f"{name}（非个股）")
        elif w >= MIN_WEIGHT or code in recent:
            out.append(code)
            lines.append(f"  持仓 {code} {name} {w:.2%}" + (f"，最近交易 {recent[code]}" if w < MIN_WEIGHT else ""))
        else:
            skipped.append(f"{name} {w:.2%}")
    pending: Dict[str, Dict[str, object]] = {}
    for r in tables.get("swing_review.csv", []):
        code = r.get("code", "").zfill(6)
        try:
            left = float(r.get("left") or 0)
        except ValueError:
            left = 0.0
        if r.get("side") == "sell" and r.get("status") == "进行中" and left > 0 and is_a_share_stock(code):
            p = pending.setdefault(code, {"name": r.get("name", ""), "first": r.get("day", ""), "left": 0.0, "legs": 0})
            p["first"] = min(str(p["first"]), r.get("day", ""))
            p["left"] = float(p["left"]) + left
            p["legs"] = int(p["legs"]) + 1
    for code, p in pending.items():
        if code in out:
            continue  # 还持有（部分买回或没卖完）的已经在持仓里
        out.append(code)
        legs = f"，{p['legs']} 次卖出" if int(p["legs"]) > 1 else ""
        notes[code] = f"待买回（{str(p['first'])[5:]} 起卖出{legs}，还剩 {float(p['left']):,.0f} 股没买回）"
        lines.append(f"  待买回 {code} {p['name']}：{notes[code]}")
    if skipped:
        lines.append("  不分析：" + "、".join(skipped))
    return out, notes, lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--notes-out", help="待买回说明写到这个 JSON")
    ap.add_argument("--today", help="YYYY-MM-DD，测试用")
    args = ap.parse_args(argv)

    token = next((os.getenv(e, "").strip() for e in TOKEN_ENVS if os.getenv(e, "").strip()), "")
    if not token:
        log("[holdings_list] 没有只读 token，沿用 STOCK_LIST 变量")
        return 1
    repo = os.getenv(REPO_ENV, "").strip() or DEFAULT_REPO
    tables = {}
    try:
        for f in FILES:
            tables[f] = rows(fetch(repo, f"portfolio/{f}", token))
    except Exception as exc:  # noqa: BLE001  任何一份取不到都退回变量
        log(f"[holdings_list] 取 {repo} 的账本失败（{f}）：{exc}；沿用 STOCK_LIST 变量")
        return 1
    today = date.fromisoformat(args.today) if args.today else date.today()
    codes, notes, lines = build(tables, today)
    for line in lines:
        log(f"[holdings_list] {line}")
    if len(codes) < MIN_STOCKS:
        log(f"[holdings_list] 只有 {len(codes)} 只，少于 {MIN_STOCKS}，沿用 STOCK_LIST 变量")
        return 1
    if args.notes_out:
        os.makedirs(os.path.dirname(args.notes_out) or ".", exist_ok=True)
        with open(args.notes_out, "w", encoding="utf-8") as fh:
            json.dump(notes, fh, ensure_ascii=False)
    log(f"[holdings_list] 名单 {len(codes)} 只（待买回 {len(notes)} 只）")
    print(",".join(codes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
