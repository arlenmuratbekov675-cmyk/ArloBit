# ArloBit Solana Scanner v0.7

Standalone terminal scanner for fresh Solana token pairs using the free
DexScreener API.

## Requirements

- Python 3.14+
- No API key
- No private keys
- No auto-buy

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
python scanner_v0.py --once --limit 20 --max-age-hours 24 --candidate-limit 100
```

`--once` runs a single scan cycle and exits. If neither `--once` nor `--loop` is
provided, the scanner behaves like `--once`.

## Loop Mode

`--loop` runs continuously and scans every 3 minutes by default:

```powershell
python scanner_v0.py --loop
```

Use a custom interval in seconds:

```powershell
python scanner_v0.py --loop --interval 180
```

Recommended Windows PowerShell command:

```powershell
$env:UV_SYSTEM_CERTS="true"
$env:TELEGRAM_BOT_TOKEN="123456789:your_bot_token"
$env:TELEGRAM_CHAT_ID="123456789"
$env:SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
python scanner_v0.py --loop --interval 180
```

Stop loop mode with `Ctrl+C`. The scanner catches API/RPC failures per cycle,
prints health output, and continues instead of exiting permanently.

## Data Sources

v0.7 starts from fresh DexScreener Solana token candidates:

- `GET /token-profiles/latest/v1`
- `GET /token-boosts/latest/v1`

Those endpoints do not include full pair metrics, so the scanner fetches pair
details with:

- `GET /token-pairs/v1/solana/{tokenAddress}`

Pair age comes from `pairCreatedAt`. By default, rows are included when pair age
is greater than 10 minutes and less than 24 hours. Missing `pairCreatedAt` is
shown as `unknown` and scored as `RISKY`.

v0.7 also checks the Solana mint account for each token:

- `getAccountInfo` on the token mint address
- SPL Token Mint layout parsing from base64 account data
- `mint_auth` shows whether mint authority is still active
- `freeze_auth` shows whether freeze authority is still active
- `getAccountInfo` calls are spaced by 0.3-0.5s by default
- HTTP 429 responses are retried once after a short backoff

The default RPC is:

```text
https://api.mainnet-beta.solana.com
```

Set a custom RPC endpoint with:

```powershell
$env:SOLANA_RPC_URL="https://your-rpc.example"
python scanner_v0.py
```

## Telegram Alerts

Telegram is optional. If `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is missing,
the scanner still runs and prints:

```text
Telegram disabled: missing env vars
```

The scanner auto-loads a local `.env` file at startup, so you can place
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `SOLANA_RPC_URL` there instead of
exporting them manually.

SAFE scoring can also be tuned from `.env` or CLI:

```powershell
$env:SAFE_MIN_LIQUIDITY_USD="30000"
$env:MIN_SAFE_VOLUME_LIQUIDITY_RATIO="0.01"
$env:MAX_SAFE_VOLUME_LIQUIDITY_RATIO="1.0"
$env:RPC_GET_ACCOUNT_INFO_MIN_DELAY_SECONDS="0.3"
$env:RPC_GET_ACCOUNT_INFO_MAX_DELAY_SECONDS="0.5"
$env:RPC_429_BACKOFF_SECONDS="1.0"
python scanner_v0.py --once
```

Alerts are sent only for rows that are already `SAFE`, have `mint_auth=no`,
have `freeze_auth=no`, and are between 10 minutes and 24 hours old. The scanner
deduplicates alerts by mint in a run and keeps a local `.arlobit_alerts.json`
state file to avoid alerting the same mint again across restarts. The same state
file also limits Telegram alerts to 2 per hour.

To create a Telegram bot:

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`.
3. Follow the prompts and copy the bot token.

To get `TELEGRAM_CHAT_ID`:

1. Send a message to your new bot, or add it to a group and send a message there.
2. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser.
3. Find `chat.id` in the JSON response.

Set env vars in PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="123456789:your_bot_token"
$env:TELEGRAM_CHAT_ID="123456789"
$env:SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
python scanner_v0.py --limit 20 --candidate-limit 100
```

See `.env.example` for the expected variable names.

Set a custom Telegram rate limit with:

```powershell
$env:TELEGRAM_ALERT_LIMIT_PER_HOUR="4"
python scanner_v0.py --loop
```

## Paper Trading

v0.7 includes simulated paper trading only. It never buys, sells, signs, or uses
private keys.

When a token is `SAFE` and passes the Telegram alert criteria, the scanner opens
a paper trade in `paper_trades.json` with:

- mint
- symbol
- entry price
- entry time
- liquidity at entry
- source

On each scan cycle, open paper trades are rechecked against DexScreener. The
scanner tracks current PnL, max gain, and max drawdown. A paper trade closes
automatically on:

- take profit: `+50%`
- stop loss: `-30%`
- rug: price drops at least `50%` from entry
- timeout: `6 hours`

Paper trade open and close messages are sent to Telegram when Telegram is
configured, using the same hourly rate limit.

Show paper trade stats:

```powershell
python scanner_v0.py --stats
```

Stats include an `exit reasons` breakdown for `take_profit`, `stop_loss`, `rug`,
`timeout`, and `other`.

## Verdicts

`SAFE` requires several conditions to pass, including fresh age, at least
`$30,000` liquidity by default, useful 5-minute volume, sane
volume/liquidity ratio, no
sharp 5-minute drawdown, inactive mint authority, and inactive freeze authority.

`RISKY` is used for unclear or mixed data, including missing age, low liquidity,
no 5-minute volume, mild volume/liquidity anomalies, RPC failure, or unparseable
mint account data.

`SCAM_LIKELY` is used for clear danger signals:

- active mint authority
- active freeze authority
- very low liquidity
- severe volume/liquidity anomaly
- price change 5m below `-30%`
- extreme 5m pump with weak liquidity
