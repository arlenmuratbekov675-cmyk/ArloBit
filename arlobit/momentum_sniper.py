"""Isolated paper-only MOMENTUM_SNIPER strategy.

This module is research-only. It does not change scanner verdicts, filters,
scores, alerts, live execution, the existing paper strategy, or v3 shadow rules.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import Counter
from typing import Any

from arlobit import db

TAKE_PROFIT_PCT = 50.0
STOP_LOSS_PCT = -20.0
MAX_HOLD_SECONDS = 60 * 60
MAX_OPEN_TRADES = 3
LOOP_INTERVAL_SECONDS = 15


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _fmt(value: Any, decimals: int = 2) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number:.{decimals}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _ret(price: float | None, entry: float | None) -> float | None:
    if price is None or entry is None or entry <= 0:
        return None
    return (price / entry - 1.0) * 100.0


def _profit_factor(returns: list[float]) -> float | None:
    wins = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    if losses > 0:
        return wins / losses
    if wins > 0:
        return math.inf
    return None


def _latest_candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cycle = conn.execute("SELECT MAX(cycle_id) FROM candidate_sightings").fetchone()[0]
    if cycle is None:
        return []
    return conn.execute(
        """
        SELECT s.*, t.symbol
        FROM candidate_sightings s
        LEFT JOIN tokens t ON t.mint = s.mint
        WHERE s.cycle_id = ?
        ORDER BY s.seen_at, s.sighting_id
        """,
        (cycle,),
    ).fetchall()


def _latest_price(conn: sqlite3.Connection, mint: str, after_ts: float) -> tuple[float | None, float | None]:
    candle = conn.execute(
        """
        SELECT ts, close
        FROM ohlcv_1m
        WHERE mint = ? AND ts >= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (mint, after_ts),
    ).fetchone()
    if candle and _num(candle["close"]) is not None:
        return _num(candle["close"]), _num(candle["ts"])
    sighting = conn.execute(
        """
        SELECT seen_at, price_usd
        FROM candidate_sightings
        WHERE mint = ? AND seen_at >= ? AND price_usd IS NOT NULL
        ORDER BY seen_at DESC
        LIMIT 1
        """,
        (mint, after_ts),
    ).fetchone()
    if sighting:
        return _num(sighting["price_usd"]), _num(sighting["seen_at"])
    return None, None


def _path_stats(conn: sqlite3.Connection, mint: str, entry_time: float, entry_price: float) -> tuple[float | None, float | None]:
    rows = conn.execute(
        """
        SELECT high, low
        FROM ohlcv_1m
        WHERE mint = ? AND ts >= ? AND ts <= ?
        """,
        (mint, entry_time, entry_time + MAX_HOLD_SECONDS),
    ).fetchall()
    highs = [_num(row["high"]) for row in rows]
    lows = [_num(row["low"]) for row in rows]
    highs = [value for value in highs if value is not None and value > 0]
    lows = [value for value in lows if value is not None and value > 0]
    max_runup = _ret(max(highs), entry_price) if highs else None
    max_drawdown = _ret(min(lows), entry_price) if lows else None
    if max_drawdown is not None:
        max_drawdown = min(0.0, max_drawdown)
    return max_runup, max_drawdown


def _blocked_reason(row: sqlite3.Row) -> str | None:
    checks = (
        ("liquidity_lt_5000", _num(row["liquidity_usd"]) is not None and _num(row["liquidity_usd"]) >= 5000),
        ("vol_m5_lt_1000", _num(row["vol_m5"]) is not None and _num(row["vol_m5"]) >= 1000),
        ("vol_liq_ratio_lt_0_5", _num(row["vol_liq_ratio"]) is not None and _num(row["vol_liq_ratio"]) >= 0.5),
        (
            "buy_sell_ratio_outside_0_45_0_75",
            _num(row["buy_sell_ratio_m5"]) is not None and 0.45 <= _num(row["buy_sell_ratio_m5"]) <= 0.75,
        ),
        ("age_lt_180", _num(row["age_minutes"]) is not None and _num(row["age_minutes"]) >= 180),
        ("not_sellable", str(row["sellable"] or "").lower() == "yes"),
        ("mint_authority_active", row["mint_authority_active"] != 1),
        ("freeze_authority_active", row["freeze_authority_active"] != 1),
        ("missing_price", _num(row["price_usd"]) is not None and _num(row["price_usd"]) > 0),
    )
    for reason, ok in checks:
        if not ok:
            return reason
    return None


def _open_trade(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> bool:
    is_second_wave = 1 if (_num(row["age_minutes"]) or 0.0) >= 180 else 0
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO momentum_sniper_trades (
            mint, symbol, entry_time, entry_price, liquidity_usd, vol_m5,
            vol_liq_ratio, buy_sell_ratio_m5, age_minutes, is_second_wave,
            status, max_runup_pct, max_drawdown_pct, blocked_reason,
            created_at, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,'open',0,0,NULL,?,?)
        """,
        (
            row["mint"],
            row["symbol"],
            _num(row["seen_at"]),
            _num(row["price_usd"]),
            _num(row["liquidity_usd"]),
            _num(row["vol_m5"]),
            _num(row["vol_liq_ratio"]),
            _num(row["buy_sell_ratio_m5"]),
            _num(row["age_minutes"]),
            is_second_wave,
            now,
            now,
        ),
    )
    return cursor.rowcount > 0


def update_open_trades(conn: sqlite3.Connection, now: float) -> int:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM momentum_sniper_trades WHERE status='open' ORDER BY entry_time").fetchall()
    closed = 0
    for trade in rows:
        entry_price = _num(trade["entry_price"])
        entry_time = _num(trade["entry_time"]) or now
        current_price, price_ts = _latest_price(conn, trade["mint"], entry_time)
        pnl = _ret(current_price, entry_price)
        runup, drawdown = _path_stats(conn, trade["mint"], entry_time, entry_price or 0.0)
        runup = max(_num(trade["max_runup_pct"]) or 0.0, runup if runup is not None else pnl if pnl is not None else 0.0)
        drawdown = min(_num(trade["max_drawdown_pct"]) or 0.0, drawdown if drawdown is not None else pnl if pnl is not None else 0.0)

        exit_reason = None
        if pnl is not None and pnl >= TAKE_PROFIT_PCT:
            exit_reason = "take_profit"
        elif pnl is not None and pnl <= STOP_LOSS_PCT:
            exit_reason = "stop_loss"
        elif now - entry_time >= MAX_HOLD_SECONDS:
            exit_reason = "max_hold"

        if exit_reason:
            conn.execute(
                """
                UPDATE momentum_sniper_trades
                SET exit_time=?, exit_price=?, exit_reason=?, pnl_pct=?, status='closed',
                    max_runup_pct=?, max_drawdown_pct=?, updated_at=?
                WHERE id=?
                """,
                (price_ts or now, current_price, exit_reason, pnl, runup, drawdown, now, trade["id"]),
            )
            closed += 1
        else:
            conn.execute(
                """
                UPDATE momentum_sniper_trades
                SET max_runup_pct=?, max_drawdown_pct=?, updated_at=?
                WHERE id=?
                """,
                (runup, drawdown, now, trade["id"]),
            )
    return closed


def _run_once_with_stats(conn: sqlite3.Connection) -> tuple[list[str], dict[str, int]]:
    now = _now()
    closed = update_open_trades(conn, now)
    rows = _latest_candidate_rows(conn)
    opened = 0
    blocked: Counter[str] = Counter()
    open_count = conn.execute("SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open'").fetchone()[0]
    open_mints = {
        row[0] for row in conn.execute("SELECT mint FROM momentum_sniper_trades WHERE status='open'").fetchall()
    }

    for row in rows:
        reason = _blocked_reason(row)
        if reason is None and row["mint"] in open_mints:
            reason = "already_open_for_mint"
        if reason is None and open_count >= MAX_OPEN_TRADES:
            reason = "max_open_trades"
        if reason is not None:
            blocked[reason] += 1
            continue
        if _open_trade(conn, row, now):
            opened += 1
            open_count += 1
            open_mints.add(row["mint"])
        else:
            blocked["duplicate_trade"] += 1

    conn.commit()
    open_after = conn.execute("SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open'").fetchone()[0]
    lines = [
        "=== MOMENTUM_SNIPER RUN ===",
        f"candidates evaluated: {len(rows)}",
        f"opened: {opened}",
        f"closed: {closed}",
        "blocked reason counts:",
    ]
    if blocked:
        lines.extend(f"- {reason}: {count}" for reason, count in blocked.most_common())
    else:
        lines.append("- none: 0")
    lines.append(f"open trades count: {open_after}")
    lines.append("=== END RUN ===")
    stats = {
        "candidates_evaluated": len(rows),
        "opened": opened,
        "closed": closed,
        "open_trades": open_after,
    }
    return lines, stats


def run_once(conn: sqlite3.Connection) -> list[str]:
    lines, _stats = _run_once_with_stats(conn)
    return lines


def run_loop() -> None:
    print("MOMENTUM_SNIPER loop started; press Ctrl+C to stop", flush=True)
    try:
        while True:
            try:
                conn = db.connect()
                try:
                    _lines, stats = _run_once_with_stats(conn)
                finally:
                    conn.close()
                print(
                    f"{_iso(_now())} "
                    f"candidates evaluated={stats['candidates_evaluated']} "
                    f"opened={stats['opened']} "
                    f"closed={stats['closed']} "
                    f"open trades={stats['open_trades']}",
                    flush=True,
                )
            except Exception as exc:  # Keep service mode alive for transient DB/API issues.
                print(f"{_iso(_now())} loop error: {exc}", flush=True)
            time.sleep(LOOP_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("MOMENTUM_SNIPER loop stopped", flush=True)


def _bucket_liquidity(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 10000:
        return "5k-10k"
    if value < 25000:
        return "10k-25k"
    if value < 50000:
        return "25k-50k"
    return "50k+"


def _bucket_volume(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 2500:
        return "1k-2.5k"
    if value < 5000:
        return "2.5k-5k"
    if value < 10000:
        return "5k-10k"
    return "10k+"


def _bucket_vol_liq(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1:
        return "0.5-1"
    if value < 2:
        return "1-2"
    return "2+"


def _bucket_ratio(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.55:
        return "0.45-0.55"
    if value < 0.65:
        return "0.55-0.65"
    return "0.65-0.75"


def _summary(values: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    if not values:
        return None, None, None, None
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    return (
        sum(values) / len(values),
        statistics.median(values),
        sum(wins) / len(wins) if wins else None,
        sum(losses) / len(losses) if losses else None,
    )


def _group_lines(title: str, groups: dict[str, list[sqlite3.Row]]) -> list[str]:
    lines = [title, "bucket                  n closed win_rate avg_pnl median_pnl"]
    for bucket, rows in sorted(groups.items()):
        closed = [row for row in rows if row["status"] == "closed" and row["pnl_pct"] is not None]
        returns = [_num(row["pnl_pct"]) for row in closed]
        returns = [value for value in returns if value is not None]
        avg, med, _avg_win, _avg_loss = _summary(returns)
        wins = sum(1 for value in returns if value > 0)
        win_rate = wins / len(returns) * 100 if returns else None
        lines.append(
            f"{bucket:<22} {len(rows):>3} {len(closed):>6}"
            f" {_pct(win_rate):>8} {_fmt(avg):>7} {_fmt(med):>10}"
        )
    return lines


def report_lines(conn: sqlite3.Connection) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM momentum_sniper_trades ORDER BY entry_time").fetchall()
    closed = [row for row in rows if row["status"] == "closed"]
    open_rows = [row for row in rows if row["status"] == "open"]
    returns = [_num(row["pnl_pct"]) for row in closed]
    returns = [value for value in returns if value is not None]
    avg, med, avg_win, avg_loss = _summary(returns)
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    best = max(closed, key=lambda row: _num(row["pnl_pct"]) or -math.inf) if closed else None
    worst = min(closed, key=lambda row: _num(row["pnl_pct"]) or math.inf) if closed else None
    max_drawdown = min((_num(row["max_drawdown_pct"]) for row in rows if _num(row["max_drawdown_pct"]) is not None), default=None)

    lines = [
        "=== MOMENTUM_SNIPER REPORT ===",
        "isolated paper-only strategy; no live execution, alerts, scanner verdicts, scoring, or existing paper strategy changes",
        f"total trades: {len(rows)}",
        f"open trades: {len(open_rows)}",
        f"closed trades: {len(closed)}",
        f"win rate: {_pct(len(wins) / len(returns) * 100 if returns else None)}",
        f"avg win: {_fmt(avg_win)}",
        f"avg loss: {_fmt(avg_loss)}",
        f"profit factor: {_fmt(_profit_factor(returns))}",
        f"average pnl: {_fmt(avg)}",
        f"median pnl: {_fmt(med)}",
        f"best trade: {best['mint'] if best else '-'} {_fmt(best['pnl_pct'] if best else None)}",
        f"worst trade: {worst['mint'] if worst else '-'} {_fmt(worst['pnl_pct'] if worst else None)}",
        f"max drawdown: {_fmt(max_drawdown)}",
        "",
    ]

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["is_second_wave"]), []).append(row)
    lines.extend(_group_lines("By is_second_wave:", groups))

    group_specs = (
        ("By liquidity bucket:", lambda row: _bucket_liquidity(_num(row["liquidity_usd"]))),
        ("By volume bucket:", lambda row: _bucket_volume(_num(row["vol_m5"]))),
        ("By vol_liq_ratio bucket:", lambda row: _bucket_vol_liq(_num(row["vol_liq_ratio"]))),
        ("By buy_sell_ratio bucket:", lambda row: _bucket_ratio(_num(row["buy_sell_ratio_m5"]))),
        ("By exit_reason:", lambda row: str(row["exit_reason"] or row["status"])),
    )
    for title, bucket_fn in group_specs:
        groups = {}
        for row in rows:
            groups.setdefault(bucket_fn(row), []).append(row)
        lines.extend(["", *_group_lines(title, groups)])
    lines.append("=== END REPORT ===")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MOMENTUM_SNIPER isolated paper-only strategy")
    parser.add_argument("--run-once", action="store_true", help="evaluate latest candidates and update open trades")
    parser.add_argument("--report", action="store_true", help="print MOMENTUM_SNIPER report")
    parser.add_argument("--loop", action="store_true", help="run MOMENTUM_SNIPER continuously every 15 seconds")
    args = parser.parse_args(argv)

    if args.loop:
        run_loop()
        return 0

    conn = db.connect()
    try:
        if args.run_once:
            print("\n".join(run_once(conn)))
        if args.report or not (args.run_once or args.report):
            print("\n".join(report_lines(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
