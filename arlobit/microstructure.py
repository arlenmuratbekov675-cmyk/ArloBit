"""Transaction-level microstructure research for ArloBit.

This module only collects and reports research data. It is not imported by
scanner, paper trading, live execution, alerts, scoring, or V3 rules.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import Counter
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - existing project normally has requests.
    requests = None

from arlobit import db, early_buyers

DEFAULT_MINT_LIMIT = 20
DEFAULT_TIMEOUT = 20
RECENT_LOOKBACK_SECONDS = 10 * 60
RECENT_LOOKAHEAD_SECONDS = 5 * 60
SOL_MINT = early_buyers.SOL_MINT
USDC_MINT = early_buyers.USDC_MINT
LAMPORTS_PER_SOL = early_buyers.LAMPORTS_PER_SOL


def _now() -> float:
    return time.time()


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _fmt(value: Any, decimals: int = 3) -> str:
    number = _num(value)
    return "-" if number is None else f"{number:.{decimals}f}"


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _raw_token_amount(item: dict[str, Any]) -> float | None:
    raw = item.get("rawTokenAmount")
    if isinstance(raw, dict):
        amount = _num(raw.get("tokenAmount"))
        decimals = _int(raw.get("decimals"))
        if amount is not None and decimals is not None:
            return amount / (10**decimals)
    return _num(item.get("tokenAmount"))


def _native_sol_amount(native_value: dict[str, Any] | None) -> float | None:
    if not isinstance(native_value, dict):
        return None
    amount = _num(native_value.get("amount"))
    return None if amount is None else amount / LAMPORTS_PER_SOL


def _stable_amount(items: list[Any]) -> float | None:
    total = 0.0
    found = False
    for item in items:
        if not isinstance(item, dict) or item.get("mint") != USDC_MINT:
            continue
        amount = _raw_token_amount(item)
        if amount is not None:
            total += amount
            found = True
    return total if found else None


def _price(usd_amount: float | None, token_amount: float | None) -> float | None:
    if usd_amount is None or token_amount is None or token_amount <= 0:
        return None
    return usd_amount / token_amount


def _latest_candidate_mints(conn: sqlite3.Connection, limit: int) -> list[tuple[str, float | None]]:
    rows = conn.execute(
        """
        SELECT mint, MAX(seen_at) AS last_seen
        FROM candidate_sightings
        GROUP BY mint
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(row[0], _num(row[1])) for row in rows]


def _event_rows_from_tx(tx: dict[str, Any], mint: str) -> list[tuple[Any, ...]]:
    timestamp = _num(tx.get("timestamp"))
    signature = _first_string(tx.get("signature"))
    slot = _int(tx.get("slot"))
    router = _first_string(tx.get("source"))
    program = _first_string(tx.get("type"))
    success = 0 if tx.get("transactionError") else 1
    swap = ((tx.get("events") or {}).get("swap") or {}) if isinstance(tx.get("events"), dict) else {}
    if not isinstance(swap, dict):
        return []

    rows: list[tuple[Any, ...]] = []
    now = _now()
    sol_in = _native_sol_amount(swap.get("nativeInput"))
    sol_out = _native_sol_amount(swap.get("nativeOutput"))
    usd_in = _stable_amount(swap.get("tokenInputs") or [])
    usd_out = _stable_amount(swap.get("tokenOutputs") or [])

    for item in swap.get("tokenOutputs") or []:
        if not isinstance(item, dict) or item.get("mint") != mint:
            continue
        wallet = _first_string(item.get("userAccount"), item.get("toUserAccount"))
        token_amount = _raw_token_amount(item)
        rows.append(
            (
                mint,
                signature,
                slot,
                timestamp,
                wallet,
                "buy",
                token_amount,
                sol_in,
                usd_in,
                _price(usd_in, token_amount),
                router,
                program,
                success,
                now,
            )
        )

    for item in swap.get("tokenInputs") or []:
        if not isinstance(item, dict) or item.get("mint") != mint:
            continue
        wallet = _first_string(item.get("userAccount"), item.get("fromUserAccount"))
        token_amount = _raw_token_amount(item)
        rows.append(
            (
                mint,
                signature,
                slot,
                timestamp,
                wallet,
                "sell",
                token_amount,
                sol_out,
                usd_out,
                _price(usd_out, token_amount),
                router,
                program,
                success,
                now,
            )
        )
    return rows


def collect_recent(limit: int, timeout: int) -> list[str]:
    conn = db.connect()
    try:
        mints = _latest_candidate_mints(conn, limit)
    finally:
        conn.close()

    lines = ["=== MICROSTRUCTURE COLLECT RECENT ===", f"recent mints selected: {len(mints)}"]
    if requests is None:
        lines.append("missing API/data source: requests package is unavailable")
        lines.append("=== END COLLECT ===")
        return lines

    early_buyers.load_dotenv()
    api_key = early_buyers.helius_api_key()
    if not api_key:
        lines.append("missing API/data source: Helius enhanced transactions API key")
        lines.append("set HELIUS_API_KEY or a Helius SOLANA_RPC_URL with api-key")
        lines.append("tables are available; no token_events collected")
        lines.append("=== END COLLECT ===")
        return lines

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBitMicrostructure/0.1"})
    inserted = 0
    failures: Counter[str] = Counter()
    conn = db.connect()
    try:
        for mint, last_seen in mints:
            start_time = (last_seen - RECENT_LOOKBACK_SECONDS) if last_seen else None
            end_time = (last_seen + RECENT_LOOKAHEAD_SECONDS) if last_seen else None
            try:
                txs = early_buyers.fetch_enhanced_transactions(session, api_key, mint, start_time, end_time, timeout)
            except Exception as exc:
                failures[str(exc)] += 1
                continue
            rows: list[tuple[Any, ...]] = []
            for tx in txs:
                rows.extend(_event_rows_from_tx(tx, mint))
            if not rows:
                failures["no swap events for mint"] += 1
                continue
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO token_events (
                    mint, signature, slot, block_time, wallet, side, token_amount,
                    sol_amount, usd_amount, price, router, program, success, created_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            inserted += cursor.rowcount
            conn.commit()
            time.sleep(0.25)
    finally:
        conn.close()

    lines.append("data source: Helius enhanced transactions")
    lines.append(f"events inserted: {inserted}")
    if failures:
        lines.append("collection notes:")
        lines.extend(f"- {reason}: {count}" for reason, count in failures.most_common(10))
    lines.append("=== END COLLECT ===")
    return lines


def _entropy(values: list[Any]) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    counts = Counter(filtered)
    total = sum(counts.values())
    entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    max_entropy = math.log2(len(counts)) if len(counts) > 1 else 0.0
    return entropy / max_entropy if max_entropy > 0 else 0.0


def _amount_bucket(value: float | None) -> str | None:
    if value is None or value <= 0:
        return None
    return str(math.floor(math.log10(value)))


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], 0.0
    return statistics.mean(values), statistics.pstdev(values)


def _features_for_mint(mint: str, events: list[sqlite3.Row], now: float) -> tuple[Any, ...] | None:
    times = [_num(row["block_time"]) for row in events if _num(row["block_time"]) is not None]
    if not times:
        return None
    end = max(times)
    last_10 = [row for row in events if _num(row["block_time"]) is not None and row["block_time"] >= end - 10]
    last_30 = [row for row in events if _num(row["block_time"]) is not None and row["block_time"] >= end - 30]
    last_60 = [row for row in events if _num(row["block_time"]) is not None and row["block_time"] >= end - 60]
    buys_10 = [row for row in last_10 if row["side"] == "buy"]
    sells_10 = [row for row in last_10 if row["side"] == "sell"]
    buy_sell_ratio = len(buys_10) / (len(buys_10) + len(sells_10)) if buys_10 or sells_10 else None

    sorted_times = sorted(_num(row["block_time"]) for row in last_60 if _num(row["block_time"]) is not None)
    gaps = [right - left for left, right in zip(sorted_times, sorted_times[1:]) if right >= left]
    interarrival_mean, interarrival_std = _mean_std(gaps)
    event_acceleration = (len(last_10) * 6 / len(last_60)) if last_60 else None

    buy_amounts = [
        _num(row["usd_amount"]) if _num(row["usd_amount"]) is not None else _num(row["sol_amount"])
        for row in last_60
        if row["side"] == "buy"
    ]
    buy_size_entropy = _entropy([_amount_bucket(value) for value in buy_amounts])
    wallet_entropy = _entropy([row["wallet"] for row in last_60])
    unique_10 = {row["wallet"] for row in last_10 if row["wallet"]}
    unique_60 = {row["wallet"] for row in last_60 if row["wallet"]}
    unique_wallet_growth = len(unique_10) / len(unique_60) if unique_60 else None
    failed_buy_count = sum(1 for row in last_60 if row["side"] == "buy" and row["success"] == 0)
    total_buys = sum(1 for row in last_60 if row["side"] == "buy")
    failed_buy_ratio = failed_buy_count / total_buys if total_buys else None
    positive_buy_amounts = sorted((value for value in buy_amounts if value is not None and value > 0), reverse=True)
    large_buy_concentration = (
        sum(positive_buy_amounts[:3]) / sum(positive_buy_amounts) if positive_buy_amounts else None
    )

    return (
        mint,
        now,
        len(last_10),
        len(last_30),
        len(last_60),
        len(buys_10),
        len(sells_10),
        buy_sell_ratio,
        interarrival_mean,
        interarrival_std,
        event_acceleration,
        buy_size_entropy,
        wallet_entropy,
        unique_wallet_growth,
        failed_buy_count,
        failed_buy_ratio,
        large_buy_concentration,
        now,
    )


def calculate_features() -> list[str]:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    try:
        mints = [row[0] for row in conn.execute("SELECT DISTINCT mint FROM token_events").fetchall()]
        now = _now()
        rows: list[tuple[Any, ...]] = []
        for mint in mints:
            events = conn.execute(
                "SELECT * FROM token_events WHERE mint=? ORDER BY block_time, id",
                (mint,),
            ).fetchall()
            features = _features_for_mint(mint, events, now)
            if features is not None:
                rows.append(features)
        conn.executemany(
            """
            INSERT INTO microstructure_features (
                mint, calculated_at, tx_count_10s, tx_count_30s, tx_count_60s,
                buy_count_10s, sell_count_10s, buy_sell_ratio_10s,
                interarrival_mean, interarrival_std, event_acceleration,
                buy_size_entropy, wallet_entropy, unique_wallet_growth,
                failed_buy_count, failed_buy_ratio, large_buy_concentration, created_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return [
        "=== MICROSTRUCTURE FEATURES ===",
        f"mints with events: {len(mints)}",
        f"feature rows written: {len(rows)}",
        "=== END FEATURES ===",
    ]


def _top_lines(conn: sqlite3.Connection, title: str, column: str, limit: int = 10) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT mint, {column}, tx_count_60s, event_acceleration, wallet_entropy, large_buy_concentration
        FROM microstructure_features
        WHERE calculated_at = (
            SELECT MAX(calculated_at) FROM microstructure_features mf2
            WHERE mf2.mint = microstructure_features.mint
        )
        ORDER BY {column} DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    lines = [title, "mint                                      value    tx60  accel  wallet_H  large_buy_conc"]
    if not rows:
        lines.append("(none)")
    for row in rows:
        lines.append(
            f"{row['mint']:<41} {_fmt(row[column]):>7}"
            f" {row['tx_count_60s']:>5} {_fmt(row['event_acceleration']):>6}"
            f" {_fmt(row['wallet_entropy']):>9} {_fmt(row['large_buy_concentration']):>15}"
        )
    return lines


def report_lines() -> list[str]:
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    try:
        event_count = conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
        mint_count = conn.execute("SELECT COUNT(DISTINCT mint) FROM token_events").fetchone()[0]
        feature_count = conn.execute("SELECT COUNT(*) FROM microstructure_features").fetchone()[0]
        lines = [
            "=== MICROSTRUCTURE REPORT ===",
            "research-only transaction/event layer; no trading, alerts, scoring, scanner, or strategy integration",
            f"events: {event_count}",
            f"mints covered: {mint_count}",
            f"feature rows: {feature_count}",
            "",
        ]
        if event_count == 0:
            lines.append("data insufficient: token_events is empty")
            lines.append("run --collect-recent with Helius enhanced transaction API access")
            lines.append("=== END REPORT ===")
            return lines
        if feature_count == 0:
            lines.append("data insufficient: no microstructure_features rows yet")
            lines.append("run --features")
            lines.append("=== END REPORT ===")
            return lines
        lines.extend(_top_lines(conn, "Top by tx_count_60s:", "tx_count_60s"))
        lines.extend(["", *_top_lines(conn, "Top by event_acceleration:", "event_acceleration")])
        lines.extend(["", *_top_lines(conn, "Top by wallet_entropy:", "wallet_entropy")])
        lines.extend(["", *_top_lines(conn, "Top by large_buy_concentration:", "large_buy_concentration")])
        lines.append("=== END REPORT ===")
        return lines
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit microstructure research layer")
    parser.add_argument("--collect-recent", action="store_true", help="collect recent token event data")
    parser.add_argument("--features", action="store_true", help="compute microstructure features from token_events")
    parser.add_argument("--report", action="store_true", help="print microstructure report")
    parser.add_argument("--limit", type=int, default=DEFAULT_MINT_LIMIT, help="recent mint limit for collection")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    args = parser.parse_args(argv)

    ran = False
    if args.collect_recent:
        print("\n".join(collect_recent(args.limit, args.timeout)))
        ran = True
    if args.features:
        print("\n".join(calculate_features()))
        ran = True
    if args.report or not ran:
        print("\n".join(report_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
