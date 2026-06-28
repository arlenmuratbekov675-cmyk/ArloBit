# ArloBit Solana Scanner v0.2

Standalone terminal scanner for fresh Solana token pairs using the free
DexScreener API.

## Requirements

- Python 3.14+
- No API key
- No private keys
- No auto-buy
- No Telegram integration

## Install

```powershell
python -m pip install -r requirements.txt
```

The `truststore` dependency lets `requests` use the operating system certificate
store, which is useful on Windows and managed networks.

## Run

```powershell
python scanner_v0.py
```

Optional:

```powershell
python scanner_v0.py --limit 20 --max-age-hours 24 --candidate-limit 100
```

## Data Source

v0.2 starts from fresh DexScreener Solana token candidates:

- `GET /token-profiles/latest/v1`
- `GET /token-boosts/latest/v1`

Those endpoints do not include full pair metrics, so the scanner fetches pair
details with:

- `GET /token-pairs/v1/solana/{tokenAddress}`

Pair age comes from `pairCreatedAt`. By default, rows are included when pair age
is greater than 10 minutes and less than 24 hours. Missing `pairCreatedAt` is
shown as `unknown` and scored as `RISKY`.

## Verdicts

`SAFE` requires several conditions to pass, including fresh age, strong
liquidity, useful 5-minute volume, non-anomalous volume/liquidity ratio, and no
sharp 5-minute drawdown.

`RISKY` is used for unclear or mixed data, including missing age, low liquidity,
no 5-minute volume, or mild volume/liquidity anomalies.

`SCAM_LIKELY` is used for clear danger signals:

- very low liquidity
- severe volume/liquidity anomaly
- price change 5m below `-30%`
- extreme 5m pump with weak liquidity
