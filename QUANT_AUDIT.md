# ArloBit Quantitative Strategy Audit — 2026-07-01

Reproducible analysis: `python analyze_trades.py` (merges and dedupes every
`paper_trades*.json`, n = 95 unique closed trades across all strategy versions).

---

## Executive summary

1. **The strategy has negative expectancy: −9.2%/trade (95% CI −19.7 to +1.2), win rate
   30.5% (CI 21–40%) vs the 39.2% breakeven required by the +50/−30 exit geometry.**
   Not yet statistically damning (t = −1.73), but there is zero evidence of edge.
2. **Every current filter is a safety filter, not a return predictor.** The pipeline is
   good at avoiding honeypots and obvious scams (rug rate 9.5%), but "not a scam" ≠
   "goes up". All 5 recent trades passed every filter with score 7–8 and all lost.
3. **The one filter with measurable data points the wrong way.** Liquidity ≥ $50k — the
   current hard gate — selects the historically *worst* bucket (20% win, −16.4 avg)
   while sub-$50k entries did better (38% win, −3.9 avg). Not statistically significant
   (Welch t = 1.21), but there is no support for the gate at all.
4. **The entry trigger chases 5-minute momentum, and that is anti-edge in this data.**
   Entries after a +10%+ 5m pump: 0–17% win rate. Entries after a −10%+ 5m dip: the only
   positive-average bucket. Short-horizon mean reversion dominates at this timescale.
5. **The candidate stream itself is adverse-selected.** Both sources are *paid-promotion*
   feeds (DexScreener profiles/boosts = tokens whose devs paid to be seen). No fixed
   take-profit level between +20% and +50% makes this stream profitable (best case
   −10.1%/trade at TP +30%).
6. **The single highest-ROI change is not a filter or a model — it is data collection.**
   The scanner evaluates thousands of candidates per day (blocked-reason counters show
   ~7,000+ rejections) and throws the outcomes away. Rejected candidates' outcomes are
   never tracked, so false negatives are unmeasurable and no filter can be validated.
   Shadow-log everything, track outcomes, and the dataset grows ~1,000×
   faster than paper trading (capped at 2 entries/hour).

---

## Task 1 — Feature-by-feature audit

### Data caveats (read first)

- Enrichment fields (holder %, creator age/balance, score, sell impact) were only
  persisted starting v0.9: **n = 5–7** for those features. They are *unmeasurable*, not
  "proven useless". Older versions also had known enrichment bugs (see git log).
- Data pools across strategy versions with different thresholds → regime drift.
- Outcomes are truncated by the strategy's own exits (−30 SL / +50 TP / 6h), so
  "win rate" measures the exit geometry as much as the entry.
- Data-quality bugs found while auditing:
  - `price_change_5m` contains a 159,227% outlier (bad tick, old data).
  - `top_10_holders_pct == top_1_holder_pct` exactly in 4/6 recent trades — holder
    aggregation appears to sometimes count a single wallet. Verify
    `aggregate_real_wallet_holders` against Solscan for a few live mints.
  - Stop-losses set at −30% fill at −38.5% average (−8.5 pts of gap-through caused by
    180 s polling). Rug exits average −63%.

### Results (n = 95 unless noted)

| Feature | n | Finding | Verdict |
|---|---|---|---|
| **Liquidity at entry** | 95 | corr(win) = −0.16. <$30k: 47% win, +0.1 avg. $50–100k: 15–21% win, −15 to −40 avg. ≥$100k: 33% win, +0.4 | **Current ≥$50k gate is unsupported and likely inverted.** Replace hard gate with logging; protect against thin books via sell-impact instead |
| **Vol/liq ratio** | 34 | Best band 0.25–0.35 (29% win); 0.35–0.50 (allowed today): 13% win | No evidence for the 0.10–0.50 "Goldilocks" band. Demote to logged feature |
| **5m price change at entry** | 34 | Monotone-ish: dip buys (<−10%) +1.2 avg; pump chases (>+10%) −27 to −29 avg | **Strongest directional finding.** Stop buying green 5m candles; test dip-entry trigger |
| **Token age** | 34 | <45 min: 14% win. No monotone signal. Only ≥12h bucket ~breakeven | 30-min minimum may still be too early; unproven either way |
| **Volume 5m** | 34 | corr ≈ +0.11, buckets all negative | No signal |
| **Top1 / Top10 holders** | 5 | All 5 trades in the 4.6–9.8% top10 range lost | Unmeasurable. Keep top1>20% as scam gate only |
| **Creator age / balance** | 5 | All "good" creators; all lost | Unmeasurable as alpha; keep <1 day / <0.1 SOL as scam gate |
| **Sell impact** | 7 | All impacts <2.1%; no spread | Unmeasurable; keep >15% as honeypot gate |
| **Mint/freeze authority** | 95* | Never active among entered trades (gate blocks them upstream) | Pure scam gate; zero variance → no measurable alpha; keep (free) |
| **ArloBit score (6–8)** | 5 | 7s and 8s both 0% win | The score sums safety checks — it is not designed to predict returns and doesn't |
| **Source (profile/boost)** | 95 | profile: 30% win, −11.6 avg; boost: 33% win, +0.9 avg | Not significant; both adverse-selected feeds |
| **Entry hour (UTC)** | 95 | 12–20 UTC: 20–22% win, −14 to −20 avg; 00–08 UTC: 37% win, −4.7 avg | Weak, free to keep logging; do not gate yet |
| **Narrative (crude regex)** | 95 | KOL/person-named tokens −14.3 avg; animals −16.0; "other" −4.1 | Weak prior that celebrity-clone tokens underperform |
| **Max gain reached** | 95 | 46% of trades touch +20% before dying; 13 trades hit +30–50% and still closed −42 avg | Not an entry feature — an *exit-design* smoking gun (see Task 7) |

### False positives / false negatives

- **False positives:** 65/95 entries verdict SAFE → lost. FP rate on entries = 68.4%.
- **False negatives: unmeasurable.** The funnel counters
  (`liquidity_too_low` 1,483, `creator_risky` 1,142, `scam_creator` 1,042,
  `score_too_low` 991, `scam_holder` 937, …) count rejections but no outcomes are ever
  recorded for rejected candidates. This is the biggest structural hole in the system.

### Filters recommended for removal / demotion

| Filter | Action | Why |
|---|---|---|
| Liquidity ≥ $50k entry gate | **Demote to logged feature** (keep a $10–20k floor for tradability) | Data points the other way; sell-impact ≤15% already guards thin books |
| Vol/liq 0.10–0.50 band | **Demote to logged feature** | No supporting evidence; blocks 1,022 candidates/period unvalidated |
| `score_too_low` (<6) gate | **Demote**; keep score as a logged feature | Score is a safety checklist, uncorrelated with returns |
| Top10 > 40% "risky_holder" block | Relax to log-only (keep top1>20% and top10>60% scam gates) | Unvalidated middle band |
| Honeypot / mint / freeze / creator <1d / <0.1 SOL | **Keep** | Cheap catastrophic-loss insurance, near-zero opportunity cost |

Do not delete the removed filters' *computation* — keep computing and logging every
feature so the shadow dataset can validate them properly.

---

## Task 2 — Missing alpha, ranked by expected ROI

Ranked. Costs assume hobby/research scale.

| # | Signal | Why it should work | Edge | Effort | Data | Cost/mo | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | **Shadow outcome logging of ALL candidates** | Turns ~2k candidates/day into labeled data; enables everything below | Enabler | 2–4 days | Already flowing | $0 | Very high |
| 2 | **Buy/sell flow from data you already fetch** — DexScreener pair payload contains `txns.{m5,h1,h6,h24}.buys/sells`, volume & priceChange at 4 windows. Currently discarded. Buy/sell imbalance, swap acceleration (m5 vs h1 rate), volume acceleration | Order-flow imbalance is the classic short-horizon predictor; zero new API calls | Med | 1–2 days | Free (already in response) | $0 | High that it's worth testing |
| 3 | **Entry trigger inversion (dip entries)** | Directly supported by the n=34 5m-momentum finding | Med | Hours | Have it | $0 | Medium |
| 4 | **Deployer history / repeat-rugger DB** — for each creator wallet: prior mints (Helius `getSignaturesForAddress` + parse), prior token outcomes, cumulative rug count | Serial ruggers are extremely common on pump.fun; a "this dev's last 3 tokens died <1h" flag is a proven community heuristic | Med-High | 1–2 wks | Helius free/dev tier | $0–49 | High for avoidance, medium for alpha |
| 5 | **Holder growth velocity + fresh-wallet ratio of buyers** (share of buyers with wallet age <1 day = bundled/sybil buys) | Organic growth vs. bot-inflated volume; fresh-wallet buys signal wash trading | Med-High | 1–2 wks | Helius RPC | $0–49 | Medium |
| 6 | **Sniper/bundle detection** — cluster of wallets buying in the first blocks funded by the same source | Bundled launches dump on retail with near-certainty | Med-High | 2–3 wks | Helius | $0–49 | Medium-high (well-documented pattern) |
| 7 | **LP events** — LP lock/burn status, liquidity add/remove stream (Helius webhooks on Raydium/Meteora programs) | LP unlocked + removals = rug precursor; LP burn = commitment signal | Med (mostly loss-avoidance) | 1–2 wks | Helius webhooks | $0–49 | High for rug filter |
| 8 | **Smart-money overlap** — maintain a roster of wallets with verified realized PnL; flag tokens they buy early | The strongest known memecoin signal, but the hardest to build honestly | High | 1–2 mo (or buy) | Birdeye wallet PnL / GMGN (unofficial) / own indexer | $99–250 | Medium (edge decays as it's crowded) |
| 9 | **Social velocity** — pump.fun live feed, Telegram channel mention rate, X mention acceleration | Memecoins are attention assets; attention leads price | High but noisy | 2–4 wks | pump.fun free; X API $200/mo (Basic) | $0–200 | Medium |
| 10 | **Time-of-day / day-of-week gating** | 12–20 UTC underperforms in-sample | Low | Hours | Have it | $0 | Low (n small) |
| 11 | **Narrative tagging** (KOL-clone, animal, AI, political) | Weak in-sample evidence KOL-clones underperform; narrative rotation is real but hard to time | Low-Med | Days (LLM tagger) | Token name/metadata | ~$5 (Haiku) | Low-medium |
| 12 | Candidate source expansion — pump.fun graduations, Raydium new pools, GMGN/Birdeye trending, rather than only paid-promo feeds | Fixes adverse selection at the source | Med-High | 1 wk each | pump.fun/Raydium free | $0 | Medium-high |

Skip for now: funding-mixer forensics, wallet-graph ML, FDV/mcap velocity (redundant
with price/volume features), Moonshot metrics (niche), weekend effect (insufficient n).

---

## Task 3 — News layer design

Honest assessment first: **for 0–24h-old memecoins, macro crypto news (CoinDesk, The
Block, exchange listings) is nearly irrelevant** — the tradable "news" is attention
flow (Task 2 #9) and narrative fit. A news engine earns its keep in two narrow ways:

1. **Regime flag (cheap, do first):** SOL 1h/24h return + liquidation/exploit headlines
   → single risk-on/risk-off scalar. When SOL dumps 5%+, memecoin win rates collapse;
   pausing entries during risk-off is likely worth more than any headline analysis.
   Implementation: SOL price from Jupiter/CoinGecko free API + RSS keyword scan
   (exploit, hack, halt, depeg) over CoinDesk/TheBlock/Decrypt feeds. Effort: 1–2 days.
2. **Narrative tracker (second):** poll pump.fun trending + DexScreener trending +
   X/Telegram if budget allows; extract trending tokens/keywords; LLM
   (claude-haiku-4-5) tags each new candidate with narrative + "matches currently
   hot narrative?" boolean + sentiment. Feed as *features into the shadow dataset* —
   not directly into live scoring until validated.

Architecture:

```
feeds/  rss_poller (CoinDesk, TheBlock, Decrypt, SolanaFloor, Helius blog)
        pumpfun_poller (new launches + trending)
        sol_regime (price + funding)
     -> normalizer -> scorer (Haiku: {narrative, sentiment -1..+1, relevance})
     -> features table (joined to candidates by time window)
     -> regime flag consumed by scanner (pause / size down)
```

Cost: RSS $0; LLM scoring ~$5–15/mo at Haiku pricing; X API is the only expensive
input ($200/mo) — defer it. Expected edge: LOW for headlines, MEDIUM for regime flag,
MEDIUM for narrative fit. Confidence: medium. **Build after Tasks 1-fixes and Task 5,
not before.**

---

## Task 4 — On-chain metrics catalogue

| Metric | Why it works | Power | Difficulty | API | Cost/mo |
|---|---|---|---|---|---|
| Unique buyers per minute | Organic demand breadth; bots repeat, humans diversify | High | Med (parse swap txs) | Helius enhanced txs / webhooks | $0–49 |
| Buy/sell count imbalance | Direct demand pressure | High | **Trivial — already in DexScreener payload** | DexScreener | $0 |
| Swap count acceleration (m5 rate vs h1 rate) | Momentum in *participation*, less fakeable than volume | Med-High | Trivial (same payload) | DexScreener | $0 |
| Holder count growth velocity | Distribution breadth; rugs concentrate, winners distribute | Med-High | Med | Helius `getTokenAccountsByMint` sampled | $0–49 |
| Fresh-wallet ratio of recent buyers | Sybil/bundle detection | Med-High | Med-High | Helius (wallet first-tx lookup) | $49 |
| First-block sniper concentration | Bundled launches → guaranteed dump | Med-High | Med-High | Helius historical txs | $49 |
| LP lock/burn status | Rug precursor | Med (avoidance) | Med | RPC (LP token holders) / Rugcheck API free | $0 |
| LP add/remove events | Live rug alarm for open positions | Med (avoidance) | Med | Helius webhooks | $0–49 |
| Deployer prior-token outcomes | Serial ruggers repeat | Med-High | Med | Helius signatures + own DB | $0–49 |
| Deployer funding source (CEX vs fresh vs bridge) | Fresh-funded anonymous devs rug more | Med | Med-High | Helius | $49 |
| Top-holder cohort flow (are top10 accumulating or distributing since entry) | Insiders exit before price does | Med-High | Med | RPC polling of top accounts | $0 |
| Smart-money wallet buys | Best-documented signal; crowded | High (decaying) | High (build) / Low (buy) | Birdeye wallet-pnl $99+ / GMGN unofficial | $0–250 |
| Token supply changes / mint events | Only matters if mint authority active — already gated | Low | Done | RPC | $0 |
| pump.fun bonding-curve progress & graduation | Standardized lifecycle stage; graduation is a liquidity event | Med | Low-Med | pump.fun API (free/unofficial) | $0 |

Recommended stack: **Helius developer tier ($0 free → $49/mo)** covers 80% of this.
Birdeye/GMGN only when you get to smart-money (v2.0). Total research budget: $0–50/mo
until smart-money phase, then ~$150–300/mo.

---

## Task 5 — Backtesting / research framework

Constraint that drives the whole design: **DexScreener has no historical API — you
cannot backtest data you didn't record.** Paper trading at ≤2 entries/hour produces
~10–20 closed trades/day at best; you need thousands. The fix is shadow logging:

```
collector (extend scanner loop, keep it dumb):
  every cycle, for EVERY candidate seen (accepted or rejected):
    snapshot full feature vector + all blocked_reasons -> SQLite `candidates`
outcome_tracker (separate process):
  for each new mint, poll price at +5m +15m +30m +1h +2h +6h +24h
  (DexScreener token-pairs endpoint, batchable; ~1 req/mint/checkpoint)
  -> SQLite `outcomes` (price path, max gain, max drawdown, liq path, rug flag)
replay engine (pure offline, vectorized):
  entry rule  = boolean expression over candidate feature columns
  exit rule   = function of recorded price path (TP/SL/trail/time grid)
  metrics     = EV/trade, win rate, tail loss, trades/day, max drawdown
  search      = grid or random over rule space; walk-forward split by week;
                report in-sample vs out-of-sample; penalize multiple testing
                (only promote variants whose OOS EV > 0 with n >= 300)
```

- Storage: single SQLite file (upgradeable to parquet+duckdb); one row per
  candidate-snapshot, one per outcome checkpoint.
- Throughput: scanner already sees ~1,500–2,500 candidates/day (per blocked counters).
  Two weeks of collection ≈ 20–40k labeled examples → enough to evaluate 1,000+
  variants offline in seconds (it's just boolean masks over arrays).
- Guardrails against overfitting: fixed holdout weeks never used in search;
  pre-register a small set of hypotheses per batch; require economic rationale for any
  promoted rule; re-verify promoted rules in paper trading before believing them.
- Effort: 1–2 weeks total. Cost: $0. **This is the highest-ROI item in the entire
  document.**

---

## Task 6 — Machine learning

**Not now.** Current usable dataset: 95 truncated, version-drifted trades with mostly
missing features. Any model trained on this memorizes noise.

- **Prerequisite:** shadow dataset (Task 5). Define the label on *candidates*, not
  trades — e.g. "max gain ≥ +30% within 6h without −50% first". That gives thousands of
  labels/week instead of tens.
- **Data thresholds:** univariate stats + hand rules until ~2,000 labeled candidates
  with ≥300 positives. First model at ~5,000 (with ≥500 positives): logistic regression
  (calibrated, interpretable baseline), then LightGBM/XGBoost with monotonic
  constraints and walk-forward CV grouped by day (never random splits — leakage).
  CatBoost/RandomForest add nothing over LightGBM here. Neural/temporal models: not
  before ~100k examples, and probably never worth it at this scale.
- **Isolation Forest:** only defensible near-term use is unsupervised anomaly flags
  (weird volume/holder configurations) as a *feature*, not a gate.
- Expected improvement when done properly: modest — ML re-weights features you feed
  it; the alpha lives in the features (Task 2), not the model. Confidence: high.

---

## Task 7 — Risk management & exits

Current geometry (TP +50 / SL −30 / rug / 6h) requires 39.2% win rate; actual 30.5%.
Measured facts:

- SL −30 fills at **−38.5% average** (180s polling gap-through). Rugs: −63% average.
- 46% of trades touch +20% unrealized before closing; 13/95 touched +30–50% and still
  closed at −42% average. Enormous unrealized gains are being round-tripped.
- 48% of trades never reach +10% — dead on arrival, then bleed to −41 average.
- No fixed TP in +20…+50 fixes the stream (best: −10.1%/trade at TP+30).

| Mechanism | Assessment | Verdict |
|---|---|---|
| **Faster exit polling for open trades** (180s → 20–30s, or Helius websocket) | Directly attacks the −8.5pt SL slippage; also earlier rug detection | **Do now.** ~+3–5 pts/losing trade, high confidence |
| **Time stop** ("never reached +10% within 45–60 min and currently red → exit") | 48% of trades are DOA; converting −38 exits into ~−10/−15 exits is the largest measurable EV lever in the current data | **Do now**, validate exact params once price paths are logged |
| **Trailing stop** (arm at +25–30%, trail 15–20 pts) | Attacks the round-trip problem (13 trades gave back +30–50%). Cannot be sized precisely without price paths — log them | High priority right after path logging exists |
| **Partial TP** (e.g. 50% off at +25%) | Cuts variance, slightly improves EV vs TP+50 given 45% touch +25%; complements trailing stop on the remainder | Worth testing offline |
| **Break-even stop after +20%** | Special case of trailing; likely too tight for memecoin volatility (wicks) — test offline | Maybe |
| **Volatility stop / adaptive exits** | Needs per-token vol estimates from price paths; premature | Later (v1.5+) |
| **Kelly sizing / EV optimization** | Kelly of a negative-EV strategy is zero. Sizing comes *after* a validated positive edge; then fractional Kelly (¼) capped at 1–2% per trade | Not now — and this is the honest answer |

Also: log the full price path (every poll: price, liquidity, timestamp) for every open
paper trade. Without paths, no exit design is testable.

---

## Task 8 — Roadmap (prioritized by expected ROI)

### v1.1 — "Measure everything" (1–2 weeks, $0)
1. Shadow candidate logging: every candidate, full feature vector incl. currently
   discarded DexScreener fields (txns buys/sells all windows, volume/priceChange all
   windows, fdv, marketCap), all blocked reasons → SQLite.
2. Outcome tracker: price path checkpoints for every logged mint (+5m…+24h).
3. Price-path logging for open paper trades + exit polling 180s→30s.
4. Fix holder aggregation bug (top10==top1) and the price_change outlier guard.
5. Demote unvalidated hard gates (liq ≥$50k, vol/liq band, score ≥6, top10 30–40%) to
   logged features; keep scam gates. Add time stop (+10%/45min rule) as first exit fix.
   *KPI: 20k+ labeled candidates collected; paper expectancy > −5%/trade.*

### v1.2 — "Replay & re-derive" (2–4 weeks, $0)
1. Offline replay engine + walk-forward search over entry×exit grids (Task 5).
2. Re-derive entry rules from shadow data (test: dip-entry trigger, buy/sell imbalance,
   swap acceleration, liquidity bands, hour-of-day).
3. Trailing-stop / partial-TP parameter search on recorded paths.
4. SOL regime flag (pause entries on risk-off).
   *KPI: ≥1 entry rule with OOS EV > 0 on ≥300 shadow samples.*

### v1.5 — "On-chain depth" (1–2 months, ~$50/mo)
1. Deployer reputation DB (prior launches & outcomes per creator wallet).
2. LP lock/burn check + LP-event webhooks (live rug alarm).
3. Fresh-wallet ratio + first-block sniper/bundle detection.
4. Candidate source expansion: pump.fun graduations + Raydium new pools (fix adverse
   selection of paid-promo feeds).
5. First ML baseline (logistic → LightGBM) if ≥5k labeled candidates; narrative tagger.
   *KPI: validated positive-EV strategy on ≥4 consecutive OOS weeks of paper trading.*

### v2.0 — "Flow & attention" (3+ months, $150–300/mo)
1. Smart-money wallet roster (own indexer or Birdeye/GMGN), wallet clustering.
2. Social velocity ingestion (pump.fun trending, Telegram, X if budget).
3. Ensemble scoring (calibrated model + regime + narrative), fractional-Kelly sizing.
4. Execution realism: slippage model from Jupiter quotes at entry/exit sizes.
   *Gate to any live capital: ≥8 weeks positive OOS paper expectancy including fees
   and modeled slippage.*

---

## Bottom line

ArloBit currently answers "is this token a scam?" reasonably well and "will it go up?"
not at all — and it discards ~99% of the data it already pays API calls to see. Before
adding any new signal, build the shadow dataset and replay engine; every other decision
in this document becomes measurable instead of arguable within two weeks of collection.
