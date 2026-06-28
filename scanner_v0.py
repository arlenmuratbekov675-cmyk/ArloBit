#!/usr/bin/env python3
"""
ArloBit Solana Scanner v0.3

Scans fresh Solana token profiles/boosts from the free DexScreener API,
fetches pair details, filters by pairCreatedAt, and prints a compact
terminal risk table. v0.3 adds Solana RPC mint/freeze authority checks.

This script never trades, never loads private keys, and never sends messages.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import truststore
except ImportError:
    truststore = None
else:
    truststore.inject_into_ssl()

import requests


BASE_URL = "https://api.dexscreener.com"
SOLANA = "solana"
PROFILE_URL = f"{BASE_URL}/token-profiles/latest/v1"
BOOSTS_URL = f"{BASE_URL}/token-boosts/latest/v1"
TOKEN_PAIRS_URL = f"{BASE_URL}/token-pairs/v1/{{chain_id}}/{{token_address}}"
DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

DEFAULT_MIN_AGE_MINUTES = 10
DEFAULT_MAX_AGE_HOURS = 24
SAFE_MIN_LIQUIDITY_USD = 20_000
SAFE_MIN_VOLUME_5M = 5_000
VERY_LOW_LIQUIDITY_USD = 1_000
LOW_LIQUIDITY_USD = 5_000
MAX_SAFE_VOLUME_LIQUIDITY_RATIO = 1.0
ANOMALY_VOLUME_LIQUIDITY_RATIO = 2.0
MIN_SAFE_PRICE_CHANGE_5M = -30
EXTREME_PUMP_5M = 150
SPL_MINT_ACCOUNT_MIN_SIZE = 82


@dataclass(frozen=True)
class Candidate:
    token_address: str
    source: str


@dataclass(frozen=True)
class PairRow:
    token: str
    symbol: str
    price: float
    liquidity: float
    volume_5m: float
    age_minutes: float | None
    price_change_5m: float
    mint_authority_active: bool | None
    freeze_authority_active: bool | None
    verdict: str
    signals: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class MintAuthorityStatus:
    mint_authority_active: bool | None
    freeze_authority_active: bool | None
    issue: str | None = None


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_json(session: requests.Session, url: str, timeout: int) -> Any:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def as_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def fetch_mint_account(
    session: requests.Session,
    rpc_url: str,
    token_address: str,
    timeout: int,
) -> MintAuthorityStatus:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            token_address,
            {"encoding": "base64", "commitment": "confirmed"},
        ],
    }

    try:
        response = session.post(rpc_url, json=payload, timeout=timeout)
        response.raise_for_status()
        rpc_payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return MintAuthorityStatus(None, None, f"rpc_failed:{exc}")

    if rpc_payload.get("error"):
        message = rpc_payload["error"].get("message", "unknown")
        return MintAuthorityStatus(None, None, f"rpc_error:{message}")

    value = (rpc_payload.get("result") or {}).get("value")
    if not value:
        return MintAuthorityStatus(None, None, "mint_account_missing")

    data = value.get("data")
    if not isinstance(data, list) or not data:
        return MintAuthorityStatus(None, None, "mint_data_missing")

    try:
        raw = base64.b64decode(data[0])
        return parse_spl_mint_account(raw)
    except (TypeError, ValueError) as exc:
        return MintAuthorityStatus(None, None, f"mint_unparseable:{exc}")


def parse_spl_mint_account(raw: bytes) -> MintAuthorityStatus:
    if len(raw) < SPL_MINT_ACCOUNT_MIN_SIZE:
        return MintAuthorityStatus(None, None, "mint_account_too_short")

    mint_authority_option = int.from_bytes(raw[0:4], "little")
    freeze_authority_option = int.from_bytes(raw[46:50], "little")
    valid_options = {0, 1}
    if mint_authority_option not in valid_options or freeze_authority_option not in valid_options:
        return MintAuthorityStatus(None, None, "mint_option_unparseable")

    return MintAuthorityStatus(
        mint_authority_active=mint_authority_option == 1,
        freeze_authority_active=freeze_authority_option == 1,
    )


def fetch_candidates(session: requests.Session, timeout: int) -> tuple[list[Candidate], list[str]]:
    endpoints = (
        ("profile", PROFILE_URL),
        ("boost", BOOSTS_URL),
    )
    seen: set[str] = set()
    candidates: list[Candidate] = []
    issues: list[str] = []

    for source, url in endpoints:
        try:
            payload = get_json(session, url, timeout)
        except requests.RequestException as exc:
            issues.append(f"{source}: {exc}")
            continue

        for item in as_items(payload):
            if item.get("chainId") != SOLANA:
                continue
            token_address = str(item.get("tokenAddress") or "").strip()
            if not token_address or token_address in seen:
                continue
            seen.add(token_address)
            candidates.append(Candidate(token_address=token_address, source=source))

    return candidates, issues


def fetch_token_pairs(session: requests.Session, token_address: str, timeout: int) -> list[dict[str, Any]]:
    url = TOKEN_PAIRS_URL.format(chain_id=SOLANA, token_address=token_address)
    payload = get_json(session, url, timeout)
    if isinstance(payload, list):
        return [pair for pair in payload if isinstance(pair, dict) and pair.get("chainId") == SOLANA]
    if isinstance(payload, dict):
        pairs = payload.get("pairs") or []
        return [pair for pair in pairs if isinstance(pair, dict) and pair.get("chainId") == SOLANA]
    return []


def pair_age_minutes(pair: dict[str, Any], now_ms: int) -> float | None:
    created_at = pair.get("pairCreatedAt")
    if created_at is None:
        return None
    created_at_ms = number(created_at)
    if created_at_ms <= 0:
        return None
    return max(0.0, (now_ms - created_at_ms) / 60_000)


def base_token_for_candidate(pair: dict[str, Any], token_address: str) -> dict[str, Any]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    if str(base.get("address") or "") == token_address:
        return base
    if str(quote.get("address") or "") == token_address:
        return quote
    return base


def score_pair(
    liquidity: float,
    volume_5m: float,
    age_minutes: float | None,
    price_change_5m: float,
    mint_status: MintAuthorityStatus,
) -> tuple[str, tuple[str, ...]]:
    danger: list[str] = []
    risk: list[str] = []
    pass_count = 0

    if age_minutes is None:
        risk.append("missing_age")
    elif age_minutes <= DEFAULT_MIN_AGE_MINUTES:
        risk.append("too_new")
    elif age_minutes < DEFAULT_MAX_AGE_HOURS * 60:
        pass_count += 1
    else:
        risk.append("old_pair")

    if liquidity < VERY_LOW_LIQUIDITY_USD:
        danger.append("very_low_liq")
    elif liquidity < LOW_LIQUIDITY_USD:
        risk.append("low_liq")
    elif liquidity > SAFE_MIN_LIQUIDITY_USD:
        pass_count += 1
    else:
        risk.append("weak_liq")

    if volume_5m > SAFE_MIN_VOLUME_5M:
        pass_count += 1
    elif volume_5m <= 0:
        risk.append("no_5m_volume")
    else:
        risk.append("low_5m_volume")

    volume_liquidity_ratio = volume_5m / liquidity if liquidity > 0 else float("inf")
    if volume_liquidity_ratio >= ANOMALY_VOLUME_LIQUIDITY_RATIO:
        danger.append("vol_liq_anomaly")
    elif volume_liquidity_ratio > MAX_SAFE_VOLUME_LIQUIDITY_RATIO:
        risk.append("high_vol_liq")
    else:
        pass_count += 1

    if price_change_5m <= -70:
        danger.append("crash_5m")
    elif price_change_5m <= MIN_SAFE_PRICE_CHANGE_5M:
        danger.append("drop_5m")
    elif price_change_5m >= EXTREME_PUMP_5M and liquidity < SAFE_MIN_LIQUIDITY_USD:
        danger.append("pump_weak_liq")
    else:
        pass_count += 1

    if mint_status.mint_authority_active is True:
        danger.append("mint_auth_active")
    elif mint_status.mint_authority_active is False:
        pass_count += 1
    else:
        risk.append("mint_auth_unknown")

    if mint_status.freeze_authority_active is True:
        danger.append("freeze_auth_active")
    elif mint_status.freeze_authority_active is False:
        pass_count += 1
    else:
        risk.append("freeze_auth_unknown")

    if mint_status.issue:
        risk.append(mint_status.issue.split(":", 1)[0])

    if danger:
        return "SCAM_LIKELY", tuple(danger + risk)
    if risk or pass_count < 6:
        return "RISKY", tuple(risk or ["mixed"])
    return "SAFE", ("fresh",)


def to_row(
    pair: dict[str, Any],
    candidate: Candidate,
    now_ms: int,
    mint_status: MintAuthorityStatus,
) -> PairRow:
    token = base_token_for_candidate(pair, candidate.token_address)
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume_5m = number((pair.get("volume") or {}).get("m5"))
    price_change_5m = number((pair.get("priceChange") or {}).get("m5"))
    age_minutes = pair_age_minutes(pair, now_ms)
    verdict, signals = score_pair(liquidity, volume_5m, age_minutes, price_change_5m, mint_status)

    return PairRow(
        token=str(token.get("name") or "Unknown"),
        symbol=str(token.get("symbol") or "?"),
        price=number(pair.get("priceUsd")),
        liquidity=liquidity,
        volume_5m=volume_5m,
        age_minutes=age_minutes,
        price_change_5m=price_change_5m,
        mint_authority_active=mint_status.mint_authority_active,
        freeze_authority_active=mint_status.freeze_authority_active,
        verdict=verdict,
        signals=signals,
        source=candidate.source,
    )


def collect_rows(
    session: requests.Session,
    limit: int,
    timeout: int,
    min_age_minutes: int,
    max_age_hours: int,
    candidate_limit: int,
    rpc_url: str,
) -> tuple[list[PairRow], list[str], int]:
    candidates, issues = fetch_candidates(session, timeout)
    now_ms = int(time.time() * 1000)
    seen_pairs: set[str] = set()
    mint_cache: dict[str, MintAuthorityStatus] = {}
    rows: list[PairRow] = []

    for candidate in candidates[:candidate_limit]:
        mint_status = mint_cache.get(candidate.token_address)
        if mint_status is None:
            mint_status = fetch_mint_account(session, rpc_url, candidate.token_address, timeout)
            mint_cache[candidate.token_address] = mint_status

        try:
            pairs = fetch_token_pairs(session, candidate.token_address, timeout)
        except requests.RequestException as exc:
            issues.append(f"{candidate.source}:{candidate.token_address}: {exc}")
            continue

        for pair in pairs:
            pair_address = str(pair.get("pairAddress") or "")
            if not pair_address or pair_address in seen_pairs:
                continue
            seen_pairs.add(pair_address)

            row = to_row(pair, candidate, now_ms, mint_status)
            if row.age_minutes is None:
                rows.append(row)
                continue
            if min_age_minutes < row.age_minutes < max_age_hours * 60:
                rows.append(row)

    rows.sort(
        key=lambda row: (
            row.verdict != "SAFE",
            row.verdict == "SCAM_LIKELY",
            row.age_minutes is None,
            row.age_minutes or float("inf"),
            -row.volume_5m,
        )
    )
    return rows[:limit], issues, len(candidates)


def money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def price(value: float) -> str:
    if value == 0:
        return "$0"
    if value < 0.0001:
        return f"${value:.8f}"
    if value < 1:
        return f"${value:.6f}"
    return f"${value:.4f}"


def age(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 24 * 60:
        return f"{value / (24 * 60):.1f}d"
    if value >= 60:
        return f"{value / 60:.1f}h"
    return f"{value:.0f}m"


def terminal_text(value: str, max_length: int) -> str:
    clean = value.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
        sys.stdout.encoding or "utf-8"
    )
    return clean[:max_length]


def bool_status(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def render_table(rows: list[PairRow]) -> str:
    headers = [
        "token",
        "symbol",
        "price",
        "liquidity",
        "volume_5m",
        "age",
        "price_change_5m",
        "mint_auth",
        "freeze_auth",
        "verdict",
        "signals",
    ]
    table_rows = [
        [
            terminal_text(row.token, 20),
            terminal_text(row.symbol, 10),
            price(row.price),
            money(row.liquidity),
            money(row.volume_5m),
            age(row.age_minutes),
            f"{row.price_change_5m:.2f}%",
            bool_status(row.mint_authority_active),
            bool_status(row.freeze_authority_active),
            row.verdict,
            ",".join(row.signals)[:28],
        ]
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in table_rows)) if table_rows else len(header)
        for index, header in enumerate(headers)
    ]
    line = " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    divider = "-+-".join("-" * width for width in widths)
    body = [" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in table_rows]
    return "\n".join([line, divider, *body])


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan fresh Solana pairs from DexScreener.")
    parser.add_argument("--limit", type=positive_int, default=12, help="maximum rows to print")
    parser.add_argument("--timeout", type=positive_int, default=15, help="HTTP timeout in seconds")
    parser.add_argument("--min-age-minutes", type=positive_int, default=DEFAULT_MIN_AGE_MINUTES)
    parser.add_argument("--max-age-hours", type=positive_int, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument(
        "--candidate-limit",
        type=positive_int,
        default=80,
        help="maximum fresh profile/boost tokens to resolve into pair details",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL),
        help="Solana RPC URL; defaults to SOLANA_RPC_URL env var or public mainnet RPC",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_age_minutes >= args.max_age_hours * 60:
        print("--min-age-minutes must be lower than --max-age-hours", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBit/0.3"})

    rows, issues, candidate_count = collect_rows(
        session=session,
        limit=args.limit,
        timeout=args.timeout,
        min_age_minutes=args.min_age_minutes,
        max_age_hours=args.max_age_hours,
        candidate_limit=args.candidate_limit,
        rpc_url=args.rpc_url,
    )

    print(render_table(rows))
    print(f"\nScanned {candidate_count} latest Solana profile/boost candidates.")

    if issues:
        print("\nAPI issues:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)

    if not rows:
        print(
            f"\nNo Solana pairs matched age > {args.min_age_minutes}m and < {args.max_age_hours}h.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
