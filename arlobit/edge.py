"""Combination edge discovery for ArloBit research data.

This module searches offline feature combinations against completed labels.
It is reporting only: no scanner filters, live score, execution, or trading
logic are changed here.
"""

from __future__ import annotations

import argparse
import itertools
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from arlobit import db
from arlobit import velocity
from arlobit import wallets

LABEL_VERSION = 1
MIN_SAMPLE_N = 25
NUMERIC_BUCKETS = 4
MAX_SINGLE_PREDICATES = 56
MAX_COMBINATIONS_PER_SIZE = 250_000


@dataclass(frozen=True)
class LabelRow:
    mint: str
    reached_50: int
    reached_100: int
    reached_500: int
    rugged: int
    ret_24h: float | None
    max_runup_pct: float | None
    max_drawdown_pct: float | None


@dataclass(frozen=True)
class Predicate:
    feature: str
    bucket: str
    mints: frozenset[str]
    source: str

    @property
    def name(self) -> str:
        return f"{self.feature}={self.bucket}"


@dataclass(frozen=True)
class CombinationResult:
    predicates: tuple[Predicate, ...]
    n: int
    win_rate: float
    expectancy: float | None
    profit_factor: float | None
    average_return: float | None
    reached_50_rate: float
    reached_100_rate: float
    reached_500_rate: float
    rug_rate: float
    lift_vs_baseline: float | None
    rug_lift_vs_baseline: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    rug_p_value: float | None
    evidence_score: float
    rug_evidence_score: float

    @property
    def size(self) -> int:
        return len(self.predicates)

    @property
    def description(self) -> str:
        return " AND ".join(predicate.name for predicate in self.predicates)


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


def _pf(value: float | None) -> str:
    if value is None:
        return "-"
    if math.isinf(value):
        return "inf"
    return _fmt(value)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _profit_factor(returns: list[float]) -> float | None:
    gross_win = sum(value for value in returns if value > 0)
    gross_loss = -sum(value for value in returns if value < 0)
    if gross_loss > 0:
        return gross_win / gross_loss
    if gross_win > 0:
        return math.inf
    return None


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
    variance = p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)
    if variance <= 0:
        return None
    z = (success_a / n_a - success_b / n_b) / math.sqrt(variance)
    return math.erfc(abs(z) / math.sqrt(2))


def _bucket_edges(values: list[float], bucket_count: int = NUMERIC_BUCKETS) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < bucket_count:
        return []
    edges = [
        ordered[int((len(ordered) - 1) * index / bucket_count)]
        for index in range(1, bucket_count)
    ]
    return sorted(set(edges))


def _numeric_bucket(value: float, edges: list[float]) -> str:
    low = None
    for edge in edges:
        if value <= edge:
            return f"<= {_fmt(edge)}" if low is None else f"({_fmt(low)}, {_fmt(edge)}]"
        low = edge
    return f"> {_fmt(low)}"


def _label_rows(conn: sqlite3.Connection) -> dict[str, LabelRow]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT mint, reached_50, reached_100, reached_500, rugged,
               ret_24h, max_runup_pct, max_drawdown_pct
        FROM labels
        WHERE label_version = ? AND max_runup_pct IS NOT NULL
        """,
        (LABEL_VERSION,),
    ).fetchall()
    return {
        row["mint"]: LabelRow(
            mint=row["mint"],
            reached_50=int(row["reached_50"] == 1),
            reached_100=int(row["reached_100"] == 1),
            reached_500=int(row["reached_500"] == 1),
            rugged=int(row["rugged"] == 1),
            ret_24h=_num(row["ret_24h"]),
            max_runup_pct=_num(row["max_runup_pct"]),
            max_drawdown_pct=_num(row["max_drawdown_pct"]),
        )
        for row in rows
    }


def _base_features(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint,
               s.age_minutes, s.liquidity_usd, s.vol_m5, s.buys_m5, s.sells_m5,
               s.buy_sell_ratio_m5, s.vol_liq_ratio, s.top1_pct, s.top10_pct,
               s.top20_pct, s.holder_status, s.creator_quality, s.source,
               s.sellable, s.sell_impact_pct, s.arlobit_score
        FROM labels l
        JOIN candidate_sightings s ON s.sighting_id = l.base_sighting_id
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        """,
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = row["mint"]
        liquidity = _num(row["liquidity_usd"])
        age = _num(row["age_minutes"])
        features.setdefault("candidate.age_minutes", {})[mint] = age
        features.setdefault("candidate.liquidity_usd", {})[mint] = liquidity
        features.setdefault("candidate.vol_m5", {})[mint] = _num(row["vol_m5"])
        features.setdefault("candidate.buys_m5", {})[mint] = _num(row["buys_m5"])
        features.setdefault("candidate.sells_m5", {})[mint] = _num(row["sells_m5"])
        features.setdefault("candidate.buy_sell_ratio_m5", {})[mint] = _num(row["buy_sell_ratio_m5"])
        features.setdefault("candidate.vol_liq_ratio", {})[mint] = _num(row["vol_liq_ratio"])
        features.setdefault("candidate.top1_pct", {})[mint] = _num(row["top1_pct"])
        features.setdefault("candidate.top10_pct", {})[mint] = _num(row["top10_pct"])
        features.setdefault("candidate.top20_pct", {})[mint] = _num(row["top20_pct"])
        features.setdefault("candidate.sell_impact_pct", {})[mint] = _num(row["sell_impact_pct"])
        features.setdefault("candidate.arlobit_score", {})[mint] = _num(row["arlobit_score"])
        features.setdefault("candidate.creator_quality", {})[mint] = row["creator_quality"]
        features.setdefault("candidate.holder_status", {})[mint] = row["holder_status"]
        features.setdefault("candidate.source", {})[mint] = row["source"]
        features.setdefault("candidate.sellable", {})[mint] = row["sellable"]
        features.setdefault("bucket.liquidity", {})[mint] = _liquidity_bucket(liquidity)
        features.setdefault("bucket.token_age", {})[mint] = _age_bucket(age)
    return features


def _velocity_features(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    velocity.refresh(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT mint, liquidity_change_5m, liquidity_change_15m, liquidity_change_1h,
               volume_change_5m, volume_change_15m, volume_change_1h,
               buy_count_change, sell_count_change, buy_sell_ratio_change,
               price_change_velocity, volume_acceleration, liquidity_acceleration
        FROM token_velocity
        WHERE label_version = ? AND max_runup_pct IS NOT NULL
        """,
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = row["mint"]
        for feature in (
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
        ):
            features.setdefault(f"velocity.{feature}", {})[mint] = _num(row[feature])
    return features


def _wallet_features(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    wallets.refresh_wallet_intelligence(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint,
               COUNT(eb.buyer_wallet) AS early_buyer_count,
               SUM(CASE WHEN eb.is_repeat_buyer = 1 THEN 1 ELSE 0 END) AS repeat_buyer_count,
               AVG(ws.confidence_score) AS avg_wallet_confidence,
               MAX(ws.confidence_score) AS max_wallet_confidence,
               SUM(CASE WHEN ws.reputation = 'ELITE' THEN 1 ELSE 0 END) AS elite_wallets,
               SUM(CASE WHEN ws.reputation = 'SMART' THEN 1 ELSE 0 END) AS smart_wallets,
               SUM(CASE WHEN ws.reputation = 'GOOD' THEN 1 ELSE 0 END) AS good_wallets,
               SUM(CASE WHEN ws.reputation = 'RISKY' THEN 1 ELSE 0 END) AS risky_wallets,
               SUM(CASE WHEN ws.reputation = 'SCAM_CLUSTER' THEN 1 ELSE 0 END) AS scam_cluster_wallets,
               MAX(CASE ws.reputation
                   WHEN 'ELITE' THEN 5 WHEN 'SMART' THEN 4 WHEN 'GOOD' THEN 3
                   WHEN 'NEUTRAL' THEN 2 WHEN 'RISKY' THEN 1 WHEN 'SCAM_CLUSTER' THEN 0
                   ELSE NULL END) AS best_reputation_rank,
               MIN(CASE ws.reputation
                   WHEN 'ELITE' THEN 5 WHEN 'SMART' THEN 4 WHEN 'GOOD' THEN 3
                   WHEN 'NEUTRAL' THEN 2 WHEN 'RISKY' THEN 1 WHEN 'SCAM_CLUSTER' THEN 0
                   ELSE NULL END) AS worst_reputation_rank
        FROM labels l
        LEFT JOIN early_buyers eb ON eb.mint = l.mint
        LEFT JOIN wallet_stats ws ON ws.buyer_wallet = eb.buyer_wallet
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        GROUP BY l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = row["mint"]
        features.setdefault("wallet.early_buyer_count", {})[mint] = _num(row["early_buyer_count"])
        features.setdefault("wallet.repeat_buyer_count", {})[mint] = _num(row["repeat_buyer_count"])
        features.setdefault("wallet.avg_confidence", {})[mint] = _num(row["avg_wallet_confidence"])
        features.setdefault("wallet.max_confidence", {})[mint] = _num(row["max_wallet_confidence"])
        features.setdefault("wallet.elite_count", {})[mint] = _num(row["elite_wallets"])
        features.setdefault("wallet.smart_count", {})[mint] = _num(row["smart_wallets"])
        features.setdefault("wallet.good_count", {})[mint] = _num(row["good_wallets"])
        features.setdefault("wallet.risky_count", {})[mint] = _num(row["risky_wallets"])
        features.setdefault("wallet.scam_cluster_count", {})[mint] = _num(row["scam_cluster_wallets"])
        features.setdefault("wallet.best_reputation", {})[mint] = _reputation_rank_label(row["best_reputation_rank"])
        features.setdefault("wallet.worst_reputation", {})[mint] = _reputation_rank_label(row["worst_reputation_rank"])

    pair_rows = conn.execute(
        """
        SELECT l.mint,
               COUNT(wc.wallet_a) AS cooccurring_pairs,
               MAX(wc.times_seen_together) AS max_pair_repeats,
               AVG(wc.times_seen_together) AS avg_pair_repeats,
               SUM(CASE
                   WHEN COALESCE(a.reputation, 'NEUTRAL') IN ('RISKY', 'SCAM_CLUSTER')
                     OR COALESCE(b.reputation, 'NEUTRAL') IN ('RISKY', 'SCAM_CLUSTER')
                   THEN 1 ELSE 0 END) AS suspicious_pairs
        FROM labels l
        LEFT JOIN early_buyers eb1 ON eb1.mint = l.mint
        LEFT JOIN early_buyers eb2 ON eb2.mint = l.mint AND eb1.buyer_wallet < eb2.buyer_wallet
        LEFT JOIN wallet_cooccurrences wc
          ON wc.wallet_a = eb1.buyer_wallet AND wc.wallet_b = eb2.buyer_wallet
        LEFT JOIN wallet_stats a ON a.buyer_wallet = wc.wallet_a
        LEFT JOIN wallet_stats b ON b.buyer_wallet = wc.wallet_b
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        GROUP BY l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    for row in pair_rows:
        mint = row["mint"]
        features.setdefault("cluster.cooccurring_pairs", {})[mint] = _num(row["cooccurring_pairs"])
        features.setdefault("cluster.max_pair_repeats", {})[mint] = _num(row["max_pair_repeats"])
        features.setdefault("cluster.avg_pair_repeats", {})[mint] = _num(row["avg_pair_repeats"])
        features.setdefault("cluster.suspicious_pairs", {})[mint] = _num(row["suspicious_pairs"])
    return features


def _axiom_features(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.mint,
               COUNT(ax.signal_id) AS axiom_signal_count,
               COUNT(DISTINCT ax.wallet) AS axiom_wallet_count,
               COUNT(DISTINCT ax.signal_type) AS axiom_signal_type_count
        FROM labels l
        LEFT JOIN axiom_signals ax ON ax.mint = l.mint
        WHERE l.label_version = ? AND l.max_runup_pct IS NOT NULL
        GROUP BY l.mint
        """,
        (LABEL_VERSION,),
    ).fetchall()
    features: dict[str, dict[str, Any]] = {}
    for row in rows:
        mint = row["mint"]
        features.setdefault("axiom.signal_count", {})[mint] = _num(row["axiom_signal_count"])
        features.setdefault("axiom.wallet_count", {})[mint] = _num(row["axiom_wallet_count"])
        features.setdefault("axiom.signal_type_count", {})[mint] = _num(row["axiom_signal_type_count"])
    return features


def _liquidity_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 10_000:
        return "<10k"
    if value < 25_000:
        return "10k-25k"
    if value < 50_000:
        return "25k-50k"
    if value < 100_000:
        return "50k-100k"
    return ">=100k"


def _age_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 15:
        return "<15m"
    if value < 60:
        return "15m-1h"
    if value < 360:
        return "1h-6h"
    if value < 1440:
        return "6h-24h"
    return ">=24h"


def _reputation_rank_label(rank: Any) -> str | None:
    parsed = _num(rank)
    if parsed is None:
        return None
    mapping = {
        5: "ELITE",
        4: "SMART",
        3: "GOOD",
        2: "NEUTRAL",
        1: "RISKY",
        0: "SCAM_CLUSTER",
    }
    return mapping.get(int(parsed))


def collect_feature_maps(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    features: dict[str, dict[str, Any]] = {}
    for collector in (_base_features, _velocity_features, _wallet_features, _axiom_features):
        for name, values in collector(conn).items():
            features[name] = values
    return features


def build_predicates(
    feature_maps: dict[str, dict[str, Any]],
    labels: dict[str, LabelRow],
) -> list[Predicate]:
    labelled_mints = set(labels)
    predicates: list[Predicate] = []
    for feature, raw_values in sorted(feature_maps.items()):
        values = {mint: value for mint, value in raw_values.items() if mint in labelled_mints and value not in (None, "")}
        if len(values) < MIN_SAMPLE_N:
            continue
        numeric_pairs = [(mint, _num(value)) for mint, value in values.items()]
        numeric_pairs = [(mint, value) for mint, value in numeric_pairs if value is not None]
        is_numeric = len(numeric_pairs) >= max(MIN_SAMPLE_N, int(len(values) * 0.8))
        if is_numeric:
            edges = _bucket_edges([value for _mint, value in numeric_pairs])
            if not edges:
                continue
            buckets: dict[str, set[str]] = {}
            for mint, value in numeric_pairs:
                buckets.setdefault(_numeric_bucket(value, edges), set()).add(mint)
        else:
            buckets = {}
            for mint, value in values.items():
                buckets.setdefault(str(value), set()).add(mint)
        for bucket, mints in buckets.items():
            if len(mints) >= MIN_SAMPLE_N:
                predicates.append(Predicate(feature=feature, bucket=bucket, mints=frozenset(mints), source=feature.split(".", 1)[0]))
    return predicates


def _metrics(
    predicates: tuple[Predicate, ...],
    selected_mints: set[str],
    labels: dict[str, LabelRow],
    baseline: dict[str, float],
) -> CombinationResult | None:
    if len(selected_mints) < MIN_SAMPLE_N:
        return None
    rows = [labels[mint] for mint in selected_mints if mint in labels]
    n = len(rows)
    if n < MIN_SAMPLE_N:
        return None
    r50_count = sum(row.reached_50 for row in rows)
    r100_count = sum(row.reached_100 for row in rows)
    r500_count = sum(row.reached_500 for row in rows)
    rug_count = sum(row.rugged for row in rows)
    returns = [row.ret_24h for row in rows if row.ret_24h is not None]
    r50 = r50_count / n
    r100 = r100_count / n
    r500 = r500_count / n
    rug = rug_count / n
    outside_n = len(labels) - n
    outside_r100 = sum(row.reached_100 for mint, row in labels.items() if mint not in selected_mints)
    outside_rug = sum(row.rugged for mint, row in labels.items() if mint not in selected_mints)
    p_value = _two_proportion_p(r100_count, n, outside_r100, outside_n)
    rug_p_value = _two_proportion_p(rug_count, n, outside_rug, outside_n)
    ci_low, ci_high = _wilson_interval(r100_count, n)
    lift = r100 / baseline["r100"] if baseline["r100"] else None
    rug_lift = rug / baseline["rug"] if baseline["rug"] else None
    p_strength = 0 if p_value is None else -math.log10(max(p_value, 1e-12))
    rug_p_strength = 0 if rug_p_value is None else -math.log10(max(rug_p_value, 1e-12))
    positive_lift = max(0.0, (lift or 0.0) - 1.0)
    rug_penalty = max(0.0, (rug_lift or 1.0) - 1.0)
    evidence_score = p_strength * 2.0 + positive_lift * math.sqrt(n) - rug_penalty * 1.5
    rug_evidence_score = rug_p_strength * 2.0 + max(0.0, (rug_lift or 0.0) - 1.0) * math.sqrt(n)
    avg_return = _mean(returns)
    return CombinationResult(
        predicates=predicates,
        n=n,
        win_rate=r100,
        expectancy=avg_return,
        profit_factor=_profit_factor(returns),
        average_return=avg_return,
        reached_50_rate=r50,
        reached_100_rate=r100,
        reached_500_rate=r500,
        rug_rate=rug,
        lift_vs_baseline=lift,
        rug_lift_vs_baseline=rug_lift,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        rug_p_value=rug_p_value,
        evidence_score=evidence_score,
        rug_evidence_score=rug_evidence_score,
    )


def _baseline(labels: dict[str, LabelRow]) -> dict[str, float]:
    rows = list(labels.values())
    n = len(rows)
    returns = [row.ret_24h for row in rows if row.ret_24h is not None]
    return {
        "n": float(n),
        "r50": sum(row.reached_50 for row in rows) / n if n else 0.0,
        "r100": sum(row.reached_100 for row in rows) / n if n else 0.0,
        "r500": sum(row.reached_500 for row in rows) / n if n else 0.0,
        "rug": sum(row.rugged for row in rows) / n if n else 0.0,
        "avg_return": _mean(returns) or 0.0,
    }


def _single_score(predicate: Predicate, labels: dict[str, LabelRow], baseline: dict[str, float]) -> float:
    result = _metrics((predicate,), set(predicate.mints), labels, baseline)
    if result is None:
        return -999.0
    p_strength = 0 if result.p_value is None else -math.log10(max(result.p_value, 1e-12))
    rug_strength = 0 if result.rug_p_value is None else -math.log10(max(result.rug_p_value, 1e-12))
    positive = max(0.0, (result.lift_vs_baseline or 0.0) - 1.0)
    negative = max(0.0, (result.rug_lift_vs_baseline or 0.0) - 1.0)
    return max(p_strength + positive * 2.0, rug_strength + negative * 2.0) + math.log10(result.n)


def search_combinations(
    predicates: list[Predicate],
    labels: dict[str, LabelRow],
) -> tuple[list[CombinationResult], dict[str, Any]]:
    baseline = _baseline(labels)
    ranked_predicates = sorted(
        predicates,
        key=lambda predicate: _single_score(predicate, labels, baseline),
        reverse=True,
    )[:MAX_SINGLE_PREDICATES]
    results: list[CombinationResult] = []
    tested = 0
    rejected = 0
    skipped_same_feature = 0
    for size in (2, 3, 4):
        size_tested = 0
        for combo in itertools.combinations(ranked_predicates, size):
            if size_tested >= MAX_COMBINATIONS_PER_SIZE:
                break
            feature_names = {predicate.feature for predicate in combo}
            if len(feature_names) != len(combo):
                skipped_same_feature += 1
                continue
            size_tested += 1
            tested += 1
            selected = set(combo[0].mints)
            for predicate in combo[1:]:
                selected.intersection_update(predicate.mints)
                if len(selected) < MIN_SAMPLE_N:
                    break
            result = _metrics(combo, selected, labels, baseline)
            if result is None:
                rejected += 1
                continue
            results.append(result)
    meta = {
        "candidate_predicates": len(predicates),
        "ranked_predicates_used": len(ranked_predicates),
        "tested_combinations": tested,
        "rejected_insufficient_sample": rejected,
        "skipped_same_feature": skipped_same_feature,
        "baseline": baseline,
    }
    return results, meta


def discover(conn: sqlite3.Connection) -> tuple[list[CombinationResult], dict[str, Any]]:
    labels = _label_rows(conn)
    feature_maps = collect_feature_maps(conn)
    predicates = build_predicates(feature_maps, labels)
    results, meta = search_combinations(predicates, labels)
    meta["features_loaded"] = len(feature_maps)
    meta["labelled_samples"] = len(labels)
    meta["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return results, meta


def _result_key(result: CombinationResult) -> tuple[float, float, float, int]:
    p_strength = 0 if result.p_value is None else -math.log10(max(result.p_value, 1e-12))
    return (
        result.evidence_score,
        p_strength,
        result.lift_vs_baseline or 0.0,
        result.n,
    )


def _rug_key(result: CombinationResult) -> tuple[float, float, float, int]:
    p_strength = 0 if result.rug_p_value is None else -math.log10(max(result.rug_p_value, 1e-12))
    return (
        result.rug_evidence_score,
        p_strength,
        result.rug_lift_vs_baseline or 0.0,
        result.n,
    )


def _header() -> str:
    return (
        "sz   n  win_rate expectancy profit_factor avg_return    +50    +100    +500"
        " rug_rate lift100 rug_lift          95% CI        p    rug_p  combination"
    )


def _line(result: CombinationResult) -> str:
    ci = f"[{_pct(result.ci_low)}, {_pct(result.ci_high)}]"
    return (
        f"{result.size:>2} {result.n:>4} {_pct(result.win_rate):>9}"
        f" {_fmt(result.expectancy):>10} {_pf(result.profit_factor):>13}"
        f" {_fmt(result.average_return):>10} {_pct(result.reached_50_rate):>7}"
        f" {_pct(result.reached_100_rate):>7} {_pct(result.reached_500_rate):>7}"
        f" {_pct(result.rug_rate):>8} {_fmt(result.lift_vs_baseline):>7}"
        f" {_fmt(result.rug_lift_vs_baseline):>8} {ci:>19}"
        f" {_fmt(result.p_value, 4):>8} {_fmt(result.rug_p_value, 4):>8}  {result.description}"
    )


def report_lines(conn: sqlite3.Connection) -> list[str]:
    results, meta = discover(conn)
    baseline = meta["baseline"]
    significant = [
        result
        for result in results
        if result.p_value is not None
        and result.p_value <= 0.05
        and (result.lift_vs_baseline or 0) > 1.0
        and (result.rug_lift_vs_baseline or 1.0) <= 1.25
    ]
    rug_predictors = [
        result
        for result in results
        if result.rug_p_value is not None
        and result.rug_p_value <= 0.05
        and (result.rug_lift_vs_baseline or 0) > 1.0
    ]
    significant.sort(key=_result_key, reverse=True)
    rug_predictors.sort(key=_rug_key, reverse=True)
    candidates: list[str] = []
    for result in significant:
        for predicate in result.predicates:
            if predicate.feature not in candidates and len(candidates) < 12:
                candidates.append(predicate.feature)

    lines = [
        "=== ARLOBIT EDGE COMBINATION REPORT ===",
        f"generated_at: {meta['generated_at']}",
        f"labelled samples: {meta['labelled_samples']}",
        f"features loaded: {meta['features_loaded']}",
        f"candidate bucket predicates: {meta['candidate_predicates']}",
        f"predicates used for combinations: {meta['ranked_predicates_used']}",
        f"tested combinations: {meta['tested_combinations']}",
        f"rejected insufficient sample: {meta['rejected_insufficient_sample']}",
        f"skipped same-feature combinations: {meta['skipped_same_feature']}",
        f"minimum accepted sample size: {MIN_SAMPLE_N}",
        "",
        "Baseline:",
        f"+50%: {_pct(baseline['r50'])}",
        f"+100%: {_pct(baseline['r100'])}",
        f"+500%: {_pct(baseline['r500'])}",
        f"rug rate: {_pct(baseline['rug'])}",
        f"average return: {_fmt(baseline['avg_return'])}",
        "",
        "Top statistically significant combinations:",
        _header(),
    ]
    if significant:
        lines.extend(_line(result) for result in significant[:25])
    else:
        lines.append("(none passed p<=0.05, positive lift, rug_lift<=1.25, and sample-size filters)")
    lines.extend(["", "Top rug predictors:", _header()])
    if rug_predictors:
        lines.extend(_line(result) for result in rug_predictors[:25])
    else:
        lines.append("(none passed p<=0.05 and sample-size filters)")
    lines.extend(["", "Recommended ArloBit v3 scoring candidates:"])
    if candidates:
        lines.extend(f"- {feature}" for feature in candidates)
    else:
        lines.append("- No statistically significant combination edge validated yet.")
    lines.extend(
        [
            "",
            "Notes:",
            "- Combination search is offline research only and is not connected to trading.",
            "- Ranking prioritizes p-value, sample size, pump lift, and rug penalty rather than raw return alone.",
            "- Results are still exploratory until validated on fresh holdout data.",
            "=== END REPORT ===",
        ]
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit offline combination edge discovery")
    parser.add_argument("--report", action="store_true", help="print combination edge report")
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
