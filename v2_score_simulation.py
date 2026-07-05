from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arlobit import db
from arlobit.velocity import refresh as refresh_velocity


FEATURES = [
    "liquidity_change_1h",
    "volume_change_1h",
    "liquidity_change_15m",
    "buy_count_change",
    "buys_m5",
    "price_change_velocity",
    "volume_change_15m",
]


@dataclass
class Row:
    mint: str
    seen_at: float
    current_score: float | None
    v2_score: float | None
    combined_score: float | None
    ret_24h: float | None
    max_runup_pct: float
    max_drawdown_pct: float
    reached_50: int
    reached_100: int
    reached_500: int
    rugged: int


def num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def percentile_rank(values: list[float], value: float, higher_is_better: bool = True) -> float:
    ordered = sorted(values)
    if len(ordered) <= 1:
        return 50.0
    below = sum(1 for item in ordered if item <= value)
    pct = (below - 1) / (len(ordered) - 1) * 100
    return pct if higher_is_better else 100 - pct


def load_rows(conn: sqlite3.Connection) -> list[Row]:
    refresh_velocity(conn)
    conn.row_factory = sqlite3.Row
    raw = conn.execute(
        """
        SELECT l.mint, s.seen_at, s.arlobit_score,
               tv.liquidity_change_1h, tv.volume_change_1h, tv.liquidity_change_15m,
               tv.buy_count_change, s.buys_m5, tv.price_change_velocity,
               tv.volume_change_15m,
               l.ret_24h, l.max_runup_pct, l.max_drawdown_pct,
               l.reached_50, l.reached_100, l.reached_500, l.rugged
        FROM labels l
        JOIN candidate_sightings s ON s.sighting_id = l.base_sighting_id
        LEFT JOIN token_velocity tv ON tv.mint = l.mint
        WHERE l.label_version = 1
          AND l.max_runup_pct IS NOT NULL
          AND l.max_drawdown_pct IS NOT NULL
        ORDER BY s.seen_at, l.mint
        """
    ).fetchall()
    feature_values = {
        feature: [num(row[feature]) for row in raw if num(row[feature]) is not None]
        for feature in FEATURES
    }
    current_scores = [num(row["arlobit_score"]) for row in raw if num(row["arlobit_score"]) is not None]
    rows: list[Row] = []
    for row in raw:
        components = []
        for feature in FEATURES:
            value = num(row[feature])
            values = feature_values[feature]
            if value is None or not values:
                continue
            # Higher is generally better for the selected alpha candidates, except
            # buy_count_change where the report's signal was moderated declines.
            higher_is_better = feature != "buy_count_change"
            components.append(percentile_rank(values, value, higher_is_better=higher_is_better))
        v2_score = sum(components) / len(components) if components else None
        current_score = num(row["arlobit_score"])
        combined = None
        if v2_score is not None and current_score is not None and current_scores:
            current_pct = percentile_rank(current_scores, current_score, higher_is_better=True)
            combined = 0.5 * current_pct + 0.5 * v2_score
        elif v2_score is not None:
            combined = v2_score
        rows.append(
            Row(
                mint=row["mint"],
                seen_at=float(row["seen_at"]),
                current_score=current_score,
                v2_score=v2_score,
                combined_score=combined,
                ret_24h=num(row["ret_24h"]),
                max_runup_pct=float(row["max_runup_pct"]),
                max_drawdown_pct=float(row["max_drawdown_pct"]),
                reached_50=int(row["reached_50"] == 1),
                reached_100=int(row["reached_100"] == 1),
                reached_500=int(row["reached_500"] == 1),
                rugged=int(row["rugged"] == 1),
            )
        )
    return rows


def max_drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    mdd = 0.0
    for ret in returns:
        equity *= max(0.0, 1 + ret / 100)
        peak = max(peak, equity)
        mdd = min(mdd, (equity / peak - 1) * 100)
    return mdd


def metrics(rows: list[Row]) -> dict[str, Any]:
    returns = [row.ret_24h for row in rows if row.ret_24h is not None]
    wins = [ret for ret in returns if ret > 0]
    losses = [ret for ret in returns if ret < 0]
    return {
        "n": len(rows),
        "return_n": len(returns),
        "win_rate": sum(row.reached_100 for row in rows) / len(rows) if rows else None,
        "expectancy": sum(returns) / len(returns) if returns else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "drawdown": max_drawdown(returns) if returns else None,
        "r50": sum(row.reached_50 for row in rows) / len(rows) if rows else None,
        "r100": sum(row.reached_100 for row in rows) / len(rows) if rows else None,
        "r500": sum(row.reached_500 for row in rows) / len(rows) if rows else None,
        "rug": sum(row.rugged for row in rows) / len(rows) if rows else None,
        "avg_runup": statistics.mean([row.max_runup_pct for row in rows]) if rows else None,
    }


def select_top(rows: list[Row], attr: str, quantile: float) -> list[Row]:
    scored = [row for row in rows if getattr(row, attr) is not None]
    if not scored:
        return []
    scored.sort(key=lambda row: getattr(row, attr), reverse=True)
    n = max(1, int(len(scored) * quantile))
    return scored[:n]


def fmt(value: Any, decimals: int = 2) -> str:
    parsed = num(value)
    return "-" if parsed is None else f"{parsed:.{decimals}f}"


def pct(value: Any) -> str:
    parsed = num(value)
    return "-" if parsed is None else f"{parsed * 100:.1f}%"


def table_line(name: str, split: str, m: dict[str, Any]) -> str:
    return (
        f"| {name} | {split} | {m['n']} | {pct(m['win_rate'])} | {fmt(m['expectancy'])}% | "
        f"{fmt(m['profit_factor'])} | {fmt(m['drawdown'])}% | {pct(m['r50'])} | "
        f"{pct(m['r100'])} | {pct(m['r500'])} | {pct(m['rug'])} |"
    )


def build_report(rows: list[Row]) -> str:
    comparable = [
        row for row in rows
        if row.current_score is not None and row.v2_score is not None and row.combined_score is not None
    ]
    split_idx = int(len(comparable) * 0.70)
    train = comparable[:split_idx]
    holdout = comparable[split_idx:]
    scenarios = [
        ("Current score", "current_score"),
        ("V2 velocity score", "v2_score"),
        ("Combined score", "combined_score"),
    ]
    lines = [
        "# ArloBit v2 Score Simulation",
        "",
        "Offline research only. No trading logic, filters, scoring code, execution, private keys, or signing were changed.",
        "",
        "## Method",
        "",
        "- Universe: labelled tokens with completed `labels` rows.",
        "- Holdout: chronological 70/30 split because all labels are currently in one `holdout_week`.",
        "- Current score: stored `candidate_sightings.arlobit_score`.",
        "- V2 velocity score: percentile blend of `liquidity_change_1h`, `volume_change_1h`, `liquidity_change_15m`, `buy_count_change`, `buys_m5`, `price_change_velocity`, `volume_change_15m`.",
        "- Combined score: 50/50 blend of current-score percentile and v2 velocity score.",
        "- Selection rule for comparison: top 20% by each score within the evaluated split.",
        "- Comparison universe: rows where current score, v2 velocity score, and combined score are all available.",
        "",
        "## Results",
        "",
        "| Score | Split | sample size | win rate (+100%) | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, attr in scenarios:
        lines.append(table_line(name, "all top 20%", metrics(select_top(comparable, attr, 0.20))))
        lines.append(table_line(name, "train top 20%", metrics(select_top(train, attr, 0.20))))
        lines.append(table_line(name, "holdout top 20%", metrics(select_top(holdout, attr, 0.20))))
    lines.extend(
        [
            "",
            "## Baseline",
            "",
            table_header(),
            table_line("Comparable universe", "all", metrics(comparable)),
            table_line("Comparable universe", "train", metrics(train)),
            table_line("Comparable universe", "holdout", metrics(holdout)),
            "",
            "## Coverage",
            "",
            f"- Total labelled rows: {len(rows)}",
            f"- Comparable rows with all three scores: {len(comparable)}",
            f"- Rows with current score: {sum(1 for row in rows if row.current_score is not None)}",
            f"- Rows with v2 velocity score: {sum(1 for row in rows if row.v2_score is not None)}",
            "",
            "## Interpretation",
            "",
            "- Treat this as feature research, not a deployment threshold.",
            "- The v2 velocity score is intentionally simple and only tests whether the alpha-report candidates contain signal.",
            "- A score should only be considered for ArloBit v2 after more labelled weeks and separate walk-forward validation.",
        ]
    )
    return "\n".join(lines) + "\n"


def table_header() -> str:
    return "\n".join(
        [
            "| Score | Split | sample size | win rate (+100%) | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )


def main() -> None:
    conn = db.connect()
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    Path("V2_SCORE_SIMULATION.md").write_text(build_report(rows), encoding="utf-8")
    print(f"wrote V2_SCORE_SIMULATION.md ({len(rows)} labelled rows)")


if __name__ == "__main__":
    main()
