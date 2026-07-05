"""Wallet intelligence research layer.

This module derives wallet reputation only from stored ArloBit research data:
early buyers plus completed outcome labels. It does not affect scanner filters,
scores, execution, alerts, or trading decisions.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from arlobit import db

LABEL_VERSION = 1
REPUTATIONS = ("ELITE", "SMART", "GOOD", "NEUTRAL", "RISKY", "SCAM_CLUSTER")


@dataclass
class WalletAggregate:
    buyer_wallet: str
    first_seen_at: float | None
    last_seen_at: float | None
    early_buy_count: int
    total_tokens_seen: int
    total_completed: int
    total_pumps: int
    total_50pct: int
    total_500pct: int
    total_rugs: int
    average_return_24h: float | None
    average_max_runup: float | None
    average_drawdown: float | None
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    confidence_score: float
    reputation: str


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, decimals: int = 1) -> str:
    number = _num(value)
    return "-" if number is None else f"{number:.{decimals}f}"


def _pct(value: Any) -> str:
    number = _num(value)
    return "-" if number is None else f"{number * 100:.1f}%"


def _rank_map(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 0.5}
    return {wallet: index / (len(ordered) - 1) for index, (wallet, _value) in enumerate(ordered)}


def _inverse_rank_map(values: dict[str, float]) -> dict[str, float]:
    ranks = _rank_map(values)
    return {wallet: 1.0 - rank for wallet, rank in ranks.items()}


def refresh_wallet_token_outcomes(conn: sqlite3.Connection) -> None:
    now = time.time()
    conn.execute("DELETE FROM wallet_token_outcomes")
    conn.execute(
        """
        INSERT INTO wallet_token_outcomes (
            buyer_wallet, mint, first_buy_time, reached_50, reached_100, reached_500,
            rugged, ret_24h, max_runup_pct, max_drawdown_pct, label_version, updated_at
        )
        SELECT eb.buyer_wallet, eb.mint, eb.first_buy_time,
               l.reached_50, l.reached_100, l.reached_500, l.rugged,
               l.ret_24h, l.max_runup_pct, l.max_drawdown_pct, l.label_version, ?
        FROM early_buyers eb
        LEFT JOIN labels l ON l.mint = eb.mint AND l.label_version = ?
        """,
        (now, LABEL_VERSION),
    )


def refresh_wallet_cooccurrences(conn: sqlite3.Connection) -> None:
    now = time.time()
    conn.execute("DELETE FROM wallet_cooccurrences")
    conn.execute(
        """
        INSERT INTO wallet_cooccurrences (wallet_a, wallet_b, times_seen_together, updated_at)
        SELECT eb1.buyer_wallet,
               eb2.buyer_wallet,
               COUNT(DISTINCT eb1.mint) AS times_seen_together,
               ?
        FROM early_buyers eb1
        JOIN early_buyers eb2
          ON eb1.mint = eb2.mint
         AND eb1.buyer_wallet < eb2.buyer_wallet
        GROUP BY eb1.buyer_wallet, eb2.buyer_wallet
        """,
        (now,),
    )


def _base_aggregates(conn: sqlite3.Connection) -> dict[str, WalletAggregate]:
    rows = conn.execute(
        """
        SELECT eb.buyer_wallet,
               MIN(eb.first_buy_time) AS first_seen_at,
               MAX(eb.first_buy_time) AS last_seen_at,
               COUNT(*) AS early_buy_count,
               COUNT(DISTINCT eb.mint) AS total_tokens_seen,
               COUNT(l.mint) AS total_completed,
               SUM(CASE WHEN l.reached_100 = 1 THEN 1 ELSE 0 END) AS total_pumps,
               SUM(CASE WHEN l.reached_50 = 1 THEN 1 ELSE 0 END) AS total_50pct,
               SUM(CASE WHEN l.reached_500 = 1 THEN 1 ELSE 0 END) AS total_500pct,
               SUM(CASE WHEN l.rugged = 1 THEN 1 ELSE 0 END) AS total_rugs,
               AVG(l.ret_24h) AS average_return_24h,
               AVG(l.max_runup_pct) AS average_max_runup,
               AVG(l.max_drawdown_pct) AS average_drawdown
        FROM early_buyers eb
        LEFT JOIN labels l
          ON l.mint = eb.mint
         AND l.label_version = ?
         AND l.max_runup_pct IS NOT NULL
         AND l.max_drawdown_pct IS NOT NULL
        GROUP BY eb.buyer_wallet
        """,
        (LABEL_VERSION,),
    ).fetchall()
    aggregates: dict[str, WalletAggregate] = {}
    for row in rows:
        (
            wallet,
            first_seen_at,
            last_seen_at,
            early_buy_count,
            total_tokens_seen,
            total_completed,
            total_pumps,
            total_50pct,
            total_500pct,
            total_rugs,
            average_return_24h,
            average_max_runup,
            average_drawdown,
        ) = row
        completed = int(total_completed or 0)
        pumps = int(total_pumps or 0)
        win_rate = pumps / completed if completed else None
        aggregates[wallet] = WalletAggregate(
            buyer_wallet=wallet,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            early_buy_count=int(early_buy_count or 0),
            total_tokens_seen=int(total_tokens_seen or 0),
            total_completed=completed,
            total_pumps=pumps,
            total_50pct=int(total_50pct or 0),
            total_500pct=int(total_500pct or 0),
            total_rugs=int(total_rugs or 0),
            average_return_24h=average_return_24h,
            average_max_runup=average_max_runup,
            average_drawdown=average_drawdown,
            win_rate=win_rate,
            profit_factor=None,
            expectancy=average_return_24h,
            confidence_score=0.0,
            reputation="NEUTRAL",
        )
    return aggregates


def _apply_profit_factor(conn: sqlite3.Connection, aggregates: dict[str, WalletAggregate]) -> None:
    rows = conn.execute(
        """
        SELECT eb.buyer_wallet,
               SUM(CASE WHEN l.ret_24h > 0 THEN l.ret_24h ELSE 0 END) AS gross_win,
               SUM(CASE WHEN l.ret_24h < 0 THEN -l.ret_24h ELSE 0 END) AS gross_loss
        FROM early_buyers eb
        JOIN labels l ON l.mint = eb.mint AND l.label_version = ?
        WHERE l.ret_24h IS NOT NULL
        GROUP BY eb.buyer_wallet
        """,
        (LABEL_VERSION,),
    ).fetchall()
    for wallet, gross_win, gross_loss in rows:
        aggregate = aggregates.get(wallet)
        if aggregate is None:
            continue
        win = _num(gross_win) or 0.0
        loss = _num(gross_loss) or 0.0
        if loss > 0:
            aggregate.profit_factor = win / loss
        elif win > 0:
            aggregate.profit_factor = None
        else:
            aggregate.profit_factor = 0.0


def _apply_confidence_and_reputation(aggregates: dict[str, WalletAggregate]) -> None:
    eligible = {wallet: agg for wallet, agg in aggregates.items() if agg.total_completed > 0}
    if not eligible:
        return

    max_completed = max(agg.total_completed for agg in eligible.values()) or 1
    expectancy_rank = _rank_map(
        {wallet: agg.expectancy for wallet, agg in eligible.items() if agg.expectancy is not None}
    )
    win_rank = _rank_map({wallet: agg.win_rate for wallet, agg in eligible.items() if agg.win_rate is not None})
    runup_rank = _rank_map(
        {wallet: agg.average_max_runup for wallet, agg in eligible.items() if agg.average_max_runup is not None}
    )
    drawdown_rank = _rank_map(
        {wallet: agg.average_drawdown for wallet, agg in eligible.items() if agg.average_drawdown is not None}
    )
    rug_rate = {
        wallet: agg.total_rugs / agg.total_completed
        for wallet, agg in eligible.items()
        if agg.total_completed > 0
    }
    rug_rank = _inverse_rank_map(rug_rate)
    pf_rank = _rank_map(
        {
            wallet: min(agg.profit_factor, 10.0)
            for wallet, agg in eligible.items()
            if agg.profit_factor is not None
        }
    )

    for wallet, aggregate in eligible.items():
        components = [
            ranks[wallet]
            for ranks in (expectancy_rank, win_rank, runup_rank, drawdown_rank, rug_rank, pf_rank)
            if wallet in ranks
        ]
        performance = sum(components) / len(components) if components else 0.0
        sample_support = math.sqrt(aggregate.total_completed / max_completed)
        aggregate.confidence_score = round(100.0 * performance * sample_support, 4)

    ordered = sorted(eligible.values(), key=lambda agg: (agg.confidence_score, agg.buyer_wallet))
    count = len(ordered)
    if count == 1:
        ordered[0].reputation = "NEUTRAL"
        return
    for index, aggregate in enumerate(ordered):
        percentile = index / (count - 1)
        if percentile >= 0.90:
            aggregate.reputation = "ELITE"
        elif percentile >= 0.70:
            aggregate.reputation = "SMART"
        elif percentile >= 0.50:
            aggregate.reputation = "GOOD"
        elif percentile <= 0.10:
            aggregate.reputation = "SCAM_CLUSTER"
        elif percentile <= 0.30:
            aggregate.reputation = "RISKY"
        else:
            aggregate.reputation = "NEUTRAL"


def refresh_wallet_stats(conn: sqlite3.Connection, aggregates: dict[str, WalletAggregate]) -> None:
    now = time.time()
    conn.execute("DELETE FROM wallet_stats")
    conn.executemany(
        """
        INSERT INTO wallet_stats (
            buyer_wallet, first_seen_at, last_seen_at, early_buy_count, distinct_mints,
            successful_50_count, successful_100_count, successful_500_count, rugged_count,
            avg_ret_24h, avg_max_runup_pct, avg_max_drawdown_pct,
            total_tokens_seen, total_completed, total_pumps, total_50pct, total_rugs,
            average_return_24h, average_max_runup, average_drawdown,
            win_rate, profit_factor, expectancy, confidence_score, reputation, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                agg.buyer_wallet,
                agg.first_seen_at,
                agg.last_seen_at,
                agg.early_buy_count,
                agg.total_tokens_seen,
                agg.total_50pct,
                agg.total_pumps,
                agg.total_500pct,
                agg.total_rugs,
                agg.average_return_24h,
                agg.average_max_runup,
                agg.average_drawdown,
                agg.total_tokens_seen,
                agg.total_completed,
                agg.total_pumps,
                agg.total_50pct,
                agg.total_rugs,
                agg.average_return_24h,
                agg.average_max_runup,
                agg.average_drawdown,
                agg.win_rate,
                agg.profit_factor,
                agg.expectancy,
                agg.confidence_score,
                agg.reputation,
                now,
            )
            for agg in aggregates.values()
        ],
    )


def refresh_wallet_intelligence(conn: sqlite3.Connection) -> None:
    refresh_wallet_token_outcomes(conn)
    refresh_wallet_cooccurrences(conn)
    aggregates = _base_aggregates(conn)
    _apply_profit_factor(conn, aggregates)
    _apply_confidence_and_reputation(aggregates)
    refresh_wallet_stats(conn, aggregates)
    conn.execute(
        """
        UPDATE early_buyers
        SET is_repeat_buyer = CASE
            WHEN buyer_wallet IN (
                SELECT buyer_wallet FROM early_buyers GROUP BY buyer_wallet HAVING COUNT(DISTINCT mint) > 1
            )
            THEN 1 ELSE 0 END
        """
    )
    conn.commit()


def leaderboard_lines(conn: sqlite3.Connection, limit: int = 25) -> list[str]:
    refresh_wallet_intelligence(conn)
    rows = conn.execute(
        """
        SELECT buyer_wallet, reputation, total_tokens_seen, win_rate, expectancy,
               average_return_24h, total_pumps, total_rugs
        FROM wallet_stats
        ORDER BY confidence_score DESC, expectancy DESC, total_completed DESC, total_tokens_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    lines = [
        "wallet                                      reputation    tokens  win_rate  expectancy  avg_return  pumps  rugs"
    ]
    if not rows:
        lines.append("(no wallet intelligence yet)")
        return lines
    for wallet, reputation, tokens, win_rate, expectancy, average_return, pumps, rugs in rows:
        lines.append(
            f"{wallet:<43} {reputation or 'NEUTRAL':<12} {tokens or 0:>6}"
            f" {_pct(win_rate):>9} {_fmt(expectancy):>11} {_fmt(average_return):>11}"
            f" {pumps or 0:>6} {rugs or 0:>5}"
        )
    return lines


def report_lines(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    refresh_wallet_intelligence(conn)
    lines = ["=== WALLET INTELLIGENCE REPORT ===", ""]
    lines.extend(_section(conn, "Top profitable wallets", """
        SELECT buyer_wallet, reputation, total_completed, expectancy, win_rate, total_pumps, total_rugs
        FROM wallet_stats
        WHERE total_completed > 0
        ORDER BY expectancy DESC, confidence_score DESC
        LIMIT ?
    """, limit))
    lines.extend(_section(conn, "Worst wallets", """
        SELECT buyer_wallet, reputation, total_completed, expectancy, win_rate, total_pumps, total_rugs
        FROM wallet_stats
        WHERE total_completed > 0
        ORDER BY expectancy ASC, total_rugs DESC
        LIMIT ?
    """, limit))
    lines.extend(_section(conn, "Most repeated wallets", """
        SELECT buyer_wallet, reputation, total_tokens_seen, expectancy, win_rate, total_pumps, total_rugs
        FROM wallet_stats
        ORDER BY total_tokens_seen DESC, total_completed DESC
        LIMIT ?
    """, limit, tokens_column=True))
    lines.extend(_clusters(conn, "Most suspicious clusters", limit))
    lines.extend(_section(conn, "Wallets that preceded +100% tokens most often", """
        SELECT buyer_wallet, reputation, total_completed, expectancy, win_rate, total_pumps, total_rugs
        FROM wallet_stats
        WHERE total_pumps > 0
        ORDER BY total_pumps DESC, win_rate DESC, confidence_score DESC
        LIMIT ?
    """, limit))
    counts = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN total_completed > 0 THEN 1 ELSE 0 END)
        FROM wallet_stats
        """
    ).fetchone()
    clusters = conn.execute("SELECT COUNT(*) FROM wallet_cooccurrences").fetchone()[0]
    lines.extend(
        [
            "",
            "Coverage:",
            f"wallets: {counts[0] or 0}",
            f"wallets with completed labels: {counts[1] or 0}",
            f"wallet pairs seen together: {clusters}",
            "=== END REPORT ===",
        ]
    )
    return lines


def _section(
    conn: sqlite3.Connection,
    title: str,
    query: str,
    limit: int,
    tokens_column: bool = False,
) -> list[str]:
    rows = conn.execute(query, (limit,)).fetchall()
    lines = ["", title + ":"]
    if tokens_column:
        lines.append("wallet                                      reputation    tokens  expectancy  win_rate  pumps  rugs")
    else:
        lines.append("wallet                                      reputation completed  expectancy  win_rate  pumps  rugs")
    if not rows:
        lines.append("(none)")
        return lines
    for wallet, reputation, count, expectancy, win_rate, pumps, rugs in rows:
        lines.append(
            f"{wallet:<43} {reputation or 'NEUTRAL':<12} {count or 0:>9}"
            f" {_fmt(expectancy):>11} {_pct(win_rate):>9} {pumps or 0:>6} {rugs or 0:>5}"
        )
    return lines


def _clusters(conn: sqlite3.Connection, title: str, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT wc.wallet_a, wc.wallet_b, wc.times_seen_together,
               COALESCE(a.reputation, 'NEUTRAL'), COALESCE(b.reputation, 'NEUTRAL'),
               COALESCE(a.confidence_score, 0) + COALESCE(b.confidence_score, 0) AS combined_confidence
        FROM wallet_cooccurrences wc
        LEFT JOIN wallet_stats a ON a.buyer_wallet = wc.wallet_a
        LEFT JOIN wallet_stats b ON b.buyer_wallet = wc.wallet_b
        ORDER BY wc.times_seen_together DESC, combined_confidence ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    lines = ["", title + ":"]
    lines.append("wallet_a                                    wallet_b                                    together  rep_a         rep_b")
    if not rows:
        lines.append("(none)")
        return lines
    for wallet_a, wallet_b, together, rep_a, rep_b, _combined in rows:
        lines.append(f"{wallet_a:<43} {wallet_b:<43} {together:>8}  {rep_a:<12} {rep_b:<12}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit wallet intelligence research CLI")
    parser.add_argument("--top", action="store_true", help="show wallet leaderboard")
    parser.add_argument("--report", action="store_true", help="show wallet intelligence report")
    parser.add_argument("--refresh", action="store_true", help="refresh wallet intelligence tables")
    parser.add_argument("--limit", type=int, default=25, help="row limit for leaderboard/report sections")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        if args.refresh:
            refresh_wallet_intelligence(conn)
            print("wallet intelligence refreshed")
        if args.report:
            print("\n".join(report_lines(conn, args.limit)))
        elif args.top or not args.refresh:
            print("\n".join(leaderboard_lines(conn, args.limit)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
