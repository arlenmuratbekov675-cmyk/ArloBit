"""Isolated paper-only MOMENTUM_SNIPER strategy.

This module is research-only. It does not change scanner verdicts, filters,
scores, alerts, live execution, the existing paper strategy, or v3 shadow rules.
"""

from __future__ import annotations

import argparse
import os
import math
import sqlite3
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from arlobit import db
from scanner_v0 import DEFAULT_SOLANA_RPC_URL, build_session, fetch_current_price, fetch_holder_status, helius_rpc_url

GOLDEN_WINDOW_SCALP_V1 = "GOLDEN_WINDOW_SCALP_V1"
GOLDEN_WINDOW_SCALP_V2 = "GOLDEN_WINDOW_SCALP_V2"
GOLDEN_WINDOW_SCALP_V3 = "GOLDEN_WINDOW_SCALP_V3"
STRATEGY_VERSION = GOLDEN_WINDOW_SCALP_V3
SCALP_STRATEGY_VERSIONS = {GOLDEN_WINDOW_SCALP_V1, GOLDEN_WINDOW_SCALP_V2, GOLDEN_WINDOW_SCALP_V3}
TAKE_PROFIT_PCT = 30.0
STOP_LOSS_PCT = -20.0
MAX_HOLD_SECONDS = 30 * 60
V3_TAKE_PROFIT_PCT = 50.0
V3_MAX_HOLD_SECONDS = 5 * 60
V3_MAX_OPEN_TRADES = 1
LEGACY_TAKE_PROFIT_PCT = 50.0
LEGACY_STOP_LOSS_PCT = -20.0
LEGACY_MAX_HOLD_SECONDS = 60 * 60
MAX_OPEN_TRADES = 3
LOOP_INTERVAL_SECONDS = 15
OPEN_TRADE_POLL_SECONDS = 12
MIN_HOLD_SECONDS = OPEN_TRADE_POLL_SECONDS
PRICE_REQUEST_DELAY_SECONDS = 0.5
MAX_ENRICHMENT_ATTEMPTS_PER_CYCLE = 3
ENRICHMENT_DELAY_SECONDS = 1.0
ENRICHMENT_TIMEOUT_SECONDS = 5.0


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


def _fresh_dexscreener_price(mint: str) -> tuple[float | None, float, str]:
    checked_at = _now()
    session = build_session()
    try:
        return fetch_current_price(session, mint, 10), checked_at, "dexscreener_token_pairs"
    except Exception:
        return None, checked_at, "dexscreener_token_pairs_error"
    finally:
        session.close()


def _strategy_params(trade: sqlite3.Row | None = None) -> tuple[float, float | None, int]:
    version = trade["strategy_version"] if trade is not None and "strategy_version" in trade.keys() else STRATEGY_VERSION
    if version == GOLDEN_WINDOW_SCALP_V3:
        return V3_TAKE_PROFIT_PCT, None, V3_MAX_HOLD_SECONDS
    if trade is not None and version not in SCALP_STRATEGY_VERSIONS:
        return LEGACY_TAKE_PROFIT_PCT, LEGACY_STOP_LOSS_PCT, LEGACY_MAX_HOLD_SECONDS
    return TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_HOLD_SECONDS


def _path_stats(
    conn: sqlite3.Connection,
    mint: str,
    entry_time: float,
    entry_price: float,
    max_hold_seconds: int,
) -> tuple[float | None, float | None]:
    rows = conn.execute(
        """
        SELECT high, low
        FROM ohlcv_1m
        WHERE mint = ? AND ts >= ? AND ts <= ?
        """,
        (mint, entry_time, entry_time + max_hold_seconds),
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


def _passes_age_liquidity_sells(row: sqlite3.Row) -> bool:
    return (
        _num(row["age_minutes"]) is not None
        and 10 <= _num(row["age_minutes"]) <= 60
        and _num(row["liquidity_usd"]) is not None
        and _num(row["liquidity_usd"]) >= 5000
        and _num(row["sells_m5"]) is not None
        and _num(row["sells_m5"]) >= 30.5
    )


def _passes_v2_base_filters(row: sqlite3.Row) -> bool:
    return (
        _num(row["age_minutes"]) is not None
        and 10 <= _num(row["age_minutes"]) <= 60
        and _num(row["liquidity_usd"]) is not None
        and _num(row["liquidity_usd"]) >= 10000
        and _num(row["vol_m5"]) is not None
        and _num(row["vol_m5"]) >= 10000
        and _num(row["sells_m5"]) is not None
        and _num(row["sells_m5"]) >= 30.5
        and _num(row["vol_liq_ratio"]) is not None
        and _num(row["vol_liq_ratio"]) >= 2.0
    )


def _blocked_reason_v1(row: sqlite3.Row) -> str | None:
    checks = (
        ("age_outside_10_60", _num(row["age_minutes"]) is not None and 10 <= _num(row["age_minutes"]) <= 60),
        ("liquidity_lt_5000", _num(row["liquidity_usd"]) is not None and _num(row["liquidity_usd"]) >= 5000),
        ("sells_m5_lt_30_5", _num(row["sells_m5"]) is not None and _num(row["sells_m5"]) >= 30.5),
        ("top10_pct_too_high", _num(row["top10_pct"]) is None or _num(row["top10_pct"]) <= 16.1),
        ("missing_price", _num(row["price_usd"]) is not None and _num(row["price_usd"]) > 0),
    )
    for reason, ok in checks:
        if not ok:
            return reason
    return None


def _blocked_reason_v2(row: sqlite3.Row) -> str | None:
    checks = (
        ("age_outside_10_60", _num(row["age_minutes"]) is not None and 10 <= _num(row["age_minutes"]) <= 60),
        ("liquidity_lt_10000", _num(row["liquidity_usd"]) is not None and _num(row["liquidity_usd"]) >= 10000),
        ("vol_m5_lt_10000", _num(row["vol_m5"]) is not None and _num(row["vol_m5"]) >= 10000),
        ("sells_m5_lt_30_5", _num(row["sells_m5"]) is not None and _num(row["sells_m5"]) >= 30.5),
        ("vol_liq_ratio_lt_2_0", _num(row["vol_liq_ratio"]) is not None and _num(row["vol_liq_ratio"]) >= 2.0),
        ("missing_price", _num(row["price_usd"]) is not None and _num(row["price_usd"]) > 0),
    )
    for reason, ok in checks:
        if not ok:
            return reason
    return None


def _holder_check(row: sqlite3.Row) -> str:
    top10 = _num(row["top10_pct"])
    if top10 is None:
        return "unavailable"
    if top10 <= 16.1:
        return "passed"
    return "failed_high"


def _update_holder_fields(conn: sqlite3.Connection, sighting_id: int, holder: Any) -> sqlite3.Row:
    conn.execute(
        """
        UPDATE candidate_sightings
        SET enriched=1,
            top1_pct=?,
            top10_pct=?,
            top20_pct=?,
            holder_status=?
        WHERE sighting_id=?
        """,
        (
            _num(getattr(holder, "top_1_holder_pct", None)),
            _num(getattr(holder, "top_10_holders_pct", None)),
            _num(getattr(holder, "top_20_holders_pct", None)),
            str(getattr(holder, "status", "unknown")),
            sighting_id,
        ),
    )
    return conn.execute(
        """
        SELECT s.*, t.symbol
        FROM candidate_sightings s
        LEFT JOIN tokens t ON t.mint = s.mint
        WHERE s.sighting_id = ?
        """,
        (sighting_id,),
    ).fetchone()


def _fetch_holder_with_timeout(mint: str) -> tuple[Any | None, float, bool]:
    start = time.monotonic()
    rpc_url = os.environ.get("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL)
    helius_url = helius_rpc_url(os.environ.get("HELIUS_API_KEY"))

    def fetch() -> Any:
        session = build_session()
        try:
            return fetch_holder_status(session, mint, rpc_url, helius_url, int(ENRICHMENT_TIMEOUT_SECONDS))
        finally:
            session.close()

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fetch)
    try:
        holder = future.result(timeout=ENRICHMENT_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return None, (time.monotonic() - start) * 1000.0, True
    except Exception:
        executor.shutdown(wait=False, cancel_futures=True)
        return None, (time.monotonic() - start) * 1000.0, False
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    if elapsed_ms > ENRICHMENT_TIMEOUT_SECONDS * 1000.0:
        return None, elapsed_ms, True
    return holder, elapsed_ms, False


def _maybe_enrich_top10(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    attempted_mints: set[str],
    stats: dict[str, int | float],
) -> sqlite3.Row:
    if not _passes_age_liquidity_sells(row):
        return row
    if _num(row["top10_pct"]) is not None:
        stats["top10_already_available"] += 1
        return row
    if row["mint"] in attempted_mints:
        return row
    if stats["enrichment_attempts"] >= MAX_ENRICHMENT_ATTEMPTS_PER_CYCLE:
        return row
    if stats["enrichment_attempts"] > 0:
        time.sleep(ENRICHMENT_DELAY_SECONDS)

    attempted_mints.add(row["mint"])
    stats["enrichment_attempts"] += 1
    holder, elapsed_ms, timed_out = _fetch_holder_with_timeout(row["mint"])
    stats["enrichment_time_total_ms"] += elapsed_ms
    if timed_out:
        stats["enrichment_timeouts"] += 1
        return row
    if holder is None or getattr(holder, "status", None) != "ok" or _num(getattr(holder, "top_10_holders_pct", None)) is None:
        return row

    stats["enrichment_successes"] += 1
    refreshed = _update_holder_fields(conn, int(row["sighting_id"]), holder)
    return refreshed or row


def _open_trade(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    now: float,
    strategy_version: str,
    holder_check: str | None,
) -> bool:
    is_second_wave = 0
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO momentum_sniper_trades (
            mint, symbol, entry_time, entry_price, liquidity_usd, vol_m5,
            vol_liq_ratio, buy_sell_ratio_m5, age_minutes, is_second_wave,
            status, max_runup_pct, max_drawdown_pct, blocked_reason,
            strategy_version, holder_check, entry_sighting_id, entry_price_source,
            created_at, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,'open',0,0,NULL,?,?,?,?,?,?)
        """,
        (
            row["mint"],
            row["symbol"],
            now,
            _num(row["price_usd"]),
            _num(row["liquidity_usd"]),
            _num(row["vol_m5"]),
            _num(row["vol_liq_ratio"]),
            _num(row["buy_sell_ratio_m5"]),
            _num(row["age_minutes"]),
            is_second_wave,
            strategy_version,
            holder_check,
            row["sighting_id"],
            f"candidate_sightings:{row['sighting_id']}",
            now,
            now,
        ),
    )
    return cursor.rowcount > 0


def _record_price_check(
    conn: sqlite3.Connection,
    trade: sqlite3.Row,
    checked_at: float,
    price: float | None,
    pnl: float | None,
    source: str,
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO momentum_sniper_price_checks (
            trade_id, mint, checked_at, price, pnl_pct, source, created_at
        )
        VALUES (?,?,?,?,?,?,?)
        """,
        (trade["id"], trade["mint"], checked_at, price, pnl, source, now),
    )


def update_open_trades(conn: sqlite3.Connection, now: float, request_delay_seconds: float = 0.0) -> int:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM momentum_sniper_trades WHERE status='open' ORDER BY entry_time").fetchall()
    closed = 0
    for index, trade in enumerate(rows):
        if index and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
        take_profit_pct, stop_loss_pct, max_hold_seconds = _strategy_params(trade)
        entry_price = _num(trade["entry_price"])
        entry_time = _num(trade["entry_time"]) or now
        current_price, price_ts, price_source = _fresh_dexscreener_price(trade["mint"])
        pnl = _ret(current_price, entry_price)
        _record_price_check(conn, trade, price_ts, current_price, pnl, price_source, now)
        runup = max(_num(trade["max_runup_pct"]) or 0.0, pnl if pnl is not None else 0.0)
        drawdown = min(_num(trade["max_drawdown_pct"]) or 0.0, pnl if pnl is not None else 0.0)

        exit_reason = None
        hold_seconds = now - entry_time
        version = trade["strategy_version"] if "strategy_version" in trade.keys() else None
        if hold_seconds >= MIN_HOLD_SECONDS and pnl is not None and pnl >= take_profit_pct:
            exit_reason = "take_profit"
        elif (
            version != GOLDEN_WINDOW_SCALP_V3
            and stop_loss_pct is not None
            and hold_seconds >= MIN_HOLD_SECONDS
            and pnl is not None
            and pnl <= stop_loss_pct
        ):
            exit_reason = "stop_loss"
        elif version == GOLDEN_WINDOW_SCALP_V3 and hold_seconds >= max_hold_seconds:
            exit_reason = "time_exit"
        elif version != GOLDEN_WINDOW_SCALP_V3 and hold_seconds >= max_hold_seconds:
            exit_reason = "max_hold"
        if version == GOLDEN_WINDOW_SCALP_V3 and exit_reason == "stop_loss":
            exit_reason = None

        if exit_reason:
            conn.execute(
                """
                UPDATE momentum_sniper_trades
                SET exit_time=?, exit_price=?, exit_reason=?, pnl_pct=?, status='closed',
                    max_runup_pct=?, max_drawdown_pct=?, exit_source=?, exit_source_time=?,
                    time_to_exit_seconds=?, updated_at=?
                WHERE id=?
                """,
                (
                    now,
                    current_price,
                    exit_reason,
                    pnl,
                    runup,
                    drawdown,
                    price_source,
                    price_ts,
                    hold_seconds,
                    now,
                    trade["id"],
                ),
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


def _run_once_with_stats(
    conn: sqlite3.Connection,
    open_trade_request_delay_seconds: float = 0.0,
) -> tuple[list[str], dict[str, int]]:
    now = _now()
    closed = update_open_trades(conn, now, open_trade_request_delay_seconds)
    rows = _latest_candidate_rows(conn)
    opened = 0
    blocked: Counter[str] = Counter()
    enrichment_stats: dict[str, int | float] = {
        "passing_age_liquidity_sells": 0,
        "passing_v2_filters": 0,
        "top10_already_available": 0,
        "enrichment_attempts": 0,
        "enrichment_successes": 0,
        "enrichment_timeouts": 0,
        "enrichment_time_total_ms": 0.0,
        "opened_holder_passed": 0,
        "opened_holder_unavailable": 0,
    }
    attempted_enrichment_mints: set[str] = set()
    open_count = conn.execute("SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open'").fetchone()[0]
    open_mints = {
        row[0] for row in conn.execute("SELECT mint FROM momentum_sniper_trades WHERE status='open'").fetchall()
    }
    open_v3_count = conn.execute(
        "SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open' AND strategy_version=?",
        (GOLDEN_WINDOW_SCALP_V3,),
    ).fetchone()[0]
    open_v3_mints = {
        row[0]
        for row in conn.execute(
            "SELECT mint FROM momentum_sniper_trades WHERE status='open' AND strategy_version=?",
            (GOLDEN_WINDOW_SCALP_V3,),
        ).fetchall()
    }

    for row in rows:
        if _passes_v2_base_filters(row):
            enrichment_stats["passing_v2_filters"] += 1
        reason = _blocked_reason_v2(row)
        if reason is None and row["mint"] in open_v3_mints:
            reason = "already_open_for_mint_v3"
        if reason is None and open_v3_count >= V3_MAX_OPEN_TRADES:
            reason = "max_open_trades_v3"
        if reason is not None:
            blocked[reason] += 1
            continue
        holder_check = None
        if _open_trade(conn, row, now, STRATEGY_VERSION, holder_check):
            opened += 1
            open_v3_count += 1
            open_v3_mints.add(row["mint"])
        else:
            blocked["duplicate_trade"] += 1

    conn.commit()
    open_after = conn.execute("SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open'").fetchone()[0]
    version_counts = _strategy_version_counts(conn)
    lines = [
        "=== MOMENTUM_SNIPER RUN ===",
        f"strategy version: {STRATEGY_VERSION}",
        "entry: age 10-60m, liquidity_usd >= 10000, vol_m5 >= 10000, "
        "sells_m5 >= 30.5, vol_liq_ratio >= 2.0",
        f"exit: TP +{V3_TAKE_PROFIT_PCT:.0f}%, no SL, max hold {V3_MAX_HOLD_SECONDS // 60}m",
        f"max open V3 trades: {V3_MAX_OPEN_TRADES}",
        f"candidates evaluated: {len(rows)}",
        f"candidates passing V3 filters: {int(enrichment_stats['passing_v2_filters'])}",
        f"opened total: {opened}",
        f"closed: {closed}",
        "blocked reason counts:",
    ]
    if blocked:
        lines.extend(f"- {reason}: {count}" for reason, count in blocked.most_common())
    else:
        lines.append("- none: 0")
    lines.append(f"open trades count: {open_after}")
    lines.append("strategy_version counts:")
    lines.extend(f"- {version}: {count}" for version, count in version_counts.items())
    lines.append(f"fast polling active: no (--run-once updates open trades once; --loop polls every {OPEN_TRADE_POLL_SECONDS}s)")
    lines.append("=== END RUN ===")
    stats = {
        "candidates_evaluated": len(rows),
        "opened": opened,
        "closed": closed,
        "open_trades": open_after,
        "fast_polling_active": 0,
    }
    return lines, stats


def run_once(conn: sqlite3.Connection) -> list[str]:
    lines, _stats = _run_once_with_stats(conn)
    return lines


def run_loop() -> None:
    print(
        "MOMENTUM_SNIPER loop started; "
        f"fast polling active every {OPEN_TRADE_POLL_SECONDS}s for open trades; press Ctrl+C to stop",
        flush=True,
    )
    next_entry_eval = 0.0
    try:
        while True:
            try:
                conn = db.connect()
                try:
                    now = _now()
                    if now >= next_entry_eval:
                        _lines, stats = _run_once_with_stats(conn, PRICE_REQUEST_DELAY_SECONDS)
                        next_entry_eval = now + LOOP_INTERVAL_SECONDS
                    else:
                        closed = update_open_trades(conn, now, PRICE_REQUEST_DELAY_SECONDS)
                        conn.commit()
                        open_after = conn.execute(
                            "SELECT COUNT(*) FROM momentum_sniper_trades WHERE status='open'"
                        ).fetchone()[0]
                        stats = {
                            "candidates_evaluated": 0,
                            "opened": 0,
                            "closed": closed,
                            "open_trades": open_after,
                            "fast_polling_active": 1,
                        }
                    version_counts = _strategy_version_counts(conn)
                finally:
                    conn.close()
                print(
                    f"{_iso(_now())} "
                    f"candidates evaluated={stats['candidates_evaluated']} "
                    f"opened={stats['opened']} "
                    f"closed={stats['closed']} "
                    f"open trades={stats['open_trades']} "
                    f"strategy_version counts={dict(version_counts)} "
                    f"fast polling active=yes",
                    flush=True,
                )
            except Exception as exc:  # Keep service mode alive for transient DB/API issues.
                print(f"{_iso(_now())} loop error: {exc}", flush=True)
            time.sleep(OPEN_TRADE_POLL_SECONDS)
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


def _strategy_version_counts(conn: sqlite3.Connection) -> Counter[str]:
    rows = conn.execute(
        """
        SELECT COALESCE(strategy_version, 'LEGACY_MOMENTUM_SNIPER') AS version, COUNT(*) AS n
        FROM momentum_sniper_trades
        GROUP BY COALESCE(strategy_version, 'LEGACY_MOMENTUM_SNIPER')
        ORDER BY version
        """
    ).fetchall()
    return Counter({row[0]: row[1] for row in rows})


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


def _version_summary_lines(
    version: str,
    rows: list[sqlite3.Row],
    price_checks: dict[int, sqlite3.Row],
) -> list[str]:
    version_rows = [row for row in rows if row["strategy_version"] == version]
    open_rows = [row for row in version_rows if row["status"] == "open"]
    closed = [row for row in version_rows if row["status"] == "closed"]
    returns = [_num(row["pnl_pct"]) for row in closed]
    returns = [value for value in returns if value is not None]
    avg, med, _avg_win, _avg_loss = _summary(returns)
    wins = [value for value in returns if value > 0]
    check_counts = [price_checks.get(row["id"])["n"] if price_checks.get(row["id"]) else 0 for row in version_rows]
    avg_checks = sum(check_counts) / len(check_counts) if check_counts else None
    hold_seconds = [
        _num(row["exit_time"]) - _num(row["entry_time"])
        for row in closed
        if _num(row["exit_time"]) is not None and _num(row["entry_time"]) is not None
    ]
    avg_hold_seconds = sum(hold_seconds) / len(hold_seconds) if hold_seconds else None
    zero_check_closed = [
        row for row in closed if not price_checks.get(row["id"]) or price_checks[row["id"]]["n"] == 0
    ]
    lines = [
        f"{version} summary:",
        f"- total: {len(version_rows)}",
        f"- open: {len(open_rows)}",
        f"- closed: {len(closed)}",
        f"- win rate: {_pct(len(wins) / len(returns) * 100 if returns else None)}",
        f"- avg pnl: {_fmt(avg)}",
        f"- median pnl: {_fmt(med)}",
        f"- profit factor: {_fmt(_profit_factor(returns))}",
        f"- avg hold seconds: {_fmt(avg_hold_seconds)}",
        f"- avg price checks: {_fmt(avg_checks)}",
        f"- zero price check closed trades: {len(zero_check_closed)}",
        "exit reasons:",
    ]
    exit_counts = Counter(str(row["exit_reason"] or row["status"]) for row in version_rows)
    if exit_counts:
        lines.extend(f"- {reason}: {count}" for reason, count in sorted(exit_counts.items()))
    else:
        lines.append("- none: 0")
    return lines


def _v3_detail_lines(rows: list[sqlite3.Row]) -> list[str]:
    v3_closed = [
        row for row in rows if row["strategy_version"] == GOLDEN_WINDOW_SCALP_V3 and row["status"] == "closed"
    ]
    take_profit = [row for row in v3_closed if row["exit_reason"] == "take_profit"]
    time_exit = [row for row in v3_closed if row["exit_reason"] == "time_exit"]
    tp_returns = [_num(row["pnl_pct"]) for row in take_profit]
    tx_returns = [_num(row["pnl_pct"]) for row in time_exit]
    tp_returns = [value for value in tp_returns if value is not None]
    tx_returns = [value for value in tx_returns if value is not None]
    return [
        f"{GOLDEN_WINDOW_SCALP_V3} details:",
        f"- take_profit count: {len(take_profit)}",
        f"- time_exit count: {len(time_exit)}",
        f"- average time_exit pnl: {_fmt(sum(tx_returns) / len(tx_returns) if tx_returns else None)}",
        f"- median time_exit pnl: {_fmt(statistics.median(tx_returns) if tx_returns else None)}",
        f"- average take_profit pnl: {_fmt(sum(tp_returns) / len(tp_returns) if tp_returns else None)}",
        f"- median take_profit pnl: {_fmt(statistics.median(tp_returns) if tp_returns else None)}",
    ]


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
    check_rows = conn.execute(
        """
        SELECT trade_id, COUNT(*) AS n, MIN(checked_at) AS first_check, MAX(checked_at) AS last_check
        FROM momentum_sniper_price_checks
        GROUP BY trade_id
        """
    ).fetchall()
    price_checks = {row["trade_id"]: row for row in check_rows}
    golden_rows = [row for row in rows if row["strategy_version"] == STRATEGY_VERSION]
    golden_closed = [row for row in golden_rows if row["status"] == "closed"]
    check_counts = [price_checks.get(row["id"])["n"] if price_checks.get(row["id"]) else 0 for row in golden_rows]
    avg_checks = sum(check_counts) / len(check_counts) if check_counts else None
    hold_seconds = [
        _num(row["exit_time"]) - _num(row["entry_time"])
        for row in golden_closed
        if _num(row["exit_time"]) is not None and _num(row["entry_time"]) is not None
    ]
    avg_hold_seconds = sum(hold_seconds) / len(hold_seconds) if hold_seconds else None
    zero_check_closed = [
        row for row in golden_closed if not price_checks.get(row["id"]) or price_checks[row["id"]]["n"] == 0
    ]

    lines = [
        "=== MOMENTUM_SNIPER REPORT ===",
        "isolated paper-only strategy; no live execution, alerts, scanner verdicts, scoring, or existing paper strategy changes",
        f"active strategy version: {STRATEGY_VERSION}",
        f"active exits: TP +{V3_TAKE_PROFIT_PCT:.0f}%, no SL, max hold {V3_MAX_HOLD_SECONDS // 60}m",
        f"fast polling active in --loop: yes, every {OPEN_TRADE_POLL_SECONDS}s with {PRICE_REQUEST_DELAY_SECONDS:.1f}s delay between open-trade price checks",
        f"total trades: {len(rows)}",
        f"open trades: {len(open_rows)}",
        f"closed trades: {len(closed)}",
        f"win rate: {_pct(len(wins) / len(returns) * 100 if returns else None)}",
        f"avg win: {_fmt(avg_win)}",
        f"avg loss: {_fmt(avg_loss)}",
        f"profit factor: {_fmt(_profit_factor(returns))}",
        f"average pnl: {_fmt(avg)}",
        f"median pnl: {_fmt(med)}",
        f"avg_price_checks_per_trade ({STRATEGY_VERSION}): {_fmt(avg_checks)}",
        f"avg_hold_seconds ({STRATEGY_VERSION}): {_fmt(avg_hold_seconds)}",
        f"trades_with_zero_price_checks ({STRATEGY_VERSION} closed): {len(zero_check_closed)}",
        f"best trade: {best['mint'] if best else '-'} {_fmt(best['pnl_pct'] if best else None)}",
        f"worst trade: {worst['mint'] if worst else '-'} {_fmt(worst['pnl_pct'] if worst else None)}",
        f"max drawdown: {_fmt(max_drawdown)}",
        "",
    ]
    if zero_check_closed:
        lines.append("WARNING: trades closing without price checks.")

    version_counts = _strategy_version_counts(conn)
    lines.append("By strategy_version:")
    for version, count in version_counts.items():
        lines.append(f"- {version}: {count}")
    lines.append("")
    lines.extend(_version_summary_lines(GOLDEN_WINDOW_SCALP_V1, rows, price_checks))
    lines.append("")
    lines.extend(_version_summary_lines(GOLDEN_WINDOW_SCALP_V2, rows, price_checks))
    lines.append("")
    lines.extend(_version_summary_lines(GOLDEN_WINDOW_SCALP_V3, rows, price_checks))
    lines.append("")
    lines.extend(_v3_detail_lines(rows))
    lines.append("")

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(row["is_second_wave"]), []).append(row)
    lines.extend(_group_lines("By is_second_wave:", groups))

    holder_groups: dict[str, list[sqlite3.Row]] = {}
    for row in golden_rows:
        holder_groups.setdefault(str(row["holder_check"] or "unknown"), []).append(row)
    for bucket in ("passed", "unavailable", "failed_high"):
        holder_groups.setdefault(bucket, [])
    lines.extend(["", *_group_lines(f"By holder_check ({STRATEGY_VERSION}):", holder_groups)])

    lines.extend(["", f"Price checks by trade ({STRATEGY_VERSION}):"])
    lines.append("mint symbol checks first_check last_check exit_source exit_source_time")
    for row in golden_rows:
        check = price_checks.get(row["id"])
        lines.append(
            f"{row['mint']} {row['symbol'] or '-'} "
            f"{check['n'] if check else 0} "
            f"{_iso(check['first_check']) if check else '-'} "
            f"{_iso(check['last_check']) if check else '-'} "
            f"{row['exit_source'] or '-'} "
            f"{_iso(row['exit_source_time']) if row['exit_source_time'] else '-'}"
        )

    group_specs = (
        ("By strategy_version detail:", lambda row: str(row["strategy_version"] or "LEGACY_MOMENTUM_SNIPER")),
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
