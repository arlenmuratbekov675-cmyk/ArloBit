"""Outcome tracker daemon: fills checkpoint prices and finalizes 24h labels.

Runs as its own process (`python tracker.py`), fully independent of the
scanner — the only shared state is the WAL SQLite file. All tracker state
lives in the DB, so it can be stopped for hours or days and resume exactly
where it left off:

- Pending checkpoints are polled from the `outcomes` table (due_at <= now)
  and filled via the DexScreener batch endpoint (30 mints per request).
- Checkpoints more than SKIP_OVERDUE_SECONDS late are marked 'skipped'
  instead of being filled with misleading present-day prices: point-in-time
  correctness beats completeness. The OHLCV backfill recovers the true path.
- At first_seen + 24h (+buffer) each mint gets a `labels` row. GeckoTerminal
  free 1-minute OHLCV is backfilled for the 24h window, giving exact
  highest/lowest price, max run-up/drawdown and threshold hits; when OHLCV
  is unavailable the label falls back to timely checkpoints and says so in
  path_source. Late-filled checkpoints are never used for return columns.

Label v1 definitions:
- rugged: liquidity fell below 20% of base, or price fell 90%+ below base,
  or the pair disappeared from DexScreener.
- ret_* columns are relative to the price at the first sighting (base).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

try:
    import truststore
except ImportError:
    truststore = None
else:
    truststore.inject_into_ssl()

import requests

from arlobit import db

DEX_BATCH_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"
DEX_BATCH_SIZE = 30
GT_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
GT_PAGE_LIMIT = 1000
GT_MAX_PAGES = 3
GT_MIN_SECONDS_BETWEEN_CALLS = 2.2
GT_429_SLEEP_SECONDS = 15.0

LABEL_VERSION = 1
LABEL_WINDOW_SECONDS = 24 * 3600
FINALIZE_BUFFER_SECONDS = 15 * 60
FINALIZE_MAX_PER_PASS_DEFAULT = 8
DUE_CHECKPOINTS_PER_PASS = 600
SKIP_OVERDUE_SECONDS = 2 * 3600
TIMELY_MIN_SECONDS = 90.0
TIMELY_FRACTION = 0.25

THRESHOLDS = (20, 50, 100, 200, 500)
RET_COLUMN_BY_MINUTE = {
    5: "ret_5m", 15: "ret_15m", 30: "ret_30m", 60: "ret_1h",
    120: "ret_2h", 360: "ret_6h", 720: "ret_12h", 1440: "ret_24h",
}
RUG_LIQ_FRACTION = 0.2
RUG_RET_PCT = -90.0
OHLCV_SANITY_RATIO = 10.0


class RateLimited(Exception):
    pass


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _log(message: str) -> None:
    print(f"[tracker {time.strftime('%H:%M:%S')}] {message}")


def _warn(message: str) -> None:
    print(f"[tracker] {message}", file=sys.stderr)


def checkpoint_is_timely(checkpoint_min: int, due_at: float, checked_at: float | None) -> bool:
    if checked_at is None:
        return False
    tolerance = max(TIMELY_MIN_SECONDS, checkpoint_min * 60 * TIMELY_FRACTION)
    return abs(checked_at - due_at) <= tolerance


# --------------------------------------------------------------------------
# checkpoint filling (DexScreener batch endpoint)
# --------------------------------------------------------------------------


def best_pair_for_mint(pairs: list[dict[str, Any]], mint: str, preferred_pair: str | None) -> dict[str, Any] | None:
    matches = []
    for pair in pairs:
        base = str(((pair.get("baseToken") or {}).get("address")) or "")
        quote = str(((pair.get("quoteToken") or {}).get("address")) or "")
        if mint not in (base, quote):
            continue
        if preferred_pair and str(pair.get("pairAddress") or "") == preferred_pair:
            return pair
        matches.append(pair)
    if not matches:
        return None
    return max(matches, key=lambda p: _num((p.get("liquidity") or {}).get("usd")) or 0.0)


def fetch_dex_batch(session: requests.Session, mints: list[str], timeout: int) -> list[dict[str, Any]]:
    url = DEX_BATCH_URL.format(addresses=",".join(mints))
    response = session.get(url, timeout=timeout)
    if response.status_code == 429:
        raise RateLimited("dexscreener 429")
    response.raise_for_status()
    payload = response.json()
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def base_prices(conn: Any, mints: list[str]) -> dict[str, float | None]:
    if not mints:
        return {}
    placeholders = ",".join("?" * len(mints))
    rows = conn.execute(
        "SELECT mint, price_usd FROM candidate_sightings"
        " WHERE sighting_id IN (SELECT MIN(sighting_id) FROM candidate_sightings"
        f" WHERE mint IN ({placeholders}) GROUP BY mint)",
        mints,
    ).fetchall()
    return {mint: price for mint, price in rows}


def skip_stale_checkpoints(conn: Any, now: float) -> int:
    cursor = conn.execute(
        "UPDATE outcomes SET status='skipped', checked_at=? WHERE checked_at IS NULL AND due_at < ?",
        (now, now - SKIP_OVERDUE_SECONDS),
    )
    conn.commit()
    return cursor.rowcount


def fill_due_checkpoints(conn: Any, session: requests.Session, now: float, timeout: int) -> tuple[int, int]:
    due = conn.execute(
        "SELECT o.mint, o.checkpoint_min, o.due_at, t.pair_address FROM outcomes o"
        " JOIN tokens t ON t.mint = o.mint"
        " WHERE o.checked_at IS NULL AND o.due_at <= ? ORDER BY o.due_at LIMIT ?",
        (now, DUE_CHECKPOINTS_PER_PASS),
    ).fetchall()
    if not due:
        return 0, 0

    mints = list(dict.fromkeys(row[0] for row in due))
    preferred = {row[0]: row[3] for row in due}
    bases = base_prices(conn, mints)

    pair_by_mint: dict[str, dict[str, Any] | None] = {}
    fetched_mints: set[str] = set()
    for start in range(0, len(mints), DEX_BATCH_SIZE):
        chunk = mints[start:start + DEX_BATCH_SIZE]
        try:
            pairs = fetch_dex_batch(session, chunk, timeout)
        except RateLimited:
            raise
        except requests.RequestException as exc:
            _warn(f"batch price fetch failed for {len(chunk)} mints (will retry): {exc}")
            continue
        fetched_mints.update(chunk)
        for mint in chunk:
            pair_by_mint[mint] = best_pair_for_mint(pairs, mint, preferred.get(mint))

    filled = 0
    gone = 0
    checked_at = time.time()
    updates = []
    for mint, checkpoint_min, _due_at, _pref in due:
        if mint not in fetched_mints:
            continue  # request failed; stays pending for the next pass
        pair = pair_by_mint.get(mint)
        if pair is None:
            updates.append((checked_at, None, None, None, None, "pair_gone", mint, checkpoint_min))
            gone += 1
            continue
        price = _num(pair.get("priceUsd"))
        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        vol_h24 = _num((pair.get("volume") or {}).get("h24"))
        base = bases.get(mint)
        ret = (price / base - 1) * 100 if price is not None and base and base > 0 else None
        updates.append((checked_at, price, liquidity, vol_h24, ret, "ok", mint, checkpoint_min))
        filled += 1
    conn.executemany(
        "UPDATE outcomes SET checked_at=?, price_usd=?, liquidity_usd=?, vol_h24=?, ret_pct=?, status=?"
        " WHERE mint=? AND checkpoint_min=?",
        updates,
    )
    conn.commit()
    return filled, gone


# --------------------------------------------------------------------------
# GeckoTerminal OHLCV backfill
# --------------------------------------------------------------------------

_last_gt_call = 0.0


def _gt_get(session: requests.Session, url: str, params: dict[str, Any], timeout: int) -> dict[str, Any] | None:
    global _last_gt_call
    wait = _last_gt_call + GT_MIN_SECONDS_BETWEEN_CALLS - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.time()
    response = session.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=timeout)
    if response.status_code == 429:
        raise RateLimited("geckoterminal 429")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def fetch_gt_ohlcv(
    session: requests.Session,
    pool: str,
    mint: str,
    window_start: float,
    window_end: float,
    timeout: int,
) -> list[tuple[float, float, float, float, float, float]]:
    """Fetch 1m candles covering [window_start, window_end] for `pool`.

    Returns [] when the pool is unknown to GeckoTerminal. Automatically
    switches to token=quote pricing when the mint is the pool's quote token.
    """
    url = GT_OHLCV_URL.format(pool=pool)
    token_side = "base"
    candles: dict[float, tuple[float, float, float, float, float, float]] = {}
    before = window_end + 120
    for _page in range(GT_MAX_PAGES):
        payload = _gt_get(
            session,
            url,
            {"aggregate": 1, "limit": GT_PAGE_LIMIT, "currency": "usd",
             "token": token_side, "before_timestamp": int(before)},
            timeout,
        )
        if payload is None:
            return []
        meta = payload.get("meta") or {}
        base_addr = str(((meta.get("base") or {}).get("address")) or "")
        quote_addr = str(((meta.get("quote") or {}).get("address")) or "")
        if token_side == "base" and base_addr and base_addr != mint and quote_addr == mint:
            token_side = "quote"
            candles.clear()
            before = window_end + 120
            continue
        ohlcv_list = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        page = []
        for item in ohlcv_list:
            if not isinstance(item, list) or len(item) < 6:
                continue
            ts = _num(item[0])
            if ts is None:
                continue
            page.append((ts, *(_num(v) or 0.0 for v in item[1:6])))
        if not page:
            break
        for candle in page:
            if window_start - 60 <= candle[0] <= window_end + 60:
                candles[candle[0]] = candle
        oldest = min(candle[0] for candle in page)
        if oldest <= window_start:
            break
        before = oldest
    return sorted(candles.values())


def ohlcv_is_sane(candles: list[tuple[float, ...]], base_price: float | None, window_start: float) -> bool:
    """Guard against wrong-side pricing: the candle close nearest the first
    sighting must be within 10x of the price the scanner recorded."""
    if not candles or not base_price or base_price <= 0:
        return False
    nearest = min(candles, key=lambda c: abs(c[0] - window_start))
    close = nearest[4]
    if close is None or close <= 0:
        return False
    ratio = close / base_price
    return 1 / OHLCV_SANITY_RATIO <= ratio <= OHLCV_SANITY_RATIO


# --------------------------------------------------------------------------
# label computation (pure function: unit-testable without network or DB)
# --------------------------------------------------------------------------


def compute_label(
    base_price: float | None,
    base_liquidity: float | None,
    window_start: float,
    candles: list[tuple[float, float, float, float, float, float]],
    checkpoints: list[dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    window_end = window_start + LABEL_WINDOW_SECONDS
    label: dict[str, Any] = {column: None for column in RET_COLUMN_BY_MINUTE.values()}
    label.update(
        max_runup_pct=None, max_drawdown_pct=None, path_source="none",
        rugged=None, rug_at_min=None, survived_24h=None, liq_change_24h_pct=None,
        **{f"reached_{threshold}": None for threshold in THRESHOLDS},
    )
    label["holdout_week"] = time.strftime("%G-W%V", time.gmtime(window_start))
    if not base_price or base_price <= 0:
        label["path_source"] = "no_base"
        return label

    def pct(price: float) -> float:
        return (price / base_price - 1) * 100

    timely = [
        cp for cp in checkpoints
        if cp["status"] == "ok" and cp["price_usd"] is not None
        and checkpoint_is_timely(cp["checkpoint_min"], cp["due_at"], cp["checked_at"])
    ]

    in_window = [c for c in candles if window_start - 60 <= c[0] <= window_end + 60]
    if in_window:
        label["path_source"] = "ohlcv"
        highs = [c[2] for c in in_window if c[2] and c[2] > 0]
        lows = [c[3] for c in in_window if c[3] and c[3] > 0]
        if highs:
            label["max_runup_pct"] = max(pct(h) for h in highs)
        if lows:
            label["max_drawdown_pct"] = min(0.0, min(pct(l) for l in lows))
        for minute, column in RET_COLUMN_BY_MINUTE.items():
            target = window_start + minute * 60
            past = [c for c in in_window if c[0] <= target]
            if past:
                label[column] = pct(past[-1][4])
    else:
        label["path_source"] = "checkpoints"
        observed = [pct(cp["price_usd"]) for cp in timely]
        if observed:
            label["max_runup_pct"] = max(0.0, max(observed))
            label["max_drawdown_pct"] = min(0.0, min(observed))
        for cp in timely:
            column = RET_COLUMN_BY_MINUTE.get(cp["checkpoint_min"])
            if column:
                label[column] = pct(cp["price_usd"])

    if label["max_runup_pct"] is not None:
        for threshold in THRESHOLDS:
            label[f"reached_{threshold}"] = 1 if label["max_runup_pct"] >= threshold else 0

    # rug evidence (any source; late checkpoints allowed — a dead pair stays dead)
    rug_times: list[float] = []
    for cp in checkpoints:
        if cp["status"] == "pair_gone":
            rug_times.append(float(cp["checkpoint_min"]))
        elif cp["status"] == "ok":
            liq = cp["liquidity_usd"]
            if (
                liq is not None and base_liquidity and base_liquidity > 0
                and liq < base_liquidity * RUG_LIQ_FRACTION
            ):
                rug_times.append(float(cp["checkpoint_min"]))
            if cp["price_usd"] is not None and pct(cp["price_usd"]) <= RUG_RET_PCT:
                rug_times.append(float(cp["checkpoint_min"]))
    for c in in_window:
        low = c[3]
        if low and low > 0 and pct(low) <= RUG_RET_PCT:
            rug_times.append(max(0.0, (c[0] - window_start) / 60))
    if rug_times:
        label["rugged"] = 1
        label["rug_at_min"] = min(rug_times)
    else:
        has_24h_evidence = label["ret_24h"] is not None or (
            in_window and in_window[-1][0] >= window_end - 30 * 60
        )
        label["rugged"] = 0 if (has_24h_evidence or timely or in_window) else None

    if label["rugged"] == 1:
        label["survived_24h"] = 0
    elif label["rugged"] == 0 and (
        label["ret_24h"] is not None or (in_window and in_window[-1][0] >= window_end - 30 * 60)
    ):
        label["survived_24h"] = 1

    final_cp = next((cp for cp in checkpoints if cp["checkpoint_min"] == 1440 and cp["status"] == "ok"), None)
    if final_cp and final_cp["liquidity_usd"] is not None and base_liquidity and base_liquidity > 0:
        label["liq_change_24h_pct"] = (final_cp["liquidity_usd"] / base_liquidity - 1) * 100
    return label


# --------------------------------------------------------------------------
# label finalization
# --------------------------------------------------------------------------


def finalize_labels(
    conn: Any,
    session: requests.Session,
    now: float,
    timeout: int,
    max_finalize: int,
) -> tuple[int, int]:
    deadline = now - LABEL_WINDOW_SECONDS - FINALIZE_BUFFER_SECONDS
    pending = conn.execute(
        "SELECT t.mint, t.pair_address, t.first_seen_at FROM tokens t"
        " LEFT JOIN labels l ON l.mint = t.mint AND l.label_version = ?"
        " WHERE l.mint IS NULL AND t.first_seen_at <= ? ORDER BY t.first_seen_at LIMIT ?",
        (LABEL_VERSION, deadline, max_finalize),
    ).fetchall()

    finalized = 0
    ohlcv_count = 0
    for mint, pair_address, first_seen_at in pending:
        base_row = conn.execute(
            "SELECT sighting_id, price_usd, liquidity_usd FROM candidate_sightings"
            " WHERE mint=? ORDER BY sighting_id LIMIT 1",
            (mint,),
        ).fetchone()
        base_sighting_id, base_price, base_liquidity = base_row if base_row else (None, None, None)
        checkpoints = [
            {
                "checkpoint_min": row[0], "due_at": row[1], "checked_at": row[2],
                "price_usd": row[3], "liquidity_usd": row[4], "status": row[5],
            }
            for row in conn.execute(
                "SELECT checkpoint_min, due_at, checked_at, price_usd, liquidity_usd, status"
                " FROM outcomes WHERE mint=?",
                (mint,),
            ).fetchall()
        ]

        window_start = float(first_seen_at)
        window_end = window_start + LABEL_WINDOW_SECONDS
        candles: list[tuple[float, float, float, float, float, float]] = []
        if pair_address:
            try:
                candles = fetch_gt_ohlcv(session, pair_address, mint, window_start, window_end, timeout)
            except RateLimited:
                raise
            except requests.RequestException as exc:
                _warn(f"ohlcv fetch failed for {mint[:8]}... (label falls back to checkpoints): {exc}")
        if candles and not ohlcv_is_sane(candles, base_price, window_start):
            _warn(f"ohlcv failed sanity check for {mint[:8]}...; using checkpoints instead")
            candles = []
        if candles:
            conn.executemany(
                "INSERT OR IGNORE INTO ohlcv_1m (mint, ts, open, high, low, close, volume)"
                " VALUES (?,?,?,?,?,?,?)",
                [(mint, *candle) for candle in candles],
            )
            ohlcv_count += 1

        label = compute_label(base_price, base_liquidity, window_start, candles, checkpoints, now)
        conn.execute(
            "INSERT OR IGNORE INTO labels (mint, label_version, computed_at, base_sighting_id,"
            " base_price, base_liquidity, max_runup_pct, max_drawdown_pct, path_source,"
            " ret_5m, ret_15m, ret_30m, ret_1h, ret_2h, ret_6h, ret_12h, ret_24h,"
            " reached_20, reached_50, reached_100, reached_200, reached_500,"
            " rugged, rug_at_min, survived_24h, liq_change_24h_pct, holdout_week)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mint, LABEL_VERSION, time.time(), base_sighting_id, base_price, base_liquidity,
                label["max_runup_pct"], label["max_drawdown_pct"], label["path_source"],
                label["ret_5m"], label["ret_15m"], label["ret_30m"], label["ret_1h"],
                label["ret_2h"], label["ret_6h"], label["ret_12h"], label["ret_24h"],
                label["reached_20"], label["reached_50"], label["reached_100"],
                label["reached_200"], label["reached_500"],
                label["rugged"], label["rug_at_min"], label["survived_24h"],
                label["liq_change_24h_pct"], label["holdout_week"],
            ),
        )
        conn.commit()
        finalized += 1
    return finalized, ohlcv_count


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def run_pass(session: requests.Session, timeout: int, max_finalize: int) -> None:
    now = time.time()
    conn = db.connect()
    try:
        skipped = skip_stale_checkpoints(conn, now)
        try:
            filled, gone = fill_due_checkpoints(conn, session, now, timeout)
            labeled, with_ohlcv = finalize_labels(conn, session, now, timeout, max_finalize)
        except RateLimited as exc:
            _warn(f"rate limited ({exc}); backing off until next pass")
            time.sleep(GT_429_SLEEP_SECONDS)
            return
        pending = conn.execute("SELECT COUNT(*) FROM outcomes WHERE checked_at IS NULL").fetchone()[0]
        if filled or gone or skipped or labeled:
            _log(
                f"checkpoints: {filled} filled, {gone} pair_gone, {skipped} skipped stale;"
                f" labels: {labeled} finalized ({with_ohlcv} with ohlcv); {pending} pending"
            )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit outcome tracker (research only, never trades)")
    parser.add_argument("--interval", type=int, default=60, help="seconds between passes (default 60)")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds")
    parser.add_argument("--max-finalize", type=int, default=FINALIZE_MAX_PER_PASS_DEFAULT,
                        help="max labels finalized per pass (GeckoTerminal budget)")
    args = parser.parse_args(argv)

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBitTracker/0.1"})
    _log(f"tracker started: db={db.db_path()} interval={args.interval}s once={args.once}")
    while True:
        try:
            run_pass(session, args.timeout, args.max_finalize)
        except KeyboardInterrupt:
            _log("stopped")
            return 0
        except Exception as exc:
            _warn(f"pass failed (will retry): {exc!r}")
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
