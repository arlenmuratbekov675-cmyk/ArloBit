"""Liquidity/volume velocity research layer.

Computes point-in-time velocity features from candidate_sightings and links
them to completed labels for offline analysis. This module never changes
scanner filters, scoring, execution, or trading behavior.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sqlite3
import statistics
import time
from collections import defaultdict
from typing import Any

from arlobit import db

LABEL_VERSION = 1
WINDOW_SECONDS = {"5m": 5 * 60, "15m": 15 * 60, "1h": 60 * 60}
FEATURES = (
    "liquidity_change_5m",
    "liquidity_change_15m",
    "liquidity_change_1h",
    "volume_change_5m",
    "volume_change_15m",
    "volume_change_1h",
    "buy_count_change",
    "sell_count_change",
    "buy_sell_ratio_change",
    "price_change_velocity",
    "volume_acceleration",
    "liquidity_acceleration",
)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _pct_change(current: Any, previous: Any) -> float | None:
    cur = _num(current)
    prev = _num(previous)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur / prev - 1.0) * 100.0


def _diff(current: Any, previous: Any) -> float | None:
    cur = _num(current)
    prev = _num(previous)
    if cur is None or prev is None:
        return None
    return cur - prev


def _rate(change_pct: float | None, minutes: float) -> float | None:
    if change_pct is None or minutes <= 0:
        return None
    return change_pct / minutes


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: Any, decimals: int = 2) -> str:
    number = _num(value)
    return "-" if number is None else f"{number:.{decimals}f}"


def _label_base_sightings(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        mint: sighting_id
        for mint, sighting_id in conn.execute(
            "SELECT mint, base_sighting_id FROM labels WHERE label_version=? AND base_sighting_id IS NOT NULL",
            (LABEL_VERSION,),
        ).fetchall()
    }


def _target_sightings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    label_bases = _label_base_sightings(conn)
    rows: list[sqlite3.Row] = []
    conn.row_factory = sqlite3.Row
    if label_bases:
        placeholders = ",".join("?" * len(label_bases))
        rows.extend(
            conn.execute(
                f"SELECT * FROM candidate_sightings WHERE sighting_id IN ({placeholders})",
                list(label_bases.values()),
            ).fetchall()
        )
    labelled_mints = set(label_bases)
    latest = conn.execute(
        """
        SELECT s.*
        FROM candidate_sightings s
        JOIN (
            SELECT mint, MAX(seen_at) AS seen_at
            FROM candidate_sightings
            GROUP BY mint
        ) latest ON latest.mint = s.mint AND latest.seen_at = s.seen_at
        """
    ).fetchall()
    seen = {row["mint"] for row in rows}
    for row in latest:
        if row["mint"] not in labelled_mints and row["mint"] not in seen:
            rows.append(row)
            seen.add(row["mint"])
    return sorted(rows, key=lambda row: (row["mint"], row["seen_at"], row["sighting_id"]))


def _history_by_mint(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sighting_id, mint, seen_at, price_usd, liquidity_usd, vol_m5,
               buys_m5, sells_m5, buy_sell_ratio_m5, vol_accel
        FROM candidate_sightings
        ORDER BY mint, seen_at, sighting_id
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["mint"]].append(row)
    return grouped


def _window_observation(history: list[sqlite3.Row], target_seen_at: float, window_seconds: int) -> sqlite3.Row | None:
    timestamps = [row["seen_at"] for row in history]
    index = bisect.bisect_left(timestamps, target_seen_at + window_seconds)
    if index >= len(history):
        return None
    candidate = history[index]
    max_late = max(window_seconds * 2, window_seconds + 10 * 60)
    if candidate["seen_at"] - target_seen_at > max_late:
        return None
    return candidate


def _nearest_next(history: list[sqlite3.Row], target_seen_at: float) -> sqlite3.Row | None:
    timestamps = [row["seen_at"] for row in history]
    index = bisect.bisect_right(timestamps, target_seen_at)
    return history[index] if index < len(history) else None


def _labels_by_mint(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {
        row["mint"]: row
        for row in conn.execute(
            """
            SELECT mint, reached_50, reached_100, reached_500, rugged, ret_24h,
                   max_runup_pct, max_drawdown_pct, label_version
            FROM labels
            WHERE label_version=?
            """,
            (LABEL_VERSION,),
        ).fetchall()
    }


def compute_velocity_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    history_by_mint = _history_by_mint(conn)
    targets = _target_sightings(conn)
    labels = _labels_by_mint(conn)
    computed_at = time.time()
    rows: list[tuple[Any, ...]] = []
    for target in targets:
        mint = target["mint"]
        history = history_by_mint.get(mint, [])
        observations = {
            name: _window_observation(history, target["seen_at"], seconds)
            for name, seconds in WINDOW_SECONDS.items()
        }
        nearest = _nearest_next(history, target["seen_at"])

        liquidity_change_5m = _pct_change(observations["5m"]["liquidity_usd"] if observations["5m"] else None, target["liquidity_usd"])
        liquidity_change_15m = _pct_change(observations["15m"]["liquidity_usd"] if observations["15m"] else None, target["liquidity_usd"])
        liquidity_change_1h = _pct_change(observations["1h"]["liquidity_usd"] if observations["1h"] else None, target["liquidity_usd"])
        volume_change_5m = _pct_change(observations["5m"]["vol_m5"] if observations["5m"] else None, target["vol_m5"])
        volume_change_15m = _pct_change(observations["15m"]["vol_m5"] if observations["15m"] else None, target["vol_m5"])
        volume_change_1h = _pct_change(observations["1h"]["vol_m5"] if observations["1h"] else None, target["vol_m5"])
        buy_count_change = _diff(nearest["buys_m5"] if nearest else None, target["buys_m5"])
        sell_count_change = _diff(nearest["sells_m5"] if nearest else None, target["sells_m5"])
        buy_sell_ratio_change = _diff(nearest["buy_sell_ratio_m5"] if nearest else None, target["buy_sell_ratio_m5"])
        price_velocity = None
        if nearest and nearest["seen_at"] > target["seen_at"]:
            price_change = _pct_change(nearest["price_usd"], target["price_usd"])
            price_velocity = _rate(price_change, (nearest["seen_at"] - target["seen_at"]) / 60.0)

        volume_acceleration = _num(target["vol_accel"])
        if volume_acceleration is None:
            short_rate = _rate(volume_change_5m, 5)
            med_rate = _rate(volume_change_15m, 15)
            volume_acceleration = _diff(short_rate, med_rate)
        liquidity_acceleration = _diff(_rate(liquidity_change_5m, 5), _rate(liquidity_change_15m, 15))

        label = labels.get(mint)
        rows.append(
            (
                mint,
                target["sighting_id"],
                target["seen_at"],
                computed_at,
                liquidity_change_5m,
                liquidity_change_15m,
                liquidity_change_1h,
                volume_change_5m,
                volume_change_15m,
                volume_change_1h,
                buy_count_change,
                sell_count_change,
                buy_sell_ratio_change,
                price_velocity,
                volume_acceleration,
                liquidity_acceleration,
                label["reached_50"] if label else None,
                label["reached_100"] if label else None,
                label["reached_500"] if label else None,
                label["rugged"] if label else None,
                label["ret_24h"] if label else None,
                label["max_runup_pct"] if label else None,
                label["max_drawdown_pct"] if label else None,
                label["label_version"] if label else None,
            )
        )
    return rows


def refresh_token_velocity(conn: sqlite3.Connection) -> int:
    rows = compute_velocity_rows(conn)
    conn.execute("DELETE FROM token_velocity")
    conn.executemany(
        """
        INSERT INTO token_velocity (
            mint, sighting_id, seen_at, computed_at,
            liquidity_change_5m, liquidity_change_15m, liquidity_change_1h,
            volume_change_5m, volume_change_15m, volume_change_1h,
            buy_count_change, sell_count_change, buy_sell_ratio_change,
            price_change_velocity, volume_acceleration, liquidity_acceleration,
            reached_50, reached_100, reached_500, rugged,
            ret_24h, max_runup_pct, max_drawdown_pct, label_version
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def mann_whitney_p(a: list[float], b: list[float]) -> float | None:
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return None
    combined = [(value, 0) for value in a] + [(value, 1) for value in b]
    combined.sort(key=lambda item: item[0])
    ranks = [0.0] * len(combined)
    tie_term = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        rank = (i + j + 1) / 2
        for k in range(i, j):
            ranks[k] = rank
        tied = j - i
        if tied > 1:
            tie_term += tied**3 - tied
        i = j
    r1 = sum(ranks[index] for index, (_value, group) in enumerate(combined) if group == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var_u = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1)))
    if var_u <= 0:
        return None
    z = (u1 - n1 * n2 / 2) / math.sqrt(var_u)
    return math.erfc(abs(z) / math.sqrt(2))


def point_biserial(values: list[float], flags: list[int]) -> float | None:
    if len(values) != len(flags) or len(values) < 3 or len(set(flags)) < 2:
        return None
    mean_all = statistics.mean(values)
    positives = [value for value, flag in zip(values, flags) if flag == 1]
    negatives = [value for value, flag in zip(values, flags) if flag == 0]
    if not positives or not negatives:
        return None
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return None
    p = len(positives) / len(values)
    q = len(negatives) / len(values)
    return ((statistics.mean(positives) - statistics.mean(negatives)) / stdev) * math.sqrt(p * q)


def _bucket_edges(values: list[float], bucket_count: int = 5) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < bucket_count:
        return []
    edges = []
    for i in range(1, bucket_count):
        edges.append(ordered[int((len(ordered) - 1) * i / bucket_count)])
    return sorted(set(edges))


def _bucket_label(low: float | None, high: float | None) -> str:
    if low is None:
        return f"<= {_fmt(high)}"
    if high is None:
        return f"> {_fmt(low)}"
    return f"({_fmt(low)}, {_fmt(high)}]"


def _bucket_for(value: float, edges: list[float]) -> tuple[str, float | None, float | None]:
    low = None
    for edge in edges:
        if value <= edge:
            return _bucket_label(low, edge), low, edge
        low = edge
    return _bucket_label(low, None), low, None


def _rates(rows: list[sqlite3.Row]) -> tuple[float | None, float | None, float | None, float | None]:
    if not rows:
        return None, None, None, None
    n = len(rows)
    return (
        sum(1 for row in rows if row["reached_50"] == 1) / n,
        sum(1 for row in rows if row["reached_100"] == 1) / n,
        sum(1 for row in rows if row["reached_500"] == 1) / n,
        sum(1 for row in rows if row["rugged"] == 1) / n,
    )


def refresh_velocity_signals(conn: sqlite3.Connection) -> int:
    conn.row_factory = sqlite3.Row
    labelled = conn.execute(
        "SELECT * FROM token_velocity WHERE label_version=? AND max_runup_pct IS NOT NULL",
        (LABEL_VERSION,),
    ).fetchall()
    conn.execute("DELETE FROM velocity_signals")
    if not labelled:
        conn.commit()
        return 0
    baseline_50, baseline_100, baseline_500, baseline_rug = _rates(labelled)
    computed_at = time.time()
    signal_rows: list[tuple[Any, ...]] = []
    for feature in FEATURES:
        feature_values = [(row, _num(row[feature])) for row in labelled]
        feature_values = [(row, value) for row, value in feature_values if value is not None]
        if len(feature_values) < 20:
            continue
        all_values = [value for _row, value in feature_values]
        edges = _bucket_edges(all_values)
        if not edges:
            continue
        pumped = [value for row, value in feature_values if row["reached_100"] == 1]
        not_pumped = [value for row, value in feature_values if row["reached_100"] != 1]
        rugged = [value for row, value in feature_values if row["rugged"] == 1]
        not_rugged = [value for row, value in feature_values if row["rugged"] != 1]
        pump_p = mann_whitney_p(pumped, not_pumped)
        rug_p = mann_whitney_p(rugged, not_rugged)
        by_bucket: dict[str, tuple[float | None, float | None, list[sqlite3.Row]]] = {}
        for row, value in feature_values:
            label, low, high = _bucket_for(value, edges)
            if label not in by_bucket:
                by_bucket[label] = (low, high, [])
            by_bucket[label][2].append(row)
        for label, (low, high, bucket_rows) in by_bucket.items():
            reached_50, reached_100, reached_500, rug_rate = _rates(bucket_rows)
            ret_values = [_num(row["ret_24h"]) for row in bucket_rows]
            runup_values = [_num(row["max_runup_pct"]) for row in bucket_rows]
            drawdown_values = [_num(row["max_drawdown_pct"]) for row in bucket_rows]
            signal_rows.append(
                (
                    feature,
                    label,
                    low,
                    high,
                    len(bucket_rows),
                    reached_50,
                    reached_100,
                    reached_500,
                    rug_rate,
                    _mean([value for value in ret_values if value is not None]),
                    _mean([value for value in runup_values if value is not None]),
                    _mean([value for value in drawdown_values if value is not None]),
                    reached_100 / baseline_100 if reached_100 is not None and baseline_100 else None,
                    rug_rate / baseline_rug if rug_rate is not None and baseline_rug else None,
                    pump_p,
                    rug_p,
                    computed_at,
                )
            )
    conn.executemany(
        """
        INSERT INTO velocity_signals (
            feature_name, bucket_label, bucket_min, bucket_max, n,
            reached_50_rate, reached_100_rate, reached_500_rate, rug_rate,
            avg_ret_24h, avg_max_runup_pct, avg_max_drawdown_pct,
            pump_lift, rug_lift, pump_p_value, rug_p_value, computed_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        signal_rows,
    )
    conn.commit()
    return len(signal_rows)


def refresh(conn: sqlite3.Connection) -> tuple[int, int]:
    velocity_rows = refresh_token_velocity(conn)
    signal_rows = refresh_velocity_signals(conn)
    return velocity_rows, signal_rows


def correlation_rows(conn: sqlite3.Connection) -> list[tuple[str, int, float | None, float | None, float | None, float | None]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM token_velocity WHERE label_version=? AND max_runup_pct IS NOT NULL",
        (LABEL_VERSION,),
    ).fetchall()
    output = []
    for feature in FEATURES:
        pairs = [(row[feature], row["reached_100"], row["rugged"]) for row in rows if _num(row[feature]) is not None]
        values = [_num(value) for value, _pump, _rug in pairs]
        clean_values = [value for value in values if value is not None]
        pump_flags = [int(pump == 1) for value, pump, _rug in pairs if _num(value) is not None]
        rug_flags = [int(rug == 1) for value, _pump, rug in pairs if _num(value) is not None]
        pump_corr = point_biserial(clean_values, pump_flags)
        rug_corr = point_biserial(clean_values, rug_flags)
        pumped = [value for value, flag in zip(clean_values, pump_flags) if flag == 1]
        not_pumped = [value for value, flag in zip(clean_values, pump_flags) if flag == 0]
        rugged = [value for value, flag in zip(clean_values, rug_flags) if flag == 1]
        not_rugged = [value for value, flag in zip(clean_values, rug_flags) if flag == 0]
        output.append((feature, len(clean_values), pump_corr, mann_whitney_p(pumped, not_pumped), rug_corr, mann_whitney_p(rugged, not_rugged)))
    return output


def report_lines(conn: sqlite3.Connection) -> list[str]:
    refresh(conn)
    lines = ["=== LIQUIDITY VELOCITY REPORT ===", ""]
    counts = conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN label_version=? THEN 1 ELSE 0 END),
               SUM(CASE WHEN label_version=? AND max_runup_pct IS NOT NULL THEN 1 ELSE 0 END)
        FROM token_velocity
        """,
        (LABEL_VERSION, LABEL_VERSION),
    ).fetchone()
    lines.extend(
        [
            f"velocity rows: {counts[0] or 0}",
            f"rows linked to labels: {counts[1] or 0}",
            f"rows usable for analysis: {counts[2] or 0}",
            "",
            "Feature correlation with +100% pumps and rugs:",
            "feature                         n  pump_corr  pump_p   rug_corr   rug_p",
        ]
    )
    for feature, n, pump_corr, pump_p, rug_corr, rug_p in correlation_rows(conn):
        lines.append(
            f"{feature:<30} {n:>4} {_fmt(pump_corr):>10} {_fmt(pump_p, 4):>7}"
            f" {_fmt(rug_corr):>10} {_fmt(rug_p, 4):>7}"
        )
    lines.extend(["", "Best velocity buckets by +100% pump lift:", _bucket_header()])
    lines.extend(_bucket_lines(conn, order="pump"))
    lines.extend(["", "Worst velocity buckets by rug lift:", _bucket_header()])
    lines.extend(_bucket_lines(conn, order="rug"))
    lines.append("=== END REPORT ===")
    return lines


def _bucket_header() -> str:
    return "feature                         bucket                n  +100_rate  rug_rate  pump_lift  rug_lift  avg_runup  p_pump"


def _bucket_lines(conn: sqlite3.Connection, order: str, limit: int = 12) -> list[str]:
    order_sql = (
        "pump_lift DESC, reached_100_rate DESC, n DESC"
        if order == "pump"
        else "rug_lift DESC, rug_rate DESC, n DESC"
    )
    rows = conn.execute(
        f"""
        SELECT feature_name, bucket_label, n, reached_100_rate, rug_rate,
               pump_lift, rug_lift, avg_max_runup_pct, pump_p_value
        FROM velocity_signals
        WHERE n >= 10
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return ["(none)"]
    return [
        f"{feature:<30} {bucket:<20} {n:>4} {_fmt(pump_rate):>9}"
        f" {_fmt(rug_rate):>9} {_fmt(pump_lift):>10} {_fmt(rug_lift):>9}"
        f" {_fmt(avg_runup):>10} {_fmt(pump_p, 4):>7}"
        for feature, bucket, n, pump_rate, rug_rate, pump_lift, rug_lift, avg_runup, pump_p in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit velocity research layer")
    parser.add_argument("--refresh", action="store_true", help="refresh token velocity and aggregate signals")
    parser.add_argument("--report", action="store_true", help="print velocity research report")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        if args.refresh:
            velocity_rows, signal_rows = refresh(conn)
            print(f"velocity refreshed: token_velocity={velocity_rows} velocity_signals={signal_rows}")
        if args.report or not args.refresh:
            print("\n".join(report_lines(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
