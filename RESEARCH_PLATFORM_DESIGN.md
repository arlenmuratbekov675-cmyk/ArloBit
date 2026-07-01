# ArloBit Research Platform — Design Document

**Version:** 1.0 — 2026-07-02
**Status:** Design only. No code has been written against this document yet.
**Companion:** `QUANT_AUDIT.md` (the evidence this design responds to)

---

## 0. Design goals and principles

The mission changes from *finding trades* to *building a labeled dataset that can
find strategies*. Every decision below follows five principles:

1. **Never discard an observation.** Every candidate sighting, accepted or rejected,
   becomes a row. Rejection reasons are data, not filtering.
2. **Point-in-time correctness.** A feature row must contain only what was knowable at
   `seen_at`. Labels are computed strictly from data timestamped *after* it. No
   lookahead, ever. This is the single discipline that separates a research dataset
   from a pile of numbers.
3. **Cheap-first tiering.** DexScreener fields are free (already fetched) — record them
   for *everything*. RPC/Jupiter enrichment costs time and rate limits — record it for
   as much as the budget allows, and record *that it's missing* when it's missing.
4. **The scanner is a sensor, not a brain.** It collects and paper-trades; all
   intelligence (replay, analysis, strategy search) runs offline against SQLite.
5. **Boring technology.** One SQLite file, one Python package, no servers, no queues,
   no cloud. Complexity must be purchased with evidence, and we have none yet.

### Assumptions challenged (changes vs. the brief)

| Brief asked for | Reality check | Design decision |
|---|---|---|
| 1m and 15m volume / price change | **DexScreener does not provide them.** Pair payloads expose exactly four windows: `m5`, `h1`, `h6`, `h24` | Store all four real windows. Finer granularity comes from our own snapshot cadence and OHLCV backfill (below) |
| Exact highest/lowest price per checkpoint | A poller only sees prices when it looks; exact extremes are unobservable from polling | **GeckoTerminal OHLCV backfill**: at label time (24 h), fetch free 1-minute candles for the pool (one request per token). Gives *exact* max run-up, max drawdown, and threshold hits. Polling checkpoints remain as fallback when GeckoTerminal lacks the pool |
| Holder change at every checkpoint | Holder counts cost multiple RPC calls each; 8 checkpoints × hundreds of mints/day would burn the entire RPC budget on the least-proven feature | v1: holder concentration at snapshot time only (already computed). v2: holder *count* sampled at 30 m / 6 h / 24 h for tokens that survived, behind a config flag |
| Creator quality per candidate | Creator lookups are the most expensive enrichment (signature history + balance) | Enrich once per mint, cache forever in `tokens` (creator facts are static). Only holder/sellability are re-checked |
| "Every candidate must be saved" | A mint is sighted repeatedly across cycles; "one row per candidate" is ambiguous | **One row per sighting** (mint × cycle). Time-series of sightings is itself a feature source (liquidity growth, volume acceleration between cycles). Deduplication is the replay engine's job, not the collector's |
| Think like RenTech | RenTech's actual lesson at our scale isn't models — it's data hygiene: versioned labels, holdout discipline, no lookahead, and measuring *everything* | Encoded in schema (label_version, holdout_week), pipeline rules (§5), and "what not to build" (§12) |

### Should ArloBit be redesigned completely?

**No — strangler pattern, not rewrite.** `scanner_v0.py` (~2,800 lines) works, runs
24/7, and is the data sensor everything depends on. Rewriting it risks silent
collection gaps for zero research benefit. Instead: new code lives in an `arlobit/`
package; the scanner gains **two one-line hooks** (record sightings, record
enrichments) and otherwise keeps running unchanged. Scanner logic migrates into the
package gradually, later, if ever. The *research* side, which doesn't exist yet, is
designed clean from day one.

---

## 1. Architecture

```
                        ┌─────────────────────────────────────────┐
   DexScreener          │  scanner_v0.py  (existing, +2 hooks)    │
   profiles/boosts ───► │  fetch candidates → pairs → filters     │
   Solana RPC/Helius ─► │  → enrichment (top N) → paper trades    │
   Jupiter quotes  ───► │  → telegram alerts                      │
                        └───────┬─────────────────────────────────┘
                                │ collector.record_sightings(raw_pairs, rows)
                                │ collector.record_enrichment(rows)
                                ▼
                     ┌────────────────────┐
                     │  data/arlobit.db   │  SQLite, WAL mode
                     │  (single source    │  tokens / snapshots / outcomes /
                     │   of truth)        │  labels / ohlcv_1m / paper_trades
                     └───┬────────────┬───┘
              writes ▲   │ reads      │ reads
                     │   ▼            ▼
   ┌─────────────────┴─────┐   ┌──────────────────────────────────────┐
   │ tracker.py (daemon)   │   │  research CLI (offline, on demand)   │
   │ due-checkpoint poller │   │  replay    — simulate strategies     │
   │ + 24h labeler         │   │  lab       — run strategies/*.toml   │
   │ + OHLCV backfill      │   │  features  — importance/IC/MI/corr   │
   │ (DexScreener batch,   │   │  report    — research dashboard (md) │
   │  GeckoTerminal)       │   │  export    — ML matrix (csv/parquet) │
   └───────────────────────┘   └──────────────────────────────────────┘
```

Three processes, deliberately decoupled:

- **Scanner** (existing loop, 180 s): unchanged behavior + fire-and-forget writes.
  If the DB write fails, scanning continues (log to stderr, drop the batch — the
  scanner must never die because of research).
- **Tracker** (new daemon, ~60 s loop): polls due outcome checkpoints via
  DexScreener's batch endpoint, finalizes 24 h labels, backfills OHLCV. Separate
  process so a tracker crash never stops collection and vice versa. SQLite WAL
  supports one writer at a time; both writers use short transactions with
  `busy_timeout` — contention is negligible at these rates.
- **Research CLI** (`python -m arlobit.research …`): pure readers. Pandas/numpy
  allowed here (added to a separate `requirements-research.txt`); the scanner and
  tracker stay stdlib+requests only.

---

## 2. SQLite schema (deliverable #2)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- One row per scan cycle: provenance + funnel stats.
CREATE TABLE scan_cycles (
    cycle_id      INTEGER PRIMARY KEY,
    started_at    REAL NOT NULL,           -- unix seconds, UTC
    finished_at   REAL,
    candidate_count INTEGER,
    pairs_seen    INTEGER,
    enriched_count INTEGER,
    scanner_version TEXT,                  -- e.g. "0.9.4" — regime tracking
    issues        TEXT                     -- JSON array
);

-- One row per mint: static / near-static facts, written once, updated rarely.
CREATE TABLE tokens (
    mint            TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    pair_address    TEXT,                  -- primary pair used for tracking
    dex_id          TEXT,                  -- raydium / meteora / pumpswap...
    dex_url         TEXT,
    pair_created_at REAL,                  -- unix seconds
    first_seen_at   REAL NOT NULL,
    first_source    TEXT,                  -- profile | boost
    -- static enrichment (once per mint)
    mint_authority_active   INTEGER,       -- 0/1/NULL=unknown
    freeze_authority_active INTEGER,
    creator_wallet          TEXT,
    creator_wallet_age_days REAL,          -- as of enrichment time
    creator_sol_balance     REAL,
    creator_quality         TEXT,
    enriched_at             REAL,
    enrich_error            TEXT
);

-- One row per sighting (mint × cycle). The core feature table. Wide and flat
-- on purpose: one observation per row is what every ML library wants.
CREATE TABLE snapshots (
    snapshot_id   INTEGER PRIMARY KEY,
    cycle_id      INTEGER NOT NULL REFERENCES scan_cycles(cycle_id),
    mint          TEXT    NOT NULL REFERENCES tokens(mint),
    seen_at       REAL    NOT NULL,
    source        TEXT,                    -- profile | boost
    -- raw DexScreener market state (ALL currently-discarded fields included)
    price_usd     REAL,
    liquidity_usd REAL,
    fdv           REAL,
    market_cap    REAL,
    age_minutes   REAL,
    vol_m5  REAL, vol_h1  REAL, vol_h6  REAL, vol_h24 REAL,
    buys_m5  INTEGER, sells_m5  INTEGER,
    buys_h1  INTEGER, sells_h1  INTEGER,
    buys_h6  INTEGER, sells_h6  INTEGER,
    buys_h24 INTEGER, sells_h24 INTEGER,
    pc_m5 REAL, pc_h1 REAL, pc_h6 REAL, pc_h24 REAL,   -- priceChange windows
    -- derived at write time (cheap, deterministic)
    vol_liq_ratio     REAL,                -- vol_m5 / liquidity
    buy_sell_ratio_m5 REAL,                -- buys/(buys+sells), NULL if 0 txns
    swap_accel        REAL,                -- (txns_m5*12) / max(txns_h1,1)
    vol_accel         REAL,                -- (vol_m5*12)  / max(vol_h1,1)
    -- point-in-time enrichment (NULL = not enriched this cycle; that fact matters)
    sellable          TEXT,                -- yes | no | unknown | NULL
    sell_impact_pct   REAL,
    sell_route_found  INTEGER,
    top1_pct  REAL, top5_pct REAL, top10_pct REAL, top20_pct REAL,
    holder_status     TEXT,
    arlobit_score     REAL,
    -- decision trail
    verdict           TEXT,                -- SAFE | RISKY | SCAM_LIKELY
    signals           TEXT,                -- JSON array
    blocked_reasons   TEXT,                -- JSON array, [] = would have entered
    entered_paper     INTEGER DEFAULT 0
);
CREATE INDEX idx_snap_mint_time ON snapshots(mint, seen_at);
CREATE INDEX idx_snap_time      ON snapshots(seen_at);
CREATE INDEX idx_snap_cycle     ON snapshots(cycle_id);

-- One row per (mint, checkpoint). Pre-created when a mint is first seen;
-- the tracker fills rows as they come due.
CREATE TABLE outcomes (
    mint          TEXT    NOT NULL REFERENCES tokens(mint),
    checkpoint_min INTEGER NOT NULL,       -- 5,15,30,60,120,360,720,1440
    due_at        REAL    NOT NULL,        -- first_seen_at + checkpoint
    checked_at    REAL,                    -- NULL = pending
    price_usd     REAL,
    liquidity_usd REAL,
    vol_h24       REAL,
    ret_pct       REAL,                    -- vs price at first snapshot
    status        TEXT NOT NULL DEFAULT 'pending',
                                           -- pending|ok|pair_gone|error|skipped
    PRIMARY KEY (mint, checkpoint_min)
);
CREATE INDEX idx_outcomes_due ON outcomes(due_at) WHERE checked_at IS NULL;

-- Exact price paths, backfilled once per token at label time (GeckoTerminal,
-- free, 1 request per pool for 24h of minute candles). Nullable-by-absence:
-- tokens missing here fall back to outcomes granularity.
CREATE TABLE ohlcv_1m (
    mint     TEXT NOT NULL,
    ts       REAL NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume   REAL,
    PRIMARY KEY (mint, ts)
);

-- One row per (mint, label_version). Materialized once outcomes are complete.
-- label_version lets us re-define labels later WITHOUT schema change (Part 7).
CREATE TABLE labels (
    mint            TEXT    NOT NULL,
    label_version   INTEGER NOT NULL,      -- 1 = definitions below
    computed_at     REAL,
    base_snapshot_id INTEGER,              -- features-at-first-sighting anchor
    base_price      REAL,
    base_liquidity  REAL,
    -- path statistics over (first_seen_at, +24h], OHLCV-exact when available
    max_runup_pct    REAL,                 -- highest high vs base
    max_drawdown_pct REAL,                 -- lowest low vs base
    path_source      TEXT,                 -- ohlcv | checkpoints
    ret_5m REAL, ret_15m REAL, ret_30m REAL, ret_1h REAL,
    ret_2h REAL, ret_6h REAL, ret_12h REAL, ret_24h REAL,
    reached_20  INTEGER, reached_50  INTEGER, reached_100 INTEGER,
    reached_200 INTEGER, reached_500 INTEGER,
    rugged          INTEGER,               -- liq < 20% of base OR ret <= -90% OR pair gone
    rug_at_min      REAL,
    survived_24h    INTEGER,
    liq_change_24h_pct REAL,
    holdout_week    TEXT,                  -- ISO week of first_seen — split key
    PRIMARY KEY (mint, label_version)
);

-- Paper trades move from JSON into the same DB (single source of truth),
-- keyed to the snapshot that triggered them.
CREATE TABLE paper_trades (
    trade_id     INTEGER PRIMARY KEY,
    mint         TEXT NOT NULL,
    snapshot_id  INTEGER REFERENCES snapshots(snapshot_id),
    entry_time   REAL, entry_price REAL,
    exit_time    REAL, exit_price REAL, exit_reason TEXT,
    final_pnl_pct REAL, max_gain_pct REAL, max_drawdown_pct REAL,
    status       TEXT
);

-- Per-poll price path of OPEN paper trades (exit research needs paths).
CREATE TABLE trade_ticks (
    trade_id INTEGER NOT NULL REFERENCES paper_trades(trade_id),
    ts       REAL NOT NULL,
    price_usd REAL, liquidity_usd REAL,
    PRIMARY KEY (trade_id, ts)
);

-- The ML view: one row per token, features strictly from the first sighting,
-- labels from the future. Training a model is `SELECT * FROM ml_dataset`.
CREATE VIEW ml_dataset AS
SELECT s.*, t.mint_authority_active, t.freeze_authority_active,
       t.creator_wallet_age_days, t.creator_sol_balance, t.creator_quality,
       t.pair_created_at, t.first_source,
       l.max_runup_pct, l.max_drawdown_pct,
       l.ret_1h, l.ret_6h, l.ret_24h,
       l.reached_20, l.reached_50, l.reached_100, l.reached_200, l.reached_500,
       l.rugged, l.survived_24h, l.holdout_week
FROM labels l
JOIN snapshots s ON s.snapshot_id = l.base_snapshot_id
JOIN tokens    t ON t.mint = l.mint
WHERE l.label_version = 1;
```

**Rug definition (label v1):** liquidity < 20 % of base, or return ≤ −90 %, or pair
no longer resolvable. Written down so it can be criticized and versioned.

**Capacity check:** ~1,500–2,500 sightings/day × ~70 columns ≈ 2–4 MB/day → ~1 GB/year.
Checkpoints: ~300–500 new mints/day × 8 = ≤4,000 polls/day; DexScreener batch endpoint
(30 mints/request, 300 req/min limit) needs ~150 requests/day. GeckoTerminal backfill:
≤500 req/day vs a 30/min free limit. All comfortably inside free tiers.

---

## 3. Folder structure (deliverable #3)

```
ArloBit/
├── scanner_v0.py              # unchanged behavior + 2 collector hooks
├── tracker.py                 # thin entry: python tracker.py (daemon)
├── arlobit/
│   ├── __init__.py
│   ├── db.py                  # schema DDL, migrations, batched writers
│   ├── collector.py           # hook functions called from scanner_v0
│   ├── tracker.py             # checkpoint poller, labeler, ohlcv backfill
│   ├── sources/
│   │   ├── dexscreener.py     # batch price endpoint client
│   │   └── geckoterminal.py   # ohlcv client
│   └── research/
│       ├── __main__.py        # CLI: replay | lab | features | report | export
│       ├── dataset.py         # load ml_dataset / snapshots into pandas
│       ├── replay.py          # vectorized entry/exit simulation
│       ├── lab.py             # TOML strategy configs → replay → report
│       ├── features.py        # IC, MI, correlations, bucket tables
│       └── report.py          # markdown dashboard generator
├── strategies/                # *.toml strategy definitions (Part 5)
│   └── baseline_v09.toml      # today's live rules, as config
├── reports/                   # generated markdown reports (gitignored)
├── data/
│   └── arlobit.db             # gitignored
├── requirements.txt           # scanner+tracker: requests, truststore only
├── requirements-research.txt  # pandas, numpy, scipy (research CLI only)
├── QUANT_AUDIT.md
└── RESEARCH_PLATFORM_DESIGN.md
```

---

## 4. Research pipeline (deliverable #4)

**Collector (inside scanner loop, Part 1 + Part 8):**
1. Scanner builds rows exactly as today. Two hooks added:
   - after pair fetch: `collector.record_sightings(cycle, raw_pairs_by_mint, rows)`
     — receives the **raw pair dicts** (before `to_row()` discards fdv/marketCap/
     txns/h1/h6/h24 fields) plus the computed rows with verdict/blocked reasons.
   - after enrichment: `collector.record_enrichment(rows)` — updates the same
     snapshot rows with sellability/holders/score, and upserts static creator/mint
     facts into `tokens`.
2. All writes buffered in memory during the cycle, flushed in **one transaction**
   at cycle end (`executemany`). Expected cost: <50 ms per cycle. Any exception is
   caught, logged, and dropped — collection must never crash scanning.
3. On first sighting of a mint: insert `tokens` row + pre-create its 8 pending
   `outcomes` rows.

**Tracker (separate process, Part 2):**
1. Every ~60 s: `SELECT` due pending checkpoints (partial index makes this O(due)),
   group mints into batches of 30, hit DexScreener batch endpoint, write
   price/liquidity/return, mark `ok`/`pair_gone`.
2. When a mint's 1440-min checkpoint completes (or pair is gone): fetch GeckoTerminal
   1-minute OHLCV for the pool, store to `ohlcv_1m`, compute the `labels` row
   (exact run-up/drawdown/threshold flags when OHLCV exists; checkpoint-granular
   otherwise, with `path_source` recording which).
3. Retry policy: transient errors retried next loop up to 3×, then `error`.
   A checkpoint more than one interval overdue (tracker was down) is still fetched —
   `checked_at` records reality, and the labeler prefers OHLCV anyway.

**Analysis (offline, Parts 4–6):** pure readers over `ml_dataset` / `snapshots`;
detailed in §5–§7.

**Pipeline rules (the RenTech part):**
- Features may only come from columns timestamped ≤ `seen_at` of the anchor snapshot.
- Every analysis and replay reports **in-sample vs holdout** split by `holdout_week`
  (e.g. even ISO weeks = research, odd = holdout; never tune on holdout).
- Any strategy promoted from replay must state: n, EV/trade, EV 95 % CI, trades/day,
  and the number of variants searched to find it (multiple-testing honesty).

---

## 5. Replay pipeline (deliverable #5, Parts 3 + 5)

**Model.** A strategy is `(entry predicate, dedupe rule, exit rule, cost model)`.

- **Entry predicate:** boolean expression over snapshot columns. Declarative TOML —
  every leaf is `column {min=, max=, eq=, in=}`; groups AND by default, `any_of`
  blocks for OR. No code, per Part 5.
- **Dedupe:** `first_sighting_only` (default) or `first_qualifying_sighting` —
  a mint enters at most once per strategy run.
- **Exit rule:** simulated against the token's price path — `ohlcv_1m` when present,
  else snapshots+checkpoints. Supported: fixed TP/SL, rug stop, max-hold,
  trailing (arm %, giveback %), time-stop (if below X % after Y min), partial TP.
  Exit fills honor **path granularity pessimistically** (fill at the candle that
  breaches, at the breaching price minus slippage), and the report flags what share
  of exits came from coarse paths.
- **Cost model:** flat slippage+fee % per side (default 2 % + 0.5 %), later
  replaceable by Jupiter-quote-derived impact.

**Example — `strategies/example.toml`:**

```toml
name = "tight_holders_young"
[entry]
liquidity_usd = { min = 30000 }
arlobit_score = { min = 8 }
creator_wallet_age_days = { min = 90 }
top10_pct = { max = 25 }
sell_impact_pct = { max = 10 }
age_minutes = { max = 120 }
[entry.rate_limit]
max_per_hour = 4
[exit]
take_profit_pct = 50
stop_loss_pct = -30
trail = { arm_pct = 30, giveback_pct = 15 }
time_stop = { after_min = 45, if_below_pct = 10 }
max_hold_hours = 6
[costs]
slippage_pct_per_side = 2.0
fee_pct_per_side = 0.5
```

**Execution.** `python -m arlobit.research lab strategies/example.toml` →
loads dataset once into pandas, evaluates the predicate as vectorized boolean masks
(thousands of variants/second), simulates exits per entered token, writes
`reports/<name>_<date>.md` with: trades, win rate, EV/trade ± CI, profit factor,
median return, max drawdown, exposure, trades/day, in-sample vs holdout, and the
10 best/worst trades for eyeballing.

**Grid search.** `lab --sweep` accepts value lists per parameter
(`liquidity_usd.min = [10000, 20000, 30000, 50000]`) → cartesian product → ranked
table. Guardrail: results ranked by **holdout** EV only; the report prints
`variants_tested` and a Bonferroni-flavored warning threshold so a lucky 1-in-500
variant isn't mistaken for edge.

---

## 6. Feature analysis (Part 4) and dashboard (Part 6)

`python -m arlobit.research features [--label reached_50] [--horizon 6h]` computes,
for every numeric feature: coverage (non-null %), distribution (p5/25/50/75/95),
Spearman IC vs forward return per horizon, point-biserial correlation and **mutual
information** vs the binary label (quantile-binned, with a permutation-shuffled
baseline so tiny-sample MI isn't over-read), quintile bucket table (n, win rate,
EV, profit factor, median, max DD), and FP/FN rates at each quintile threshold.
For categorical features: per-category bucket table. Plus a feature×feature Spearman
correlation matrix (redundancy map) and, once a model exists, permutation importance.

`python -m arlobit.research report` renders the research dashboard as markdown:
best/worst liquidity ranges, creator-age ranges, holder distributions, hours-of-day,
weekdays, score ranges, narrative tags (regex tagger v1, LLM tagger later), and the
top feature *pairs* by joint lift (all pairwise splits at quartile cut-points — cheap
at this scale and it answers "best combinations" without inviting deep-tree
overfitting). Every cell carries `n` and a CI; cells with n < 30 render greyed.

---

## 7. ML-readiness (Part 7)

The DB is ML-ready **by construction**, not by future migration:

- `ml_dataset` view = one row per token, numeric features, multiple label columns.
  `export --format csv|parquet` feeds sklearn/LightGBM/XGBoost/CatBoost directly.
- Multiple targets already materialized: classification (`reached_50`, `rugged`,
  `survived_24h`) and regression (`ret_6h`, `max_runup_pct`).
- `label_version` supports redefining labels without touching features.
- `holdout_week` bakes the walk-forward split into the data so every future
  model uses the same split discipline.
- Missingness is honest (NULL = not measured), which tree models handle natively.
- Trigger points from `QUANT_AUDIT.md` still stand: first baseline (logistic
  regression) at ~5,000 labeled tokens with ≥500 positives; LightGBM after that.
  At ~300–500 new mints/day, 5,000 labels ≈ **2 weeks of collection**.

---

## 8. Performance (Part 8)

- WAL + `synchronous=NORMAL` + `busy_timeout=5000`; scanner and tracker are the only
  writers, each using short single transactions.
- Collector: in-memory buffer, one `executemany` transaction per cycle (<50 ms).
  DB failures never propagate to the scan loop.
- Tracker runs in its own process; scanner latency unaffected.
- Partial index on pending outcomes keeps the due-poll query O(pending).
- No new API load on the scanner path: raw pair dicts are already in memory.
  Tracker adds ~150 DexScreener batch requests + ≤500 GeckoTerminal requests/day.
- Retention: raw `ohlcv_1m` is the only fast-growing table (~1,440 rows/token);
  at 500 tokens/day ≈ 0.7 M rows/day ≈ 25 MB/day. Keep 90 days, then prune candles
  for tokens whose labels are finalized (labels retain the derived statistics).
  Everything else is kept forever.

---

## 9. Migration plan (deliverable #6)

1. **Freeze behavior.** No filter/threshold changes during migration — the collector
   must observe the strategy as-is (it becomes `strategies/baseline_v09.toml`).
2. **Step 1 — schema + collector hooks** (scanner keeps writing JSON too).
   Run one live cycle, verify snapshot rows against terminal output.
3. **Step 2 — tracker daemon.** Backfill note: tokens first seen before the tracker
   started get labels from OHLCV alone (GeckoTerminal covers the past — a mint seen
   yesterday can still be labeled *today*). This also means the dataset starts
   accruing retroactively from day one of collection, not day one of the tracker.
4. **Step 3 — paper trades to DB.** Import `paper_trades*.json` (95 historical trades)
   into `paper_trades` for continuity; scanner writes trades+ticks to DB; JSON files
   become export-only (`--export-json` kept for compatibility).
5. **Step 4 — research CLI** once ≥1 week of data exists.
6. **Rollback:** hooks are wrapped in try/except and behind `ARLOBIT_RESEARCH=0`;
   killing the tracker stops only outcome collection. The scanner never depends on
   the DB to function.

Explicitly *not* migrating: the scanner's internal structure, alerting, or CLI.

---

## 10. Implementation order & effort (deliverables #7, #8)

| # | Work item | Effort | Depends on | Why this order |
|---|---|---|---|---|
| 1 | `db.py` schema + `collector.py` + scanner hooks | 2–3 days | — | Every day without it is lost data — **data accrues only while collectors run** |
| 2 | `tracker.py` checkpoints + labeler + GeckoTerminal backfill | 2–3 days | 1 | Labels are the product; backfill starts paying immediately |
| 3 | Paper trades → DB + `trade_ticks` path logging | 1 day | 1 | Unblocks exit research (trailing/time stops) on real paths |
| 4 | `dataset.py` + `features.py` + first feature report | 2–3 days | 1–2 + a week of data | First real answer to "which features matter" |
| 5 | `replay.py` + `lab.py` (TOML strategies, sweeps) | 3–5 days | 4 | The 1,000-variants-offline capability |
| 6 | `report.py` dashboard | 1–2 days | 4 | Cheap once features.py exists |
| 7 | v2 extras: holder-count sampling, LLM narrative tagger, ML export polish | 1 wk | 5–6 | Only after the core loop proves itself |

Total: **~2–3 weeks of focused work**; items 1–3 (the part where calendar time
matters) fit in **under a week**. Items 4–6 can happen leisurely while data accrues.

## 11. Expected benefit (deliverable #9)

- **Label velocity: ~150× paper trading.** Paper trading yields ~10–20 closed
  trades/day (2/hour cap, 30 % fill). Shadow collection labels every sighted mint:
  ~300–500/day, unconditionally, including everything today's filters reject —
  which finally measures **false negatives**, unmeasurable today (QUANT_AUDIT.md).
- Time to first statistically meaningful answer (n≈5,000, ≥500 positives):
  **~2 weeks** after item 2 ships, vs ~9 months of paper trading.
- Every claim in `QUANT_AUDIT.md` flagged "unmeasurable (n=5)" becomes measurable:
  holder concentration, creator age/balance, sell impact, score, source.
- Exit engineering (trailing/time stops — the two most promising levers) becomes
  simulable on exact minute-level paths instead of guessed from `max_gain`.
- Strategy iteration latency collapses from *weeks per variant* (live paper trading)
  to *seconds per thousand variants* (vectorized replay), with holdout discipline
  built in rather than bolted on.

## 12. What should NOT be built (deliverable #10)

| Non-goal | Why |
|---|---|
| Live trading, wallets, keys, auto-buy | Out of scope by decree; also: no proven edge exists to trade |
| Web dashboard / server / Grafana | Markdown reports answer research questions; a server is maintenance with zero label velocity |
| Postgres, DuckDB, Timescale, cloud DBs | SQLite handles 100× this write load; migration is a rename away *if ever* needed |
| Queues, Airflow, Docker, microservices | Two processes and a cron-like loop; orchestration would exceed the code it orchestrates |
| ML training now | Premature until ~5k labels; the schema already guarantees zero rework when ready |
| News engine / Twitter ingestion | QUANT_AUDIT.md: low expected edge for 0–24 h memecoins; X API $200/mo; revisit at v2.0 |
| Smart-money wallet indexer | Highest-effort signal; buy or build only after the free features are exhausted |
| Per-checkpoint holder counts (v1) | Burns the whole RPC budget on an unproven feature; sampled version gated to v2 |
| Full scanner_v0.py rewrite | Working sensor; rewriting risks collection gaps for zero research gain |
| Websocket price streaming | Minute-candle backfill gives exact paths already; streaming adds infra for precision the strategy horizon doesn't need |
| Multi-chain support | Solana-only until an edge exists on Solana |

---

## 13. Open questions (decide before item 1 is coded)

1. **Checkpoint anchor:** first sighting (design default) — or *every* sighting?
   Every-sighting multiplies tracker load ~5× for marginal value; revisit only if
   replay shows mid-life entries are where the edge is (OHLCV paths already let
   replay evaluate mid-life entries without extra checkpoints).
2. **Enrichment budget:** keep enriching only top-N rows (status quo), or spend idle
   cycle time enriching a random sample of *rejected* candidates so holder/creator
   features get unbiased coverage? Recommended: random 10–20 mints/cycle if the
   180 s budget allows — unbiased coverage is worth real RPC spend.
3. **Holdout split:** even/odd ISO weeks (design default) vs final-4-weeks. Even/odd
   chosen to spread regime shifts across both sets; revisit at first model training.
