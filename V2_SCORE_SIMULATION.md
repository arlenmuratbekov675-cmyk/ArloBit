# ArloBit v2 Score Simulation

Offline research only. No trading logic, filters, scoring code, execution, private keys, or signing were changed.

## Method

- Universe: labelled tokens with completed `labels` rows.
- Holdout: chronological 70/30 split because all labels are currently in one `holdout_week`.
- Current score: stored `candidate_sightings.arlobit_score`.
- V2 velocity score: percentile blend of `liquidity_change_1h`, `volume_change_1h`, `liquidity_change_15m`, `buy_count_change`, `buys_m5`, `price_change_velocity`, `volume_change_15m`.
- Combined score: 50/50 blend of current-score percentile and v2 velocity score.
- Selection rule for comparison: top 20% by each score within the evaluated split.
- Comparison universe: rows where current score, v2 velocity score, and combined score are all available.

## Results

| Score | Split | sample size | win rate (+100%) | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Current score | all top 20% | 44 | 22.7% | -59.95% | 0.04 | -100.00% | 45.5% | 22.7% | 0.0% | 38.6% |
| Current score | train top 20% | 31 | 22.6% | -62.50% | 0.04 | -100.00% | 41.9% | 22.6% | 0.0% | 38.7% |
| Current score | holdout top 20% | 13 | 30.8% | -43.95% | 0.20 | -100.00% | 61.5% | 30.8% | 0.0% | 30.8% |
| V2 velocity score | all top 20% | 44 | 47.7% | -53.56% | 0.16 | -100.00% | 72.7% | 47.7% | 4.5% | 47.7% |
| V2 velocity score | train top 20% | 31 | 41.9% | -55.16% | 0.15 | -100.00% | 67.7% | 41.9% | 3.2% | 45.2% |
| V2 velocity score | holdout top 20% | 13 | 53.8% | -47.80% | 0.19 | -100.00% | 84.6% | 53.8% | 7.7% | 53.8% |
| Combined score | all top 20% | 44 | 43.2% | -52.65% | 0.12 | -100.00% | 63.6% | 43.2% | 4.5% | 36.4% |
| Combined score | train top 20% | 31 | 38.7% | -51.60% | 0.15 | -100.00% | 61.3% | 38.7% | 3.2% | 38.7% |
| Combined score | holdout top 20% | 13 | 53.8% | -62.97% | 0.00 | -100.00% | 76.9% | 53.8% | 0.0% | 46.2% |

## Baseline

| Score | Split | sample size | win rate (+100%) | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Comparable universe | all | 224 | 24.6% | -37.04% | 0.29 | -100.00% | 43.8% | 24.6% | 2.2% | 22.3% |
| Comparable universe | train | 156 | 21.8% | -39.39% | 0.25 | -100.00% | 41.0% | 21.8% | 1.9% | 21.8% |
| Comparable universe | holdout | 68 | 30.9% | -31.67% | 0.39 | -100.00% | 50.0% | 30.9% | 2.9% | 23.5% |

## Coverage

- Total labelled rows: 443
- Comparable rows with all three scores: 224
- Rows with current score: 224
- Rows with v2 velocity score: 443

## Interpretation

- Treat this as feature research, not a deployment threshold.
- The v2 velocity score is intentionally simple and only tests whether the alpha-report candidates contain signal.
- A score should only be considered for ArloBit v2 after more labelled weeks and separate walk-forward validation.
