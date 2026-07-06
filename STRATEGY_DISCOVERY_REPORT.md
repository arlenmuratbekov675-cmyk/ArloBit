# Strategy Discovery Report

Research-only offline strategy search. No scanner, filter, current score, execution, or trading logic was changed.

## Search Summary

- Generated at: 2026-07-06 01:13:13 UTC
- Labelled completed trades: 478
- Chronological train trades: 334
- Chronological holdout trades: 144
- Features loaded: 43
- Candidate predicates: 109
- Predicates used after train ranking: 68
- Strategies tested: 398883
- Strategies evaluated with n >= 30: 3743
- Rejected for insufficient completed trades: 395140
- Skipped same-feature combinations: 54048

## Baseline

| split | n | win rate | expectancy | profit factor | max drawdown | +50% | +100% | +500% | rug rate | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 478 | 21.5% | 373721.70% | 7734.74 | -100.00% | 38.3% | 21.5% | 2.3% | 21.1% | -7.62 |
| train | 334 | 20.1% | -30.03% | 0.35 | -100.00% | 36.5% | 20.1% | 1.5% | 18.0% | -7.37 |
| holdout | 144 | 25.0% | 1240618.07% | 23020.34 | -100.00% | 42.4% | 25.0% | 4.2% | 28.5% | -3.23 |

## Rejection Summary

- train_expectancy_not_positive: 2532
- too_few_holdout_trades: 890
- too_few_train_trades: 279
- holdout_expectancy_not_positive: 16
- accepted: 11
- small_sample_outlier_risk: 10
- holdout_no_win_lift: 5

## TOP 20 STRATEGIES

| size | split | n | win rate | expectancy | profit factor | average return | max drawdown | +50% | +100% | +500% | rug rate | Sharpe | 95% CI | p-value | lift | robustness | rule |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2 | all | 37 | 32.4% | 19.19% | 1.50 | 19.19% | -100.00% | 51.4% | 32.4% | 0.0% | 18.9% | 0.90 | [19.6%, 48.5%] | 0.0937 | 1.51 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] |
| 2 | train | 18 | 33.3% | 21.60% | 1.52 | 21.60% | -100.00% | 55.6% | 33.3% | 0.0% | 5.6% | 0.64 | [16.3%, 56.3%] | 0.1482 | 1.66 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] |
| 2 | holdout | 19 | 31.6% | 16.90% | 1.47 | 16.90% | -100.00% | 47.4% | 31.6% | 0.0% | 31.6% | 0.64 | [15.4%, 54.0%] | 0.4772 | 1.26 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] |
| 3 | all | 37 | 32.4% | 19.19% | 1.50 | 19.19% | -100.00% | 51.4% | 32.4% | 0.0% | 18.9% | 0.90 | [19.6%, 48.5%] | 0.0937 | 1.51 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | train | 18 | 33.3% | 21.60% | 1.52 | 21.60% | -100.00% | 55.6% | 33.3% | 0.0% | 5.6% | 0.64 | [16.3%, 56.3%] | 0.1482 | 1.66 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | holdout | 19 | 31.6% | 16.90% | 1.47 | 16.90% | -100.00% | 47.4% | 31.6% | 0.0% | 31.6% | 0.64 | [15.4%, 54.0%] | 0.4772 | 1.26 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | all | 37 | 32.4% | 19.19% | 1.50 | 19.19% | -100.00% | 51.4% | 32.4% | 0.0% | 18.9% | 0.90 | [19.6%, 48.5%] | 0.0937 | 1.51 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | train | 18 | 33.3% | 21.60% | 1.52 | 21.60% | -100.00% | 55.6% | 33.3% | 0.0% | 5.6% | 0.64 | [16.3%, 56.3%] | 0.1482 | 1.66 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | holdout | 19 | 31.6% | 16.90% | 1.47 | 16.90% | -100.00% | 47.4% | 31.6% | 0.0% | 31.6% | 0.64 | [15.4%, 54.0%] | 0.4772 | 1.26 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 37 | 32.4% | 19.19% | 1.50 | 19.19% | -100.00% | 51.4% | 32.4% | 0.0% | 18.9% | 0.90 | [19.6%, 48.5%] | 0.0937 | 1.51 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.early_buyer_count=<= 0.00 |
| 3 | train | 18 | 33.3% | 21.60% | 1.52 | 21.60% | -100.00% | 55.6% | 33.3% | 0.0% | 5.6% | 0.64 | [16.3%, 56.3%] | 0.1482 | 1.66 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.early_buyer_count=<= 0.00 |
| 3 | holdout | 19 | 31.6% | 16.90% | 1.47 | 16.90% | -100.00% | 47.4% | 31.6% | 0.0% | 31.6% | 0.64 | [15.4%, 54.0%] | 0.4772 | 1.26 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 37 | 32.4% | 19.19% | 1.50 | 19.19% | -100.00% | 51.4% | 32.4% | 0.0% | 18.9% | 0.90 | [19.6%, 48.5%] | 0.0937 | 1.51 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.elite_count=<= 0.00 |
| 3 | train | 18 | 33.3% | 21.60% | 1.52 | 21.60% | -100.00% | 55.6% | 33.3% | 0.0% | 5.6% | 0.64 | [16.3%, 56.3%] | 0.1482 | 1.66 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.elite_count=<= 0.00 |
| 3 | holdout | 19 | 31.6% | 16.90% | 1.47 | 16.90% | -100.00% | 47.4% | 31.6% | 0.0% | 31.6% | 0.64 | [15.4%, 54.0%] | 0.4772 | 1.26 | 4.79 | velocity.price_change_velocity=(0.04, 17868324.31] AND velocity.buy_sell_ratio_change=(-0.05, 0.13] AND wallet.elite_count=<= 0.00 |
| 2 | all | 44 | 29.5% | 28.93% | 1.76 | 28.93% | -100.00% | 56.8% | 29.5% | 2.3% | 18.2% | 1.22 | [18.2%, 44.2%] | 0.1757 | 1.37 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] |
| 2 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] |
| 2 | holdout | 24 | 29.2% | 22.43% | 1.52 | 22.43% | -100.00% | 50.0% | 29.2% | 4.2% | 29.2% | 0.58 | [14.9%, 49.2%] | 0.6056 | 1.17 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] |
| 3 | all | 44 | 29.5% | 28.93% | 1.76 | 28.93% | -100.00% | 56.8% | 29.5% | 2.3% | 18.2% | 1.22 | [18.2%, 44.2%] | 0.1757 | 1.37 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | holdout | 24 | 29.2% | 22.43% | 1.52 | 22.43% | -100.00% | 50.0% | 29.2% | 4.2% | 29.2% | 0.58 | [14.9%, 49.2%] | 0.6056 | 1.17 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | all | 44 | 29.5% | 28.93% | 1.76 | 28.93% | -100.00% | 56.8% | 29.5% | 2.3% | 18.2% | 1.22 | [18.2%, 44.2%] | 0.1757 | 1.37 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | holdout | 24 | 29.2% | 22.43% | 1.52 | 22.43% | -100.00% | 50.0% | 29.2% | 4.2% | 29.2% | 0.58 | [14.9%, 49.2%] | 0.6056 | 1.17 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 44 | 29.5% | 28.93% | 1.76 | 28.93% | -100.00% | 56.8% | 29.5% | 2.3% | 18.2% | 1.22 | [18.2%, 44.2%] | 0.1757 | 1.37 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.early_buyer_count=<= 0.00 |
| 3 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.early_buyer_count=<= 0.00 |
| 3 | holdout | 24 | 29.2% | 22.43% | 1.52 | 22.43% | -100.00% | 50.0% | 29.2% | 4.2% | 29.2% | 0.58 | [14.9%, 49.2%] | 0.6056 | 1.17 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 44 | 29.5% | 28.93% | 1.76 | 28.93% | -100.00% | 56.8% | 29.5% | 2.3% | 18.2% | 1.22 | [18.2%, 44.2%] | 0.1757 | 1.37 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.elite_count=<= 0.00 |
| 3 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.elite_count=<= 0.00 |
| 3 | holdout | 24 | 29.2% | 22.43% | 1.52 | 22.43% | -100.00% | 50.0% | 29.2% | 4.2% | 29.2% | 0.58 | [14.9%, 49.2%] | 0.6056 | 1.17 | 4.59 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND wallet.elite_count=<= 0.00 |
| 3 | all | 43 | 27.9% | 28.20% | 1.72 | 28.20% | -100.00% | 55.8% | 27.9% | 2.3% | 18.6% | 1.16 | [16.7%, 42.7%] | 0.2877 | 1.30 | 4.08 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND candidate.source=profile |
| 3 | train | 20 | 30.0% | 36.73% | 2.14 | 36.73% | -99.96% | 65.0% | 30.0% | 0.0% | 5.0% | 1.21 | [14.5%, 51.9%] | 0.2522 | 1.50 | 4.08 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND candidate.source=profile |
| 3 | holdout | 23 | 26.1% | 20.78% | 1.46 | 20.78% | -100.00% | 47.8% | 26.1% | 4.3% | 30.4% | 0.49 | [12.5%, 46.5%] | 0.8955 | 1.04 | 4.08 | velocity.buy_sell_ratio_change=(-0.05, 0.13] AND candidate.buy_sell_ratio_m5=(0.55, 0.64] AND candidate.source=profile |

## Worst 20 Strategies

| size | split | n | win rate | expectancy | profit factor | average return | max drawdown | +50% | +100% | +500% | rug rate | Sharpe | 95% CI | p-value | lift | robustness | rule |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 3 | all | 51 | 29.4% | -88.41% | 0.00 | -88.41% | -100.00% | 49.0% | 29.4% | 2.0% | 74.5% | -48.53 | [18.7%, 43.0%] | 0.1484 | 1.36 | -3.30 | bucket.liquidity=10k-25k AND candidate.source=profile AND candidate.sells_m5=> 98.00 |
| 2 | all | 52 | 30.8% | -86.50% | 0.00 | -86.50% | -100.00% | 50.0% | 30.8% | 1.9% | 73.1% | -33.29 | [19.9%, 44.3%] | 0.0867 | 1.43 | -2.69 | bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 |
| 3 | all | 52 | 30.8% | -86.50% | 0.00 | -86.50% | -100.00% | 50.0% | 30.8% | 1.9% | 73.1% | -33.29 | [19.9%, 44.3%] | 0.0867 | 1.43 | -2.69 | bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | all | 52 | 30.8% | -86.50% | 0.00 | -86.50% | -100.00% | 50.0% | 30.8% | 1.9% | 73.1% | -33.29 | [19.9%, 44.3%] | 0.0867 | 1.43 | -2.69 | bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 52 | 30.8% | -86.50% | 0.00 | -86.50% | -100.00% | 50.0% | 30.8% | 1.9% | 73.1% | -33.29 | [19.9%, 44.3%] | 0.0867 | 1.43 | -2.69 | bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 52 | 30.8% | -86.50% | 0.00 | -86.50% | -100.00% | 50.0% | 30.8% | 1.9% | 73.1% | -33.29 | [19.9%, 44.3%] | 0.0867 | 1.43 | -2.69 | bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 AND wallet.elite_count=<= 0.00 |
| 3 | all | 30 | 33.3% | -85.62% | 0.00 | -85.62% | -100.00% | 56.7% | 33.3% | 0.0% | 66.7% | -30.34 | [19.2%, 51.2%] | 0.1049 | 1.55 | -2.98 | velocity.price_change_velocity=<= -218785.69 AND bucket.liquidity=10k-25k AND candidate.sells_m5=> 98.00 |
| 3 | all | 46 | 30.4% | -85.53% | 0.00 | -85.53% | -100.00% | 50.0% | 30.4% | 0.0% | 71.7% | -29.50 | [19.1%, 44.8%] | 0.1231 | 1.41 | -2.88 | bucket.liquidity=10k-25k AND velocity.sell_count_change=<= -74.00 AND candidate.sells_m5=> 98.00 |
| 3 | all | 48 | 29.2% | -84.27% | 0.01 | -84.27% | -100.00% | 50.0% | 29.2% | 0.0% | 70.8% | -26.19 | [18.2%, 43.2%] | 0.1759 | 1.35 | -3.03 | bucket.liquidity=10k-25k AND candidate.source=profile AND velocity.sell_count_change=<= -74.00 |
| 3 | all | 34 | 35.3% | -84.26% | 0.00 | -84.26% | -100.00% | 55.9% | 35.3% | 0.0% | 70.6% | -22.74 | [21.5%, 52.1%] | 0.0431 | 1.64 | -2.69 | bucket.token_age=15m-1h AND velocity.sell_count_change=<= -74.00 AND candidate.sells_m5=> 98.00 |
| 3 | all | 37 | 29.7% | -84.13% | 0.02 | -84.13% | -100.00% | 59.5% | 29.7% | 2.7% | 59.5% | -19.14 | [17.5%, 45.8%] | 0.2076 | 1.38 | -3.36 | candidate.liquidity_usd=(18203.28, 32848.94] AND candidate.source=profile AND velocity.sell_count_change=<= -74.00 |
| 3 | all | 37 | 29.7% | -84.13% | 0.02 | -84.13% | -100.00% | 59.5% | 29.7% | 2.7% | 59.5% | -19.14 | [17.5%, 45.8%] | 0.2076 | 1.38 | -3.36 | candidate.liquidity_usd=(18203.28, 32848.94] AND candidate.source=profile AND candidate.sells_m5=> 98.00 |
| 2 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 |
| 2 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND candidate.sells_m5=> 98.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 AND candidate.sells_m5=> 98.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 AND cluster.cooccurring_pairs=<= 0.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND velocity.sell_count_change=<= -74.00 AND wallet.elite_count=<= 0.00 |
| 3 | all | 39 | 30.8% | -84.05% | 0.02 | -84.05% | -100.00% | 59.0% | 30.8% | 2.6% | 56.4% | -20.15 | [18.6%, 46.4%] | 0.1439 | 1.43 | -2.94 | candidate.liquidity_usd=(18203.28, 32848.94] AND candidate.sells_m5=> 98.00 AND cluster.cooccurring_pairs=<= 0.00 |

## Most Robust Strategies

| size | split | n | win rate | expectancy | profit factor | average return | max drawdown | +50% | +100% | +500% | rug rate | Sharpe | 95% CI | p-value | lift | robustness | rule |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 |
| 2 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 |
| 2 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 |
| 2 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.elite_count=<= 0.00 |
| 2 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.elite_count=<= 0.00 |
| 2 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.elite_count=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 3 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 4 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 4 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 4 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | all | 44 | 72.7% | -12.86% | 0.76 | -12.86% | -100.00% | 90.9% | 72.7% | 11.4% | 29.5% | -0.81 | [58.2%, 83.7%] | 0.0000 | 3.38 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | train | 27 | 70.4% | -35.00% | 0.33 | -35.00% | -100.00% | 92.6% | 70.4% | 3.7% | 29.6% | -2.20 | [51.5%, 84.1%] | 0.0000 | 3.51 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 4 | holdout | 17 | 76.5% | 22.30% | 1.41 | 22.30% | -100.00% | 88.2% | 76.5% | 23.5% | 29.4% | 0.29 | [52.7%, 90.4%] | 0.0000 | 3.06 | 13.69 | velocity.liquidity_change_1h=> 6.44 AND cluster.suspicious_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 AND wallet.elite_count=<= 0.00 |
| 2 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 |
| 2 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.suspicious_pairs=<= 0.00 |
| 2 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.early_buyer_count=<= 0.00 |
| 2 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.early_buyer_count=<= 0.00 |
| 2 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.early_buyer_count=<= 0.00 |
| 2 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.elite_count=<= 0.00 |
| 2 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.elite_count=<= 0.00 |
| 2 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND wallet.elite_count=<= 0.00 |
| 3 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND cluster.suspicious_pairs=<= 0.00 |
| 3 | all | 43 | 67.4% | 4154708.00% | 69080.62 | 4154708.00% | -100.00% | 88.4% | 67.4% | 16.3% | 41.9% | -0.67 | [52.5%, 79.5%] | 0.0000 | 3.13 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | train | 22 | 59.1% | -49.20% | 0.21 | -49.20% | -100.00% | 86.4% | 59.1% | 4.5% | 40.9% | -3.45 | [38.7%, 76.7%] | 0.0000 | 2.95 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |
| 3 | holdout | 21 | 76.2% | 8507310.79% | 146039.29 | 8507310.79% | -100.00% | 90.5% | 76.2% | 28.6% | 42.9% | 0.44 | [54.9%, 89.4%] | 0.0000 | 3.05 | 12.49 | velocity.liquidity_change_15m=> 4.91 AND cluster.cooccurring_pairs=<= 0.00 AND wallet.early_buyer_count=<= 0.00 |

## Features That Repeatedly Appear In Winning Strategies

- velocity.buy_sell_ratio_change: 11
- candidate.buy_sell_ratio_m5: 6
- velocity.price_change_velocity: 5
- cluster.cooccurring_pairs: 2
- cluster.suspicious_pairs: 2
- wallet.early_buyer_count: 2
- wallet.elite_count: 2
- candidate.source: 1

## Method Notes

- Buckets are derived from the chronological training split, then applied to holdout.
- Strategy rules are simple AND combinations of 2, 3, or 4 feature buckets.
- A strategy must have at least 30 completed trades overall, plus train and holdout coverage.
- Acceptance requires positive expectancy and profit factor on both train and holdout.
- Ranking uses capped expectancy for robustness so one extreme token cannot dominate the list.
- The raw average return and expectancy columns are still reported for transparency.

## Conclusion

Validated offline candidates exist, but they remain research candidates only until tested on fresh data.
