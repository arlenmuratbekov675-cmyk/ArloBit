"""Early-buyer research collection for ArloBit v2.

This module is research-only. It reads newly discovered mints from the SQLite
research DB, fetches parsed Helius transaction history where available, stores
early buyer wallets, and links them to existing outcome labels. It never signs
transactions and is not used by scanner scoring or trading decisions.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import truststore
except ImportError:
    truststore = None
else:
    truststore.inject_into_ssl()

import requests

from arlobit import db

HELIUS_ENHANCED_BASE = "https://mainnet.helius-rpc.com/v0"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_BUYER_LIMIT = 50
DEFAULT_MINT_LIMIT = 25
DEFAULT_TIMEOUT = 20
MAX_PAGES_PER_MINT = 5
PAGE_LIMIT = 100
LOOKBACK_SECONDS = 5 * 60
LOOKAHEAD_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class EarlyBuyer:
    mint: str
    buyer_wallet: str
    first_buy_time: float | None
    buy_amount_sol: float | None
    buy_amount_usd: float | None
    token_amount: float | None
    tx_signature: str | None
    slot: int | None
    source: str
    is_dev_wallet: int | None


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def helius_api_key() -> str | None:
    direct = os.environ.get("HELIUS_API_KEY")
    if direct:
        return direct
    for name in ("SOLANA_RPC_URL", "HELIUS_RPC_URL"):
        value = os.environ.get(name)
        if not value or "helius" not in value:
            continue
        parsed = urlparse(value)
        api_key = parse_qs(parsed.query).get("api-key")
        if api_key and api_key[0]:
            return api_key[0]
    return None


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
            return amount / (10 ** decimals)
    return _num(item.get("tokenAmount"))


def _native_sol_amount(native_value: dict[str, Any] | None) -> float | None:
    if not isinstance(native_value, dict):
        return None
    amount = _num(native_value.get("amount"))
    if amount is None:
        return None
    return amount / LAMPORTS_PER_SOL


def _tx_time(tx: dict[str, Any]) -> float | None:
    return _num(tx.get("timestamp"))


def _tx_signature(tx: dict[str, Any]) -> str | None:
    return _first_string(tx.get("signature"))


def _tx_slot(tx: dict[str, Any]) -> int | None:
    return _int(tx.get("slot"))


def _usd_from_stable_inputs(swap: dict[str, Any]) -> float | None:
    total = 0.0
    found = False
    for item in swap.get("tokenInputs") or []:
        if not isinstance(item, dict) or item.get("mint") != USDC_MINT:
            continue
        amount = _raw_token_amount(item)
        if amount is not None:
            total += amount
            found = True
    return total if found else None


def _dev_wallet_for_mint(conn: Any, mint: str) -> str | None:
    row = conn.execute(
        "SELECT creator_wallet FROM tokens WHERE mint=? AND creator_wallet IS NOT NULL",
        (mint,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT creator_wallet FROM candidate_sightings"
        " WHERE mint=? AND creator_wallet IS NOT NULL ORDER BY sighting_id LIMIT 1",
        (mint,),
    ).fetchone()
    return row[0] if row and row[0] else None


def extract_buyers_from_transaction(tx: dict[str, Any], mint: str, dev_wallet: str | None) -> list[EarlyBuyer]:
    """Extract wallet buys for `mint` from one Helius enhanced transaction.

    Primary path is `events.swap.tokenOutputs`, which most closely represents
    the wallet receiving the target mint in a swap. Fallbacks keep coverage when
    Helius omits a normalized swap event for a venue.
    """
    timestamp = _tx_time(tx)
    signature = _tx_signature(tx)
    slot = _tx_slot(tx)
    buyers: list[EarlyBuyer] = []

    swap = ((tx.get("events") or {}).get("swap") or {}) if isinstance(tx.get("events"), dict) else {}
    if isinstance(swap, dict):
        native_sol = _native_sol_amount(swap.get("nativeInput"))
        usd_amount = _usd_from_stable_inputs(swap)
        for item in swap.get("tokenOutputs") or []:
            if not isinstance(item, dict) or item.get("mint") != mint:
                continue
            wallet = _first_string(item.get("userAccount"), item.get("toUserAccount"))
            token_amount = _raw_token_amount(item)
            if wallet and token_amount and token_amount > 0:
                buyers.append(
                    EarlyBuyer(
                        mint=mint,
                        buyer_wallet=wallet,
                        first_buy_time=timestamp,
                        buy_amount_sol=native_sol,
                        buy_amount_usd=usd_amount,
                        token_amount=token_amount,
                        tx_signature=signature,
                        slot=slot,
                        source="helius_enhanced_swap",
                        is_dev_wallet=1 if dev_wallet and wallet == dev_wallet else 0,
                    )
                )
        for inner in swap.get("innerSwaps") or []:
            if not isinstance(inner, dict):
                continue
            for item in inner.get("tokenOutputs") or []:
                if not isinstance(item, dict) or item.get("mint") != mint:
                    continue
                wallet = _first_string(item.get("userAccount"), item.get("toUserAccount"))
                token_amount = _raw_token_amount(item)
                if wallet and token_amount and token_amount > 0:
                    buyers.append(
                        EarlyBuyer(
                            mint=mint,
                            buyer_wallet=wallet,
                            first_buy_time=timestamp,
                            buy_amount_sol=native_sol,
                            buy_amount_usd=usd_amount,
                            token_amount=token_amount,
                            tx_signature=signature,
                            slot=slot,
                            source="helius_enhanced_inner_swap",
                            is_dev_wallet=1 if dev_wallet and wallet == dev_wallet else 0,
                        )
                    )

    if buyers:
        return buyers

    native_by_wallet: dict[str, float] = {}
    for item in tx.get("nativeTransfers") or []:
        if not isinstance(item, dict):
            continue
        wallet = _first_string(item.get("fromUserAccount"))
        amount = _num(item.get("amount"))
        if wallet and amount and amount > 0:
            native_by_wallet[wallet] = native_by_wallet.get(wallet, 0.0) + amount / LAMPORTS_PER_SOL

    for item in tx.get("tokenTransfers") or []:
        if not isinstance(item, dict) or item.get("mint") != mint:
            continue
        wallet = _first_string(item.get("toUserAccount"))
        token_amount = _num(item.get("tokenAmount"))
        if wallet and token_amount and token_amount > 0:
            buyers.append(
                EarlyBuyer(
                    mint=mint,
                    buyer_wallet=wallet,
                    first_buy_time=timestamp,
                    buy_amount_sol=native_by_wallet.get(wallet),
                    buy_amount_usd=None,
                    token_amount=token_amount,
                    tx_signature=signature,
                    slot=slot,
                    source="helius_token_transfer_fallback",
                    is_dev_wallet=1 if dev_wallet and wallet == dev_wallet else 0,
                )
            )
    return buyers


def enhanced_transactions_url(address: str, api_key: str, params: dict[str, Any]) -> str:
    query = dict(params)
    query["api-key"] = api_key
    return f"{HELIUS_ENHANCED_BASE}/addresses/{address}/transactions?{urlencode(query)}"


def fetch_enhanced_transactions(
    session: requests.Session,
    api_key: str,
    address: str,
    start_time: float | None,
    end_time: float | None,
    timeout: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": PAGE_LIMIT,
        "sort-order": "asc",
        "commitment": "confirmed",
        "token-accounts": "all",
    }
    if start_time is not None:
        params["gte-time"] = int(start_time)
    if end_time is not None:
        params["lte-time"] = int(end_time)

    transactions: list[dict[str, Any]] = []
    after_signature: str | None = None
    for _ in range(MAX_PAGES_PER_MINT):
        page_params = dict(params)
        if after_signature:
            page_params["after-signature"] = after_signature
        response = session.get(enhanced_transactions_url(address, api_key, page_params), timeout=timeout)
        if response.status_code == 429:
            raise RuntimeError("helius enhanced transactions rate limited")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            break
        page = [item for item in payload if isinstance(item, dict)]
        transactions.extend(page)
        last_signature = _tx_signature(page[-1]) if page else None
        if not last_signature or len(page) < PAGE_LIMIT:
            break
        after_signature = last_signature
    return transactions


def collect_for_mint(
    conn: Any,
    session: requests.Session,
    api_key: str,
    mint: str,
    first_seen_at: float | None,
    buyer_limit: int,
    timeout: int,
) -> int:
    dev_wallet = _dev_wallet_for_mint(conn, mint)
    start_time = first_seen_at - LOOKBACK_SECONDS if first_seen_at else None
    end_time = first_seen_at + LOOKAHEAD_SECONDS if first_seen_at else None
    transactions = fetch_enhanced_transactions(session, api_key, mint, start_time, end_time, timeout)

    by_wallet: dict[str, EarlyBuyer] = {}
    for tx in transactions:
        for buyer in extract_buyers_from_transaction(tx, mint, dev_wallet):
            current = by_wallet.get(buyer.buyer_wallet)
            if current is None or (
                buyer.first_buy_time is not None
                and (current.first_buy_time is None or buyer.first_buy_time < current.first_buy_time)
            ):
                by_wallet[buyer.buyer_wallet] = buyer
        if len(by_wallet) >= buyer_limit:
            break

    buyers = sorted(
        by_wallet.values(),
        key=lambda buyer: (
            buyer.first_buy_time if buyer.first_buy_time is not None else float("inf"),
            buyer.slot if buyer.slot is not None else 10**18,
        ),
    )[:buyer_limit]
    now = time.time()
    conn.executemany(
        """
        INSERT INTO early_buyers (
            mint, buyer_wallet, first_buy_time, buy_amount_sol, buy_amount_usd,
            token_amount, tx_signature, slot, source, is_dev_wallet,
            is_repeat_buyer, created_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mint, buyer_wallet) DO UPDATE SET
            first_buy_time=COALESCE(excluded.first_buy_time, early_buyers.first_buy_time),
            buy_amount_sol=COALESCE(excluded.buy_amount_sol, early_buyers.buy_amount_sol),
            buy_amount_usd=COALESCE(excluded.buy_amount_usd, early_buyers.buy_amount_usd),
            token_amount=COALESCE(excluded.token_amount, early_buyers.token_amount),
            tx_signature=COALESCE(excluded.tx_signature, early_buyers.tx_signature),
            slot=COALESCE(excluded.slot, early_buyers.slot),
            source=excluded.source,
            is_dev_wallet=COALESCE(excluded.is_dev_wallet, early_buyers.is_dev_wallet)
        """,
        [
            (
                buyer.mint,
                buyer.buyer_wallet,
                buyer.first_buy_time,
                buyer.buy_amount_sol,
                buyer.buy_amount_usd,
                buyer.token_amount,
                buyer.tx_signature,
                buyer.slot,
                buyer.source,
                buyer.is_dev_wallet,
                None,
                now,
            )
            for buyer in buyers
        ],
    )
    conn.commit()
    mark_repeat_buyers(conn)
    return len(buyers)


def mints_missing_buyer_data(conn: Any, limit: int) -> list[tuple[str, float | None]]:
    return conn.execute(
        """
        SELECT t.mint, t.first_seen_at
        FROM tokens t
        LEFT JOIN early_buyers eb ON eb.mint = t.mint
        WHERE eb.mint IS NULL
        ORDER BY t.first_seen_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def collect_missing(limit: int, buyer_limit: int, timeout: int) -> tuple[int, int]:
    load_dotenv()
    api_key = helius_api_key()
    if not api_key:
        raise RuntimeError("HELIUS_API_KEY or a Helius SOLANA_RPC_URL is required for early buyer collection")
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBitEarlyBuyers/0.1"})
    conn = db.connect()
    try:
        rows = mints_missing_buyer_data(conn, limit)
        total = 0
        for mint, first_seen_at in rows:
            try:
                total += collect_for_mint(conn, session, api_key, mint, first_seen_at, buyer_limit, timeout)
            except Exception as exc:
                print(f"[early_buyers] {mint[:8]} collection failed: {exc}", file=sys.stderr)
        refresh_wallet_outcomes(conn)
        return len(rows), total
    finally:
        conn.close()


def collect_for_new_mints(mints: list[tuple[str, float]], buyer_limit: int = DEFAULT_BUYER_LIMIT) -> tuple[int, int]:
    """Best-effort scanner hook. Never raises to scanner callers."""
    load_dotenv()
    api_key = helius_api_key()
    if not api_key or os.environ.get("ARLOBIT_EARLY_BUYERS", "1") == "0":
        return 0, 0
    max_mints = int(os.environ.get("ARLOBIT_EARLY_BUYERS_MAX_PER_CYCLE", "5"))
    timeout = int(os.environ.get("ARLOBIT_EARLY_BUYERS_TIMEOUT", str(DEFAULT_TIMEOUT)))
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBitEarlyBuyers/0.1"})
    conn = db.connect()
    try:
        total = 0
        selected = mints[:max_mints]
        for mint, first_seen_at in selected:
            total += collect_for_mint(conn, session, api_key, mint, first_seen_at, buyer_limit, timeout)
        refresh_wallet_outcomes(conn)
        return len(selected), total
    finally:
        conn.close()


def mark_repeat_buyers(conn: Any) -> None:
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


def refresh_wallet_outcomes(conn: Any) -> None:
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
        LEFT JOIN labels l ON l.mint = eb.mint AND l.label_version = 1
        """,
        (now,),
    )
    conn.execute("DELETE FROM wallet_stats")
    conn.execute(
        """
        INSERT INTO wallet_stats (
            buyer_wallet, first_seen_at, last_seen_at, early_buy_count, distinct_mints,
            successful_50_count, successful_100_count, successful_500_count, rugged_count,
            avg_ret_24h, avg_max_runup_pct, avg_max_drawdown_pct, updated_at
        )
        SELECT eb.buyer_wallet,
               MIN(eb.first_buy_time),
               MAX(eb.first_buy_time),
               COUNT(*),
               COUNT(DISTINCT eb.mint),
               SUM(CASE WHEN wto.reached_50 = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN wto.reached_100 = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN wto.reached_500 = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN wto.rugged = 1 THEN 1 ELSE 0 END),
               AVG(wto.ret_24h),
               AVG(wto.max_runup_pct),
               AVG(wto.max_drawdown_pct),
               ?
        FROM early_buyers eb
        LEFT JOIN wallet_token_outcomes wto
          ON wto.buyer_wallet = eb.buyer_wallet AND wto.mint = eb.mint
        GROUP BY eb.buyer_wallet
        """,
        (now,),
    )
    mark_repeat_buyers(conn)
    conn.commit()


def summary_lines(conn: Any, top_limit: int = 15) -> list[str]:
    refresh_wallet_outcomes(conn)
    total_tracked = conn.execute("SELECT COUNT(DISTINCT mint) FROM early_buyers").fetchone()[0]
    total_wallets = conn.execute("SELECT COUNT(DISTINCT buyer_wallet) FROM early_buyers").fetchone()[0]
    repeated_wallets = conn.execute(
        "SELECT COUNT(*) FROM wallet_stats WHERE distinct_mints > 1"
    ).fetchone()[0]
    missing = conn.execute(
        """
        SELECT COUNT(*)
        FROM tokens t
        LEFT JOIN early_buyers eb ON eb.mint = t.mint
        WHERE eb.mint IS NULL
        """
    ).fetchone()[0]
    lines = [
        "=== EARLY BUYERS SUMMARY ===",
        f"Total tracked mints: {total_tracked}",
        f"Total buyer wallets: {total_wallets}",
        f"Repeated wallets: {repeated_wallets}",
        f"Mints missing buyer data: {missing}",
        "",
        "Top repeated early buyers:",
        "wallet                                      mints  buys  +50  +100  +500  rugs  avg_runup%  avg_ret24h%",
    ]
    rows = conn.execute(
        """
        SELECT buyer_wallet, distinct_mints, early_buy_count, successful_50_count,
               successful_100_count, successful_500_count, rugged_count,
               avg_max_runup_pct, avg_ret_24h
        FROM wallet_stats
        WHERE distinct_mints > 1
        ORDER BY distinct_mints DESC, successful_100_count DESC, successful_50_count DESC, early_buy_count DESC
        LIMIT ?
        """,
        (top_limit,),
    ).fetchall()
    if not rows:
        lines.append("(none yet)")
    for row in rows:
        wallet, mints, buys, s50, s100, s500, rugs, avg_runup, avg_ret24 = row
        lines.append(
            f"{wallet:<43} {mints:>5} {buys:>5} {s50:>4} {s100:>5} {s500:>5} {rugs:>5}"
            f" {fmt(avg_runup):>10} {fmt(avg_ret24):>11}"
        )
    lines.extend(["", "Recent mints missing buyer data:"])
    missing_rows = conn.execute(
        """
        SELECT t.mint, t.first_seen_at
        FROM tokens t
        LEFT JOIN early_buyers eb ON eb.mint = t.mint
        WHERE eb.mint IS NULL
        GROUP BY t.mint
        ORDER BY t.first_seen_at DESC
        LIMIT 10
        """
    ).fetchall()
    if not missing_rows:
        lines.append("(none)")
    for mint, first_seen_at in missing_rows:
        seen = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(first_seen_at)) if first_seen_at else "-"
        lines.append(f"{mint}  first_seen_utc={seen}")
    lines.append("=== END SUMMARY ===")
    return lines


def fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit early-buyer research module")
    parser.add_argument("--summary", action="store_true", help="print early-buyer summary")
    parser.add_argument("--collect-missing", action="store_true", help="collect early buyers for mints missing data")
    parser.add_argument("--limit", type=int, default=DEFAULT_MINT_LIMIT, help="mint limit for --collect-missing")
    parser.add_argument("--buyer-limit", type=int, default=DEFAULT_BUYER_LIMIT, help="buyers stored per mint")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")
    args = parser.parse_args(argv)

    if args.collect_missing:
        mints, buyers = collect_missing(args.limit, args.buyer_limit, args.timeout)
        print(f"[early_buyers] processed_mints={mints} stored_buyers={buyers}")

    if args.summary or not args.collect_missing:
        conn = db.connect()
        try:
            print("\n".join(summary_lines(conn)))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
