"""SQLite schema and connection handling for the ArloBit research dataset.

Design notes (see RESEARCH_PLATFORM_DESIGN.md):
- One WAL-mode SQLite file is the single source of truth.
- `candidate_sightings` is deliberately one wide, flat table (one observation per
  row) instead of a separate candidate_features table: that is the shape every
  analysis tool and ML library consumes directly, and NULL columns are free in
  SQLite. Enrichment columns are NULL when a sighting was not enriched — that
  missingness is itself data.
- Schema changes are tracked via PRAGMA user_version; labels get a label_version
  column so label definitions can evolve without schema migrations.

Run `python -m arlobit.db` for a quick row-count summary.
"""

from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join("data", "arlobit.db")
SCHEMA_VERSION = 2

OUTCOME_CHECKPOINTS_MIN = (5, 15, 30, 60, 120, 360, 720, 1440)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_cycles (
    cycle_id        INTEGER PRIMARY KEY,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    candidate_count INTEGER,
    pairs_seen      INTEGER,
    sightings       INTEGER,
    enriched_count  INTEGER,
    sampled_count   INTEGER,
    scanner_version TEXT,
    issues          TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    pair_address    TEXT,
    dex_id          TEXT,
    dex_url         TEXT,
    pair_created_at REAL,
    first_seen_at   REAL NOT NULL,
    first_source    TEXT,
    mint_authority_active   INTEGER,
    freeze_authority_active INTEGER,
    creator_wallet          TEXT,
    creator_wallet_age_days REAL,
    creator_sol_balance     REAL,
    creator_quality         TEXT,
    enriched_at             REAL,
    enrich_error            TEXT
);

CREATE TABLE IF NOT EXISTS candidate_sightings (
    sighting_id   INTEGER PRIMARY KEY,
    cycle_id      INTEGER NOT NULL REFERENCES scan_cycles(cycle_id),
    mint          TEXT    NOT NULL REFERENCES tokens(mint),
    pair_address  TEXT,
    seen_at       REAL    NOT NULL,
    source        TEXT,
    in_scan_window INTEGER NOT NULL DEFAULT 1,
    -- raw DexScreener market state (all four windows the API provides)
    price_usd     REAL,
    liquidity_usd REAL,
    fdv           REAL,
    market_cap    REAL,
    age_minutes   REAL,
    vol_m5 REAL, vol_h1 REAL, vol_h6 REAL, vol_h24 REAL,
    buys_m5  INTEGER, sells_m5  INTEGER,
    buys_h1  INTEGER, sells_h1  INTEGER,
    buys_h6  INTEGER, sells_h6  INTEGER,
    buys_h24 INTEGER, sells_h24 INTEGER,
    pc_m5 REAL, pc_h1 REAL, pc_h6 REAL, pc_h24 REAL,
    -- derived at write time
    vol_liq_ratio     REAL,
    buy_sell_ratio_m5 REAL,
    swap_accel        REAL,
    vol_accel         REAL,
    -- authority state (fetched for every candidate, point-in-time)
    mint_authority_active   INTEGER,
    freeze_authority_active INTEGER,
    -- enrichment (NULL = not measured this sighting)
    enriched        INTEGER NOT NULL DEFAULT 0,
    sample_rejected INTEGER NOT NULL DEFAULT 0,
    sellable          TEXT,
    sell_impact_pct   REAL,
    sell_route_found  INTEGER,
    sell_check_error  TEXT,
    top1_pct REAL, top5_pct REAL, top10_pct REAL, top20_pct REAL,
    holder_status     TEXT,
    creator_wallet          TEXT,
    creator_wallet_age_days REAL,
    creator_sol_balance     REAL,
    creator_quality         TEXT,
    arlobit_score     REAL,
    -- decision trail
    verdict         TEXT,
    signals         TEXT,
    blocked_reasons TEXT,
    entered_paper   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sight_mint_time ON candidate_sightings(mint, seen_at);
CREATE INDEX IF NOT EXISTS idx_sight_time      ON candidate_sightings(seen_at);
CREATE INDEX IF NOT EXISTS idx_sight_cycle     ON candidate_sightings(cycle_id);

CREATE TABLE IF NOT EXISTS outcomes (
    mint           TEXT    NOT NULL REFERENCES tokens(mint),
    checkpoint_min INTEGER NOT NULL,
    due_at         REAL    NOT NULL,
    checked_at     REAL,
    price_usd      REAL,
    liquidity_usd  REAL,
    vol_h24        REAL,
    ret_pct        REAL,
    status         TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (mint, checkpoint_min)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_due ON outcomes(due_at) WHERE checked_at IS NULL;

CREATE TABLE IF NOT EXISTS ohlcv_1m (
    mint   TEXT NOT NULL,
    ts     REAL NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    PRIMARY KEY (mint, ts)
);

CREATE TABLE IF NOT EXISTS labels (
    mint             TEXT    NOT NULL,
    label_version    INTEGER NOT NULL,
    computed_at      REAL,
    base_sighting_id INTEGER,
    base_price       REAL,
    base_liquidity   REAL,
    max_runup_pct    REAL,
    max_drawdown_pct REAL,
    path_source      TEXT,
    ret_5m REAL, ret_15m REAL, ret_30m REAL, ret_1h REAL,
    ret_2h REAL, ret_6h REAL, ret_12h REAL, ret_24h REAL,
    reached_20  INTEGER, reached_50  INTEGER, reached_100 INTEGER,
    reached_200 INTEGER, reached_500 INTEGER,
    rugged        INTEGER,
    rug_at_min    REAL,
    survived_24h  INTEGER,
    liq_change_24h_pct REAL,
    holdout_week  TEXT,
    PRIMARY KEY (mint, label_version)
);

CREATE TABLE IF NOT EXISTS db_paper_trades (
    trade_id     INTEGER PRIMARY KEY,
    mint         TEXT NOT NULL,
    sighting_id  INTEGER REFERENCES candidate_sightings(sighting_id),
    entry_time   REAL, entry_price REAL,
    exit_time    REAL, exit_price REAL, exit_reason TEXT,
    final_pnl_pct REAL, max_gain_pct REAL, max_drawdown_pct REAL,
    status       TEXT,
    -- full trade dict (signals, holder/creator/score fields, ...); structured
    -- columns above are authoritative for lifecycle facts, this backs
    -- backward-compatible stats/CSV export without duplicating ~30 columns
    payload_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_identity ON db_paper_trades(mint, entry_time);

CREATE TABLE IF NOT EXISTS paper_trade_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS trade_ticks (
    trade_id  INTEGER NOT NULL REFERENCES db_paper_trades(trade_id),
    ts        REAL NOT NULL,
    price_usd REAL,
    liquidity_usd REAL,
    PRIMARY KEY (trade_id, ts)
);

CREATE VIEW IF NOT EXISTS ml_dataset AS
SELECT s.*,
       t.pair_created_at, t.first_source,
       l.max_runup_pct, l.max_drawdown_pct, l.path_source,
       l.ret_1h, l.ret_6h, l.ret_24h,
       l.reached_20, l.reached_50, l.reached_100, l.reached_200, l.reached_500,
       l.rugged, l.survived_24h, l.holdout_week
FROM labels l
JOIN candidate_sightings s ON s.sighting_id = l.base_sighting_id
JOIN tokens t ON t.mint = l.mint
WHERE l.label_version = 1;
"""


def db_path() -> str:
    return os.environ.get("ARLOBIT_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the research DB, creating directory and schema if needed."""
    target = path or db_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    if version < 2:
        # v1 DBs already have db_paper_trades without payload_json; CREATE
        # TABLE IF NOT EXISTS above is a no-op for them, so add it explicitly.
        _add_column_if_missing(conn, "db_paper_trades", "payload_json", "TEXT")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def summary(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    tables = (
        "scan_cycles",
        "tokens",
        "candidate_sightings",
        "outcomes",
        "labels",
        "ohlcv_1m",
        "db_paper_trades",
        "trade_ticks",
    )
    return [(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables]


if __name__ == "__main__":
    connection = connect()
    print(f"db: {db_path()}")
    for table_name, count in summary(connection):
        print(f"  {table_name:<20} {count}")
    connection.close()
