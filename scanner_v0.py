#!/usr/bin/env python3
"""
ArloBit Solana Scanner v0.9.4

Scans fresh Solana token profiles/boosts from the free DexScreener API,
fetches pair details, filters by pairCreatedAt, and prints a compact
terminal risk table. v0.9.4 fixes enrichment pipeline ordering.

This script never trades and never loads private keys.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass, replace
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
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={api_key}"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_STATE_FILE = ".arlobit_alerts.json"
PAPER_TRADES_FILE = "paper_trades.json"
PAPER_TRADES_CSV_FILE = "paper_trades.csv"
DEFAULT_LOOP_INTERVAL_SECONDS = 180
DEFAULT_JUPITER_SELL_AMOUNT_RAW = 1_000_000
MAX_PAPER_ENTRIES_PER_HOUR = 2
NATIVE_EDGE_CHECK_DELAY_SECONDS = 0.5

DEFAULT_MIN_AGE_MINUTES = 10
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_TELEGRAM_ALERT_LIMIT_PER_HOUR = 2
PAPER_TAKE_PROFIT_PERCENT = 50
PAPER_STOP_LOSS_PERCENT = -30
PAPER_RUG_DROP_PERCENT = -50
PAPER_MAX_HOLD_SECONDS = 6 * 60 * 60
SAFE_MIN_AGE_MINUTES = 30
SAFE_MIN_LIQUIDITY_USD = 50_000
SAFE_MIN_VOLUME_5M = 5_000
VERY_LOW_LIQUIDITY_USD = 1_000
LOW_LIQUIDITY_USD = 5_000
MIN_SAFE_VOLUME_LIQUIDITY_RATIO = 0.10
MAX_SAFE_VOLUME_LIQUIDITY_RATIO = 0.50
ANOMALY_VOLUME_LIQUIDITY_RATIO = 2.0
BLOCKED_PAPER_REASONS = (
    "too_young",
    "liquidity_too_low",
    "vol_liq_too_high",
    "vol_liq_too_low",
    "honeypot_no_route",
    "sell_impact_too_high",
    "sellability_unknown",
    "blocked_holder_unknown",
    "blocked_creator_unknown",
    "blocked_score_unavailable",
    "scam_holder",
    "scam_creator",
    "risky_holder",
    "creator_risky",
    "score_too_low",
    "paper_entry_hourly_limit",
)
KNOWN_NON_WALLET_HOLDERS = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "9W959DqEETiGZocYWCQPaJ6vGgFbkYQM7HwxV23cw3kF",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VgVwfmJw7pN5G6C",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
    "MeteoraDLMMProgram1111111111111111111111111111",
}
MIN_SAFE_PRICE_CHANGE_5M = -30
EXTREME_PUMP_5M = 150
SPL_MINT_ACCOUNT_MIN_SIZE = 82
RPC_GET_ACCOUNT_INFO_MIN_DELAY_SECONDS = 0.3
RPC_GET_ACCOUNT_INFO_MAX_DELAY_SECONDS = 0.5
RPC_429_BACKOFF_SECONDS = 1.0


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


def masked_env_status(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return f"{name}: loaded (len={len(value)})"
    return f"{name}: missing"


def print_startup_env_status() -> None:
    print(masked_env_status("TELEGRAM_BOT_TOKEN"))
    print(masked_env_status("TELEGRAM_CHAT_ID"))


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
    sellable: str
    sell_price_impact_pct: float | None
    sell_route_found: bool
    sell_check_error: str | None
    arlobit_score: float
    top_1_holder_pct: float | None
    top_10_holders_pct: float | None
    top_20_holders_pct: float | None
    holder_data_status: str
    creator_wallet: str | None
    creator_sol_balance: float | None
    creator_wallet_age_days: float | None
    creator_quality: str
    verdict: str
    signals: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class SellabilityStatus:
    sellable: str = "unknown"
    price_impact_pct: float | None = None
    route_found: bool = False
    error: str | None = None
    output_mint: str | None = None


@dataclass(frozen=True)
class HolderStatus:
    top_1_holder_pct: float | None = None
    top_10_holders_pct: float | None = None
    top_20_holders_pct: float | None = None
    status: str = "unknown"
    error: str | None = None
    method: str | None = None
    result_count: int | None = None


@dataclass(frozen=True)
class CreatorStatus:
    wallet: str | None = None
    sol_balance: float | None = None
    wallet_age_days: float | None = None
    quality: str = "unknown"
    error: str | None = None
    signature: str | None = None


@dataclass(frozen=True)
class MintAuthorityStatus:
    mint_authority_active: bool | None
    freeze_authority_active: bool | None
    issue: str | None = None
    mint_authority: str | None = None


@dataclass(frozen=True)
class ScanResult:
    rows: list[PairRow]
    issues: list[str]
    candidate_count: int
    pairs_scanned: int
    safe_count: int
    alerts_sent: int
    paper_opened: int
    paper_closed: int
    blocked_paper_reason_counts: dict[str, int]


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


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {character: index for index, character in enumerate(BASE58_ALPHABET)}


def base58_decode(value: str) -> bytes | None:
    decoded = 0
    for character in value:
        digit = BASE58_INDEX.get(character)
        if digit is None:
            return None
        decoded = decoded * 58 + digit
    raw = decoded.to_bytes((decoded.bit_length() + 7) // 8, "big") if decoded else b""
    padding = 0
    for character in value:
        if character == "1":
            padding += 1
        else:
            break
    return b"\x00" * padding + raw


def is_valid_solana_pubkey(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    address = value.strip()
    if not address or address != value:
        return False
    decoded = base58_decode(address)
    return decoded is not None and len(decoded) == 32


def safe_pubkey(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 14:
        return text
    return f"{text[:6]}...{text[-6:]}"


def sanitize_rpc_value(value: Any) -> Any:
    if isinstance(value, str):
        return safe_pubkey(value)
    if isinstance(value, list):
        return [sanitize_rpc_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_rpc_value(item) for key, item in value.items()}
    return value


def print_rpc_error_debug(method: str, params: list[Any] | dict[str, Any], http_status: int | str, body: Any, error: Any) -> None:
    print(
        "[rpc-debug] "
        f"method={method} "
        f"params={json.dumps(sanitize_rpc_value(params), sort_keys=True)} "
        f"http_status={http_status} "
        f"http_response={json.dumps(sanitize_rpc_value(body), sort_keys=True)} "
        f"rpc_error={json.dumps(sanitize_rpc_value(error), sort_keys=True)}",
        file=sys.stderr,
    )

def rpc_request_payload(
    session: requests.Session,
    rpc_url: str,
    method: str,
    params: list[Any] | dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = session.post(rpc_url, json=payload, timeout=timeout)
    response.raise_for_status()
    rpc_payload = response.json()
    if rpc_payload.get("error"):
        print_rpc_error_debug(method, params, response.status_code, rpc_payload, rpc_payload.get("error"))
        message = (rpc_payload.get("error") or {}).get("message", "unknown")
        raise requests.RequestException(f"{method}:{message}")
    return rpc_payload


def base58_encode(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    padding = 0
    for byte in raw:
        if byte == 0:
            padding += 1
        else:
            break
    return "1" * padding + (encoded or "1")


def rpc_request(
    session: requests.Session,
    rpc_url: str,
    method: str,
    params: list[Any] | dict[str, Any],
    timeout: int,
) -> Any:
    rpc_payload = rpc_request_payload(session, rpc_url, method, params, timeout)
    return rpc_payload.get("result")


def helius_rpc_url(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return HELIUS_RPC_URL.format(api_key=api_key)


def token_amount_raw(item: dict[str, Any]) -> float:
    for key in ("amount", "balance"):
        value = item.get(key)
        if value is not None:
            return number(value)
    token_amount = item.get("tokenAmount") or item.get("token_amount") or {}
    if isinstance(token_amount, dict):
        return number(token_amount.get("amount"))
    return 0.0


def token_ui_amount_from_fields(fields: dict[str, Any]) -> float | None:
    ui_amount = fields.get("uiAmount")
    if ui_amount is not None:
        return number(ui_amount, default=0.0)
    ui_amount_string = fields.get("uiAmountString")
    if ui_amount_string is not None:
        return number(ui_amount_string, default=0.0)
    amount = fields.get("amount")
    decimals = fields.get("decimals")
    if amount is None or decimals is None:
        return None
    try:
        return number(amount) / (10 ** int(decimals))
    except (TypeError, ValueError, OverflowError):
        return None


def token_account_owner(item: dict[str, Any]) -> str | None:
    for key in ("owner", "ownerAddress", "owner_address"):
        owner = first_string(item.get(key))
        if owner:
            return owner
    account = item.get("account") or {}
    data = (account.get("data") or {}) if isinstance(account, dict) else {}
    parsed = (data.get("parsed") or {}) if isinstance(data, dict) else {}
    info = (parsed.get("info") or {}) if isinstance(parsed, dict) else {}
    return first_string(info.get("owner"))


def token_account_amount(item: dict[str, Any]) -> float:
    direct_amount = token_ui_amount_from_fields(item)
    if direct_amount is not None:
        return direct_amount
    account = item.get("account") or {}
    data = (account.get("data") or {}) if isinstance(account, dict) else {}
    parsed = (data.get("parsed") or {}) if isinstance(data, dict) else {}
    info = (parsed.get("info") or {}) if isinstance(parsed, dict) else {}
    token_amount = (info.get("tokenAmount") or {}) if isinstance(info, dict) else {}
    if isinstance(token_amount, dict):
        parsed_amount = token_ui_amount_from_fields(token_amount)
        if parsed_amount is not None:
            return parsed_amount
    return 0.0


def is_known_non_wallet(address: str | None) -> bool:
    return not address or address in KNOWN_NON_WALLET_HOLDERS


def account_is_program_owned(account: dict[str, Any] | None) -> bool:
    if not isinstance(account, dict):
        return False
    if account.get("executable") is True:
        return True
    return str(account.get("owner") or "") in KNOWN_NON_WALLET_HOLDERS


def fetch_program_owned_addresses(
    session: requests.Session,
    rpc_url: str,
    addresses: list[str],
    timeout: int,
) -> set[str]:
    program_owned: set[str] = set()
    unique_addresses = [
        address
        for address in dict.fromkeys(addresses)
        if not is_known_non_wallet(address) and is_valid_solana_pubkey(address)
    ]
    for index in range(0, len(unique_addresses), 100):
        chunk = unique_addresses[index : index + 100]
        result = rpc_request(session, rpc_url, "getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}], timeout)
        values = (result or {}).get("value") or []
        if not isinstance(values, list):
            continue
        for address, account in zip(chunk, values, strict=False):
            if account_is_program_owned(account):
                program_owned.add(address)
    return program_owned


def aggregate_real_wallet_holders(
    session: requests.Session,
    rpc_url: str,
    accounts: list[dict[str, Any]],
    timeout: int,
) -> dict[str, float]:
    balances: dict[str, float] = {}
    for account in accounts:
        owner = token_account_owner(account)
        if is_known_non_wallet(owner):
            continue
        amount = token_account_amount(account)
        if owner and amount > 0:
            balances[owner] = balances.get(owner, 0.0) + amount
    program_owned = fetch_program_owned_addresses(session, rpc_url, list(balances), timeout)
    for address in program_owned:
        balances.pop(address, None)
    return balances


def holder_percentages(holder_balances: dict[str, float], supply: float) -> tuple[float | None, float | None, float | None]:
    if supply <= 0 or not holder_balances:
        return None, None, None
    ordered = sorted((amount for amount in holder_balances.values() if amount > 0), reverse=True)
    if not ordered:
        return None, None, None
    top_1 = ordered[0] / supply * 100
    top_10 = sum(ordered[:10]) / supply * 100
    top_20 = sum(ordered[:20]) / supply * 100
    return top_1, top_10, top_20


def fetch_token_supply(session: requests.Session, rpc_url: str, token_address: str, timeout: int) -> float:
    if not is_valid_solana_pubkey(token_address):
        raise ValueError(f"invalid_mint:{safe_pubkey(token_address)}")
    result = rpc_request(session, rpc_url, "getTokenSupply", [token_address], timeout)
    value = (result or {}).get("value") or {}
    supply = token_ui_amount_from_fields(value)
    if supply is None:
        raise ValueError(f"supply_decimals_missing:{safe_pubkey(token_address)}")
    return supply


def fetch_holder_status_helius(
    session: requests.Session,
    helius_url: str,
    rpc_url: str,
    token_address: str,
    timeout: int,
) -> HolderStatus:
    if not is_valid_solana_pubkey(token_address):
        return HolderStatus(
            status="unknown",
            error=f"invalid_mint:{safe_pubkey(token_address)}",
            method="helius_getTokenAccounts",
            result_count=0,
        )
    result = rpc_request(
        session,
        helius_url,
        "getTokenAccounts",
        {"mint": token_address, "page": 1, "limit": 100, "options": {"showZeroBalance": False}},
        timeout,
    )
    accounts = (result or {}).get("token_accounts") or (result or {}).get("items") or []
    if not isinstance(accounts, list) or not accounts:
        return HolderStatus(status="unknown", error="helius_no_holder_accounts", method="helius_getTokenAccounts", result_count=0)
    supply = fetch_token_supply(session, rpc_url, token_address, timeout)
    holder_balances = aggregate_real_wallet_holders(
        session,
        rpc_url,
        [account for account in accounts if isinstance(account, dict)],
        timeout,
    )
    top_1, top_10, top_20 = holder_percentages(holder_balances, supply)
    if top_1 is None or top_10 is None or top_20 is None:
        return HolderStatus(
            status="unknown",
            error="helius_holder_unusable",
            method="helius_getTokenAccounts",
            result_count=len(accounts),
        )
    return HolderStatus(top_1, top_10, top_20, "ok", method="helius_getTokenAccounts", result_count=len(accounts))


def enrich_largest_token_accounts(
    session: requests.Session,
    rpc_url: str,
    largest_accounts: list[dict[str, Any]],
    timeout: int,
) -> list[dict[str, Any]]:
    addresses = [str(account.get("address") or "") for account in largest_accounts if isinstance(account, dict)]
    addresses = [address for address in addresses if is_valid_solana_pubkey(address)]
    if not addresses:
        return []
    result = rpc_request(session, rpc_url, "getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}], timeout)
    values = (result or {}).get("value") or []
    if not isinstance(values, list):
        return []
    enriched: list[dict[str, Any]] = []
    for source, account in zip(largest_accounts, values, strict=False):
        if not isinstance(source, dict) or not isinstance(account, dict):
            continue
        row = dict(source)
        row["account"] = account
        enriched.append(row)
    return enriched


def fetch_holder_status_rpc(
    session: requests.Session,
    rpc_url: str,
    token_address: str,
    timeout: int,
) -> HolderStatus:
    if not is_valid_solana_pubkey(token_address):
        return HolderStatus(
            status="unknown",
            error=f"invalid_mint:{safe_pubkey(token_address)}",
            method="getTokenLargestAccounts",
            result_count=0,
        )
    response = rpc_request_payload(session, rpc_url, "getTokenLargestAccounts", [token_address], timeout)
    result = (response or {}).get("result") or {}
    accounts = result.get("value") or []
    if not isinstance(accounts, list) or not accounts:
        return HolderStatus(status="unknown", error="rpc_no_largest_accounts", method="getTokenLargestAccounts", result_count=0)
    supply = fetch_token_supply(session, rpc_url, token_address, timeout)
    enriched_accounts = enrich_largest_token_accounts(session, rpc_url, accounts, timeout)
    holder_balances = aggregate_real_wallet_holders(session, rpc_url, enriched_accounts, timeout)
    top_1, top_10, top_20 = holder_percentages(holder_balances, supply)
    if top_1 is None or top_10 is None or top_20 is None:
        return HolderStatus(
            status="unknown",
            error="rpc_holder_unusable",
            method="getTokenLargestAccounts",
            result_count=len(accounts),
        )
    return HolderStatus(top_1, top_10, top_20, "ok", method="getTokenLargestAccounts", result_count=len(accounts))


def fetch_holder_status(
    session: requests.Session,
    token_address: str,
    rpc_url: str,
    helius_url: str | None,
    timeout: int,
) -> HolderStatus:
    errors: list[str] = []
    if helius_url:
        try:
            return fetch_holder_status_rpc(session, helius_url, token_address, timeout)
        except Exception as exc:
            errors.append(f"helius_largest:{exc}")
        try:
            return fetch_holder_status_helius(session, helius_url, helius_url, token_address, timeout)
        except Exception as exc:
            errors.append(f"helius_accounts:{exc}")
        if rpc_url != helius_url:
            try:
                return fetch_holder_status_rpc(session, rpc_url, token_address, timeout)
            except Exception as exc:
                errors.append(f"rpc:{exc}")
        return HolderStatus(status="unknown", error=";".join(errors) if errors else "holder_unknown")
    try:
        return fetch_holder_status_rpc(session, rpc_url, token_address, timeout)
    except Exception as exc:
        errors.append(f"rpc:{exc}")
    return HolderStatus(status="unknown", error=";".join(errors) if errors else "holder_unknown")


def first_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def nested_dict(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get(key), dict):
        return value[key]
    return {}


def extract_creator_wallet(asset: dict[str, Any]) -> str | None:
    content = nested_dict(asset, "content")
    metadata = nested_dict(content, "metadata")
    creator_paths = [
        asset.get("creators"),
        metadata.get("creators"),
    ]
    for creators in creator_paths:
        if isinstance(creators, list):
            for creator in creators:
                if isinstance(creator, dict):
                    address = first_string(creator.get("address"))
                    if address:
                        return address
    for key in ("authorities",):
        values = asset.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    address = first_string(item.get("address"))
                    if address:
                        return address
    ownership = asset.get("ownership") or {}
    if isinstance(ownership, dict):
        return first_string(ownership.get("owner"))
    return None


def fetch_helius_asset(
    session: requests.Session,
    helius_url: str,
    token_address: str,
    timeout: int,
) -> dict[str, Any] | None:
    if not is_valid_solana_pubkey(token_address):
        raise ValueError(f"invalid_mint:{safe_pubkey(token_address)}")
    result = rpc_request(session, helius_url, "getAsset", {"id": token_address}, timeout)
    return result if isinstance(result, dict) else None


def account_key_string(value: Any) -> str | None:
    if isinstance(value, str):
        return first_string(value)
    if isinstance(value, dict):
        return first_string(value.get("pubkey"))
    return None


def transaction_account_keys(transaction: dict[str, Any]) -> list[Any]:
    tx = transaction.get("transaction") or {}
    if not isinstance(tx, dict):
        return []
    message = tx.get("message") or {}
    if not isinstance(message, dict):
        return []
    account_keys = message.get("accountKeys") or []
    return account_keys if isinstance(account_keys, list) else []


def fetch_creator_from_mint_history(
    session: requests.Session,
    rpc_url: str,
    token_address: str,
    timeout: int,
) -> tuple[str | None, str | None, str | None]:
    if not is_valid_solana_pubkey(token_address):
        return None, None, f"invalid_mint:{safe_pubkey(token_address)}"
    options = {"limit": 1000, "commitment": "confirmed"}
    signatures = rpc_request(session, rpc_url, "getSignaturesForAddress", [token_address, options], timeout)
    if not isinstance(signatures, list) or not signatures:
        return None, None, "creator_mint_signatures_missing"
    oldest = signatures[-1] if isinstance(signatures[-1], dict) else {}
    signature = first_string(oldest.get("signature"))
    if not signature:
        return None, None, "creator_mint_signature_unusable"
    tx_options = {
        "encoding": "jsonParsed",
        "commitment": "confirmed",
        "maxSupportedTransactionVersion": 0,
    }
    transaction = rpc_request(session, rpc_url, "getTransaction", [signature, tx_options], timeout)
    if not isinstance(transaction, dict):
        return None, signature, "creator_transaction_missing"
    account_keys = transaction_account_keys(transaction)
    if not account_keys:
        return None, signature, "creator_transaction_account_keys_missing"
    wallet = account_key_string(account_keys[0])
    if not wallet:
        return None, signature, "creator_fee_payer_missing"
    return wallet, signature, None


def fetch_sol_balance(session: requests.Session, rpc_url: str, wallet: str, timeout: int) -> float | None:
    if not is_valid_solana_pubkey(wallet):
        raise ValueError(f"invalid_wallet:{safe_pubkey(wallet)}")
    result = rpc_request(session, rpc_url, "getBalance", [wallet, {"commitment": "confirmed"}], timeout)
    value = number((result or {}).get("value"), default=-1.0)
    if value < 0:
        return None
    return value / 1_000_000_000


def fetch_wallet_age_days(session: requests.Session, rpc_url: str, wallet: str, timeout: int) -> float | None:
    if not is_valid_solana_pubkey(wallet):
        raise ValueError(f"invalid_wallet:{safe_pubkey(wallet)}")
    before: str | None = None
    oldest_block_time: int | None = None
    for _ in range(25):
        options: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before:
            options["before"] = before
        result = rpc_request(session, rpc_url, "getSignaturesForAddress", [wallet, options], timeout)
        if not isinstance(result, list) or not result:
            break
        for item in result:
            if isinstance(item, dict) and item.get("blockTime"):
                block_time = int(item["blockTime"])
                oldest_block_time = block_time if oldest_block_time is None else min(oldest_block_time, block_time)
        last_signature = result[-1].get("signature") if isinstance(result[-1], dict) else None
        if not last_signature or len(result) < 1000:
            break
        before = str(last_signature)
    if oldest_block_time is None:
        return None
    return max(0.0, (time.time() - oldest_block_time) / 86_400)


def creator_quality(balance: float | None, age_days: float | None) -> str:
    if balance is None or age_days is None:
        return "unknown"
    if age_days < 1 or balance < 0.1:
        return "risky"
    if age_days < 7:
        return "risky"
    return "good"


def fetch_creator_status(
    session: requests.Session,
    token_address: str,
    rpc_url: str,
    helius_url: str | None,
    timeout: int,
) -> CreatorStatus:
    wallet: str | None = None
    signature: str | None = None
    errors: list[str] = []
    creator_rpc_url = helius_url or rpc_url
    try:
        wallet, signature, error = fetch_creator_from_mint_history(session, creator_rpc_url, token_address, timeout)
        if error:
            errors.append(error)
    except Exception as exc:
        errors.append(f"creator_mint_history:{exc}")
    if not wallet:
        return CreatorStatus(quality="unknown", error=";".join([*errors, "creator_missing"]), signature=signature)
    try:
        balance = fetch_sol_balance(session, creator_rpc_url, wallet, timeout)
    except Exception as exc:
        return CreatorStatus(wallet=wallet, quality="error", error=f"creator_balance:{exc}", signature=signature)
    try:
        age_days = fetch_wallet_age_days(session, creator_rpc_url, wallet, timeout)
    except Exception as exc:
        return CreatorStatus(wallet=wallet, sol_balance=balance, quality="error", error=f"creator_age:{exc}", signature=signature)
    quality = creator_quality(balance, age_days)
    if quality == "unknown":
        errors.append("creator_age_or_balance_unknown")
    return CreatorStatus(
        wallet=wallet,
        sol_balance=balance,
        wallet_age_days=age_days,
        quality=quality,
        error=";".join(errors) if errors else None,
        signature=signature,
    )


def fetch_mint_account(
    session: requests.Session,
    rpc_url: str,
    token_address: str,
    timeout: int,
    retry_backoff: float = RPC_429_BACKOFF_SECONDS,
) -> MintAuthorityStatus:
    if not is_valid_solana_pubkey(token_address):
        return MintAuthorityStatus(None, None, f"invalid_mint:{safe_pubkey(token_address)}")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            token_address,
            {"encoding": "base64", "commitment": "confirmed"},
        ],
    }

    last_error: str | None = None
    for attempt in range(2):
        try:
            response = session.post(rpc_url, json=payload, timeout=timeout)
            if response.status_code == 429:
                last_error = "rpc_429"
                if attempt == 0:
                    time.sleep(retry_backoff)
                    continue
                return MintAuthorityStatus(None, None, last_error)
            response.raise_for_status()
            rpc_payload = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = f"rpc_failed:{exc}"
            return MintAuthorityStatus(None, None, last_error)
    else:
        return MintAuthorityStatus(None, None, last_error or "rpc_failed")

    if rpc_payload.get("error"):
        message = rpc_payload["error"].get("message", "unknown")
        print_rpc_error_debug("getAccountInfo", payload["params"], response.status_code, rpc_payload, rpc_payload.get("error"))
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
        mint_authority=base58_encode(raw[4:36]) if mint_authority_option == 1 else None,
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
            if not is_valid_solana_pubkey(token_address):
                issues.append(f"{source}: invalid_mint_skipped:{safe_pubkey(token_address)}")
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


def fetch_current_price(session: requests.Session, token_address: str, timeout: int) -> float | None:
    pairs = fetch_token_pairs(session, token_address, timeout)
    if not pairs:
        return None
    pairs.sort(key=lambda pair: number((pair.get("liquidity") or {}).get("usd")), reverse=True)
    price_usd = number(pairs[0].get("priceUsd"), default=-1.0)
    if price_usd <= 0:
        return None
    return price_usd


def route_found_in_quote(payload: dict[str, Any]) -> bool:
    route_plan = payload.get("routePlan")
    if isinstance(route_plan, list) and route_plan:
        return True
    return number(payload.get("outAmount")) > 0


def jupiter_price_impact_pct(value: Any) -> float:
    impact = number(value)
    if 0 <= impact <= 1:
        return impact * 100
    return impact


def is_jupiter_no_route(payload: dict[str, Any]) -> bool:
    values = [
        str(payload.get("error") or ""),
        str(payload.get("errorCode") or ""),
        str(payload.get("message") or ""),
    ]
    text = " ".join(values).lower()
    return "route" in text and any(term in text for term in ("not found", "could not", "no "))


def fetch_jupiter_quote(
    session: requests.Session,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    timeout: int,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(max(1, amount_raw)),
        "slippageBps": "500",
    }
    try:
        response = session.get(JUPITER_QUOTE_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return None, f"jupiter_request_failed:{exc}", False

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.ok and isinstance(payload, dict):
        return payload, None, False
    if isinstance(payload, dict) and is_jupiter_no_route(payload):
        return None, "no_route", True
    return None, f"jupiter_http_{response.status_code}", False


def check_sellability(
    session: requests.Session,
    token_address: str,
    timeout: int,
    amount_raw: int = DEFAULT_JUPITER_SELL_AMOUNT_RAW,
) -> SellabilityStatus:
    errors: list[str] = []
    no_route_seen = False

    for output_mint in (SOL_MINT, USDC_MINT):
        if token_address == output_mint:
            continue
        payload, error, no_route = fetch_jupiter_quote(
            session=session,
            input_mint=token_address,
            output_mint=output_mint,
            amount_raw=amount_raw,
            timeout=timeout,
        )
        if payload and route_found_in_quote(payload):
            return SellabilityStatus(
                sellable="yes",
                price_impact_pct=jupiter_price_impact_pct(payload.get("priceImpactPct")),
                route_found=True,
                output_mint=output_mint,
            )
        if no_route:
            no_route_seen = True
            continue
        if error:
            errors.append(error)

    if errors:
        return SellabilityStatus(sellable="unknown", error=";".join(errors))
    if no_route_seen:
        return SellabilityStatus(sellable="no", route_found=False, error="no_route")
    return SellabilityStatus(sellable="unknown", error="no_quote_result")


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
    sellability: SellabilityStatus | None = None,
    safe_min_liquidity_usd: float = SAFE_MIN_LIQUIDITY_USD,
    min_safe_volume_liquidity_ratio: float = MIN_SAFE_VOLUME_LIQUIDITY_RATIO,
    max_safe_volume_liquidity_ratio: float = MAX_SAFE_VOLUME_LIQUIDITY_RATIO,
) -> tuple[str, tuple[str, ...]]:
    danger: list[str] = []
    risk: list[str] = []
    pass_count = 0

    if age_minutes is None:
        risk.append("missing_age")
    elif age_minutes < SAFE_MIN_AGE_MINUTES:
        risk.append("too_new")
    elif age_minutes < DEFAULT_MAX_AGE_HOURS * 60:
        pass_count += 1
    else:
        risk.append("old_pair")

    if liquidity < VERY_LOW_LIQUIDITY_USD:
        danger.append("very_low_liq")
    elif liquidity < LOW_LIQUIDITY_USD:
        risk.append("low_liq")
    elif liquidity >= safe_min_liquidity_usd:
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
    elif volume_liquidity_ratio > max_safe_volume_liquidity_ratio:
        risk.append("high_vol_liq")
    elif volume_liquidity_ratio < min_safe_volume_liquidity_ratio:
        risk.append("low_vol_liq")
    else:
        pass_count += 1

    if price_change_5m <= -70:
        danger.append("crash_5m")
    elif price_change_5m <= MIN_SAFE_PRICE_CHANGE_5M:
        danger.append("drop_5m")
    elif price_change_5m >= EXTREME_PUMP_5M and liquidity < safe_min_liquidity_usd:
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

    if sellability is not None:
        if sellability.sellable == "yes":
            if sellability.price_impact_pct is None:
                risk.append("sellability_unknown")
            elif sellability.price_impact_pct > 40:
                danger.append("sell_impact_extreme")
            elif sellability.price_impact_pct > 15:
                risk.append("sell_impact_too_high")
            else:
                pass_count += 1
        elif sellability.sellable == "no" and not sellability.route_found:
            if age_minutes is not None and age_minutes > DEFAULT_MIN_AGE_MINUTES:
                danger.append("honeypot_no_route")
            else:
                risk.append("honeypot_no_route")
        else:
            risk.append("sellability_unknown")

    if danger:
        return "SCAM_LIKELY", tuple(danger + risk)
    if risk or pass_count < 6:
        return "RISKY", tuple(risk or ["mixed"])
    return "SAFE", ("fresh",)


def arlobit_score(row: PairRow, holder: HolderStatus, creator: CreatorStatus) -> float:
    score = 0.0
    if holder.status == "ok" and holder.top_10_holders_pct is not None and holder.top_10_holders_pct < 30:
        score += 3
    if creator.wallet_age_days is not None and creator.wallet_age_days > 30:
        score += 2
    if row.liquidity >= 100_000:
        score += 2
    if row.age_minutes is not None and 30 <= row.age_minutes <= 6 * 60:
        score += 1
    if row.sellable == "yes" and row.sell_price_impact_pct is not None and row.sell_price_impact_pct <= 15:
        score += 1
    if row.mint_authority_active is False:
        score += 0.5
    if row.freeze_authority_active is False:
        score += 0.5
    return score


def apply_native_edge(row: PairRow, holder: HolderStatus, creator: CreatorStatus) -> PairRow:
    danger: list[str] = []
    risk: list[str] = []

    if holder.status != "ok":
        risk.append("blocked_holder_unknown")
    else:
        if holder.top_1_holder_pct is not None and holder.top_1_holder_pct > 20:
            danger.append("scam_holder")
        if holder.top_10_holders_pct is not None:
            if holder.top_10_holders_pct > 60:
                danger.append("scam_holder")
            elif holder.top_10_holders_pct > 40:
                risk.append("risky_holder")

    if creator.quality in {"unknown", "error"}:
        risk.append("blocked_creator_unknown")
    else:
        if creator.wallet_age_days is not None and creator.wallet_age_days < 1:
            danger.append("scam_creator")
        elif creator.wallet_age_days is not None and creator.wallet_age_days < 7:
            risk.append("creator_age_under_7d")
        if creator.sol_balance is not None and creator.sol_balance < 0.1:
            danger.append("scam_creator")

    if danger:
        verdict = "SCAM_LIKELY"
    elif risk:
        verdict = "RISKY"
    else:
        verdict = row.verdict

    signals = tuple(dict.fromkeys((*row.signals, *danger, *risk)))
    return replace(
        row,
        arlobit_score=arlobit_score(row, holder, creator),
        top_1_holder_pct=holder.top_1_holder_pct,
        top_10_holders_pct=holder.top_10_holders_pct,
        top_20_holders_pct=holder.top_20_holders_pct,
        holder_data_status=holder.status,
        creator_wallet=creator.wallet,
        creator_sol_balance=creator.sol_balance,
        creator_wallet_age_days=creator.wallet_age_days,
        creator_quality=creator.quality,
        verdict=verdict,
        signals=signals,
    )


def to_row(
    pair: dict[str, Any],
    candidate: Candidate,
    now_ms: int,
    mint_status: MintAuthorityStatus,
    safe_min_liquidity_usd: float,
    min_safe_volume_liquidity_ratio: float,
    max_safe_volume_liquidity_ratio: float,
) -> PairRow:
    token = base_token_for_candidate(pair, candidate.token_address)
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume_5m = number((pair.get("volume") or {}).get("m5"))
    price_change_5m = number((pair.get("priceChange") or {}).get("m5"))
    age_minutes = pair_age_minutes(pair, now_ms)
    verdict, signals = score_pair(
        liquidity,
        volume_5m,
        age_minutes,
        price_change_5m,
        mint_status,
        None,
        safe_min_liquidity_usd,
        min_safe_volume_liquidity_ratio,
        max_safe_volume_liquidity_ratio,
    )

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
        sellable="unknown",
        sell_price_impact_pct=None,
        sell_route_found=False,
        sell_check_error=None,
        arlobit_score=0,
        top_1_holder_pct=None,
        top_10_holders_pct=None,
        top_20_holders_pct=None,
        holder_data_status="unknown",
        creator_wallet=None,
        creator_sol_balance=None,
        creator_wallet_age_days=None,
        creator_quality="unknown",
        verdict=verdict,
        signals=signals,
        source=candidate.source,
    )


def apply_sellability(
    row: PairRow,
    sellability: SellabilityStatus,
    mint_status: MintAuthorityStatus,
    safe_min_liquidity_usd: float,
    min_safe_volume_liquidity_ratio: float,
    max_safe_volume_liquidity_ratio: float,
) -> PairRow:
    verdict, signals = score_pair(
        row.liquidity,
        row.volume_5m,
        row.age_minutes,
        row.price_change_5m,
        mint_status,
        sellability,
        safe_min_liquidity_usd,
        min_safe_volume_liquidity_ratio,
        max_safe_volume_liquidity_ratio,
    )
    sellable = sellability.sellable
    if (
        sellability.sellable == "no"
        and not sellability.route_found
        and (row.age_minutes is None or row.age_minutes < DEFAULT_MIN_AGE_MINUTES)
    ):
        sellable = "unknown"
    return replace(
        row,
        sellable=sellable,
        sell_price_impact_pct=sellability.price_impact_pct,
        sell_route_found=sellability.route_found,
        sell_check_error=sellability.error,
        verdict=verdict,
        signals=signals,
    )


def debug_percent(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.4f}"


def print_debug_enrich(mint: str, holder: HolderStatus, creator: CreatorStatus) -> None:
    holder_unknown_reason = holder.error if holder.status != "ok" else "none"
    creator_unknown_reason = creator.error if creator.quality in {"unknown", "error"} else "none"
    print(
        "[debug-enrich] "
        f"mint={mint} "
        f"holder_method={holder.method or 'unknown'} "
        f"holder_status={holder.status} "
        f"holder_result_count={holder.result_count if holder.result_count is not None else 'unknown'} "
        f"top1={debug_percent(holder.top_1_holder_pct)} "
        f"top10={debug_percent(holder.top_10_holders_pct)} "
        f"top20={debug_percent(holder.top_20_holders_pct)} "
        f"holder_unknown_reason={holder_unknown_reason} "
        f"creator_signature={creator.signature or 'unknown'} "
        f"creator_wallet={creator.wallet or 'unknown'} "
        f"creator_age_days={debug_percent(creator.wallet_age_days)} "
        f"creator_quality={creator.quality} "
        f"creator_unknown_reason={creator_unknown_reason}"
    )


def collect_rows(
    session: requests.Session,
    limit: int,
    timeout: int,
    min_age_minutes: int,
    max_age_hours: int,
    candidate_limit: int,
    rpc_url: str,
    safe_min_liquidity_usd: float,
    min_safe_volume_liquidity_ratio: float,
    max_safe_volume_liquidity_ratio: float,
    rpc_min_delay: float,
    rpc_max_delay: float,
    rpc_429_backoff: float,
    helius_url: str | None,
    debug_enrich: bool = False,
) -> tuple[list[PairRow], list[str], int, int]:
    candidates, issues = fetch_candidates(session, timeout)
    now_ms = int(time.time() * 1000)
    seen_pairs: set[str] = set()
    mint_cache: dict[str, MintAuthorityStatus] = {}
    sellability_cache: dict[str, SellabilityStatus] = {}
    holder_cache: dict[str, HolderStatus] = {}
    creator_cache: dict[str, CreatorStatus] = {}
    rows: list[PairRow] = []
    rpc_calls = 0
    debug_enrich_count = 0
    debug_enrich_mints: set[str] = set()

    for candidate in candidates[:candidate_limit]:
        mint_status = mint_cache.get(candidate.token_address)
        if mint_status is None:
            if rpc_calls > 0:
                time.sleep(random.uniform(rpc_min_delay, rpc_max_delay))
            mint_status = fetch_mint_account(
                session,
                rpc_url,
                candidate.token_address,
                timeout,
                rpc_429_backoff,
            )
            rpc_calls += 1
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

            row = to_row(
                pair,
                candidate,
                now_ms,
                mint_status,
                safe_min_liquidity_usd,
                min_safe_volume_liquidity_ratio,
                max_safe_volume_liquidity_ratio,
            )
            if row.age_minutes is None:
                rows.append(row)
                continue
            if min_age_minutes < row.age_minutes < max_age_hours * 60:
                rows.append(row)

    rows.sort(
        key=lambda row: (
            row.verdict != "SAFE",
            row.verdict == "SCAM_LIKELY",
            -row.arlobit_score,
            row.age_minutes is None,
            row.age_minutes or float("inf"),
            -row.volume_5m,
        )
    )
    enriched_rows: list[PairRow] = []
    for row in rows[:limit]:
        mint_status = mint_cache[row.token_address]
        sellability = sellability_cache.get(row.token_address)
        if sellability is None:
            sellability = check_sellability(session, row.token_address, timeout)
            sellability_cache[row.token_address] = sellability
            if sellability.sellable == "unknown" and sellability.error:
                issues.append(f"jupiter:{row.token_address}: {sellability.error}")
        enriched_row = apply_sellability(
            row,
            sellability,
            mint_status,
            safe_min_liquidity_usd,
            min_safe_volume_liquidity_ratio,
            max_safe_volume_liquidity_ratio,
        )

        holder_status = holder_cache.get(row.token_address)
        creator_status = creator_cache.get(row.token_address)
        if holder_status is None or creator_status is None:
            time.sleep(NATIVE_EDGE_CHECK_DELAY_SECONDS)
        if holder_status is None:
            holder_status = fetch_holder_status(
                session,
                row.token_address,
                rpc_url,
                helius_url,
                timeout,
            )
            holder_cache[row.token_address] = holder_status
            if holder_status.status != "ok" and holder_status.error:
                issues.append(f"holders:{row.token_address}: {holder_status.error}")
        if creator_status is None:
            creator_status = fetch_creator_status(
                session,
                row.token_address,
                rpc_url,
                helius_url,
                timeout,
            )
            creator_cache[row.token_address] = creator_status
            if creator_status.quality in {"unknown", "error"} and creator_status.error:
                issues.append(f"creator:{row.token_address}: {creator_status.error}")
        enriched_row = apply_native_edge(enriched_row, holder_status, creator_status)
        if debug_enrich and debug_enrich_count < 3 and row.token_address not in debug_enrich_mints:
            print_debug_enrich(row.token_address, holder_status, creator_status)
            debug_enrich_mints.add(row.token_address)
            debug_enrich_count += 1
        enriched_rows.append(enriched_row)
    rows = enriched_rows
    rows.sort(
        key=lambda row: (
            row.verdict != "SAFE",
            row.verdict == "SCAM_LIKELY",
            -row.arlobit_score,
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


def percent_status(value: Any) -> str:
    if value is None:
        return "unknown"
    return f"{number(value):.2f}%"


def age_days_status(value: Any) -> str:
    if value is None:
        return "unknown"
    return f"{number(value):.1f}d"


def telegram_alert_enabled() -> tuple[bool, str | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, token, chat_id
    return True, token, chat_id


def should_alert(row: PairRow, min_age_minutes: int, max_age_hours: int) -> bool:
    if row.verdict != "SAFE":
        return False
    if row.arlobit_score < 6:
        return False
    if row.holder_data_status != "ok":
        return False
    if row.creator_quality in {"unknown", "error"}:
        return False
    if row.sellable != "yes":
        return False
    if row.sell_price_impact_pct is None or row.sell_price_impact_pct > 15:
        return False
    if row.mint_authority_active is not False or row.freeze_authority_active is not False:
        return False
    if row.age_minutes is None:
        return False
    return min_age_minutes < row.age_minutes < max_age_hours * 60


def empty_blocked_paper_reason_counts() -> dict[str, int]:
    return {reason: 0 for reason in BLOCKED_PAPER_REASONS}


def paper_entry_blocked_reasons(row: PairRow) -> list[str]:
    reasons: list[str] = []
    if row.age_minutes is None or row.age_minutes < SAFE_MIN_AGE_MINUTES:
        reasons.append("too_young")
    if row.liquidity < SAFE_MIN_LIQUIDITY_USD:
        reasons.append("liquidity_too_low")

    volume_liquidity_ratio = row.volume_5m / row.liquidity if row.liquidity > 0 else float("inf")
    if volume_liquidity_ratio > MAX_SAFE_VOLUME_LIQUIDITY_RATIO:
        reasons.append("vol_liq_too_high")
    elif volume_liquidity_ratio < MIN_SAFE_VOLUME_LIQUIDITY_RATIO:
        reasons.append("vol_liq_too_low")
    if row.sellable == "no" and not row.sell_route_found:
        reasons.append("honeypot_no_route")
    elif row.sellable != "yes" and row.sell_check_error:
        reasons.append("sellability_unknown")
    elif row.sell_price_impact_pct is not None and row.sell_price_impact_pct > 15:
        reasons.append("sell_impact_too_high")
    if row.holder_data_status != "ok":
        reasons.append("blocked_holder_unknown")
        reasons.append("blocked_score_unavailable")
    elif row.top_1_holder_pct is not None and row.top_1_holder_pct > 20:
        reasons.append("scam_holder")
    elif row.top_10_holders_pct is not None and row.top_10_holders_pct > 40:
        reasons.append("scam_holder" if row.top_10_holders_pct > 60 else "risky_holder")
    if row.creator_quality in {"unknown", "error"}:
        reasons.append("blocked_creator_unknown")
        reasons.append("blocked_score_unavailable")
    elif row.creator_quality == "risky":
        reasons.append("creator_risky")
        if (
            (row.creator_wallet_age_days is not None and row.creator_wallet_age_days < 1)
            or (row.creator_sol_balance is not None and row.creator_sol_balance < 0.1)
        ):
            reasons.append("scam_creator")
    if row.arlobit_score < 6:
        reasons.append("score_too_low")
    return list(dict.fromkeys(reasons))


def count_blocked_paper_reasons(rows: list[PairRow]) -> dict[str, int]:
    counts = empty_blocked_paper_reason_counts()
    for row in rows:
        for reason in paper_entry_blocked_reasons(row):
            counts[reason] += 1
    return counts


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
            f"Sellable: {row.sellable}",
            f"Sell impact: {percent_status(row.sell_price_impact_pct)}",
            f"ArloBit score: {row.arlobit_score:.1f}",
            f"Top10 holders: {percent_status(row.top_10_holders_pct)}",
            f"Holder data: {row.holder_data_status}",
            f"Creator quality: {row.creator_quality}",
            f"Creator wallet age: {age_days_status(row.creator_wallet_age_days)}",
            f"DexScreener: {row.dex_url}",
            f"Verdict: {row.verdict}",
            f"Signals: {', '.join(row.signals)}",
        ]
    )


def paper_open_message(trade: dict[str, Any]) -> str:
    return "\n".join(
        [
            "ArloBit PAPER trade opened",
            f"Token: {trade.get('symbol', '?')}",
            f"Mint: {trade.get('mint', '?')}",
            f"Entry: {price(number(trade.get('entry_price')))}",
            f"Liquidity: {money(number(trade.get('liquidity_at_entry')))}",
            f"Sellable: {trade.get('sellable', 'unknown')}",
            f"Sell impact: {percent_status(trade.get('sell_price_impact_pct'))}",
            f"Score: {number(trade.get('arlobit_score')):.1f}",
            f"Holder top10: {percent_status(trade.get('top_10_holders_pct'))}",
            f"Creator quality: {trade.get('creator_quality', 'unknown')}",
            f"Source: {trade.get('source', 'unknown')}",
            "Mode: simulated paper trade only",
        ]
    )


def paper_close_message(trade: dict[str, Any]) -> str:
    return "\n".join(
        [
            "ArloBit PAPER trade closed",
            f"Token: {trade.get('symbol', '?')}",
            f"Mint: {trade.get('mint', '?')}",
            f"Exit: {price(number(trade.get('exit_price')))}",
            f"Reason: {trade.get('exit_reason', 'unknown')}",
            f"Final PnL: {number(trade.get('final_pnl_percent')):.2f}%",
            f"Max gain: {number(trade.get('max_gain')):.2f}%",
            f"Max drawdown: {number(trade.get('max_drawdown')):.2f}%",
            "Mode: simulated paper trade only",
        ]
    )


def load_paper_trades() -> dict[str, Any]:
    try:
        with open(PAPER_TRADES_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"trades": []}
    if not isinstance(state, dict) or not isinstance(state.get("trades"), list):
        return {"trades": []}
    return state


def save_paper_trades(state: dict[str, Any]) -> None:
    with open(PAPER_TRADES_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def reset_paper_trades() -> str:
    timestamp_suffix = time.strftime("%Y%m%d_%H%M%S")
    backup_file = f"paper_trades_backup_{timestamp_suffix}.json"
    counter = 1
    while os.path.exists(backup_file):
        backup_file = f"paper_trades_backup_{timestamp_suffix}_{counter}.json"
        counter += 1

    try:
        with open(PAPER_TRADES_FILE, "rb") as source:
            current_data = source.read()
    except FileNotFoundError:
        current_data = json.dumps({"trades": []}, indent=2).encode("utf-8")

    with open(backup_file, "wb") as backup:
        backup.write(current_data)
    save_paper_trades({"trades": []})
    return backup_file


def open_trade_mints(state: dict[str, Any]) -> set[str]:
    return {
        str(trade.get("mint"))
        for trade in state.get("trades", [])
        if isinstance(trade, dict) and trade.get("status") == "open" and trade.get("mint")
    }


def all_trade_mints(state: dict[str, Any]) -> set[str]:
    return {
        str(trade.get("mint"))
        for trade in state.get("trades", [])
        if isinstance(trade, dict) and trade.get("mint")
    }


def recent_paper_entry_count(state: dict[str, Any], now: float, window_seconds: int = 3600) -> int:
    cutoff = now - window_seconds
    return sum(
        1
        for trade in state.get("trades", [])
        if isinstance(trade, dict) and number(trade.get("entry_time"), default=-1.0) >= cutoff
    )


def pnl_percent(entry_price: float, current_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return ((current_price - entry_price) / entry_price) * 100


DANGER_SIGNAL_NAMES = {
    "very_low_liq",
    "vol_liq_anomaly",
    "crash_5m",
    "drop_5m",
    "pump_weak_liq",
    "mint_auth_active",
    "freeze_auth_active",
    "honeypot_no_route",
    "sell_impact_extreme",
    "scam_holder",
    "scam_creator",
}


RISK_SIGNAL_NAMES = {
    "missing_age",
    "too_new",
    "old_pair",
    "low_liq",
    "weak_liq",
    "no_5m_volume",
    "low_5m_volume",
    "high_vol_liq",
    "low_vol_liq",
    "mint_auth_unknown",
    "freeze_auth_unknown",
    "honeypot_no_route",
    "sell_impact_too_high",
    "sellability_unknown",
    "blocked_holder_unknown",
    "blocked_creator_unknown",
    "blocked_score_unavailable",
    "risky_holder",
    "creator_age_under_7d",
    "rpc_429",
    "rpc_failed",
    "rpc_error",
    "mint_account_missing",
    "mint_data_missing",
    "mint_account_too_short",
    "mint_option_unparseable",
    "mint_unparseable",
    "mixed",
}


def split_signal_severity(signals: tuple[str, ...]) -> tuple[list[str], list[str]]:
    danger_signals = [signal for signal in signals if signal in DANGER_SIGNAL_NAMES]
    risk_signals = [signal for signal in signals if signal in RISK_SIGNAL_NAMES]
    return risk_signals, danger_signals


def paper_trade_entry_metadata(row: PairRow, now: float) -> dict[str, Any]:
    volume_liquidity_ratio = row.volume_5m / row.liquidity if row.liquidity > 0 else None
    risk_signals, danger_signals = split_signal_severity(row.signals)
    signal_set_value = "+".join(row.signals) if row.signals else "unknown"
    return {
        "entry_price": row.price,
        "entry_time": now,
        "token_name": row.token,
        "symbol": row.symbol,
        "mint": row.token_address,
        "source": row.source,
        "liquidity_usd": row.liquidity,
        "volume_5m": row.volume_5m,
        "volume_liquidity_ratio": volume_liquidity_ratio,
        "token_age_minutes": row.age_minutes,
        "price_change_5m": row.price_change_5m,
        "sellable": row.sellable,
        "sell_price_impact_pct": row.sell_price_impact_pct,
        "sell_route_found": row.sell_route_found,
        "sell_check_error": row.sell_check_error,
        "arlobit_score": row.arlobit_score,
        "top_1_holder_pct": row.top_1_holder_pct,
        "top_10_holders_pct": row.top_10_holders_pct,
        "top_20_holders_pct": row.top_20_holders_pct,
        "holder_data_status": row.holder_data_status,
        "creator_wallet": row.creator_wallet,
        "creator_sol_balance": row.creator_sol_balance,
        "creator_wallet_age_days": row.creator_wallet_age_days,
        "creator_quality": row.creator_quality,
        "signal_set": signal_set_value,
        "signals": list(row.signals),
        "risk_signals": risk_signals,
        "danger_signals": danger_signals,
        "risk_count": len(risk_signals),
        "danger_count": len(danger_signals),
        "entry_verdict": row.verdict,
    }


def update_open_paper_trades(
    session: requests.Session,
    state: dict[str, Any],
    timeout: int,
) -> tuple[int, list[str], list[str]]:
    now = time.time()
    closed = 0
    issues: list[str] = []
    messages: list[str] = []

    for trade in state.get("trades", []):
        if not isinstance(trade, dict) or trade.get("status") != "open":
            continue

        mint = str(trade.get("mint") or "")
        current_price = fetch_current_price(session, mint, timeout)
        if current_price is None:
            issues.append(f"paper:{mint}: price_unavailable")
            continue

        entry_price = number(trade.get("entry_price"))
        pnl = pnl_percent(entry_price, current_price)
        trade["current_price"] = current_price
        trade["current_pnl_percent"] = pnl
        trade["last_checked"] = now
        trade["max_gain"] = max(number(trade.get("max_gain")), pnl)
        trade["max_drawdown"] = min(number(trade.get("max_drawdown")), pnl)

        entry_time = number(trade.get("entry_time"))
        exit_reason = None
        if pnl >= PAPER_TAKE_PROFIT_PERCENT:
            exit_reason = "take_profit"
        elif pnl <= PAPER_RUG_DROP_PERCENT:
            exit_reason = "rug"
        elif pnl <= PAPER_STOP_LOSS_PERCENT:
            exit_reason = "stop_loss"
        elif entry_time > 0 and now - entry_time >= PAPER_MAX_HOLD_SECONDS:
            exit_reason = "timeout"

        if exit_reason:
            trade["status"] = "closed"
            trade["exit_price"] = current_price
            trade["exit_time"] = now
            trade["exit_reason"] = exit_reason
            trade["final_pnl_percent"] = pnl
            closed += 1
            messages.append(paper_close_message(trade))

    return closed, issues, messages


def merge_blocked_paper_reason_counts(
    existing: dict[str, Any] | None,
    increment: dict[str, int],
) -> dict[str, int]:
    merged = empty_blocked_paper_reason_counts()
    if isinstance(existing, dict):
        for reason in BLOCKED_PAPER_REASONS:
            merged[reason] = int(number(existing.get(reason)))
    for reason, count in increment.items():
        if reason in merged:
            merged[reason] += count
    return merged


def open_paper_trades(
    rows: list[PairRow],
    min_age_minutes: int,
    max_age_hours: int,
) -> tuple[int, list[str], dict[str, int]]:
    state = load_paper_trades()
    existing_mints = all_trade_mints(state)
    blocked_reason_counts = count_blocked_paper_reasons(rows)
    opened = 0
    messages: list[str] = []
    now = time.time()
    recent_entries = recent_paper_entry_count(state, now)

    for row in rows:
        if not should_alert(row, min_age_minutes, max_age_hours):
            continue
        blocked_reasons = paper_entry_blocked_reasons(row)
        if blocked_reasons:
            continue
        if recent_entries + opened >= MAX_PAPER_ENTRIES_PER_HOUR:
            blocked_reason_counts["paper_entry_hourly_limit"] += 1
            continue
        if row.token_address in existing_mints:
            continue

        trade = {
            **paper_trade_entry_metadata(row, now),
            "liquidity_at_entry": row.liquidity,
            "volume_5m_at_entry": row.volume_5m,
            "age_minutes_at_entry": row.age_minutes,
            "volume_liquidity_ratio_at_entry": row.volume_5m / row.liquidity if row.liquidity > 0 else None,
            "verdict": row.verdict,
            "source": row.source,
            "status": "open",
            "current_price": row.price,
            "current_pnl_percent": 0.0,
            "max_gain": 0.0,
            "max_drawdown": 0.0,
            "dex_url": row.dex_url,
        }
        state["trades"].append(trade)
        existing_mints.add(row.token_address)
        opened += 1
        messages.append(paper_open_message(trade))

    state["blocked_paper_reason_counts"] = merge_blocked_paper_reason_counts(
        state.get("blocked_paper_reason_counts"),
        blocked_reason_counts,
    )
    if opened or any(blocked_reason_counts.values()):
        save_paper_trades(state)
    return opened, messages, blocked_reason_counts


def update_and_save_paper_trades(
    session: requests.Session,
    timeout: int,
) -> tuple[int, list[str], list[str]]:
    state = load_paper_trades()
    closed, issues, messages = update_open_paper_trades(session, state, timeout)
    if state.get("trades"):
        save_paper_trades(state)
    return closed, issues, messages


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
    hourly_limit: int,
) -> tuple[int, list[str]]:
    enabled, token, chat_id = telegram_alert_enabled()
    if not enabled:
        print("Telegram disabled: missing env vars")
        return 0, []

    endpoint = TELEGRAM_API_URL.format(token=token)
    now = time.time()
    state = prune_telegram_state(load_telegram_state(), now)
    remaining = max(0, hourly_limit - len(state["sent_at"]))
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


def send_telegram_messages(
    session: requests.Session,
    messages: list[str],
    timeout: int,
    hourly_limit: int,
) -> tuple[int, list[str]]:
    enabled, token, chat_id = telegram_alert_enabled()
    if not enabled or not messages:
        return 0, []

    endpoint = TELEGRAM_API_URL.format(token=token)
    state = prune_telegram_state(load_telegram_state(), time.time())
    remaining = max(0, hourly_limit - len(state["sent_at"]))
    sent = 0
    issues: list[str] = []

    for message in messages:
        if sent >= remaining:
            break
        try:
            response = session.post(
                endpoint,
                json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
                timeout=timeout,
            )
            response.raise_for_status()
            sent += 1
            state["sent_at"].append(time.time())
        except requests.RequestException as exc:
            issues.append(f"telegram:paper: {exc}")

    if sent:
        save_telegram_state(state)
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
        "sellable",
        "sell_impact",
        "score",
        "top1_pct",
        "top10_pct",
        "creator_quality",
        "holder_status",
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
            row.sellable,
            percent_status(row.sell_price_impact_pct),
            f"{row.arlobit_score:.1f}",
            percent_status(row.top_1_holder_pct),
            percent_status(row.top_10_holders_pct),
            row.creator_quality,
            row.holder_data_status,
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


def normalized_exit_reason(trade: dict[str, Any]) -> str:
    reason = str(trade.get("exit_reason") or "other")
    if reason == "max_hold_time":
        return "timeout"
    if reason in {"take_profit", "stop_loss", "rug", "timeout"}:
        return reason
    return "other"


def trade_pnl(trade: dict[str, Any]) -> float:
    return number(trade.get("final_pnl_percent"))


def trade_hold_seconds(trade: dict[str, Any]) -> float | None:
    entry_time = number(trade.get("entry_time"), default=-1.0)
    exit_time = number(trade.get("exit_time"), default=-1.0)
    if entry_time <= 0 or exit_time <= 0 or exit_time < entry_time:
        return None
    return exit_time - entry_time


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def signal_set(trade: dict[str, Any]) -> str:
    value = str(trade.get("signal_set") or "").strip()
    return value if value else "unknown"


def liquidity_bucket(trade: dict[str, Any]) -> str:
    liquidity = number(trade.get("liquidity_usd"), default=-1.0)
    if liquidity < 0:
        return "unknown"
    if liquidity < 30_000:
        return "<$30k"
    if liquidity < 50_000:
        return "$30k-$50k"
    if liquidity < 100_000:
        return "$50k-$100k"
    if liquidity < 250_000:
        return "$100k-$250k"
    return ">=$250k"


def token_age_bucket(trade: dict[str, Any]) -> str:
    age_minutes = number(trade.get("token_age_minutes"), default=-1.0)
    if age_minutes < 0:
        return "unknown"
    if age_minutes < 10:
        return "<10m"
    if age_minutes < 30:
        return "10m-30m"
    if age_minutes < 60:
        return "30m-1h"
    if age_minutes < 6 * 60:
        return "1h-6h"
    if age_minutes < 24 * 60:
        return "6h-24h"
    return ">=24h"


def volume_liquidity_ratio_bucket(trade: dict[str, Any]) -> str:
    ratio = number(trade.get("volume_liquidity_ratio"), default=-1.0)
    if ratio < 0:
        return "unknown"
    if ratio < 0.01:
        return "<0.01"
    if ratio < 0.05:
        return "0.01-0.05"
    if ratio < 0.10:
        return "0.05-0.10"
    if ratio < 0.25:
        return "0.10-0.25"
    if ratio < 0.50:
        return "0.25-0.50"
    if ratio < 1.00:
        return "0.50-1.00"
    if ratio < 2.00:
        return "1.00-2.00"
    return ">=2.00"


def score_bucket(trade: dict[str, Any]) -> str:
    score = number(trade.get("arlobit_score"))
    if score < 4:
        return "0-4"
    if score < 6:
        return "4-6"
    if score < 8:
        return "6-8"
    return "8+"


def holder_concentration_bucket(trade: dict[str, Any]) -> str:
    top10 = number(trade.get("top_10_holders_pct"), default=-1.0)
    if top10 < 0:
        return "unknown"
    if top10 < 30:
        return "<30%"
    if top10 <= 40:
        return "30-40%"
    if top10 <= 60:
        return "40-60%"
    return ">60%"


def trade_creator_quality(trade: dict[str, Any]) -> str:
    quality = str(trade.get("creator_quality") or "unknown").strip()
    return quality if quality else "unknown"


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, float]:
    count = len(trades)
    pnls = [trade_pnl(trade) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    hold_times = [hold for trade in trades if (hold := trade_hold_seconds(trade)) is not None]
    return {
        "count": float(count),
        "pnl": sum(pnls),
        "wins": float(len(wins)),
        "win_rate": (len(wins) / count * 100) if count else 0.0,
        "avg_pnl": (sum(pnls) / count) if count else 0.0,
        "avg_hold": (sum(hold_times) / len(hold_times)) if hold_times else -1.0,
    }


def grouped_trade_summaries(
    trades: list[dict[str, Any]],
    key_fn: Any,
) -> list[tuple[str, dict[str, float]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        groups.setdefault(key_fn(trade), []).append(trade)
    return sorted(
        ((name, summarize_trades(group)) for name, group in groups.items()),
        key=lambda item: (item[1]["pnl"], item[0]),
    )


def render_summary_table(title: str, rows: list[tuple[str, dict[str, float]]]) -> list[str]:
    lines = [title]
    if not rows:
        return lines + ["  n/a"]
    name_width = max(len("bucket"), *(len(name) for name, _ in rows))
    lines.append(f"  {'bucket'.ljust(name_width)} | trades | win rate | pnl | avg pnl | avg hold")
    lines.append(f"  {'-' * name_width}-+--------+----------+-----+---------+---------")
    for name, summary in rows:
        avg_hold = None if summary["avg_hold"] < 0 else summary["avg_hold"]
        lines.append(
            "  "
            f"{name.ljust(name_width)} | "
            f"{int(summary['count']):>6} | "
            f"{summary['win_rate']:>7.2f}% | "
            f"{summary['pnl']:>6.2f}% | "
            f"{summary['avg_pnl']:>7.2f}% | "
            f"{format_duration(avg_hold):>7}"
        )
    return lines


def render_trade_rankings(title: str, trades: list[dict[str, Any]]) -> list[str]:
    lines = [title]
    if not trades:
        return lines + ["  n/a"]
    lines.append("  pnl      | hold      | exit        | source   | symbol       | mint")
    lines.append("  ---------+-----------+-------------+----------+--------------+-------------")
    for trade in trades:
        symbol = terminal_text(trade.get("symbol") or "?", 12)
        mint = terminal_text(trade.get("mint") or "", 12)
        source = terminal_text(trade.get("source") or "unknown", 8)
        reason = terminal_text(normalized_exit_reason(trade), 11)
        lines.append(
            "  "
            f"{trade_pnl(trade):>7.2f}% | "
            f"{format_duration(trade_hold_seconds(trade)):>9} | "
            f"{reason:<11} | "
            f"{source:<8} | "
            f"{symbol:<12} | "
            f"{mint}"
        )
    return lines


def export_paper_trades_csv(path: str = PAPER_TRADES_CSV_FILE) -> int:
    trades = [trade for trade in load_paper_trades().get("trades", []) if isinstance(trade, dict)]
    preferred_columns = [
        "status",
        "token_name",
        "symbol",
        "mint",
        "source",
        "entry_verdict",
        "signal_set",
        "signals",
        "risk_signals",
        "danger_signals",
        "risk_count",
        "danger_count",
        "entry_time",
        "exit_time",
        "hold_seconds",
        "entry_price",
        "exit_price",
        "current_price",
        "final_pnl_percent",
        "current_pnl_percent",
        "max_gain",
        "max_drawdown",
        "exit_reason",
        "liquidity_usd",
        "volume_5m",
        "volume_liquidity_ratio",
        "token_age_minutes",
        "price_change_5m",
        "sellable",
        "sell_price_impact_pct",
        "sell_route_found",
        "sell_check_error",
        "arlobit_score",
        "top_1_holder_pct",
        "top_10_holders_pct",
        "top_20_holders_pct",
        "holder_data_status",
        "creator_wallet",
        "creator_sol_balance",
        "creator_wallet_age_days",
        "creator_quality",
        "verdict",
        "liquidity_at_entry",
        "volume_5m_at_entry",
        "age_minutes_at_entry",
        "volume_liquidity_ratio_at_entry",
        "dex_url",
    ]
    extra_columns = sorted({key for trade in trades for key in trade.keys()} - set(preferred_columns))
    columns = preferred_columns + extra_columns

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for trade in trades:
            row = dict(trade)
            for list_field in ("signals", "risk_signals", "danger_signals"):
                if isinstance(row.get(list_field), list | tuple):
                    row[list_field] = ",".join(str(signal) for signal in row[list_field])
            hold_seconds = trade_hold_seconds(trade)
            row["hold_seconds"] = "" if hold_seconds is None else f"{hold_seconds:.0f}"
            writer.writerow(row)

    return len(trades)


def render_paper_stats() -> str:
    state = load_paper_trades()
    trades = [trade for trade in state.get("trades", []) if isinstance(trade, dict)]
    blocked_counts = merge_blocked_paper_reason_counts(
        state.get("blocked_paper_reason_counts"),
        empty_blocked_paper_reason_counts(),
    )
    total = len(trades)
    open_count = sum(1 for trade in trades if trade.get("status") == "open")
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    wins = [number(trade.get("final_pnl_percent")) for trade in closed if number(trade.get("final_pnl_percent")) > 0]
    losses = [number(trade.get("final_pnl_percent")) for trade in closed if number(trade.get("final_pnl_percent")) <= 0]
    closed_count = len(closed)
    final_pnls = [number(trade.get("final_pnl_percent")) for trade in closed]

    win_rate = (len(wins) / closed_count * 100) if closed_count else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    best = max(final_pnls) if final_pnls else 0.0
    worst = min(final_pnls) if final_pnls else 0.0
    total_pnl = sum(final_pnls)
    hold_times = [hold for trade in closed if (hold := trade_hold_seconds(trade)) is not None]
    avg_hold_time = (sum(hold_times) / len(hold_times)) if hold_times else None

    worst_trades = sorted(closed, key=trade_pnl)[:10]
    best_trades = sorted(closed, key=trade_pnl, reverse=True)[:10]

    lines = [
        "Paper trading stats",
        f"total trades: {total}",
        f"open: {open_count}",
        f"closed: {closed_count}",
        f"win rate: {win_rate:.2f}%",
        f"avg win: {avg_win:.2f}%",
        f"avg loss: {avg_loss:.2f}%",
        f"average hold time: {format_duration(avg_hold_time)}",
        f"best: {best:.2f}%",
        f"worst: {worst:.2f}%",
        f"total simulated pnl: {total_pnl:.2f}%",
        "blocked_paper_reason counts:",
        *[f"  {reason}: {blocked_counts[reason]}" for reason in BLOCKED_PAPER_REASONS],
        "",
        *render_summary_table("PnL by exit_reason", grouped_trade_summaries(closed, normalized_exit_reason)),
        "",
        *render_trade_rankings("Worst 10 trades", worst_trades),
        "",
        *render_trade_rankings("Best 10 trades", best_trades),
        "",
        *render_summary_table("PnL by signal set", grouped_trade_summaries(closed, signal_set)),
        "",
        *render_summary_table("PnL by liquidity bucket", grouped_trade_summaries(closed, liquidity_bucket)),
        "",
        *render_summary_table("PnL by token age bucket", grouped_trade_summaries(closed, token_age_bucket)),
        "",
        *render_summary_table(
            "PnL by volume/liquidity ratio bucket",
            grouped_trade_summaries(closed, volume_liquidity_ratio_bucket),
        ),
        "",
        *render_summary_table("PnL by score bucket", grouped_trade_summaries(closed, score_bucket)),
        "",
        *render_summary_table(
            "PnL by holder concentration bucket",
            grouped_trade_summaries(closed, holder_concentration_bucket),
        ),
        "",
        *render_summary_table("PnL by creator_quality", grouped_trade_summaries(closed, trade_creator_quality)),
    ]
    return "\n".join(lines)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan fresh Solana pairs from DexScreener.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one scan cycle and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously until Ctrl+C")
    mode.add_argument("--stats", action="store_true", help="show paper trading stats and exit")
    mode.add_argument("--export-trades-csv", action="store_true", help="export paper_trades.json to paper_trades.csv")
    mode.add_argument("--reset-paper", action="store_true", help="back up paper_trades.json and reset paper trades")
    mode.add_argument(
        "--test-paper-alert",
        action="store_true",
        help="send one Telegram paper-trade test message and exit",
    )
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
    parser.add_argument(
        "--safe-min-liquidity-usd",
        type=positive_float,
        default=positive_float(os.environ.get("SAFE_MIN_LIQUIDITY_USD", str(SAFE_MIN_LIQUIDITY_USD))),
        help="minimum liquidity in USD required for SAFE",
    )
    parser.add_argument(
        "--min-safe-volume-liquidity-ratio",
        type=nonnegative_float,
        default=nonnegative_float(
            os.environ.get("MIN_SAFE_VOLUME_LIQUIDITY_RATIO", str(MIN_SAFE_VOLUME_LIQUIDITY_RATIO))
        ),
        help="minimum volume_5m/liquidity_usd ratio required for SAFE",
    )
    parser.add_argument(
        "--max-safe-volume-liquidity-ratio",
        type=positive_float,
        default=positive_float(
            os.environ.get("MAX_SAFE_VOLUME_LIQUIDITY_RATIO", str(MAX_SAFE_VOLUME_LIQUIDITY_RATIO))
        ),
        help="maximum volume_5m/liquidity_usd ratio allowed for SAFE",
    )
    parser.add_argument(
        "--rpc-min-delay",
        type=nonnegative_float,
        default=nonnegative_float(
            os.environ.get("RPC_GET_ACCOUNT_INFO_MIN_DELAY_SECONDS", str(RPC_GET_ACCOUNT_INFO_MIN_DELAY_SECONDS))
        ),
        help="minimum delay between Solana getAccountInfo RPC calls",
    )
    parser.add_argument(
        "--rpc-max-delay",
        type=nonnegative_float,
        default=nonnegative_float(
            os.environ.get("RPC_GET_ACCOUNT_INFO_MAX_DELAY_SECONDS", str(RPC_GET_ACCOUNT_INFO_MAX_DELAY_SECONDS))
        ),
        help="maximum delay between Solana getAccountInfo RPC calls",
    )
    parser.add_argument(
        "--rpc-429-backoff",
        type=nonnegative_float,
        default=nonnegative_float(os.environ.get("RPC_429_BACKOFF_SECONDS", str(RPC_429_BACKOFF_SECONDS))),
        help="backoff before one retry after Solana RPC 429",
    )
    parser.add_argument(
        "--telegram-limit",
        type=positive_int,
        default=positive_int(os.environ.get("TELEGRAM_ALERT_LIMIT_PER_HOUR", str(DEFAULT_TELEGRAM_ALERT_LIMIT_PER_HOUR))),
        help="maximum Telegram messages per hour across alerts and paper trade messages",
    )
    parser.add_argument(
        "--debug-enrich",
        action="store_true",
        help="print sanitized holder/creator enrichment details for the first 3 candidates",
    )
    return parser.parse_args()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "ArloBit/0.9.4"})
    return session


def timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def print_health(result: ScanResult) -> None:
    error_count = len(result.issues)
    blocked_counts = " ".join(
        f"blocked_paper_{reason}={result.blocked_paper_reason_counts.get(reason, 0)}"
        for reason in BLOCKED_PAPER_REASONS
    )
    print(
        "Health: "
        f"pairs_scanned={result.pairs_scanned} "
        f"candidates_found={result.candidate_count} "
        f"safe_count={result.safe_count} "
        f"alerts_sent={result.alerts_sent} "
        f"paper_opened={result.paper_opened} "
        f"paper_closed={result.paper_closed} "
        f"{blocked_counts} "
        f"api_rpc_errors={error_count}"
    )


def run_scan_once(args: argparse.Namespace) -> ScanResult:
    session = build_session()
    paper_closed, paper_issues, paper_close_messages = update_and_save_paper_trades(session, args.timeout)
    paper_close_alerts, paper_close_alert_issues = send_telegram_messages(
        session=session,
        messages=paper_close_messages,
        timeout=args.timeout,
        hourly_limit=args.telegram_limit,
    )

    rows, issues, candidate_count, pairs_scanned = collect_rows(
        session=session,
        limit=args.limit,
        timeout=args.timeout,
        min_age_minutes=args.min_age_minutes,
        max_age_hours=args.max_age_hours,
        candidate_limit=args.candidate_limit,
        rpc_url=args.rpc_url,
        safe_min_liquidity_usd=args.safe_min_liquidity_usd,
        min_safe_volume_liquidity_ratio=args.min_safe_volume_liquidity_ratio,
        max_safe_volume_liquidity_ratio=args.max_safe_volume_liquidity_ratio,
        rpc_min_delay=args.rpc_min_delay,
        rpc_max_delay=args.rpc_max_delay,
        rpc_429_backoff=args.rpc_429_backoff,
        helius_url=helius_rpc_url(os.environ.get("HELIUS_API_KEY")),
        debug_enrich=args.debug_enrich,
    )

    print(render_table(rows))
    print(f"\nScanned {candidate_count} latest Solana profile/boost candidates.")
    alerts_sent, telegram_issues = send_telegram_alerts(
        session=session,
        rows=rows,
        timeout=args.timeout,
        min_age_minutes=args.min_age_minutes,
        max_age_hours=args.max_age_hours,
        hourly_limit=args.telegram_limit,
    )
    issues.extend(telegram_issues)
    paper_opened, paper_open_messages, blocked_paper_reason_counts = open_paper_trades(
        rows,
        args.min_age_minutes,
        args.max_age_hours,
    )
    paper_open_alerts, paper_open_alert_issues = send_telegram_messages(
        session=session,
        messages=paper_open_messages,
        timeout=args.timeout,
        hourly_limit=args.telegram_limit,
    )
    issues.extend(paper_issues)
    issues.extend(paper_close_alert_issues)
    issues.extend(paper_open_alert_issues)
    paper_alerts_sent = paper_close_alerts + paper_open_alerts
    if paper_opened or paper_closed:
        print(f"Paper trades: opened={paper_opened} closed={paper_closed} paper_alerts_sent={paper_alerts_sent}")
    result = ScanResult(
        rows=rows,
        issues=issues,
        candidate_count=candidate_count,
        pairs_scanned=pairs_scanned,
        safe_count=sum(1 for row in rows if row.verdict == "SAFE"),
        alerts_sent=alerts_sent + paper_alerts_sent,
        paper_opened=paper_opened,
        paper_closed=paper_closed,
        blocked_paper_reason_counts=blocked_paper_reason_counts,
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


def run_test_paper_alert(args: argparse.Namespace) -> int:
    session = build_session()
    message = "\n".join(
        [
            "ArloBit PAPER trade test alert",
            "Mode: simulated paper trade only",
            "No trade was opened or executed.",
        ]
    )
    sent, issues = send_telegram_messages(
        session=session,
        messages=[message],
        timeout=args.timeout,
        hourly_limit=args.telegram_limit,
    )
    print(f"Telegram paper test alerts sent: {sent}")
    if issues:
        print("Telegram test issues:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    if sent != 1:
        print("Telegram paper test alert was not sent", file=sys.stderr)
        return 1
    return 0


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
    print_startup_env_status()
    args = parse_args()
    if args.min_age_minutes >= args.max_age_hours * 60:
        print("--min-age-minutes must be lower than --max-age-hours", file=sys.stderr)
        return 2
    if args.cycles and not args.loop:
        print("--cycles can only be used with --loop", file=sys.stderr)
        return 2
    if args.min_safe_volume_liquidity_ratio > args.max_safe_volume_liquidity_ratio:
        print("--min-safe-volume-liquidity-ratio must be <= --max-safe-volume-liquidity-ratio", file=sys.stderr)
        return 2
    if args.rpc_min_delay > args.rpc_max_delay:
        print("--rpc-min-delay must be <= --rpc-max-delay", file=sys.stderr)
        return 2

    if args.loop:
        return run_loop(args)
    if args.stats:
        print(render_paper_stats())
        return 0
    if args.export_trades_csv:
        exported = export_paper_trades_csv()
        print(f"Exported {exported} paper trades to {PAPER_TRADES_CSV_FILE}")
        return 0
    if args.reset_paper:
        backup_file = reset_paper_trades()
        print(f"Backed up paper trades to {backup_file}")
        print(f"Reset {PAPER_TRADES_FILE} to an empty paper trading file")
        return 0
    if args.test_paper_alert:
        return run_test_paper_alert(args)

    run_scan_once(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
