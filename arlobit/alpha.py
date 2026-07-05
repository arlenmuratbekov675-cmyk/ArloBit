"""Alpha discovery research report.

Searches independent feature buckets across the ArloBit research tables and
ranks predictors against completed labels. This module is reporting only; it
does not change scanner logic, filters, scoring, execution, or trading.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Any

from arlobit import db
from arlobit import velocity

LABEL_VERSION = 1
MIN_BUCKET_N = 20
MIN_FEATURE_N = 30


@dataclass(frozen=True)
class FeatureValue:
    mint: str
    value: Any
    reached_50: int
    reached_100: int
    reached_500: int
    rugged: int
    ret_24h: float | None
    max_runup_pct: float | None
    max_drawdown_pct: float | None


@dataclass(frozen=True)
class BucketResult:
    feature: str
    bucket: str
    kind: str
    n: int
    win_rate: float
    avg_return: float | None
    expectancy: float | None
    rug_rate: float
    reached_50_rate: float
    reached_100_rate: float
    reached_500_rate: float
    lift_50: float | None
    lift_100: float | None
    lift_500: float | None
    rug_lift: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    score: float


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
    return "-" if number is None else f"{number:.{decimals}f}"


def _pct(value: Any) -> str:
    number = _num(value)
    return "-" if number is None else f"{number * 100:.1f}%"


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
    p_pool = (success_a + success_b) / (n_a + n_b)
    denom = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if denom == 0:
        return None
    z = (success_a / n_a - success_b / n_b) / denom
    return math.erfc(abs(z) / math.sqrt(2))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _bucket_edges(values: list[float], bucket_count: int = 5) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < bucket_count:
        return []
    return sorted(
        set(ordered[int((len(ordered) - 1) * index / bucket_count)] for index in range(1, bucket_count))
    )


def _numeric_bucket(value: float, edges: list[float]) -> str:
    low = None
    for edge in edges:
        if value <= edge:
            return f"<= {_fmt(edge)}" if low is None else f"({_fmt(low)}, {_fmt(edge)}]"
        low = edge
    return f"> {_fmt(low)}"


def _baseline(rows: list[FeatureValue]) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "r50": 0, "r100": 0, "r500": 0, "rug": 0}
    return {
        "n": n,
        "r50": sum(row.reached_50 for row in rows) / n,
        "r100": sum(row.reached_100 for row in rows) / n,
        "r500": sum(row.reached_500 for row in rows) / n,
        "rug": sum(row.rugged for row in rows) / n,
    }


def _result(feature: str, bucket: str, kind: str, rows: list[FeatureValue], all_rows: list[FeatureValue]) -> BucketResult | None:
    n = len(rows)
    if n < MIN_BUCKET_N:
        return None
    base = _baseline(all_rows)
    r50_count = sum(row.reached_50 for row in rows)
    r100_count = sum(row.reached_100 for row in rows)
    r500_count = sum(row.reached_500 for row in rows)
    rug_count = sum(row.rugged for row in rows)
    r50 = r50_count / n
    r100 = r100_count / n
    r500 = r500_count / n
    rug = rug_count / n
    returns = [_num(row.ret_24h) for row in rows]
    avg_return = _mean([value for value in returns if value is not None])
    ci_low, ci_high = _wilson_interval(r100_count, n)
    p_value = _two_proportion_p(r100_count, n, sum(row.reached_100 for row in all_rows) - r100_count, len(all_rows) - n)
    lift_100 = r100 / base["r100"] if base["r100"] else None
    rug_lift = rug / base["rug"] if base["rug"] else None
    positive_score = ((lift_100 or 0) - 1.0) * math.sqrt(n) - ((rug_lift or 1.0) - 1.0) * 0.75
    if p_value is not None:
        positive_score += min(4.0, -math.log10(max(p_value, 1e-12))) * 0.4
    return BucketResult(
        feature=feature,
        bucket=bucket,
        kind=kind,
        n=n,
        win_rate=r100,
        avg_return=avg_return,
        expectancy=avg_return,
        rug_rate=rug,
        reached_50_rate=r50,
        reached_100_rate=r100,
        reached_500_rate=r500,
        lift_50=r50 / base["r50"] if base["r50"] else None,
        lift_100=lift_100,
        lift_500=r500 / base["r500"] if base["r500"] else None,
        rug_lift=rug_lift,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        score=positive_score,
    )


def labelled_base_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint, l.reached_50, l.reached_100, l.reached_500, l.rugged,
               l.ret_24h, l.max_runup_pct, l.max_drawdown_pct,
               s.source, s.price_usd, s.liquidity_usd, s.fdv, s.market_cap,
               s.age_minutes, s.vol_m5, s.vol_h1, s.vol_h6, s.vol_h24,
               s.buys_m5, s.sells_m5, s.buy_sell_ratio_m5, s.swap_accel,
               s.vol_accel, s.vol_liq_ratio, s.pc_m5, s.pc_h1, s.pc_h6, s.pc_h24,
               s.sellable, s.sell_impact_pct, s.top1_pct, s.top10_pct, s.top20_pct,
               s.holder_status, s.creator_quality, s.arlobit_score, s.verdict
        FROM labels l
        JOIN candidate_sightings s ON s.sighting_id = l.base_sighting_id
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        """,
        (LABEL_VERSION,),
    ).fetchall()
    return [dict(row) for row in rows]


def base_feature_values(conn: sqlite3.Connection) -> dict[str, list[FeatureValue]]:
    features: dict[str, list[FeatureValue]] = {}
    rows = labelled_base_rows(conn)
    numeric = [
        "price_usd", "liquidity_usd", "fdv", "market_cap", "age_minutes",
        "vol_m5", "vol_h1", "vol_h6", "vol_h24", "buys_m5", "sells_m5",
        "buy_sell_ratio_m5", "swap_accel", "vol_accel", "vol_liq_ratio",
        "pc_m5", "pc_h1", "pc_h6", "pc_h24", "sell_impact_pct",
        "top1_pct", "top10_pct", "top20_pct", "arlobit_score",
    ]
    categorical = ["source", "sellable", "holder_status", "creator_quality", "verdict"]
    for feature in numeric + categorical:
        values = []
        for row in rows:
            value = row.get(feature)
            if value is None or value == "":
                continue
            values.append(_fv(row, value))
        if values:
            features[f"candidate.{feature}"] = values
    return features


def velocity_feature_values(conn: sqlite3.Connection) -> dict[str, list[FeatureValue]]:
    from arlobit.velocity import FEATURES as VELOCITY_FEATURES
    from arlobit.velocity import refresh as refresh_velocity

    refresh_velocity(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM token_velocity WHERE label_version=? AND max_runup_pct IS NOT NULL",
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, list[FeatureValue]] = {}
    for feature in VELOCITY_FEATURES:
        values = []
        for row in rows:
            value = row[feature]
            if value is not None:
                values.append(_fv(row, value))
        if values:
            features[f"velocity.{feature}"] = values
    signal_rows = conn.execute(
        """
        SELECT tv.*, vs.bucket_label
        FROM token_velocity tv
        JOIN velocity_signals vs
          ON tv.label_version = ?
         AND vs.feature_name = 'volume_acceleration'
         AND tv.volume_acceleration IS NOT NULL
         AND (
              (vs.bucket_min IS NULL AND tv.volume_acceleration <= vs.bucket_max)
           OR (vs.bucket_max IS NULL AND tv.volume_acceleration > vs.bucket_min)
           OR (vs.bucket_min IS NOT NULL AND vs.bucket_max IS NOT NULL
               AND tv.volume_acceleration > vs.bucket_min AND tv.volume_acceleration <= vs.bucket_max)
         )
        WHERE tv.max_runup_pct IS NOT NULL
        """,
        (LABEL_VERSION,),
    ).fetchall()
    if signal_rows:
        features["velocity.bucket.volume_acceleration"] = [_fv(row, row["bucket_label"]) for row in signal_rows]
    return features


def wallet_feature_values(conn: sqlite3.Connection) -> dict[str, list[FeatureValue]]:
    from arlobit.wallets import refresh_wallet_intelligence

    refresh_wallet_intelligence(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint, l.reached_50, l.reached_100, l.reached_500, l.rugged,
               l.ret_24h, l.max_runup_pct, l.max_drawdown_pct,
               COUNT(eb.buyer_wallet) AS early_buyer_count,
               SUM(CASE WHEN eb.is_repeat_buyer = 1 THEN 1 ELSE 0 END) AS repeat_buyer_count,
               AVG(ws.confidence_score) AS avg_wallet_confidence,
               MAX(ws.confidence_score) AS max_wallet_confidence,
               SUM(CASE WHEN ws.reputation = 'ELITE' THEN 1 ELSE 0 END) AS elite_wallets,
               SUM(CASE WHEN ws.reputation = 'SMART' THEN 1 ELSE 0 END) AS smart_wallets,
               SUM(CASE WHEN ws.reputation = 'SCAM_CLUSTER' THEN 1 ELSE 0 END) AS scam_cluster_wallets
        FROM labels l
        LEFT JOIN early_buyers eb ON eb.mint = l.mint
        LEFT JOIN wallet_stats ws ON ws.buyer_wallet = eb.buyer_wallet
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        GROUP BY l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, list[FeatureValue]] = {}
    numeric = [
        "early_buyer_count", "repeat_buyer_count", "avg_wallet_confidence",
        "max_wallet_confidence", "elite_wallets", "smart_wallets", "scam_cluster_wallets",
    ]
    for feature in numeric:
        values = []
        for row in rows:
            value = row[feature]
            if value is not None:
                values.append(_fv(row, value))
        if values:
            features[f"wallet.{feature}"] = values
    co_rows = conn.execute(
        """
        SELECT l.mint, l.reached_50, l.reached_100, l.reached_500, l.rugged,
               l.ret_24h, l.max_runup_pct, l.max_drawdown_pct,
               COUNT(wc.wallet_a) AS cooccurring_pairs,
               MAX(wc.times_seen_together) AS max_pair_repeats,
               AVG(wc.times_seen_together) AS avg_pair_repeats
        FROM labels l
        LEFT JOIN early_buyers a ON a.mint = l.mint
        LEFT JOIN early_buyers b ON b.mint = l.mint AND a.buyer_wallet < b.buyer_wallet
        LEFT JOIN wallet_cooccurrences wc ON wc.wallet_a = a.buyer_wallet AND wc.wallet_b = b.buyer_wallet
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        GROUP BY l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    for feature in ("cooccurring_pairs", "max_pair_repeats", "avg_pair_repeats"):
        values = [_fv(row, row[feature]) for row in co_rows if row[feature] is not None]
        if values:
            features[f"cluster.{feature}"] = values
    return features


def _fv(row: Any, value: Any) -> FeatureValue:
    return FeatureValue(
        mint=row["mint"],
        value=value,
        reached_50=int(row["reached_50"] == 1),
        reached_100=int(row["reached_100"] == 1),
        reached_500=int(row["reached_500"] == 1),
        rugged=int(row["rugged"] == 1),
        ret_24h=_num(row["ret_24h"]),
        max_runup_pct=_num(row["max_runup_pct"]),
        max_drawdown_pct=_num(row["max_drawdown_pct"]),
    )


def evaluate_feature(name: str, values: list[FeatureValue]) -> list[BucketResult]:
    if len(values) < MIN_FEATURE_N:
        return []
    numeric_values = [_num(row.value) for row in values]
    is_numeric = sum(value is not None for value in numeric_values) >= max(MIN_FEATURE_N, int(len(values) * 0.8))
    buckets: dict[str, list[FeatureValue]] = {}
    kind = "numeric" if is_numeric else "categorical"
    if is_numeric:
        clean = [(row, _num(row.value)) for row in values]
        clean = [(row, value) for row, value in clean if value is not None]
        edges = _bucket_edges([value for _row, value in clean])
        if not edges:
            return []
        for row, value in clean:
            buckets.setdefault(_numeric_bucket(value, edges), []).append(row)
    else:
        for row in values:
            bucket = str(row.value)
            buckets.setdefault(bucket, []).append(row)
    results = []
    all_rows = values
    for bucket, rows in buckets.items():
        result = _result(name, bucket, kind, rows, all_rows)
        if result is not None:
            results.append(result)
    return results


def collect_all_features(conn: sqlite3.Connection) -> dict[str, list[FeatureValue]]:
    features: dict[str, list[FeatureValue]] = {}
    for source in (base_feature_values, velocity_feature_values, wallet_feature_values):
        features.update(source(conn))
    return features


def discover(conn: sqlite3.Connection) -> tuple[list[BucketResult], list[str]]:
    features = collect_all_features(conn)
    all_results: list[BucketResult] = []
    no_value: list[str] = []
    for name, values in sorted(features.items()):
        results = evaluate_feature(name, values)
        if not results:
            no_value.append(name)
            continue
        all_results.extend(results)
        best_abs = max(abs((result.lift_100 or 1.0) - 1.0) for result in results)
        if best_abs < 0.15:
            no_value.append(name)
    return all_results, no_value


def _positive_key(result: BucketResult) -> tuple[float, float, int]:
    p_bonus = 0 if result.p_value is None else min(6.0, -math.log10(max(result.p_value, 1e-12)))
    return (result.score + p_bonus, result.lift_100 or 0, result.n)


def _negative_key(result: BucketResult) -> tuple[float, float, int]:
    p_bonus = 0 if result.p_value is None else min(6.0, -math.log10(max(result.p_value, 1e-12)))
    rug = result.rug_lift or 0
    inverse_pump = 1.0 / result.lift_100 if result.lift_100 else 3.0
    return (rug + inverse_pump + p_bonus * 0.25, rug, result.n)


def _line(result: BucketResult) -> str:
    ci = f"[{_pct(result.ci_low)}, {_pct(result.ci_high)}]"
    return (
        f"{result.feature:<34} {result.bucket:<20} {result.n:>4}"
        f" {_pct(result.win_rate):>8} {_fmt(result.avg_return):>9} {_fmt(result.expectancy):>10}"
        f" {_pct(result.rug_rate):>8} {_pct(result.reached_50_rate):>7}"
        f" {_pct(result.reached_100_rate):>8} {_pct(result.reached_500_rate):>8}"
        f" {_fmt(result.lift_100):>7} {ci:>19} {_fmt(result.p_value, 4):>8}"
    )


def report_lines(conn: sqlite3.Connection) -> list[str]:
    results, no_value = discover(conn)
    base_rows = [_fv(row, 1) for row in labelled_base_rows(conn)]
    base = _baseline(base_rows)
    positives = [
        result for result in results
        if result.n >= MIN_BUCKET_N and (result.lift_100 or 0) > 1.15 and result.rug_lift is not None
    ]
    negatives = [
        result for result in results
        if result.n >= MIN_BUCKET_N and ((result.rug_lift or 0) > 1.25 or (result.lift_100 or 1) < 0.75)
    ]
    positives.sort(key=_positive_key, reverse=True)
    negatives.sort(key=_negative_key, reverse=True)
    candidate_features = []
    for result in positives:
        if result.feature not in candidate_features and len(candidate_features) < 10:
            candidate_features.append(result.feature)

    lines = [
        "=== ALPHA DISCOVERY REPORT ===",
        f"labelled samples: {base['n']}",
        f"baseline +50%: {_pct(base['r50'])}",
        f"baseline +100%: {_pct(base['r100'])}",
        f"baseline +500%: {_pct(base['r500'])}",
        f"baseline rug rate: {_pct(base['rug'])}",
        "",
        "Top Positive Predictors:",
        _header(),
    ]
    lines.extend(_line(result) for result in positives[:20])
    if not positives:
        lines.append("(none)")
    lines.extend(["", "Top Negative Predictors:", _header()])
    lines.extend(_line(result) for result in negatives[:20])
    if not negatives:
        lines.append("(none)")
    lines.extend(["", "Features with no predictive value:"])
    lines.extend(f"- {name}" for name in sorted(set(no_value))[:60])
    if not no_value:
        lines.append("(none)")
    lines.extend(["", "Recommended candidates for ArloBit v2 scoring"])
    if candidate_features:
        lines.extend(f"- {feature}" for feature in candidate_features)
    else:
        lines.append("- No candidate features validated yet.")
    lines.append("=== END REPORT ===")
    return lines


def _header() -> str:
    return (
        "feature                            bucket                  n win_rate avg_ret expectancy rug_rate"
        "    +50    +100    +500 lift100              95% CI        p"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit alpha discovery research report")
    parser.add_argument("--report", action="store_true", help="print alpha discovery report")
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
