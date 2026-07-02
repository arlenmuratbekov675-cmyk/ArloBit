"""Paper trade persistence: SQLite-backed, with a JSON fallback.

Paper trades and the price ticks of open trades live in data/arlobit.db
(db_paper_trades, trade_ticks) instead of paper_trades.json. Trade identity
is (mint, entry_time) -- the same dedupe key analyze_trades.py already uses
for the historical JSON backups. Each row keeps the full trade dict
(payload_json) alongside a handful of structured columns, so existing
stats/CSV code that reads arbitrary trade fields keeps working unchanged.

This module never changes entry/exit rules -- it only changes where the
same trade dict is read from and written to. If the DB can't be opened for
any reason, every function here falls back to the JSON file, so the
scanner never depends on SQLite to run.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import time
from typing import Any

from arlobit import db

JSON_TRADES_FILE = "paper_trades.json"


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def research_enabled() -> bool:
    return os.environ.get("ARLOBIT_RESEARCH", "1") != "0"


def db_available() -> bool:
    if not research_enabled():
        return False
    try:
        conn = db.connect()
        conn.close()
        return True
    except Exception:
        return False


def trade_identity_key(trade: dict[str, Any]) -> tuple[str, float]:
    return (str(trade.get("mint")), _num(trade.get("entry_time")) or 0.0)


def dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge trades sharing (mint, entry_time): closed beats open, then the
    most recently checked snapshot wins. Mirrors analyze_trades.load_all_trades."""
    seen: dict[tuple[str, float], dict[str, Any]] = {}
    for trade in trades:
        if not isinstance(trade, dict) or not trade.get("mint"):
            continue
        key = trade_identity_key(trade)
        existing = seen.get(key)
        if existing is None:
            seen[key] = trade
            continue
        existing_closed = existing.get("status") == "closed"
        new_closed = trade.get("status") == "closed"
        if new_closed and not existing_closed:
            seen[key] = trade
        elif new_closed == existing_closed and (_num(trade.get("last_checked")) or 0) >= (
            _num(existing.get("last_checked")) or 0
        ):
            seen[key] = trade
    return list(seen.values())


# --------------------------------------------------------------------------
# JSON (fallback store + export format)
# --------------------------------------------------------------------------


def load_json_state(path: str = JSON_TRADES_FILE) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"trades": []}
    if not isinstance(state, dict) or not isinstance(state.get("trades"), list):
        return {"trades": []}
    return state


def save_json_state(state: dict[str, Any], path: str = JSON_TRADES_FILE) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# SQLite store
# --------------------------------------------------------------------------


def _row_to_trade(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    raw = row["payload_json"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    # structured columns are authoritative over a possibly-stale payload snapshot
    payload["mint"] = row["mint"]
    payload["sighting_id"] = row["sighting_id"]
    payload["entry_time"] = row["entry_time"]
    payload["entry_price"] = row["entry_price"]
    payload["exit_time"] = row["exit_time"]
    payload["exit_price"] = row["exit_price"]
    payload["exit_reason"] = row["exit_reason"]
    if row["final_pnl_pct"] is not None:
        payload["final_pnl_percent"] = row["final_pnl_pct"]
    if row["max_gain_pct"] is not None:
        payload["max_gain"] = row["max_gain_pct"]
    if row["max_drawdown_pct"] is not None:
        payload["max_drawdown"] = row["max_drawdown_pct"]
    payload["status"] = row["status"]
    payload["_trade_id"] = row["trade_id"]
    return payload


def load_db_state(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or db.connect()
    conn.row_factory = sqlite3.Row
    try:
        trades = [
            _row_to_trade(row)
            for row in conn.execute("SELECT * FROM db_paper_trades ORDER BY trade_id")
        ]
        meta_row = conn.execute(
            "SELECT value FROM paper_trade_meta WHERE key='blocked_paper_reason_counts'"
        ).fetchone()
        blocked = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
        return {"trades": trades, "blocked_paper_reason_counts": blocked}
    finally:
        if own:
            conn.close()


def save_db_state(state: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        for trade in state.get("trades", []):
            if not isinstance(trade, dict) or not trade.get("mint"):
                continue
            entry_time = _num(trade.get("entry_time")) or 0.0
            payload = json.dumps(trade, sort_keys=True, default=str)
            cursor = conn.execute(
                "INSERT INTO db_paper_trades (mint, sighting_id, entry_time, entry_price,"
                " exit_time, exit_price, exit_reason, final_pnl_pct, max_gain_pct,"
                " max_drawdown_pct, status, payload_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(mint, entry_time) DO UPDATE SET"
                " sighting_id=COALESCE(excluded.sighting_id, sighting_id),"
                " exit_time=excluded.exit_time, exit_price=excluded.exit_price,"
                " exit_reason=excluded.exit_reason, final_pnl_pct=excluded.final_pnl_pct,"
                " max_gain_pct=excluded.max_gain_pct, max_drawdown_pct=excluded.max_drawdown_pct,"
                " status=excluded.status, payload_json=excluded.payload_json"
                " RETURNING trade_id",
                (
                    trade.get("mint"),
                    trade.get("sighting_id"),
                    entry_time,
                    _num(trade.get("entry_price")),
                    _num(trade.get("exit_time")),
                    _num(trade.get("exit_price")),
                    trade.get("exit_reason"),
                    _num(trade.get("final_pnl_percent")),
                    _num(trade.get("max_gain")),
                    _num(trade.get("max_drawdown")),
                    trade.get("status"),
                    payload,
                ),
            )
            trade_id = cursor.fetchone()[0]
            if trade.get("status") == "open":
                ts = _num(trade.get("last_checked")) or entry_time
                current_price = _num(trade.get("current_price"))
                liquidity = trade.get("last_liquidity_usd")
                if liquidity is None:
                    liquidity = trade.get("liquidity_usd")
                if ts and current_price is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO trade_ticks (trade_id, ts, price_usd, liquidity_usd)"
                        " VALUES (?,?,?,?)",
                        (trade_id, ts, current_price, _num(liquidity)),
                    )
        blocked = state.get("blocked_paper_reason_counts")
        if isinstance(blocked, dict):
            conn.execute(
                "INSERT INTO paper_trade_meta (key, value) VALUES ('blocked_paper_reason_counts', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(blocked),),
            )
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_db_state() -> None:
    conn = db.connect()
    try:
        conn.execute("DELETE FROM trade_ticks")
        conn.execute("DELETE FROM db_paper_trades")
        conn.execute("DELETE FROM paper_trade_meta WHERE key='blocked_paper_reason_counts'")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# unified entry points used by scanner_v0.py
# --------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    if db_available():
        try:
            return load_db_state()
        except Exception:
            pass
    return load_json_state()


def save_state(state: dict[str, Any]) -> None:
    if db_available():
        try:
            save_db_state(state)
            return
        except Exception:
            pass
    save_json_state(state)


def export_json(path: str = JSON_TRADES_FILE) -> int:
    """Backward-compatible export: dump the live store (DB or JSON) to a
    plain paper_trades.json-shaped file."""
    state = load_state()
    save_json_state(state, path)
    return len(state.get("trades", []))


def _next_backup_path() -> str:
    timestamp_suffix = time.strftime("%Y%m%d_%H%M%S")
    backup_file = f"paper_trades_backup_{timestamp_suffix}.json"
    counter = 1
    while os.path.exists(backup_file):
        backup_file = f"paper_trades_backup_{timestamp_suffix}_{counter}.json"
        counter += 1
    return backup_file


def reset_state() -> str:
    """Archive the live store to a timestamped JSON backup, then clear it
    (DB rows deleted when available, JSON file emptied either way)."""
    state = load_state()
    backup_file = _next_backup_path()
    save_json_state(state, backup_file)
    if db_available():
        try:
            reset_db_state()
        except Exception:
            pass
    save_json_state({"trades": []})
    return backup_file


# --------------------------------------------------------------------------
# one-time historical import (merge paper_trades*.json backups into SQLite)
# --------------------------------------------------------------------------


def import_json_backups(pattern: str = "paper_trades*.json") -> tuple[int, int]:
    """Merge every paper_trades*.json backup, dedupe, and upsert into SQLite.
    Returns (files_read, unique_trades_imported)."""
    paths = sorted(glob.glob(pattern))
    all_trades: list[dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        trades = payload.get("trades", []) if isinstance(payload, dict) else []
        all_trades.extend(t for t in trades if isinstance(t, dict))
    unique = dedupe_trades(all_trades)
    save_db_state({"trades": unique})
    return len(paths), len(unique)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import historical paper_trades*.json backups into SQLite (research only, never trades)"
    )
    parser.add_argument("--pattern", default="paper_trades*.json")
    args = parser.parse_args(argv)
    files, count = import_json_backups(args.pattern)
    print(f"imported {count} unique trades from {files} files into {db.db_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
