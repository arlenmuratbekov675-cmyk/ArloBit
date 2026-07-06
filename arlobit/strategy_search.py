"""Offline strategy discovery engine for ArloBit research data.

The module generates simple rule-based strategies from research features and
validates them with a chronological train/test split. It does not change live
scanner, filter, scoring, execution, or trading code.
"""

from __future__ import annotations

import argparse
import itertools
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Any

from arlobit import db
from arlobit import edge

LABEL_VERSION = 1
REPORT_PATH = "STRATEGY_DISCOVERY_REPORT.md"
MIN_TRADES = 30
MIN_TRAIN_TRADES = 18
MIN_TEST_TRADES = 10
NUMERIC_BUCKETS = 4
MAX_PREDICATES = 68
MAX_COMBINATIONS_PER_SIZE = 350_000
RETURN_CAP_LOW = -100.0
RETURN_CAP_HIGH = 500.0


@dataclass(frozen=True)
class TradeRow:
    mint: str
    seen_at: float
    ret_24h: float
    reached_50: int
    reached_100: int
    reached_500: int
    rugged: int


@dataclass(frozen=True)
class Predicate:
    feature: str
    bucket: str
    mints: frozenset[str]

    @property
    def name(self) -> str:
        return f"{self.feature}={self.bucket}"


@dataclass(frozen=True)
class Metrics:
    n: int
    win_rate: float | None
    expectancy: float | None
    capped_expectancy: float | None
    profit_factor: float | None
    average_return: float | None
    max_drawdown: float | None
    reached_50_rate: float | None
    reached_100_rate: float | None
    reached_500_rate: float | None
    rug_rate: float | None
    sharpe: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    lift_vs_baseline: float | None


@dataclass(frozen=True)
class StrategyResult:
    predicates: tuple[Predicate, ...]
    all_metrics: Metrics
    train_metrics: Metrics
    test_metrics: Metrics
    robustness: float
    accepted: bool
    reject_reason: str

    @property
    def rule(self) -> str:
        return " AND ".join(predicate.name for predicate in self.predicates)

    @property
    def size(self) -> int:
        return len(self.predicates)


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
    if math.isinf(number):
        return "inf"
    return f"{number:.{decimals}f}"


def _pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number * 100:.1f}%"


def _cap_return(value: float) -> float:
    return min(RETURN_CAP_HIGH, max(RETURN_CAP_LOW, value))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _profit_factor(returns: list[float]) -> float | None:
    wins = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    if losses > 0:
        return wins / losses
    if wins > 0:
        return math.inf
    return None


def _max_drawdown(returns: list[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= max(0.0, 1.0 + ret / 100.0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity / peak - 1.0) * 100.0)
    return max_dd


def _sharpe(returns: list[float]) -> float | None:
    if len(returns) < 3:
        return None
    stdev = statistics.pstdev(returns)
    if stdev == 0:
        return None
    return statistics.mean(returns) / stdev * math.sqrt(len(returns))


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    phat = successes / n
    denom = 1 + z**2 / n
    center = (phat + z**2 / (2 * n)) / denom
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _two_proportion_p(success_a: int, n_a: int, success_b: int, n_b: int) -> float | None:
    if n_a <= 0 or n_b <= 0:
        return None
    pooled = (success_a + success_b) / (n_a + n_b)
    variance = pooled * (1 - pooled) * (1 / n_a + 1 / n_b)
    if variance <= 0:
        return None
    z = (success_a / n_a - success_b / n_b) / math.sqrt(variance)
    return math.erfc(abs(z) / math.sqrt(2))


def _bucket_edges(values: list[float], bucket_count: int = NUMERIC_BUCKETS) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < bucket_count:
        return []
    return sorted(
        set(ordered[int((len(ordered) - 1) * index / bucket_count)] for index in range(1, bucket_count))
    )


def _numeric_bucket(value: float, edges: list[float]) -> str:
    low = None
    for high in edges:
        if value <= high:
            return f"<= {_fmt(high)}" if low is None else f"({_fmt(low)}, {_fmt(high)}]"
        low = high
    return f"> {_fmt(low)}"


def load_trades(conn: sqlite3.Connection) -> list[TradeRow]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint, s.seen_at, l.ret_24h, l.reached_50, l.reached_100,
               l.reached_500, l.rugged
        FROM labels l
        JOIN candidate_sightings s ON s.sighting_id = l.base_sighting_id
        WHERE l.label_version = ?
          AND l.ret_24h IS NOT NULL
          AND l.max_runup_pct IS NOT NULL
          AND l.max_drawdown_pct IS NOT NULL
        ORDER BY s.seen_at, l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    return [
        TradeRow(
            mint=row["mint"],
            seen_at=float(row["seen_at"] or 0),
            ret_24h=float(row["ret_24h"]),
            reached_50=int(row["reached_50"] == 1),
            reached_100=int(row["reached_100"] == 1),
            reached_500=int(row["reached_500"] == 1),
            rugged=int(row["rugged"] == 1),
        )
        for row in rows
    ]


def _split_trades(trades: list[TradeRow]) -> tuple[list[TradeRow], list[TradeRow]]:
    split_index = int(len(trades) * 0.70)
    return trades[:split_index], trades[split_index:]


def _feature_maps(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    raw = edge.collect_feature_maps(conn)
    wanted_prefixes = (
        "velocity.",
        "candidate.age_minutes",
        "candidate.liquidity_usd",
        "bucket.",
        "candidate.creator_quality",
        "candidate.top",
        "candidate.holder_status",
        "wallet.",
        "cluster.",
        "candidate.source",
        "candidate.arlobit_score",
        "candidate.buys_m5",
        "candidate.sells_m5",
        "candidate.buy_sell_ratio_m5",
        "candidate.vol_liq_ratio",
        "candidate.vol_m5",
    )
    return {
        name: values
        for name, values in raw.items()
        if any(name.startswith(prefix) for prefix in wanted_prefixes)
    }


def build_predicates(
    feature_maps: dict[str, dict[str, Any]],
    train_trades: list[TradeRow],
    all_trades: list[TradeRow],
) -> list[Predicate]:
    train_mints = {row.mint for row in train_trades}
    all_mints = {row.mint for row in all_trades}
    predicates: list[Predicate] = []
    for feature, values in sorted(feature_maps.items()):
        train_values = {
            mint: value
            for mint, value in values.items()
            if mint in train_mints and value not in (None, "")
        }
        if len(train_values) < MIN_TRADES:
            continue
        numeric_train = [(mint, _num(value)) for mint, value in train_values.items()]
        numeric_train = [(mint, value) for mint, value in numeric_train if value is not None]
        is_numeric = len(numeric_train) >= max(MIN_TRADES, int(len(train_values) * 0.8))
        buckets: dict[str, set[str]] = {}
        if is_numeric:
            edges = _bucket_edges([value for _mint, value in numeric_train])
            if not edges:
                continue
            for mint, raw_value in values.items():
                if mint not in all_mints:
                    continue
                value = _num(raw_value)
                if value is None:
                    continue
                buckets.setdefault(_numeric_bucket(value, edges), set()).add(mint)
        else:
            train_categories = {str(value) for value in train_values.values() if value not in (None, "")}
            for mint, raw_value in values.items():
                if mint not in all_mints or raw_value in (None, ""):
                    continue
                category = str(raw_value)
                if category in train_categories:
                    buckets.setdefault(category, set()).add(mint)
        for bucket, mints in buckets.items():
            train_n = len(mints & train_mints)
            if train_n >= MIN_TRAIN_TRADES and len(mints) >= MIN_TRADES:
                predicates.append(Predicate(feature=feature, bucket=bucket, mints=frozenset(mints)))
    return predicates


def metrics_for(rows: list[TradeRow], baseline_rows: list[TradeRow]) -> Metrics:
    n = len(rows)
    if n == 0:
        return Metrics(0, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None)
    returns = [row.ret_24h for row in rows]
    capped_returns = [_cap_return(row.ret_24h) for row in rows]
    wins = sum(row.reached_100 for row in rows)
    base_wins = sum(row.reached_100 for row in baseline_rows)
    outside_n = len(baseline_rows) - n
    selected = {row.mint for row in rows}
    outside_wins = sum(row.reached_100 for row in baseline_rows if row.mint not in selected)
    p_value = _two_proportion_p(wins, n, outside_wins, outside_n)
    ci_low, ci_high = _wilson_interval(wins, n)
    baseline_rate = base_wins / len(baseline_rows) if baseline_rows else 0.0
    win_rate = wins / n
    return Metrics(
        n=n,
        win_rate=win_rate,
        expectancy=_mean(returns),
        capped_expectancy=_mean(capped_returns),
        profit_factor=_profit_factor(returns),
        average_return=_mean(returns),
        max_drawdown=_max_drawdown(returns),
        reached_50_rate=sum(row.reached_50 for row in rows) / n,
        reached_100_rate=win_rate,
        reached_500_rate=sum(row.reached_500 for row in rows) / n,
        rug_rate=sum(row.rugged for row in rows) / n,
        sharpe=_sharpe(capped_returns),
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        lift_vs_baseline=win_rate / baseline_rate if baseline_rate else None,
    )


def _trade_index(rows: list[TradeRow]) -> dict[str, TradeRow]:
    return {row.mint: row for row in rows}


def _rows_for_mints(mints: set[str], rows: list[TradeRow]) -> list[TradeRow]:
    selected = [row for row in rows if row.mint in mints]
    selected.sort(key=lambda row: (row.seen_at, row.mint))
    return selected


def _single_score(predicate: Predicate, train_rows: list[TradeRow], baseline_train: list[TradeRow]) -> float:
    rows = _rows_for_mints(set(predicate.mints), train_rows)
    metric = metrics_for(rows, baseline_train)
    if metric.n < MIN_TRAIN_TRADES:
        return -999.0
    p_strength = 0 if metric.p_value is None else -math.log10(max(metric.p_value, 1e-12))
    pf = min(metric.profit_factor or 0.0, 5.0)
    lift = max(0.0, (metric.lift_vs_baseline or 0.0) - 1.0)
    exp_component = max(-2.0, min(5.0, (metric.capped_expectancy or 0.0) / 25.0))
    rug_penalty = (metric.rug_rate or 0.0) * 1.5
    return p_strength + pf * 0.5 + lift * 2.0 + exp_component + math.log10(metric.n) - rug_penalty


def _acceptance(train: Metrics, test: Metrics, all_metrics: Metrics) -> tuple[bool, str]:
    if all_metrics.n < MIN_TRADES:
        return False, "too_few_completed_trades"
    if train.n < MIN_TRAIN_TRADES:
        return False, "too_few_train_trades"
    if test.n < MIN_TEST_TRADES:
        return False, "too_few_holdout_trades"
    if (train.expectancy or -math.inf) <= 0:
        return False, "train_expectancy_not_positive"
    if (test.expectancy or -math.inf) <= 0:
        return False, "holdout_expectancy_not_positive"
    if (train.profit_factor or 0.0) <= 1.0:
        return False, "train_profit_factor_not_positive"
    if (test.profit_factor or 0.0) <= 1.0:
        return False, "holdout_profit_factor_not_positive"
    if (test.lift_vs_baseline or 0.0) < 1.0:
        return False, "holdout_no_win_lift"
    if (all_metrics.rug_rate or 1.0) > 0.35:
        return False, "rug_rate_too_high"
    if (all_metrics.reached_500_rate or 0.0) > 0 and all_metrics.n < 50 and (all_metrics.expectancy or 0.0) > 100:
        return False, "small_sample_outlier_risk"
    return True, "accepted"


def _robustness(train: Metrics, test: Metrics, all_metrics: Metrics) -> float:
    train_exp = train.capped_expectancy or -100.0
    test_exp = test.capped_expectancy or -100.0
    exp_score = min(train_exp, test_exp) / 20.0
    pf_score = min(train.profit_factor or 0.0, test.profit_factor or 0.0, 5.0)
    lift_score = min(train.lift_vs_baseline or 0.0, test.lift_vs_baseline or 0.0)
    sample_score = math.log10(max(all_metrics.n, 1))
    p_strength = 0 if all_metrics.p_value is None else -math.log10(max(all_metrics.p_value, 1e-12))
    rug_penalty = (all_metrics.rug_rate or 0.0) * 2.0
    drawdown_penalty = abs(all_metrics.max_drawdown or 0.0) / 100.0
    return exp_score + pf_score + lift_score + sample_score + p_strength - rug_penalty - drawdown_penalty


def evaluate_strategy(
    predicates: tuple[Predicate, ...],
    all_rows: list[TradeRow],
    train_rows: list[TradeRow],
    test_rows: list[TradeRow],
) -> StrategyResult | None:
    selected = set(predicates[0].mints)
    for predicate in predicates[1:]:
        selected.intersection_update(predicate.mints)
        if len(selected) < MIN_TRADES:
            return None
    all_selected = _rows_for_mints(selected, all_rows)
    if len(all_selected) < MIN_TRADES:
        return None
    train_selected = _rows_for_mints(selected, train_rows)
    test_selected = _rows_for_mints(selected, test_rows)
    all_metrics = metrics_for(all_selected, all_rows)
    train_metrics = metrics_for(train_selected, train_rows)
    test_metrics = metrics_for(test_selected, test_rows)
    accepted, reason = _acceptance(train_metrics, test_metrics, all_metrics)
    robust = _robustness(train_metrics, test_metrics, all_metrics)
    return StrategyResult(
        predicates=predicates,
        all_metrics=all_metrics,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        robustness=robust,
        accepted=accepted,
        reject_reason=reason,
    )


def search(conn: sqlite3.Connection) -> tuple[list[StrategyResult], dict[str, Any]]:
    all_rows = load_trades(conn)
    train_rows, test_rows = _split_trades(all_rows)
    feature_maps = _feature_maps(conn)
    predicates = build_predicates(feature_maps, train_rows, all_rows)
    ranked = sorted(
        predicates,
        key=lambda predicate: _single_score(predicate, train_rows, train_rows),
        reverse=True,
    )[:MAX_PREDICATES]
    results: list[StrategyResult] = []
    tested = 0
    rejected_sample = 0
    skipped_same_feature = 0
    for size in (2, 3, 4):
        size_tested = 0
        for combo in itertools.combinations(ranked, size):
            if size_tested >= MAX_COMBINATIONS_PER_SIZE:
                break
            if len({predicate.feature for predicate in combo}) != len(combo):
                skipped_same_feature += 1
                continue
            size_tested += 1
            tested += 1
            result = evaluate_strategy(combo, all_rows, train_rows, test_rows)
            if result is None:
                rejected_sample += 1
                continue
            results.append(result)
    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "labelled_trades": len(all_rows),
        "train_trades": len(train_rows),
        "test_trades": len(test_rows),
        "features_loaded": len(feature_maps),
        "candidate_predicates": len(predicates),
        "predicates_used": len(ranked),
        "tested_strategies": tested,
        "evaluated_strategies": len(results),
        "rejected_insufficient_sample": rejected_sample,
        "skipped_same_feature": skipped_same_feature,
        "baseline_all": metrics_for(all_rows, all_rows),
        "baseline_train": metrics_for(train_rows, train_rows),
        "baseline_test": metrics_for(test_rows, test_rows),
    }
    return results, meta


def _rank_key(result: StrategyResult) -> tuple[float, float, float, int]:
    m = result.all_metrics
    pf = min(m.profit_factor or 0.0, 10.0)
    return (
        result.robustness,
        m.capped_expectancy or -999.0,
        pf,
        m.n,
    )


def _worst_key(result: StrategyResult) -> tuple[float, float, float, int]:
    m = result.all_metrics
    return (
        m.expectancy or 0.0,
        -(m.rug_rate or 0.0),
        m.profit_factor or 0.0,
        -m.n,
    )


def _feature_counts(results: list[StrategyResult]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for result in results:
        for predicate in result.predicates:
            counts[predicate.feature] = counts.get(predicate.feature, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _metric_row(strategy: StrategyResult, split: str, metric: Metrics) -> str:
    ci = f"[{_pct(metric.ci_low)}, {_pct(metric.ci_high)}]"
    return (
        f"| {strategy.size} | {split} | {metric.n} | {_pct(metric.win_rate)} | "
        f"{_fmt(metric.expectancy)}% | {_fmt(metric.profit_factor)} | {_fmt(metric.average_return)}% | "
        f"{_fmt(metric.max_drawdown)}% | {_pct(metric.reached_50_rate)} | {_pct(metric.reached_100_rate)} | "
        f"{_pct(metric.reached_500_rate)} | {_pct(metric.rug_rate)} | {_fmt(metric.sharpe)} | "
        f"{ci} | {_fmt(metric.p_value, 4)} | {_fmt(metric.lift_vs_baseline)} | {strategy.robustness:.2f} | "
        f"{strategy.rule} |"
    )


def _strategy_table(results: list[StrategyResult], include_splits: bool = False) -> list[str]:
    lines = [
        "| size | split | n | win rate | expectancy | profit factor | average return | max drawdown | +50% | +100% | +500% | rug rate | Sharpe | 95% CI | p-value | lift | robustness | rule |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    if not results:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | none |")
        return lines
    for result in results:
        lines.append(_metric_row(result, "all", result.all_metrics))
        if include_splits:
            lines.append(_metric_row(result, "train", result.train_metrics))
            lines.append(_metric_row(result, "holdout", result.test_metrics))
    return lines


def _baseline_lines(meta: dict[str, Any]) -> list[str]:
    rows = []
    for name, metric in (
        ("all", meta["baseline_all"]),
        ("train", meta["baseline_train"]),
        ("holdout", meta["baseline_test"]),
    ):
        rows.append(
            f"| {name} | {metric.n} | {_pct(metric.win_rate)} | {_fmt(metric.expectancy)}% | "
            f"{_fmt(metric.profit_factor)} | {_fmt(metric.max_drawdown)}% | {_pct(metric.reached_50_rate)} | "
            f"{_pct(metric.reached_100_rate)} | {_pct(metric.reached_500_rate)} | {_pct(metric.rug_rate)} | "
            f"{_fmt(metric.sharpe)} |"
        )
    return [
        "| split | n | win rate | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate | Sharpe |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *rows,
    ]


def report_markdown(conn: sqlite3.Connection) -> str:
    results, meta = search(conn)
    accepted = [result for result in results if result.accepted]
    accepted.sort(key=_rank_key, reverse=True)
    robust = sorted(
        [result for result in results if result.all_metrics.n >= MIN_TRADES],
        key=_rank_key,
        reverse=True,
    )
    worst = sorted(results, key=_worst_key)[:20]
    winning_features = _feature_counts(accepted[:100])
    rejection_counts: dict[str, int] = {}
    for result in results:
        rejection_counts[result.reject_reason] = rejection_counts.get(result.reject_reason, 0) + 1

    lines = [
        "# Strategy Discovery Report",
        "",
        "Research-only offline strategy search. No scanner, filter, current score, execution, or trading logic was changed.",
        "",
        "## Search Summary",
        "",
        f"- Generated at: {meta['generated_at']}",
        f"- Labelled completed trades: {meta['labelled_trades']}",
        f"- Chronological train trades: {meta['train_trades']}",
        f"- Chronological holdout trades: {meta['test_trades']}",
        f"- Features loaded: {meta['features_loaded']}",
        f"- Candidate predicates: {meta['candidate_predicates']}",
        f"- Predicates used after train ranking: {meta['predicates_used']}",
        f"- Strategies tested: {meta['tested_strategies']}",
        f"- Strategies evaluated with n >= {MIN_TRADES}: {meta['evaluated_strategies']}",
        f"- Rejected for insufficient completed trades: {meta['rejected_insufficient_sample']}",
        f"- Skipped same-feature combinations: {meta['skipped_same_feature']}",
        "",
        "## Baseline",
        "",
        *_baseline_lines(meta),
        "",
        "## Rejection Summary",
        "",
    ]
    if rejection_counts:
        for reason, count in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- No evaluated strategies.")
    lines.extend(["", "## TOP 20 STRATEGIES", ""])
    if accepted:
        lines.extend(_strategy_table(accepted[:20], include_splits=True))
    else:
        lines.append("No strategy passed the profitability, holdout, sample-size, rug-risk, and overfit filters.")
        lines.append("")
        lines.append("Best exploratory strategies before final acceptance:")
        lines.extend(_strategy_table(robust[:20], include_splits=True))
    lines.extend(["", "## Worst 20 Strategies", ""])
    lines.extend(_strategy_table(worst, include_splits=False))
    lines.extend(["", "## Most Robust Strategies", ""])
    lines.extend(_strategy_table(robust[:20], include_splits=True))
    lines.extend(["", "## Features That Repeatedly Appear In Winning Strategies", ""])
    if winning_features:
        lines.extend(f"- {feature}: {count}" for feature, count in winning_features[:25])
    else:
        lines.append("- No accepted winning strategies; no repeated winning features validated.")
    lines.extend(
        [
            "",
            "## Method Notes",
            "",
            "- Buckets are derived from the chronological training split, then applied to holdout.",
            "- Strategy rules are simple AND combinations of 2, 3, or 4 feature buckets.",
            "- A strategy must have at least 30 completed trades overall, plus train and holdout coverage.",
            "- Acceptance requires positive expectancy and profit factor on both train and holdout.",
            "- Ranking uses capped expectancy for robustness so one extreme token cannot dominate the list.",
            "- The raw average return and expectancy columns are still reported for transparency.",
            "",
            "## Conclusion",
            "",
        ]
    )
    if accepted:
        lines.append("Validated offline candidates exist, but they remain research candidates only until tested on fresh data.")
    else:
        lines.append("No statistically robust profitable strategy passed validation yet.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit offline strategy discovery engine")
    parser.add_argument("--report", action="store_true", help="print and save strategy discovery report")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        report = report_markdown(conn)
    finally:
        conn.close()
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        handle.write(report)
    if args.report or not any(vars(args).values()):
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
