"""Research collector: records every candidate sighting from scanner_v0.py.

Strangler-pattern hooks — the scanner calls these module-level functions and
nothing else. Hard rules enforced here:
- No public function ever raises: any internal error prints one [research]
  stderr line and disables collection for the rest of the cycle. The scanner
  must never slow down or die because of research.
- Set ARLOBIT_RESEARCH=0 to disable collection entirely.
- All writes happen in one transaction per cycle (finalize_cycle).

Rejected-sample enrichment (unbiased feature coverage):
- Up to ARLOBIT_SAMPLE_MAX (default 15) random rejected mints per cycle get the
  same sellability/holder/creator enrichment as accepted candidates, marked
  sample_rejected=1.
- Runs only after paper trading is done, only if no 429s were seen this cycle,
  and only inside a wall-clock budget (ARLOBIT_SAMPLE_DEADLINE_SECONDS, default
  150, measured from cycle start) so it can never push a cycle past the loop
  interval.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import replace
from typing import Any, Callable

from arlobit import db

SAMPLE_MAX_DEFAULT = 15
SAMPLE_DEADLINE_SECONDS_DEFAULT = 150.0
SAMPLE_DELAY_SECONDS = 0.5
MAX_CYCLE_ISSUES_FOR_SAMPLING = 10
MAX_STORED_ISSUES = 50

_active: "CycleRecorder | None" = None


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _bool_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return os.environ.get("ARLOBIT_RESEARCH", "1") != "0"


def _warn(message: str) -> None:
    print(f"[research] {message}", file=sys.stderr)


class CycleRecorder:
    def __init__(self, min_age_minutes: float, max_age_hours: float, scanner_version: str) -> None:
        self.started_at = time.time()
        self.min_age_minutes = min_age_minutes
        self.max_age_minutes = max_age_hours * 60
        self.scanner_version = scanner_version
        self.ok = True
        # key = (mint, pair_address) -> mutable sighting record
        self.sightings: dict[tuple[str, str], dict[str, Any]] = {}
        # (mint, dex_url) -> key, to match enriched PairRows back to raw pairs
        self.url_index: dict[tuple[str, str], tuple[str, str]] = {}
        self.enriched_mints: set[str] = set()
        self.sampled_mints: set[str] = set()
        self.paper_entry_mints: set[str] = set()

    def fail(self, where: str, exc: Exception) -> None:
        self.ok = False
        _warn(f"{where} failed, dropping this cycle's research data: {exc!r}")

    # -- observation ---------------------------------------------------------

    def observe_pair(self, row: Any, pair: dict[str, Any]) -> None:
        mint = str(row.token_address)
        pair_address = str(pair.get("pairAddress") or "")
        key = (mint, pair_address)
        if key in self.sightings:
            return
        age = row.age_minutes
        in_window = age is None or (self.min_age_minutes < age < self.max_age_minutes)

        volume = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        price_change = pair.get("priceChange") or {}

        record: dict[str, Any] = {
            "mint": mint,
            "pair_address": pair_address,
            "seen_at": time.time(),
            "source": str(row.source or ""),
            "in_scan_window": 1 if in_window else 0,
            "symbol": str(row.symbol or ""),
            "name": str(row.token or ""),
            "dex_id": str(pair.get("dexId") or ""),
            "dex_url": str(row.dex_url or ""),
            "pair_created_at": (_num(pair.get("pairCreatedAt")) or 0) / 1000 or None,
            "price_usd": _num(pair.get("priceUsd")),
            "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
            "fdv": _num(pair.get("fdv")),
            "market_cap": _num(pair.get("marketCap")),
            "age_minutes": _num(age),
            "mint_authority_active": _bool_int(row.mint_authority_active),
            "freeze_authority_active": _bool_int(row.freeze_authority_active),
            "enriched": 0,
            "sample_rejected": 0,
            "row": row,
        }
        for window in ("m5", "h1", "h6", "h24"):
            record[f"vol_{window}"] = _num(volume.get(window))
            window_txns = txns.get(window) or {}
            record[f"buys_{window}"] = _int(window_txns.get("buys"))
            record[f"sells_{window}"] = _int(window_txns.get("sells"))
            record[f"pc_{window}"] = _num(price_change.get(window))

        liquidity = record["liquidity_usd"]
        vol_m5, vol_h1 = record["vol_m5"], record["vol_h1"]
        buys_m5, sells_m5 = record["buys_m5"], record["sells_m5"]
        buys_h1, sells_h1 = record["buys_h1"], record["sells_h1"]
        record["vol_liq_ratio"] = (
            vol_m5 / liquidity if vol_m5 is not None and liquidity and liquidity > 0 else None
        )
        m5_txns = (buys_m5 or 0) + (sells_m5 or 0)
        record["buy_sell_ratio_m5"] = (buys_m5 or 0) / m5_txns if m5_txns > 0 else None
        h1_txns = (buys_h1 or 0) + (sells_h1 or 0)
        record["swap_accel"] = (m5_txns * 12) / h1_txns if h1_txns > 0 else None
        record["vol_accel"] = (
            (vol_m5 * 12) / vol_h1 if vol_m5 is not None and vol_h1 and vol_h1 > 0 else None
        )

        self.sightings[key] = record
        self.url_index[(mint, record["dex_url"])] = key

    def observe_enriched(self, rows: list[Any]) -> None:
        for row in rows:
            key = self.url_index.get((str(row.token_address), str(row.dex_url or "")))
            if key is None:
                continue
            record = self.sightings[key]
            record["row"] = row
            record["enriched"] = 1
            self.enriched_mints.add(record["mint"])

    def note_paper_entry(self, mint: str) -> None:
        self.paper_entry_mints.add(str(mint))

    # -- rejected-sample enrichment ------------------------------------------

    def sample_rejected(
        self,
        session: Any,
        timeout: int,
        rpc_url: str,
        helius_url: str | None,
        issues: list[str],
        check_sellability: Callable[..., Any],
        fetch_holder_status: Callable[..., Any],
        fetch_creator_status: Callable[..., Any],
        apply_native_edge: Callable[..., Any],
    ) -> None:
        deadline = self.started_at + _env_float(
            "ARLOBIT_SAMPLE_DEADLINE_SECONDS", SAMPLE_DEADLINE_SECONDS_DEFAULT
        )
        sample_max = int(_env_float("ARLOBIT_SAMPLE_MAX", SAMPLE_MAX_DEFAULT))
        if sample_max <= 0 or time.time() >= deadline:
            return
        if any("429" in issue for issue in issues) or len(issues) > MAX_CYCLE_ISSUES_FOR_SAMPLING:
            _warn("skipping rejected-sample enrichment: API budget unhealthy this cycle")
            return

        candidates: dict[str, tuple[str, str]] = {}
        for key, record in self.sightings.items():
            mint = record["mint"]
            if record["in_scan_window"] and not record["enriched"] and mint not in self.enriched_mints:
                candidates.setdefault(mint, key)
        if not candidates:
            return

        # Skip mints already enriched in earlier cycles: budget goes to new coverage.
        conn = db.connect()
        try:
            placeholders = ",".join("?" * len(candidates))
            rows = conn.execute(
                f"SELECT mint FROM tokens WHERE enriched_at IS NOT NULL AND mint IN ({placeholders})",
                list(candidates),
            ).fetchall()
            already = {row[0] for row in rows}
        finally:
            conn.close()
        eligible = [mint for mint in candidates if mint not in already]
        if not eligible:
            return

        chosen = random.sample(eligible, min(sample_max, len(eligible)))
        for mint in chosen:
            if time.time() >= deadline:
                break
            key = candidates[mint]
            record = self.sightings[key]
            try:
                time.sleep(SAMPLE_DELAY_SECONDS)
                sellability = check_sellability(session, mint, timeout)
                holder = fetch_holder_status(session, mint, rpc_url, helius_url, timeout)
                creator = fetch_creator_status(session, mint, rpc_url, helius_url, timeout)
                enriched_row = replace(
                    record["row"],
                    sellable=str(sellability.sellable),
                    sell_price_impact_pct=sellability.price_impact_pct,
                    sell_route_found=bool(sellability.route_found),
                    sell_check_error=sellability.error,
                )
                enriched_row = apply_native_edge(enriched_row, holder, creator)
            except Exception as exc:
                _warn(f"sample enrichment failed for {mint[:8]}...: {exc!r}")
                continue
            record["row"] = enriched_row
            record["sample_rejected"] = 1
            record["enriched"] = 1
            self.sampled_mints.add(mint)

    # -- persistence -----------------------------------------------------------

    def finalize(
        self,
        issues: list[str],
        candidate_count: int,
        pairs_scanned: int,
        blocked_reasons_fn: Callable[[Any], list[str]],
    ) -> None:
        records = list(self.sightings.values())
        conn = db.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO scan_cycles (started_at, finished_at, candidate_count, pairs_seen,"
                " sightings, enriched_count, sampled_count, scanner_version, issues)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.started_at,
                    time.time(),
                    candidate_count,
                    pairs_scanned,
                    len(records),
                    len(self.enriched_mints),
                    len(self.sampled_mints),
                    self.scanner_version,
                    json.dumps(issues[:MAX_STORED_ISSUES]),
                ),
            )
            cycle_id = cursor.lastrowid

            new_mints: list[tuple[str, float]] = []
            for record in records:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO tokens (mint, symbol, name, pair_address, dex_id,"
                    " dex_url, pair_created_at, first_seen_at, first_source)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        record["mint"],
                        record["symbol"],
                        record["name"],
                        record["pair_address"],
                        record["dex_id"],
                        record["dex_url"],
                        record["pair_created_at"],
                        record["seen_at"],
                        record["source"],
                    ),
                )
                if cursor.rowcount > 0:
                    new_mints.append((record["mint"], record["seen_at"]))

            sighting_rows = []
            token_updates = []
            for record in records:
                row = record["row"]
                try:
                    blocked = blocked_reasons_fn(row) if record["in_scan_window"] else ["out_of_scan_window"]
                except Exception:
                    blocked = ["blocked_reasons_error"]
                entered = 1 if record["mint"] in self.paper_entry_mints else 0
                enriched = record["enriched"]
                sighting_rows.append(
                    (
                        cycle_id,
                        record["mint"],
                        record["pair_address"],
                        record["seen_at"],
                        record["source"],
                        record["in_scan_window"],
                        record["price_usd"],
                        record["liquidity_usd"],
                        record["fdv"],
                        record["market_cap"],
                        record["age_minutes"],
                        record["vol_m5"], record["vol_h1"], record["vol_h6"], record["vol_h24"],
                        record["buys_m5"], record["sells_m5"],
                        record["buys_h1"], record["sells_h1"],
                        record["buys_h6"], record["sells_h6"],
                        record["buys_h24"], record["sells_h24"],
                        record["pc_m5"], record["pc_h1"], record["pc_h6"], record["pc_h24"],
                        record["vol_liq_ratio"],
                        record["buy_sell_ratio_m5"],
                        record["swap_accel"],
                        record["vol_accel"],
                        record["mint_authority_active"],
                        record["freeze_authority_active"],
                        enriched,
                        record["sample_rejected"],
                        str(row.sellable) if enriched else None,
                        _num(row.sell_price_impact_pct) if enriched else None,
                        _bool_int(row.sell_route_found) if enriched else None,
                        row.sell_check_error if enriched else None,
                        _num(row.top_1_holder_pct) if enriched else None,
                        None,  # top5_pct: not computed by scanner yet
                        _num(row.top_10_holders_pct) if enriched else None,
                        _num(row.top_20_holders_pct) if enriched else None,
                        str(row.holder_data_status) if enriched else None,
                        row.creator_wallet if enriched else None,
                        _num(row.creator_wallet_age_days) if enriched else None,
                        _num(row.creator_sol_balance) if enriched else None,
                        str(row.creator_quality) if enriched else None,
                        _num(row.arlobit_score) if enriched else None,
                        str(row.verdict),
                        json.dumps(list(row.signals)),
                        json.dumps(blocked),
                        entered,
                    )
                )
                if enriched:
                    token_updates.append(
                        (
                            record["mint_authority_active"],
                            record["freeze_authority_active"],
                            row.creator_wallet,
                            _num(row.creator_wallet_age_days),
                            _num(row.creator_sol_balance),
                            str(row.creator_quality),
                            record["seen_at"],
                            record["mint"],
                        )
                    )

            conn.executemany(
                "INSERT INTO candidate_sightings ("
                " cycle_id, mint, pair_address, seen_at, source, in_scan_window,"
                " price_usd, liquidity_usd, fdv, market_cap, age_minutes,"
                " vol_m5, vol_h1, vol_h6, vol_h24,"
                " buys_m5, sells_m5, buys_h1, sells_h1, buys_h6, sells_h6, buys_h24, sells_h24,"
                " pc_m5, pc_h1, pc_h6, pc_h24,"
                " vol_liq_ratio, buy_sell_ratio_m5, swap_accel, vol_accel,"
                " mint_authority_active, freeze_authority_active,"
                " enriched, sample_rejected,"
                " sellable, sell_impact_pct, sell_route_found, sell_check_error,"
                " top1_pct, top5_pct, top10_pct, top20_pct, holder_status,"
                " creator_wallet, creator_wallet_age_days, creator_sol_balance, creator_quality,"
                " arlobit_score, verdict, signals, blocked_reasons, entered_paper"
                ") VALUES (" + ",".join("?" * 53) + ")",
                sighting_rows,
            )
            conn.executemany(
                "UPDATE tokens SET"
                " mint_authority_active=COALESCE(mint_authority_active, ?),"
                " freeze_authority_active=COALESCE(freeze_authority_active, ?),"
                " creator_wallet=COALESCE(creator_wallet, ?),"
                " creator_wallet_age_days=COALESCE(creator_wallet_age_days, ?),"
                " creator_sol_balance=COALESCE(creator_sol_balance, ?),"
                " creator_quality=COALESCE(creator_quality, ?),"
                " enriched_at=COALESCE(enriched_at, ?)"
                " WHERE mint=?",
                token_updates,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO outcomes (mint, checkpoint_min, due_at, status)"
                " VALUES (?,?,?,'pending')",
                [
                    (mint, checkpoint, first_seen + checkpoint * 60)
                    for mint, first_seen in new_mints
                    for checkpoint in db.OUTCOME_CHECKPOINTS_MIN
                ],
            )
            conn.commit()
        finally:
            conn.close()
        print(
            f"[research] cycle {cycle_id}: {len(records)} sightings"
            f" ({len(self.enriched_mints)} enriched, {len(self.sampled_mints)} sampled rejected),"
            f" {len(new_mints)} new tokens"
        )


# -- module-level safe API (what scanner_v0.py calls) ---------------------------


def begin_cycle(min_age_minutes: float, max_age_hours: float, scanner_version: str = "unknown") -> None:
    global _active
    _active = None
    if not _enabled():
        return
    try:
        _active = CycleRecorder(min_age_minutes, max_age_hours, scanner_version)
    except Exception as exc:
        _warn(f"begin_cycle failed, research disabled this cycle: {exc!r}")


def observe_pair(row: Any, pair: dict[str, Any]) -> None:
    recorder = _active
    if recorder is None or not recorder.ok:
        return
    try:
        recorder.observe_pair(row, pair)
    except Exception as exc:
        recorder.fail("observe_pair", exc)


def observe_enriched(rows: list[Any]) -> None:
    recorder = _active
    if recorder is None or not recorder.ok:
        return
    try:
        recorder.observe_enriched(rows)
    except Exception as exc:
        recorder.fail("observe_enriched", exc)


def note_paper_entry(mint: str) -> None:
    recorder = _active
    if recorder is None or not recorder.ok:
        return
    try:
        recorder.note_paper_entry(mint)
    except Exception as exc:
        recorder.fail("note_paper_entry", exc)


def sample_rejected_enrichment(**kwargs: Any) -> None:
    recorder = _active
    if recorder is None or not recorder.ok:
        return
    try:
        recorder.sample_rejected(**kwargs)
    except Exception as exc:
        recorder.fail("sample_rejected_enrichment", exc)


def finalize_cycle(
    issues: list[str],
    candidate_count: int,
    pairs_scanned: int,
    blocked_reasons_fn: Callable[[Any], list[str]],
) -> None:
    global _active
    recorder = _active
    _active = None
    if recorder is None or not recorder.ok:
        return
    try:
        recorder.finalize(issues, candidate_count, pairs_scanned, blocked_reasons_fn)
    except Exception as exc:
        _warn(f"finalize_cycle failed, dropping this cycle's research data: {exc!r}")
