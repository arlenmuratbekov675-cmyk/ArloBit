"""Axiom data-source research audit.

This module deliberately avoids private frontend endpoints and reverse
engineering. It records whether Axiom currently exposes a supported machine
interface for research collection, and reports quality for any future supported
Axiom signals stored in SQLite.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from typing import Any

from arlobit import db

SOURCE_NAME = "axiom"
DOCS_URL = "https://docs.axiom.trade/"
WALLET_TRACKING_URL = "https://docs.axiom.trade/wallet-tracking/monitor-wallets"
TRADER_SCAN_URL = "https://docs.axiom.trade/trader-scan"

AUDIT_STATUS = "unsupported_no_official_machine_api_found"
AUDIT_NOTES = {
    "finding": (
        "Axiom official docs describe web product features such as Wallet Tracking "
        "and Trader Scan, but no supported API, WebSocket, webhook, export, or "
        "developer authentication flow was found in the public docs reviewed."
    ),
    "policy": (
        "ArloBit will not use private frontend endpoints, browser-session scraping, "
        "or reverse engineering for Axiom data collection."
    ),
    "next_step": (
        "Re-audit if Axiom publishes an official API/WebSocket/export or grants "
        "documented partner access."
    ),
    "reviewed_urls": [DOCS_URL, WALLET_TRACKING_URL, TRADER_SCAN_URL],
}


def record_audit(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO axiom_source_audits (
            source_name, checked_at, status, official_docs_url,
            api_available, websocket_available, export_available, notes
        )
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(source_name) DO UPDATE SET
            checked_at=excluded.checked_at,
            status=excluded.status,
            official_docs_url=excluded.official_docs_url,
            api_available=excluded.api_available,
            websocket_available=excluded.websocket_available,
            export_available=excluded.export_available,
            notes=excluded.notes
        """,
        (
            SOURCE_NAME,
            time.time(),
            AUDIT_STATUS,
            DOCS_URL,
            0,
            0,
            0,
            json.dumps(AUDIT_NOTES, sort_keys=True),
        ),
    )
    conn.commit()


def signal_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM axiom_signals").fetchone()[0]
    linked = conn.execute(
        """
        SELECT COUNT(*)
        FROM axiom_signals s
        JOIN labels l ON l.mint = s.mint AND l.label_version = 1
        """
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT s.signal_type,
               COUNT(*) AS n,
               AVG(l.ret_24h) AS avg_ret_24h,
               AVG(l.max_runup_pct) AS avg_max_runup_pct,
               SUM(CASE WHEN l.reached_50 = 1 THEN 1 ELSE 0 END) AS reached_50,
               SUM(CASE WHEN l.reached_100 = 1 THEN 1 ELSE 0 END) AS reached_100,
               SUM(CASE WHEN l.rugged = 1 THEN 1 ELSE 0 END) AS rugged
        FROM axiom_signals s
        JOIN labels l ON l.mint = s.mint AND l.label_version = 1
        GROUP BY s.signal_type
        ORDER BY n DESC, avg_max_runup_pct DESC
        """
    ).fetchall()
    return {
        "total_signals": total,
        "signals_with_completed_labels": linked,
        "by_signal_type": rows,
    }


def latest_audit(conn: sqlite3.Connection) -> tuple[Any, ...] | None:
    return conn.execute(
        """
        SELECT source_name, checked_at, status, official_docs_url,
               api_available, websocket_available, export_available, notes
        FROM axiom_source_audits
        WHERE source_name=?
        """,
        (SOURCE_NAME,),
    ).fetchone()


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def report_lines(conn: sqlite3.Connection) -> list[str]:
    record_audit(conn)
    audit = latest_audit(conn)
    quality = signal_quality(conn)
    lines = ["=== AXIOM SOURCE REPORT ==="]
    if audit:
        source_name, checked_at, status, docs_url, api, websocket, export, notes = audit
        checked = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(checked_at))
        lines.extend(
            [
                f"source: {source_name}",
                f"checked_at: {checked}",
                f"status: {status}",
                f"official_docs_url: {docs_url}",
                f"api_available: {bool(api)}",
                f"websocket_available: {bool(websocket)}",
                f"export_available: {bool(export)}",
                f"notes: {notes}",
            ]
        )
    lines.extend(
        [
            "",
            "Collection decision:",
            "Axiom automatic collection is disabled because no supported machine interface was found.",
            "No private frontend endpoints, session scraping, or reverse-engineered WebSockets are used.",
            "",
            "Signal quality:",
            f"total_signals: {quality['total_signals']}",
            f"signals_with_completed_labels: {quality['signals_with_completed_labels']}",
        ]
    )
    rows = quality["by_signal_type"]
    if rows:
        lines.append("signal_type                      n  avg_ret24h  avg_runup  +50  +100  rugs")
        for signal_type, n, avg_ret, avg_runup, reached_50, reached_100, rugged in rows:
            lines.append(
                f"{signal_type:<30} {n:>4} {_fmt(avg_ret):>10} {_fmt(avg_runup):>10}"
                f" {reached_50 or 0:>4} {reached_100 or 0:>5} {rugged or 0:>5}"
            )
    else:
        lines.append("(no Axiom signals stored)")
    lines.append("=== END REPORT ===")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArloBit Axiom research-source audit")
    parser.add_argument("--audit", action="store_true", help="record current supported-access audit")
    parser.add_argument("--report", action="store_true", help="print Axiom source and signal-quality report")
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        if args.audit:
            record_audit(conn)
            print("Axiom source audit recorded")
        if args.report or not args.audit:
            print("\n".join(report_lines(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
