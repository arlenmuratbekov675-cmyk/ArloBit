"""ArloBit v3 shadow strategy ledger.

Paper-only forward tracker for two strategy-discovery rules. This module is
not imported by the scanner and never changes live trading, current paper
strategy, filters, scoring, signing, or execution.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from arlobit import db
from arlobit import velocity

LABEL_VERSION = 1
START_META_KEY = "v3_shadow_started_at"
WINDOW_DAYS = 14
TP_PCT = 100.0
SL_PCT = -50.0


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str

    def matches(self, row: sqlite3.Row) -> bool:
        price_velocity = _num(row["price_change_velocity"])
        ratio_change = _num(row["buy_sell_ratio_change"])
        current_ratio = _num(row["buy_sell_ratio_m5"])
        if self.rule_id == "v3_rule_1":
            return (
                price_velocity is not None
                and price_velocity > 0.04
                and ratio_change is not None
                and -0.05 < ratio_change <= 0.13
            )
        if self.rule_id == "v3_rule_2":
            return (
                ratio_change is not None
                and -0.05 < ratio_change <= 0.13
                and current_ratio is not None
                and 0.55 < current_ratio <= 0.64
            )
        return False


@dataclass(frozen=True)
class ShadowCounters:
    candidates_evaluated: int = 0
    rule_1_matches: int = 0
    rule_2_matches: int = 0
    shadow_inserted: int = 0
    outcomes_updated: int = 0


RULES = (
    Rule(
        "v3_rule_1",
        "velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13]",
    ),
    Rule(
        "v3_rule_2",
        "velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64]",
    ),
)


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


def _pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number * 100:.1f}%"


def _per_day(count: int, days: float | None) -> str:
    if days is None or days <= 0:
        return "-"
    return f"{count / days:.2f}"


def _iso(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _now() -> float:
    return time.time()


def ensure_started(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM paper_trade_meta WHERE key=?", (START_META_KEY,)).fetchone()
    if row and row[0]:
        parsed = _num(row[0])
        if parsed is not None:
            return parsed
    started_at = _now()
    conn.execute(
        "INSERT INTO paper_trade_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (START_META_KEY, str(started_at)),
    )
    conn.commit()
    return started_at


def _candidate_rows(conn: sqlite3.Connection, started_at: float) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT tv.mint, tv.sighting_id, tv.seen_at,
               tv.price_change_velocity, tv.buy_sell_ratio_change,
               tv.liquidity_change_15m, tv.liquidity_change_1h,
               tv.volume_change_15m, tv.volume_change_1h,
               tv.buy_count_change, tv.sell_count_change,
               s.price_usd, s.liquidity_usd, s.buys_m5, s.sells_m5,
               s.buy_sell_ratio_m5, s.source, s.arlobit_score,
               s.age_minutes, s.creator_quality, s.top10_pct, s.top20_pct
        FROM v3_shadow_velocity tv
        JOIN candidate_sightings s ON s.sighting_id = tv.sighting_id
        WHERE tv.seen_at >= ?
        ORDER BY tv.seen_at, tv.mint
        """,
        (started_at,),
    ).fetchall()


def _features_json(row: sqlite3.Row) -> str:
    payload = {
        "price_change_velocity": _num(row["price_change_velocity"]),
        "buy_sell_ratio_change": _num(row["buy_sell_ratio_change"]),
        "candidate_buy_sell_ratio_m5": _num(row["buy_sell_ratio_m5"]),
        "liquidity_change_15m": _num(row["liquidity_change_15m"]),
        "liquidity_change_1h": _num(row["liquidity_change_1h"]),
        "volume_change_15m": _num(row["volume_change_15m"]),
        "volume_change_1h": _num(row["volume_change_1h"]),
        "buy_count_change": _num(row["buy_count_change"]),
        "sell_count_change": _num(row["sell_count_change"]),
        "entry_price_usd": _num(row["price_usd"]),
        "liquidity_usd": _num(row["liquidity_usd"]),
        "buys_m5": _num(row["buys_m5"]),
        "sells_m5": _num(row["sells_m5"]),
        "source": row["source"],
        "arlobit_score": _num(row["arlobit_score"]),
        "age_minutes": _num(row["age_minutes"]),
        "creator_quality": row["creator_quality"],
        "top10_pct": _num(row["top10_pct"]),
        "top20_pct": _num(row["top20_pct"]),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def insert_new_shadow_trades(conn: sqlite3.Connection, started_at: float) -> int:
    rows = _candidate_rows(conn, started_at)
    now = _now()
    inserted = 0
    for row in rows:
        for rule in RULES:
            if not rule.matches(row):
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO v3_shadow_trades (
                    mint, sighting_id, entry_time, rule_id, features_json,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    row["mint"],
                    row["sighting_id"],
                    row["seen_at"],
                    rule.rule_id,
                    _features_json(row),
                    now,
                    now,
                ),
            )
            inserted += cursor.rowcount
    conn.commit()
    return inserted


def evaluate_forward_window(conn: sqlite3.Connection, lower_bound: float | None = None) -> ShadowCounters:
    """Evaluate v3 shadow rules for forward research rows.

    Intended for scanner hooks after candidate_sightings have been persisted.
    It writes only to v3_shadow_trades and never affects scanner verdicts,
    alerts, current paper trades, or score.
    """
    started_at = ensure_started(conn)
    velocity.refresh_v3_shadow_velocity(conn, started_at)
    # Forward velocity for a sighting is only knowable after a later same-mint
    # observation arrives, so a cycle-only lower bound can miss just-matured rows.
    since = started_at
    rows = _candidate_rows(conn, since)
    now = _now()
    rule_1_matches = 0
    rule_2_matches = 0
    inserted = 0
    for row in rows:
        for rule in RULES:
            if not rule.matches(row):
                continue
            if rule.rule_id == "v3_rule_1":
                rule_1_matches += 1
            elif rule.rule_id == "v3_rule_2":
                rule_2_matches += 1
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO v3_shadow_trades (
                    mint, sighting_id, entry_time, rule_id, features_json,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    row["mint"],
                    row["sighting_id"],
                    row["seen_at"],
                    rule.rule_id,
                    _features_json(row),
                    now,
                    now,
                ),
            )
            inserted += cursor.rowcount
    conn.commit()
    updated = update_outcomes(conn)
    return ShadowCounters(
        candidates_evaluated=len(rows),
        rule_1_matches=rule_1_matches,
        rule_2_matches=rule_2_matches,
        shadow_inserted=inserted,
        outcomes_updated=updated,
    )


def _label_by_mint(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {
        row["mint"]: row
        for row in conn.execute(
            """
            SELECT mint, reached_50, reached_100, reached_500, rugged,
                   ret_24h, max_runup_pct, max_drawdown_pct
            FROM labels
            WHERE label_version = ?
            """,
            (LABEL_VERSION,),
        ).fetchall()
    }


def _entry_price(features_json: str) -> float | None:
    try:
        payload = json.loads(features_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return _num(payload.get("entry_price_usd"))


def _tp_sl_from_path(conn: sqlite3.Connection, mint: str, entry_time: float, entry_price: float | None) -> str | None:
    if entry_price is None or entry_price <= 0:
        return None
    rows = conn.execute(
        """
        SELECT ts, high, low
        FROM ohlcv_1m
        WHERE mint = ? AND ts >= ? AND ts <= ?
        ORDER BY ts
        """,
        (mint, entry_time, entry_time + 24 * 60 * 60),
    ).fetchall()
    for _ts, high, low in rows:
        high_ret = None if high is None else (float(high) / entry_price - 1.0) * 100.0
        low_ret = None if low is None else (float(low) / entry_price - 1.0) * 100.0
        if low_ret is not None and low_ret <= SL_PCT:
            return "SL_50"
        if high_ret is not None and high_ret >= TP_PCT:
            return "TP_100"
    return None


def _tp_sl_result(conn: sqlite3.Connection, trade: sqlite3.Row, label: sqlite3.Row | None) -> str | None:
    path_result = _tp_sl_from_path(
        conn,
        trade["mint"],
        float(trade["entry_time"]),
        _entry_price(trade["features_json"]),
    )
    if path_result:
        return path_result
    if label is None:
        return None
    max_runup = _num(label["max_runup_pct"])
    max_drawdown = _num(label["max_drawdown_pct"])
    rugged = int(label["rugged"] == 1)
    if max_runup is not None and max_runup >= TP_PCT:
        return "TP_100_ORDER_UNKNOWN"
    if rugged or (max_drawdown is not None and max_drawdown <= SL_PCT):
        return "SL_50_ORDER_UNKNOWN"
    return "NO_TP_OR_SL_24H"


def _exit_result(label: sqlite3.Row | None) -> str | None:
    if label is None or label["ret_24h"] is None:
        return None
    if label["rugged"] == 1:
        return "rugged"
    if label["reached_500"] == 1:
        return "reached_500"
    if label["reached_100"] == 1:
        return "reached_100"
    if label["reached_50"] == 1:
        return "reached_50"
    return "completed_no_target"


def update_outcomes(conn: sqlite3.Connection) -> int:
    conn.row_factory = sqlite3.Row
    labels = _label_by_mint(conn)
    rows = conn.execute("SELECT * FROM v3_shadow_trades").fetchall()
    now = _now()
    updated = 0
    for trade in rows:
        label = labels.get(trade["mint"])
        if label is None or label["ret_24h"] is None:
            continue
        status = "closed"
        cursor = conn.execute(
            """
            UPDATE v3_shadow_trades
            SET exit_result = ?,
                tp_sl_result = ?,
                max_drawdown_pct = ?,
                max_runup_pct = ?,
                ret_24h = ?,
                status = ?,
                updated_at = ?
            WHERE shadow_trade_id = ?
            """,
            (
                _exit_result(label),
                _tp_sl_result(conn, trade, label),
                _num(label["max_drawdown_pct"]),
                _num(label["max_runup_pct"]),
                _num(label["ret_24h"]),
                status,
                now,
                trade["shadow_trade_id"],
            ),
        )
        updated += cursor.rowcount
    conn.commit()
    return updated


def refresh(conn: sqlite3.Connection) -> tuple[int, int, float]:
    started_at = ensure_started(conn)
    counters = evaluate_forward_window(conn)
    return counters.shadow_inserted, counters.outcomes_updated, started_at


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses > 0:
        return wins / losses
    if wins > 0:
        return math.inf
    return None


def _rule_summary(conn: sqlite3.Connection) -> list[str]:
    conn.row_factory = sqlite3.Row
    lines = [
        "rule_id     total open closed avg_ret_24h profit_factor avg_runup avg_drawdown tp100 sl50 rug_rate",
    ]
    for rule in RULES:
        rows = conn.execute(
            "SELECT * FROM v3_shadow_trades WHERE rule_id=? ORDER BY entry_time",
            (rule.rule_id,),
        ).fetchall()
        closed = [row for row in rows if row["status"] == "closed"]
        returns = [_num(row["ret_24h"]) for row in closed]
        returns = [value for value in returns if value is not None]
        runups = [_num(row["max_runup_pct"]) for row in closed]
        drawdowns = [_num(row["max_drawdown_pct"]) for row in closed]
        tp100 = sum(1 for row in closed if str(row["tp_sl_result"] or "").startswith("TP_100"))
        sl50 = sum(1 for row in closed if str(row["tp_sl_result"] or "").startswith("SL_50"))
        rugs = sum(1 for row in closed if row["exit_result"] == "rugged")
        closed_n = len(closed)
        lines.append(
            f"{rule.rule_id:<10} {len(rows):>5} {len(rows) - closed_n:>4} {closed_n:>6}"
            f" {_fmt(_avg(returns)):>11} {_fmt(_profit_factor(returns)):>13}"
            f" {_fmt(_avg([v for v in runups if v is not None])):>9}"
            f" {_fmt(_avg([v for v in drawdowns if v is not None])):>12}"
            f" {tp100:>5} {sl50:>4} {_pct(rugs / closed_n if closed_n else None):>8}"
        )
    return lines


def _recent_trades(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    rows = conn.execute(
        """
        SELECT mint, entry_time, rule_id, status, exit_result, tp_sl_result,
               ret_24h, max_runup_pct, max_drawdown_pct
        FROM v3_shadow_trades
        ORDER BY entry_time DESC, shadow_trade_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    lines = [
        "mint                                             entry_time           rule       status exit_result         tp_sl              ret_24h runup drawdown",
    ]
    if not rows:
        lines.append("(none yet)")
        return lines
    for row in rows:
        lines.append(
            f"{row[0]:<48} {_iso(row[1]):<20} {row[2]:<10} {row[3]:<6}"
            f" {str(row[4] or '-'): <18} {str(row[5] or '-'): <18}"
            f" {_fmt(row[6]):>7} {_fmt(row[7]):>5} {_fmt(row[8]):>8}"
        )
    return lines


def _audit_rows(conn: sqlite3.Connection, started_at: float) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT tv.mint, tv.sighting_id, tv.seen_at,
               tv.price_change_velocity, tv.buy_sell_ratio_change,
               s.buy_sell_ratio_m5
        FROM v3_shadow_velocity tv
        JOIN candidate_sightings s ON s.sighting_id = tv.sighting_id
        WHERE tv.seen_at >= ?
        ORDER BY tv.seen_at, tv.mint
        """,
        (started_at,),
    ).fetchall()


def _count(rows: list[sqlite3.Row], predicate: Any) -> int:
    return sum(1 for row in rows if predicate(row))


def _audit_report(conn: sqlite3.Connection, started_at: float) -> list[str]:
    rows = _audit_rows(conn, started_at)
    candidate_counts = conn.execute(
        """
        SELECT COUNT(*), MIN(seen_at), MAX(seen_at)
        FROM candidate_sightings
        WHERE seen_at >= ?
        """,
        (started_at,),
    ).fetchone()
    new_candidates = int(candidate_counts[0] or 0)
    max_seen_at = _num(candidate_counts[2])
    days = None
    if max_seen_at is not None and max_seen_at > started_at:
        days = (max_seen_at - started_at) / (24 * 60 * 60)

    price_available = _count(rows, lambda row: _num(row["price_change_velocity"]) is not None)
    ratio_change_available = _count(rows, lambda row: _num(row["buy_sell_ratio_change"]) is not None)
    current_ratio_available = _count(rows, lambda row: _num(row["buy_sell_ratio_m5"]) is not None)

    price_velocity_gt_004 = _count(
        rows,
        lambda row: (value := _num(row["price_change_velocity"])) is not None and value > 0.04,
    )
    ratio_change_gt_neg005 = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_change"])) is not None and value > -0.05,
    )
    ratio_change_lte_013 = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_change"])) is not None and value <= 0.13,
    )
    ratio_change_current = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_change"])) is not None and -0.05 < value <= 0.13,
    )
    current_ratio_gt_055 = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_m5"])) is not None and value > 0.55,
    )
    current_ratio_lte_064 = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_m5"])) is not None and value <= 0.64,
    )
    current_ratio_current = _count(
        rows,
        lambda row: (value := _num(row["buy_sell_ratio_m5"])) is not None and 0.55 < value <= 0.64,
    )

    rule_1_matches = _count(rows, lambda row: RULES[0].matches(row))
    rule_2_matches = _count(rows, lambda row: RULES[1].matches(row))

    relaxed_rule_3 = _count(
        rows,
        lambda row: (price := _num(row["price_change_velocity"])) is not None
        and price > 0.0
        and (ratio := _num(row["buy_sell_ratio_change"])) is not None
        and -0.25 < ratio <= 0.25,
    )
    relaxed_rule_4 = _count(
        rows,
        lambda row: (ratio_change := _num(row["buy_sell_ratio_change"])) is not None
        and -0.25 < ratio_change <= 0.25
        and (current_ratio := _num(row["buy_sell_ratio_m5"])) is not None
        and 0.45 < current_ratio <= 0.75,
    )
    historical_rows = conn.execute(
        """
        SELECT tv.mint, tv.sighting_id, tv.seen_at,
               tv.price_change_velocity, tv.buy_sell_ratio_change,
               s.buy_sell_ratio_m5
        FROM token_velocity tv
        JOIN candidate_sightings s ON s.sighting_id = tv.sighting_id
        ORDER BY tv.seen_at, tv.mint
        """
    ).fetchall()
    historical_days = None
    if len(historical_rows) >= 2:
        first_seen = _num(historical_rows[0]["seen_at"])
        last_seen = _num(historical_rows[-1]["seen_at"])
        if first_seen is not None and last_seen is not None and last_seen > first_seen:
            historical_days = (last_seen - first_seen) / (24 * 60 * 60)
    historical_rule_3 = _count(
        historical_rows,
        lambda row: (price := _num(row["price_change_velocity"])) is not None
        and price > 0.0
        and (ratio := _num(row["buy_sell_ratio_change"])) is not None
        and -0.25 < ratio <= 0.25,
    )
    historical_rule_4 = _count(
        historical_rows,
        lambda row: (ratio_change := _num(row["buy_sell_ratio_change"])) is not None
        and -0.25 < ratio_change <= 0.25
        and (current_ratio := _num(row["buy_sell_ratio_m5"])) is not None
        and 0.45 < current_ratio <= 0.75,
    )

    missing_velocity_rows = max(new_candidates - len(rows), 0)
    missing_price = len(rows) - price_available
    missing_ratio_change = len(rows) - ratio_change_available
    blockers = [
        ("no v3_shadow_velocity row for sighting", missing_velocity_rows),
        ("price_change_velocity unavailable", missing_price),
        ("buy_sell_ratio_change unavailable", missing_ratio_change),
        ("candidate.buy_sell_ratio_m5 unavailable", len(rows) - current_ratio_available),
        ("price_change_velocity <= 0.04", len(rows) - price_velocity_gt_004),
        ("buy_sell_ratio_change outside (-0.05, 0.13]", len(rows) - ratio_change_current),
        ("candidate.buy_sell_ratio_m5 outside (0.55, 0.64]", len(rows) - current_ratio_current),
    ]
    top_blocker = max(blockers, key=lambda item: item[1]) if blockers else ("-", 0)

    if rule_1_matches or rule_2_matches:
        conclusion = (
            "Conclusion: v3_shadow_velocity now covers forward sightings and active rules are producing "
            "shadow-only matches."
        )
    elif missing_velocity_rows:
        conclusion = "Conclusion: current forward zero matches are caused by missing per-sighting velocity rows."
    elif price_available == 0 or ratio_change_available == 0:
        conclusion = "Conclusion: current forward zero matches are caused by missing forward velocity values."
    else:
        conclusion = "Conclusion: current forward zero matches are caused by active rule strictness."

    return [
        "",
        "Forward strictness audit:",
        f"- new candidate sightings since forward_start: {new_candidates}",
        f"- candidate sightings with v3_shadow_velocity rows: {len(rows)}",
        f"- velocity.price_change_velocity available: {price_available}",
        f"- velocity.buy_sell_ratio_change available: {ratio_change_available}",
        f"- candidate.buy_sell_ratio_m5 available on velocity rows: {current_ratio_available}",
        "",
        "Condition pass counts on forward v3_shadow_velocity rows:",
        f"- velocity.price_change_velocity > 0.04: {price_velocity_gt_004}",
        f"- velocity.buy_sell_ratio_change > -0.05: {ratio_change_gt_neg005}",
        f"- velocity.buy_sell_ratio_change <= 0.13: {ratio_change_lte_013}",
        f"- velocity.buy_sell_ratio_change in (-0.05, 0.13]: {ratio_change_current}",
        f"- candidate.buy_sell_ratio_m5 > 0.55: {current_ratio_gt_055}",
        f"- candidate.buy_sell_ratio_m5 <= 0.64: {current_ratio_lte_064}",
        f"- candidate.buy_sell_ratio_m5 in (0.55, 0.64]: {current_ratio_current}",
        "",
        "Forward trigger estimates:",
        f"- v3_rule_1: {rule_1_matches} matches ({_per_day(rule_1_matches, days)}/day)",
        f"- v3_rule_2: {rule_2_matches} matches ({_per_day(rule_2_matches, days)}/day)",
        "",
        "Largest blocker:",
        f"- {top_blocker[0]} blocks {top_blocker[1]} candidate(s)",
        "",
        "Relaxed shadow-only proposals, not active:",
        "- v3_rule_3 proposal: velocity.price_change_velocity > 0.0 AND velocity.buy_sell_ratio_change in (-0.25, 0.25]",
        f"  forward estimate with current feature availability: {relaxed_rule_3} matches ({_per_day(relaxed_rule_3, days)}/day)",
        f"  historical support once velocity values exist: {historical_rule_3} matches ({_per_day(historical_rule_3, historical_days)}/day)",
        "- v3_rule_4 proposal: velocity.buy_sell_ratio_change in (-0.25, 0.25] AND candidate.buy_sell_ratio_m5 in (0.45, 0.75]",
        f"  forward estimate with current feature availability: {relaxed_rule_4} matches ({_per_day(relaxed_rule_4, days)}/day)",
        f"  historical support once velocity values exist: {historical_rule_4} matches ({_per_day(historical_rule_4, historical_days)}/day)",
        conclusion,
    ]


def report_lines(conn: sqlite3.Connection) -> list[str]:
    inserted, updated, started_at = refresh(conn)
    stop_at = started_at + WINDOW_DAYS * 24 * 60 * 60
    counts = conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END)
        FROM v3_shadow_trades
        """
    ).fetchone()
    lines = [
        "=== ARLOBIT V3 SHADOW REPORT ===",
        "paper-only shadow strategy; scanner-connected research logging only, not connected to live trading, current paper strategy, or scoring",
        f"forward start: {_iso(started_at)}",
        f"planned window end: {_iso(stop_at)}",
        f"new entries this run: {inserted}",
        f"outcomes updated this run: {updated}",
        f"total shadow trades: {counts[0] or 0}",
        f"open shadow trades: {counts[1] or 0}",
        f"closed shadow trades: {counts[2] or 0}",
        "",
        "Rules:",
    ]
    lines.extend(f"- {rule.rule_id}: {rule.description}" for rule in RULES)
    lines.extend(_audit_report(conn, started_at))
    lines.extend(["", "Rule performance:", *_rule_summary(conn), "", "Recent shadow trades:", *_recent_trades(conn)])
    lines.append("=== END REPORT ===")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit v3 paper-only shadow strategy tracker")
    parser.add_argument("--report", action="store_true", help="refresh and print v3 shadow report")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        if args.report or not any(vars(args).values()):
            print("\n".join(report_lines(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
