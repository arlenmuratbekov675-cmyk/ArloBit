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
SCHEMA_VERSION = 6

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

CREATE TABLE IF NOT EXISTS early_buyers (
    mint             TEXT NOT NULL REFERENCES tokens(mint),
    buyer_wallet     TEXT NOT NULL,
    first_buy_time   REAL,
    buy_amount_sol   REAL,
    buy_amount_usd   REAL,
    token_amount     REAL,
    tx_signature     TEXT,
    slot             INTEGER,
    source           TEXT,
    is_dev_wallet    INTEGER,
    is_repeat_buyer  INTEGER,
    created_at       REAL NOT NULL,
    PRIMARY KEY (mint, buyer_wallet)
);
CREATE INDEX IF NOT EXISTS idx_early_buyers_wallet ON early_buyers(buyer_wallet);
CREATE INDEX IF NOT EXISTS idx_early_buyers_mint_time ON early_buyers(mint, first_buy_time);

CREATE TABLE IF NOT EXISTS wallet_stats (
    buyer_wallet          TEXT PRIMARY KEY,
    first_seen_at         REAL,
    last_seen_at          REAL,
    early_buy_count       INTEGER NOT NULL DEFAULT 0,
    distinct_mints        INTEGER NOT NULL DEFAULT 0,
    successful_50_count   INTEGER NOT NULL DEFAULT 0,
    successful_100_count  INTEGER NOT NULL DEFAULT 0,
    successful_500_count  INTEGER NOT NULL DEFAULT 0,
    rugged_count          INTEGER NOT NULL DEFAULT 0,
    avg_ret_24h           REAL,
    avg_max_runup_pct     REAL,
    avg_max_drawdown_pct  REAL,
    total_tokens_seen     INTEGER NOT NULL DEFAULT 0,
    total_completed       INTEGER NOT NULL DEFAULT 0,
    total_pumps           INTEGER NOT NULL DEFAULT 0,
    total_50pct           INTEGER NOT NULL DEFAULT 0,
    total_rugs            INTEGER NOT NULL DEFAULT 0,
    average_return_24h    REAL,
    average_max_runup     REAL,
    average_drawdown      REAL,
    win_rate              REAL,
    profit_factor         REAL,
    expectancy            REAL,
    confidence_score      REAL,
    reputation            TEXT,
    updated_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_token_outcomes (
    buyer_wallet         TEXT NOT NULL,
    mint                 TEXT NOT NULL REFERENCES tokens(mint),
    first_buy_time       REAL,
    reached_50           INTEGER,
    reached_100          INTEGER,
    reached_500          INTEGER,
    rugged               INTEGER,
    ret_24h              REAL,
    max_runup_pct        REAL,
    max_drawdown_pct     REAL,
    label_version        INTEGER,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (buyer_wallet, mint)
);
CREATE INDEX IF NOT EXISTS idx_wallet_token_outcomes_mint ON wallet_token_outcomes(mint);
CREATE INDEX IF NOT EXISTS idx_wallet_token_outcomes_wallet ON wallet_token_outcomes(buyer_wallet);

CREATE TABLE IF NOT EXISTS wallet_cooccurrences (
    wallet_a             TEXT NOT NULL,
    wallet_b             TEXT NOT NULL,
    times_seen_together  INTEGER NOT NULL,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (wallet_a, wallet_b)
);
CREATE INDEX IF NOT EXISTS idx_wallet_cooccurrences_count ON wallet_cooccurrences(times_seen_together);

CREATE TABLE IF NOT EXISTS axiom_source_audits (
    source_name       TEXT PRIMARY KEY,
    checked_at        REAL NOT NULL,
    status            TEXT NOT NULL,
    official_docs_url TEXT,
    api_available     INTEGER NOT NULL DEFAULT 0,
    websocket_available INTEGER NOT NULL DEFAULT 0,
    export_available  INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS axiom_signals (
    signal_id          INTEGER PRIMARY KEY,
    mint               TEXT REFERENCES tokens(mint),
    wallet             TEXT,
    signal_time        REAL,
    signal_type        TEXT,
    metric_name        TEXT,
    metric_value       REAL,
    raw_value          TEXT,
    source             TEXT NOT NULL,
    source_url         TEXT,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_axiom_signals_mint_time ON axiom_signals(mint, signal_time);
CREATE INDEX IF NOT EXISTS idx_axiom_signals_wallet ON axiom_signals(wallet);
CREATE INDEX IF NOT EXISTS idx_axiom_signals_type ON axiom_signals(signal_type);

CREATE TABLE IF NOT EXISTS token_velocity (
    mint                    TEXT PRIMARY KEY REFERENCES tokens(mint),
    sighting_id             INTEGER REFERENCES candidate_sightings(sighting_id),
    seen_at                 REAL,
    computed_at             REAL NOT NULL,
    liquidity_change_5m     REAL,
    liquidity_change_15m    REAL,
    liquidity_change_1h     REAL,
    volume_change_5m        REAL,
    volume_change_15m       REAL,
    volume_change_1h        REAL,
    buy_count_change        REAL,
    sell_count_change       REAL,
    buy_sell_ratio_change   REAL,
    price_change_velocity   REAL,
    volume_acceleration     REAL,
    liquidity_acceleration  REAL,
    reached_50              INTEGER,
    reached_100             INTEGER,
    reached_500             INTEGER,
    rugged                  INTEGER,
    ret_24h                 REAL,
    max_runup_pct           REAL,
    max_drawdown_pct        REAL,
    label_version           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_token_velocity_seen_at ON token_velocity(seen_at);
CREATE INDEX IF NOT EXISTS idx_token_velocity_reached_100 ON token_velocity(reached_100);
CREATE INDEX IF NOT EXISTS idx_token_velocity_rugged ON token_velocity(rugged);

CREATE TABLE IF NOT EXISTS velocity_signals (
    feature_name             TEXT NOT NULL,
    bucket_label             TEXT NOT NULL,
    bucket_min               REAL,
    bucket_max               REAL,
    n                        INTEGER NOT NULL,
    reached_50_rate          REAL,
    reached_100_rate         REAL,
    reached_500_rate         REAL,
    rug_rate                 REAL,
    avg_ret_24h              REAL,
    avg_max_runup_pct        REAL,
    avg_max_drawdown_pct     REAL,
    pump_lift                REAL,
    rug_lift                 REAL,
    pump_p_value             REAL,
    rug_p_value              REAL,
    computed_at              REAL NOT NULL,
    PRIMARY KEY (feature_name, bucket_label)
);
CREATE INDEX IF NOT EXISTS idx_velocity_signals_feature ON velocity_signals(feature_name);
CREATE INDEX IF NOT EXISTS idx_velocity_signals_pump ON velocity_signals(reached_100_rate);
CREATE INDEX IF NOT EXISTS idx_velocity_signals_rug ON velocity_signals(rug_rate);

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
    if version < 3:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS early_buyers (
                mint             TEXT NOT NULL REFERENCES tokens(mint),
                buyer_wallet     TEXT NOT NULL,
                first_buy_time   REAL,
                buy_amount_sol   REAL,
                buy_amount_usd   REAL,
                token_amount     REAL,
                tx_signature     TEXT,
                slot             INTEGER,
                source           TEXT,
                is_dev_wallet    INTEGER,
                is_repeat_buyer  INTEGER,
                created_at       REAL NOT NULL,
                PRIMARY KEY (mint, buyer_wallet)
            );
            CREATE INDEX IF NOT EXISTS idx_early_buyers_wallet ON early_buyers(buyer_wallet);
            CREATE INDEX IF NOT EXISTS idx_early_buyers_mint_time ON early_buyers(mint, first_buy_time);

            CREATE TABLE IF NOT EXISTS wallet_stats (
                buyer_wallet          TEXT PRIMARY KEY,
                first_seen_at         REAL,
                last_seen_at          REAL,
                early_buy_count       INTEGER NOT NULL DEFAULT 0,
                distinct_mints        INTEGER NOT NULL DEFAULT 0,
                successful_50_count   INTEGER NOT NULL DEFAULT 0,
                successful_100_count  INTEGER NOT NULL DEFAULT 0,
                successful_500_count  INTEGER NOT NULL DEFAULT 0,
                rugged_count          INTEGER NOT NULL DEFAULT 0,
                avg_ret_24h           REAL,
                avg_max_runup_pct     REAL,
                avg_max_drawdown_pct  REAL,
                updated_at            REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_token_outcomes (
                buyer_wallet         TEXT NOT NULL,
                mint                 TEXT NOT NULL REFERENCES tokens(mint),
                first_buy_time       REAL,
                reached_50           INTEGER,
                reached_100          INTEGER,
                reached_500          INTEGER,
                rugged               INTEGER,
                ret_24h              REAL,
                max_runup_pct        REAL,
                max_drawdown_pct     REAL,
                label_version        INTEGER,
                updated_at           REAL NOT NULL,
                PRIMARY KEY (buyer_wallet, mint)
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_token_outcomes_mint ON wallet_token_outcomes(mint);
            CREATE INDEX IF NOT EXISTS idx_wallet_token_outcomes_wallet ON wallet_token_outcomes(buyer_wallet);
            """
        )
    if version < 4:
        _add_column_if_missing(conn, "wallet_stats", "total_tokens_seen", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "wallet_stats", "total_completed", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "wallet_stats", "total_pumps", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "wallet_stats", "total_50pct", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "wallet_stats", "total_rugs", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "wallet_stats", "average_return_24h", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "average_max_runup", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "average_drawdown", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "win_rate", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "profit_factor", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "expectancy", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "confidence_score", "REAL")
        _add_column_if_missing(conn, "wallet_stats", "reputation", "TEXT")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_wallet_stats_reputation ON wallet_stats(reputation);
            CREATE INDEX IF NOT EXISTS idx_wallet_stats_expectancy ON wallet_stats(expectancy);
            CREATE INDEX IF NOT EXISTS idx_wallet_stats_completed ON wallet_stats(total_completed);

            CREATE TABLE IF NOT EXISTS wallet_cooccurrences (
                wallet_a             TEXT NOT NULL,
                wallet_b             TEXT NOT NULL,
                times_seen_together  INTEGER NOT NULL,
                updated_at           REAL NOT NULL,
                PRIMARY KEY (wallet_a, wallet_b)
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_cooccurrences_count ON wallet_cooccurrences(times_seen_together);
            """
        )
    if version < 5:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS axiom_source_audits (
                source_name       TEXT PRIMARY KEY,
                checked_at        REAL NOT NULL,
                status            TEXT NOT NULL,
                official_docs_url TEXT,
                api_available     INTEGER NOT NULL DEFAULT 0,
                websocket_available INTEGER NOT NULL DEFAULT 0,
                export_available  INTEGER NOT NULL DEFAULT 0,
                notes             TEXT
            );

            CREATE TABLE IF NOT EXISTS axiom_signals (
                signal_id          INTEGER PRIMARY KEY,
                mint               TEXT REFERENCES tokens(mint),
                wallet             TEXT,
                signal_time        REAL,
                signal_type        TEXT,
                metric_name        TEXT,
                metric_value       REAL,
                raw_value          TEXT,
                source             TEXT NOT NULL,
                source_url         TEXT,
                created_at         REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_axiom_signals_mint_time ON axiom_signals(mint, signal_time);
            CREATE INDEX IF NOT EXISTS idx_axiom_signals_wallet ON axiom_signals(wallet);
            CREATE INDEX IF NOT EXISTS idx_axiom_signals_type ON axiom_signals(signal_type);
            """
        )
    if version < 6:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS token_velocity (
                mint                    TEXT PRIMARY KEY REFERENCES tokens(mint),
                sighting_id             INTEGER REFERENCES candidate_sightings(sighting_id),
                seen_at                 REAL,
                computed_at             REAL NOT NULL,
                liquidity_change_5m     REAL,
                liquidity_change_15m    REAL,
                liquidity_change_1h     REAL,
                volume_change_5m        REAL,
                volume_change_15m       REAL,
                volume_change_1h        REAL,
                buy_count_change        REAL,
                sell_count_change       REAL,
                buy_sell_ratio_change   REAL,
                price_change_velocity   REAL,
                volume_acceleration     REAL,
                liquidity_acceleration  REAL,
                reached_50              INTEGER,
                reached_100             INTEGER,
                reached_500             INTEGER,
                rugged                  INTEGER,
                ret_24h                 REAL,
                max_runup_pct           REAL,
                max_drawdown_pct        REAL,
                label_version           INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_token_velocity_seen_at ON token_velocity(seen_at);
            CREATE INDEX IF NOT EXISTS idx_token_velocity_reached_100 ON token_velocity(reached_100);
            CREATE INDEX IF NOT EXISTS idx_token_velocity_rugged ON token_velocity(rugged);

            CREATE TABLE IF NOT EXISTS velocity_signals (
                feature_name             TEXT NOT NULL,
                bucket_label             TEXT NOT NULL,
                bucket_min               REAL,
                bucket_max               REAL,
                n                        INTEGER NOT NULL,
                reached_50_rate          REAL,
                reached_100_rate         REAL,
                reached_500_rate         REAL,
                rug_rate                 REAL,
                avg_ret_24h              REAL,
                avg_max_runup_pct        REAL,
                avg_max_drawdown_pct     REAL,
                pump_lift                REAL,
                rug_lift                 REAL,
                pump_p_value             REAL,
                rug_p_value              REAL,
                computed_at              REAL NOT NULL,
                PRIMARY KEY (feature_name, bucket_label)
            );
            CREATE INDEX IF NOT EXISTS idx_velocity_signals_feature ON velocity_signals(feature_name);
            CREATE INDEX IF NOT EXISTS idx_velocity_signals_pump ON velocity_signals(reached_100_rate);
            CREATE INDEX IF NOT EXISTS idx_velocity_signals_rug ON velocity_signals(rug_rate);
            """
        )
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
        "early_buyers",
        "wallet_stats",
        "wallet_token_outcomes",
        "wallet_cooccurrences",
        "axiom_source_audits",
        "axiom_signals",
        "token_velocity",
        "velocity_signals",
    )
    return [(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables]


if __name__ == "__main__":
    connection = connect()
    print(f"db: {db_path()}")
    for table_name, count in summary(connection):
        print(f"  {table_name:<20} {count}")
    connection.close()
