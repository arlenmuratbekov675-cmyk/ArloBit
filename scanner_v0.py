#!/usr/bin/env python3
"""
ArloBit Solana Scanner v0.5

Scans fresh Solana token profiles/boosts from the free DexScreener API,
fetches pair details, filters by pairCreatedAt, and prints a compact
terminal risk table. v0.5 adds persistent loop mode for automatic alerts.

This script never trades and never loads private keys.
"""

from __future__ import annotations

import argparse
import base64
import json
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
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_STATE_FILE = ".arlobit_alerts.json"
DEFAULT_LOOP_INTERVAL_SECONDS = 180

DEFAULT_MIN_AGE_MINUTES = 10
DEFAULT_MAX_AGE_HOURS = 24
TELEGRAM_ALERT_LIMIT_PER_HOUR = 2
SAFE_MIN_LIQUIDITY_USD = 20_000
SAFE_MIN_VOLUME_5M = 5_000
VERY_LOW_LIQUIDITY_USD = 1_000
LOW_LIQUIDITY_USD = 5_000
MAX_SAFE_VOLUME_LIQUIDITY_RATIO = 1.0
ANOMALY_VOLUME_LIQUIDITY_RATIO = 2.0
MIN_SAFE_PRICE_CHANGE_5M = -30
EXTREME_PUMP_5M = 150
SPL_MINT_ACCOUNT_MIN_SIZE = 82


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Candidate:
    token_address: str
    source: str


@dataclass(frozen=True)
class PairRow:
    token_address: str
    dex_url: str
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


@dataclass(frozen=True)
class ScanResult:
    rows: list[PairRow]
    issues: list[str]
    candidate_count: int
    pairs_scanned: int
    safe_count: int
    alerts_sent: int


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


def dexscreener_url(pair: dict[str, Any]) -> str:
    url = str(pair.get("url") or "").strip()
    if url:
        return url
    pair_address = str(pair.get("pairAddress") or "").strip()
    if pair_address:
        return f"https://dexscreener.com/solana/{pair_address}"
    return "https://dexscreener.com/solana"


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
        token_address=candidate.token_address,
        dex_url=dexscreener_url(pair),
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
) -> tuple[list[PairRow], list[str], int, int]:
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
            if mint_status.issue:
                issues.append(f"rpc:{candidate.token_address}: {mint_status.issue}")

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
    return rows[:limit], issues, len(candidates), len(seen_pairs)


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


def telegram_alert_enabled() -> tuple[bool, str | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, token, chat_id
    return True, token, chat_id


def should_alert(row: PairRow, min_age_minutes: int, max_age_hours: int) -> bool:
    if row.verdict != "SAFE":
        return False
    if row.mint_authority_active is not False or row.freeze_authority_active is not False:
        return False
    if row.age_minutes is None:
        return False
    return min_age_minutes < row.age_minutes < max_age_hours * 60


def telegram_message(row: PairRow) -> str:
    return "\n".join(
        [
            "ArloBit SAFE Solana token",
            f"Token: {row.token} ({row.symbol})",
            f"Mint: {row.token_address}",
            f"Price: {price(row.price)}",
            f"Liquidity: {money(row.liquidity)}",
            f"Volume 5m: {money(row.volume_5m)}",
            f"Age: {age(row.age_minutes)}",
            f"Price change 5m: {row.price_change_5m:.2f}%",
            f"DexScreener: {row.dex_url}",
            f"Verdict: {row.verdict}",
            f"Signals: {', '.join(row.signals)}",
        ]
    )


def load_telegram_state() -> dict[str, Any]:
    try:
        with open(TELEGRAM_STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"sent_at": [], "mints": {}}
    if not isinstance(state, dict):
        return {"sent_at": [], "mints": {}}
    if not isinstance(state.get("sent_at"), list):
        state["sent_at"] = []
    if not isinstance(state.get("mints"), dict):
        state["mints"] = {}
    return state


def save_telegram_state(state: dict[str, Any]) -> None:
    with open(TELEGRAM_STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def prune_telegram_state(state: dict[str, Any], now: float) -> dict[str, Any]:
    cutoff = now - 3600
    sent_at = [stamp for stamp in state.get("sent_at", []) if isinstance(stamp, (int, float)) and stamp >= cutoff]
    mints = {
        mint: stamp
        for mint, stamp in state.get("mints", {}).items()
        if isinstance(mint, str) and isinstance(stamp, (int, float))
    }
    return {"sent_at": sent_at, "mints": mints}


def send_telegram_alerts(
    session: requests.Session,
    rows: list[PairRow],
    timeout: int,
    min_age_minutes: int,
    max_age_hours: int,
) -> tuple[int, list[str]]:
    enabled, token, chat_id = telegram_alert_enabled()
    if not enabled:
        print("Telegram disabled: missing env vars")
        return 0, []

    endpoint = TELEGRAM_API_URL.format(token=token)
    now = time.time()
    state = prune_telegram_state(load_telegram_state(), now)
    remaining = max(0, TELEGRAM_ALERT_LIMIT_PER_HOUR - len(state["sent_at"]))
    sent = 0
    seen_mints: set[str] = set()
    issues: list[str] = []

    for row in rows:
        if sent >= remaining:
            break
        if not should_alert(row, min_age_minutes, max_age_hours):
            continue
        if row.token_address in seen_mints or row.token_address in state["mints"]:
            continue
        seen_mints.add(row.token_address)

        payload = {
            "chat_id": chat_id,
            "text": telegram_message(row),
            "disable_web_page_preview": True,
        }
        try:
            response = session.post(endpoint, json=payload, timeout=timeout)
            response.raise_for_status()
            sent += 1
            state["sent_at"].append(time.time())
            state["mints"][row.token_address] = time.time()
        except requests.RequestException as exc:
            issues.append(f"telegram:{row.token_address}: {exc}")

    if sent:
        save_telegram_state(state)

    if remaining == 0:
        print("Telegram alerts sent: 0 (hourly limit reached)")
    else:
        print(f"Telegram alerts sent: {sent}")
    return sent, issues


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one scan cycle and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously until Ctrl+C")
    parser.add_argument(
        "--interval",
        type=positive_int,
        default=DEFAULT_LOOP_INTERVAL_SECONDS,
        help="loop interval in seconds; used with --loop",
    )
    parser.add_argument(
        "--cycles",
        type=positive_int,
        help="optional loop cycle limit, useful for local tests",
    )
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


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBit/0.5"})
    return session


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def print_health(result: ScanResult) -> None:
    error_count = len(result.issues)
    print(
        "Health: "
        f"pairs_scanned={result.pairs_scanned} "
        f"candidates_found={result.candidate_count} "
        f"safe_count={result.safe_count} "
        f"alerts_sent={result.alerts_sent} "
        f"api_rpc_errors={error_count}"
    )


def run_scan_once(args: argparse.Namespace) -> ScanResult:
    session = build_session()
    rows, issues, candidate_count, pairs_scanned = collect_rows(
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
    alerts_sent, telegram_issues = send_telegram_alerts(
        session=session,
        rows=rows,
        timeout=args.timeout,
        min_age_minutes=args.min_age_minutes,
        max_age_hours=args.max_age_hours,
    )
    issues.extend(telegram_issues)
    result = ScanResult(
        rows=rows,
        issues=issues,
        candidate_count=candidate_count,
        pairs_scanned=pairs_scanned,
        safe_count=sum(1 for row in rows if row.verdict == "SAFE"),
        alerts_sent=alerts_sent,
    )
    print_health(result)

    if issues:
        print("\nAPI issues:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)

    if not rows:
        print(
            f"\nNo Solana pairs matched age > {args.min_age_minutes}m and < {args.max_age_hours}h.",
            file=sys.stderr,
        )
    return result


def run_loop(args: argparse.Namespace) -> int:
    cycle = 0
    print(f"ArloBit loop mode started. interval={args.interval}s. Press Ctrl+C to stop.")
    try:
        while True:
            cycle += 1
            print(f"\n[{timestamp()}] Scan cycle {cycle} start")
            try:
                run_scan_once(args)
            except Exception as exc:
                print(f"[{timestamp()}] Scan cycle {cycle} failed: {exc}", file=sys.stderr)
                print("Health: pairs_scanned=0 candidates_found=0 safe_count=0 alerts_sent=0 api_rpc_errors=1")

            if args.cycles and cycle >= args.cycles:
                print(f"[{timestamp()}] Loop cycle limit reached; exiting.")
                return 0

            print(f"[{timestamp()}] Sleeping {args.interval}s before next scan.")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] Ctrl+C received; stopping ArloBit loop.")
        return 0


def main() -> int:
    load_dotenv()
    args = parse_args()
    if args.min_age_minutes >= args.max_age_hours * 60:
        print("--min-age-minutes must be lower than --max-age-hours", file=sys.stderr)
        return 2
    if args.cycles and not args.loop:
        print("--cycles can only be used with --loop", file=sys.stderr)
        return 2

    if args.loop:
        return run_loop(args)

    run_scan_once(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
