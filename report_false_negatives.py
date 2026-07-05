"""False negative research report (read-only).

False negative = mint that was rejected (never entered paper trading) but whose
label shows max_runup_pct >= 50. True negative = rejected mint with
max_runup_pct < 50. Prints medians per rejection reason and FN-vs-TN
separability (Mann-Whitney U, normal approximation with tie correction).

Usage: python report_false_negatives.py
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from statistics import median

from arlobit import db

FN_THRESHOLD = 50.0
P_SEPARABLE = 0.05


def mann_whitney_p(a: list[float], b: list[float]) -> float | None:
    """Two-sided Mann-Whitney U p-value via normal approximation."""
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return None
    combined = sorted((value, 0) for value in a) + sorted((value, 1) for value in b)
    combined.sort(key=lambda pair: pair[0])
    ranks: dict[int, float] = {}
    tie_term = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        rank = (i + j + 1) / 2  # average rank, 1-based
        for k in range(i, j):
            ranks[k] = rank
        t = j - i
        if t > 1:
            tie_term += t**3 - t
        i = j
    r1 = sum(ranks[k] for k, (_, group) in enumerate(combined) if group == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    mean_u = n1 * n2 / 2
    var_u = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1)))
    if var_u <= 0:
        return None
    z = (u1 - mean_u) / math.sqrt(var_u)
    return math.erfc(abs(z) / math.sqrt(2))


def fmt(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:,.{decimals}f}"


def main() -> None:
    conn = sqlite3.connect(db.db_path())
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT l.mint, l.max_runup_pct, l.ret_24h, l.rugged,
               s.blocked_reasons, s.price_usd, s.liquidity_usd, s.vol_m5,
               s.age_minutes, s.arlobit_score, s.top10_pct, s.creator_quality,
               s.enriched
        FROM labels l
        JOIN candidate_sightings s
          ON s.sighting_id = (SELECT MIN(sighting_id) FROM candidate_sightings
                              WHERE mint = l.mint)
        WHERE l.label_version = 1
          AND l.mint NOT IN (SELECT mint FROM db_paper_trades)
          AND l.mint NOT IN (SELECT mint FROM candidate_sightings WHERE entered_paper = 1)
        """
    ).fetchall()
    conn.close()

    labeled = [dict(row) for row in rows]
    for record in labeled:
        try:
            record["reasons"] = json.loads(record["blocked_reasons"] or "[]")
        except ValueError:
            record["reasons"] = ["<unparseable>"]
        liq, vol = record["liquidity_usd"], record["vol_m5"]
        record["vol_liq"] = vol / liq if vol is not None and liq and liq > 0 else None

    with_runup = [r for r in labeled if r["max_runup_pct"] is not None]
    fn = [r for r in with_runup if r["max_runup_pct"] >= FN_THRESHOLD]
    tn = [r for r in with_runup if r["max_runup_pct"] < FN_THRESHOLD]

    print("=== FALSE NEGATIVE REPORT ===")
    print(f"Total labeled rejects: {len(labeled)}"
          f" ({len(with_runup)} with max_runup_pct, {len(labeled) - len(with_runup)} without)")
    for threshold in (50, 100, 500):
        count = sum(1 for r in with_runup if r["max_runup_pct"] >= threshold)
        print(f"False negatives (+{threshold}%): {count}")

    # -- per-mint detail --------------------------------------------------
    print("\nFalse negative detail (sorted by max_runup_pct desc):")
    header = (f"{'mint':<12} {'runup%':>8} {'ret24h%':>8} {'rug':>3} {'enr':>3}"
              f" {'price':>12} {'liq':>10} {'vol_m5':>10} {'age_min':>8}"
              f" {'score':>5} {'top10%':>6} {'creator':>8}  blocked_reasons")
    print(header)
    for r in sorted(fn, key=lambda r: -r["max_runup_pct"]):
        print(f"{r['mint'][:12]:<12} {fmt(r['max_runup_pct']):>8} {fmt(r['ret_24h']):>8}"
              f" {r['rugged'] if r['rugged'] is not None else '-':>3}"
              f" {r['enriched']:>3}"
              f" {(f'{r_price:.6g}' if (r_price := r['price_usd']) is not None else '-'):>12}"
              f" {fmt(r['liquidity_usd']):>10} {fmt(r['vol_m5']):>10}"
              f" {fmt(r['age_minutes'], 1):>8} {fmt(r['arlobit_score'], 1):>5}"
              f" {fmt(r['top10_pct'], 1):>6}"
              f" {(r['creator_quality'] or '-'):>8}"
              f"  {','.join(r['reasons'])}")

    # -- by rejection reason ----------------------------------------------
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for r in fn:
        for reason in r["reasons"]:
            by_reason[reason].append(r)

    print("\nBy rejection reason (sorted by false negative count):")
    print(f"{'reason':<28} {'FN':>4} {'med runup%':>10} {'med liq':>10}"
          f" {'med age':>8} {'med vol_m5':>10} {'% rugged':>8}")
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        runups = [g["max_runup_pct"] for g in group]
        liqs = [g["liquidity_usd"] for g in group if g["liquidity_usd"] is not None]
        ages = [g["age_minutes"] for g in group if g["age_minutes"] is not None]
        vols = [g["vol_m5"] for g in group if g["vol_m5"] is not None]
        rug_known = [g for g in group if g["rugged"] is not None]
        rug_pct = (100 * sum(1 for g in rug_known if g["rugged"] == 1) / len(rug_known)
                   if rug_known else None)
        print(f"{reason:<28} {len(group):>4} {fmt(median(runups)):>10}"
              f" {fmt(median(liqs)) if liqs else '-':>10}"
              f" {fmt(median(ages), 1) if ages else '-':>8}"
              f" {fmt(median(vols)) if vols else '-':>10}"
              f" {fmt(rug_pct, 1) if rug_pct is not None else '-':>8}")

    # -- FN vs TN comparison ----------------------------------------------
    metrics = (
        ("liquidity_usd", "liquidity_usd"),
        ("vol_m5", "vol_m5"),
        ("age_minutes", "age_minutes"),
        ("vol_m5/liq", "vol_liq"),
        ("arlobit_score", "arlobit_score"),
    )
    print("\nTop characteristics of false negatives vs true negatives:")
    print(f"{'metric':<16} {'FN median':>12} {'TN median':>12} {'FN n':>5} {'TN n':>5}"
          f" {'p-value':>9}  separable?")
    for label, key in metrics:
        fn_values = [r[key] for r in fn if r[key] is not None]
        tn_values = [r[key] for r in tn if r[key] is not None]
        p = mann_whitney_p(fn_values, tn_values)
        decimals = 3 if key in ("vol_liq",) else (1 if key in ("age_minutes", "arlobit_score") else 0)
        separable = "-" if p is None else ("YES" if p < P_SEPARABLE else "no")
        print(f"{label:<16} {fmt(median(fn_values), decimals) if fn_values else '-':>12}"
              f" {fmt(median(tn_values), decimals) if tn_values else '-':>12}"
              f" {len(fn_values):>5} {len(tn_values):>5}"
              f" {(f'{p:.4f}' if p is not None else '-'):>9}  {separable}")

    # -- most damaging filter ----------------------------------------------
    print("\nMost common rejection reason among big winners:")
    for threshold in (100, 500):
        counts: dict[str, int] = defaultdict(int)
        winners = [r for r in fn if r["max_runup_pct"] >= threshold]
        for r in winners:
            for reason in r["reasons"]:
                counts[reason] += 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        rendered = ", ".join(f"{reason} ({count})" for reason, count in top) if top else "none"
        print(f"  +{threshold}% ({len(winners)} mints): {rendered}")

    if by_reason:
        worst, group = max(by_reason.items(), key=lambda kv: len(kv[1]))
        print(f"\nMost damaging single filter (causes most missed winners):"
              f" {worst} ({len(group)} of {len(fn)} false negatives)")
    print("\n=== END REPORT ===")


if __name__ == "__main__":
    main()
