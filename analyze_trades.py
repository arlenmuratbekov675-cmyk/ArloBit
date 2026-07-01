#!/usr/bin/env python3
"""
ArloBit trade research: merges every paper_trades*.json file, dedupes trades,
and measures the predictive power of each entry feature against outcomes.

Usage: python analyze_trades.py
"""

from __future__ import annotations

import glob
import json
import math
import sys
from collections import defaultdict
from typing import Any

TRADE_FILES = sorted(glob.glob("paper_trades*.json"))


def load_all_trades() -> list[dict[str, Any]]:
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for path in TRADE_FILES:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        trades = payload.get("trades", []) if isinstance(payload, dict) else []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            key = (str(trade.get("mint")), int(float(trade.get("entry_time") or 0)))
            existing = seen.get(key)
            # Prefer closed over open, then the most recently checked snapshot.
            if existing is None:
                seen[key] = trade
                continue
            existing_closed = existing.get("status") == "closed"
            new_closed = trade.get("status") == "closed"
            if new_closed and not existing_closed:
                seen[key] = trade
            elif new_closed == existing_closed and (trade.get("last_checked") or 0) >= (existing.get("last_checked") or 0):
                seen[key] = trade
    return list(seen.values())


def num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        rank = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rank[order[k]] = avg
            i = j + 1
        return rank

    if len(xs) < 3:
        return None
    return pearson(ranks(xs), ranks(ys))


def quantiles(values: list[float]) -> str:
    if not values:
        return "n/a"
    v = sorted(values)

    def q(p: float) -> float:
        idx = p * (len(v) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(v) - 1)
        return v[lo] + (v[hi] - v[lo]) * (idx - lo)

    return f"min={v[0]:.2f} p25={q(0.25):.2f} med={q(0.5):.2f} p75={q(0.75):.2f} max={v[-1]:.2f}"


def summarize(trades: list[dict[str, Any]]) -> str:
    n = len(trades)
    if n == 0:
        return "n=0"
    pnls = [num(t.get("final_pnl_percent")) or 0.0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return f"n={n:<3} win%={wins / n * 100:5.1f} avg={sum(pnls) / n:+7.2f} total={sum(pnls):+8.1f}"


def bucket_report(name: str, trades: list[dict[str, Any]], key_fn) -> None:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    print(f"\n--- {name} ---")
    for bucket in sorted(groups, key=lambda b: -len(groups[b])):
        print(f"  {bucket:<18} {summarize(groups[bucket])}")


def numeric_feature_report(name: str, trades: list[dict[str, Any]], key: str, splits: list[float]) -> None:
    pairs = [(num(t.get(key)), num(t.get("final_pnl_percent")) or 0.0, t) for t in trades]
    pairs = [(x, y, t) for x, y, t in pairs if x is not None]
    if not pairs:
        print(f"\n--- {name}: no data ---")
        return
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    wins = [1.0 if y > 0 else 0.0 for y in ys]
    print(f"\n--- {name} (n={len(pairs)}) ---")
    print(f"  distribution: {quantiles(xs)}")
    r_pnl = pearson(xs, ys)
    rho_pnl = spearman(xs, ys)
    r_win = pearson(xs, wins)
    print(
        f"  corr vs pnl: pearson={r_pnl:+.3f}  spearman={rho_pnl:+.3f}  corr vs win={r_win:+.3f}"
        if r_pnl is not None and rho_pnl is not None and r_win is not None
        else "  corr: n/a"
    )
    edges = [-math.inf, *splits, math.inf]
    for lo, hi in zip(edges, edges[1:]):
        bucket = [(x, y, t) for x, y, t in pairs if lo <= x < hi]
        if not bucket:
            continue
        label = f"[{lo:g}, {hi:g})"
        print(f"  {label:<20} {summarize([t for _, _, t in bucket])}")


def hour_of_day(t: dict[str, Any]) -> str:
    entry = num(t.get("entry_time"))
    if entry is None:
        return "unknown"
    import datetime

    hour = datetime.datetime.fromtimestamp(entry, datetime.timezone.utc).hour
    return f"{hour // 4 * 4:02d}-{hour // 4 * 4 + 4:02d}utc"


def narrative(t: dict[str, Any]) -> str:
    text = f"{t.get('symbol', '')} {t.get('token_name', '')}".lower()
    for tag, words in (
        ("kol/person", ("ansem", "murad", "toly", "trump", "tate", "portnoy", "pompliano", "becker", "vitaly", "alon", "tjr", "barron", "zayed", "musk", "bitboy", "luke")),
        ("bull/meme", ("bull", "moon", "pump")),
        ("animal", ("dog", "cat", "inu", "wif", "trout", "shroom", "potato")),
    ):
        if any(w in text for w in words):
            return tag
    return "other"


def main() -> None:
    trades = load_all_trades()
    closed = [t for t in trades if t.get("status") == "closed"]
    print(f"files: {len(TRADE_FILES)}  unique trades: {len(trades)}  closed: {len(closed)}")
    print(f"\nOVERALL: {summarize(closed)}")

    pnls = sorted(num(t.get("final_pnl_percent")) or 0.0 for t in closed)
    print(f"pnl distribution: {quantiles(pnls)}")

    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    print(f"avg win: {avg_win:+.2f}  avg loss: {avg_loss:+.2f}")
    if avg_win - avg_loss != 0:
        be = -avg_loss / (avg_win - avg_loss) * 100
        print(f"breakeven win rate given these exit sizes: {be:.1f}%")

    bucket_report("exit reason", closed, lambda t: str(t.get("exit_reason") or "?"))
    bucket_report("source", closed, lambda t: str(t.get("source") or "?"))
    bucket_report("creator quality", closed, lambda t: str(t.get("creator_quality") or "?"))
    bucket_report("entry hour (utc, 4h)", closed, hour_of_day)
    bucket_report("narrative (crude)", closed, narrative)

    numeric_feature_report("liquidity at entry ($)", closed, "liquidity_at_entry", [30_000, 50_000, 75_000, 100_000])
    numeric_feature_report("volume 5m at entry ($)", closed, "volume_5m_at_entry", [5_000, 10_000, 20_000])
    numeric_feature_report("vol/liq ratio", closed, "volume_liquidity_ratio_at_entry", [0.15, 0.25, 0.35])
    numeric_feature_report("token age (minutes)", closed, "age_minutes_at_entry", [45, 90, 240, 720])
    numeric_feature_report("price change 5m at entry (%)", closed, "price_change_5m", [-10, 0, 10, 20])
    numeric_feature_report("arlobit score", closed, "arlobit_score", [6.5, 7.5])
    numeric_feature_report("top1 holder %", closed, "top_1_holder_pct", [3, 5, 8])
    numeric_feature_report("top10 holders %", closed, "top_10_holders_pct", [5, 10, 20])
    numeric_feature_report("creator SOL balance", closed, "creator_sol_balance", [1, 5, 20])
    numeric_feature_report("creator wallet age (days)", closed, "creator_wallet_age_days", [45, 75, 120])
    numeric_feature_report("sell impact %", closed, "sell_price_impact_pct", [0.5, 1.0, 2.0])
    numeric_feature_report("max gain reached (%)", closed, "max_gain", [10, 20, 30, 50])

    # Exit engineering: what would alternative exits have done, using max_gain /
    # max_drawdown extremes recorded per trade (coarse: assumes gain before drawdown
    # is unknowable from this data — reports both bounds).
    print("\n--- exit engineering (from max_gain / final pnl) ---")
    for tp in (20, 25, 30, 40, 50):
        hits = [t for t in closed if (num(t.get("max_gain")) or 0.0) >= tp]
        others = [t for t in closed if (num(t.get("max_gain")) or 0.0) < tp]
        avg_rest = sum(num(t.get("final_pnl_percent")) or 0.0 for t in others) / len(others) if others else 0.0
        n = len(closed)
        ev = (len(hits) * tp + sum(num(t.get("final_pnl_percent")) or 0.0 for t in others)) / n if n else 0.0
        print(
            f"  TP at +{tp:>2}%: reached by {len(hits):>2}/{n} ({len(hits) / n * 100:4.1f}%)  "
            f"EV if full exit at TP = {ev:+6.2f}%/trade (others avg {avg_rest:+.2f})"
        )

    # duplicated creators
    creators = defaultdict(list)
    for t in closed:
        w = t.get("creator_wallet")
        if w:
            creators[w].append(t)
    repeats = {w: ts for w, ts in creators.items() if len(ts) > 1}
    print(f"\n--- repeat creator wallets: {len(repeats)} ---")
    for w, ts in repeats.items():
        print(f"  {w[:8]}...  {summarize(ts)}  symbols={[t.get('symbol') for t in ts]}")

    # holder data sanity: top10 == top1 exactly
    same = [t for t in closed if t.get("top_1_holder_pct") is not None and t.get("top_1_holder_pct") == t.get("top_10_holders_pct")]
    print(f"\nholder-data sanity: top10 == top1 exactly in {len(same)}/{len(closed)} closed trades (suspicious if large)")


if __name__ == "__main__":
    main()
