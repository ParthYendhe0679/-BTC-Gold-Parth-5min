# 📈 BTC/Gold-Parth 5min

A FastAPI service that scans **Bitcoin (BTC/USD)** and **Gold (XAU/USD)** on the
5-minute chart with seven independent, rule-based strategies and sends
confirmed candle-close signals to Telegram.

> **Signal generation only.** No order execution, position sizing, or account
> access. Signals are educational and not guaranteed outcomes. See
> [Backtest results](docs/BACKTEST_RESULTS.md): none of the strategies shows a
> reliable edge after costs on 5-minute data.

---

## Architecture

```
Render web service (1 Uvicorn worker)
└─ FastAPI lifespan
   ├─ scanner task (app/scanner.py) — wakes 8 s after every 5-min candle close
   │   per asset, concurrently and isolated:
   │   Twelve Data (timezone=UTC, 1 request) ─► candle validation (app/candles.py)
   │     ─► MarketContext (shared indicator cache)
   │     ─► 7 strategies (app/strategies/*) — one failing never blocks the others
   │     ─► cursor + SQLite dedup (app/state.py) ─► delivery queue
   ├─ Telegram delivery worker — HTML, classified retries, latency recorded
   └─ Telegram command poller (/start /help /status /strategies /test)
HTTP: GET / · GET /health · GET /scanner/status · GET /scanner/test-alert · POST /webhook
```

| Module | Role |
|---|---|
| `app/candles.py` | Rejects forming, future-dated, duplicate, malformed and inconsistent candles |
| `app/data_fetcher.py` | Twelve Data client: UTC timestamps, bounded retries, credit tracking, secret redaction |
| `app/strategies/` | Common `Signal` schema, indicators, and the 7 strategies |
| `app/strategy.py` | **Original** Supertrend / EMA math (unchanged, used by the wrappers) |
| `app/scanner.py` | Bar-aligned scheduler, overlap lock, cursors, dedup, staleness, delivery |
| `app/state.py` | SQLite: sent signals, delivery status and latency, per-strategy cursors |
| `app/telegram.py` | Message format and the Telegram sender |
| `app/backtest.py`, `scripts/` | Backtester, history downloader, bounded optimiser |

## Strategies

All strategies use **completed candles only**. Each is enabled by default and
can be switched off independently (see [Configuration](#configuration)).
The entry reference is the close of the confirmation candle.

| Id | Kind | Summary |
|---|---|---|
| `SUPERTREND_10_3` | retained | Supertrend ATR 10 × 3.0. Signal = trend flip on a closed candle. Stop shown = Supertrend line |
| `SUPERTREND_20_3` | retained | Supertrend ATR 20 × 3.0, same rules |
| `EMA5_CROSS` | retained | Close crosses to the other side of EMA 5 (very frequent, about 50/day per asset) |
| `FIB_GOLDEN_ZONE` | new | Retracement into the 50–61.8% zone of a confirmed impulse, then a confirmation candle |
| `POS_5EMA` | new | 5 EMA alert-candle breakout (inspired by publicly described concepts, **not** a reproduction) |
| `RSI2_MEAN_REVERSION` | new | Connors-style RSI(2) extreme + turn candle in the trend direction |
| `LIQUIDITY_SWEEP` | new | Trade beyond a liquidity level, then a close back through it (reclaim) |

### Retained strategies: what changed

The math is unchanged. Alerts are now raised on the **direction flip of a specific
closed candle**. Previously an alert fired when the current direction differed
from the last alert the bot had managed to send, which could produce late alerts
stamped with the wrong candle and price.

### FIB_GOLDEN_ZONE (v1.0)

* **Swing:** a pivot high at bar *j* is higher than the 5 bars before it and ≥ the 5 bars after. It is only used from bar *j+5* (confirmation), so there is no repainting.
* **Impulse:** on a new swing high H, Lo = the lowest low since the previous swing low. Require H − Lo ≥ 3 × ATR14. Bearish is the mirror image. The newest impulse replaces the active setup.
* **Zone:** 50%–61.8% retracement.
* **BUY:** price touched the 50% level within the last 2 candles, and the confirmation candle has close > open, close > previous high, and close ≥ 61.8% level and < H. Reward/risk to T1 must be ≥ 1.0.
* **Invalidation:** low < Lo, a close below the 78.6% level, a high above H before entry, or 48 candles elapsed. One alert per swing.
* **Stop:** min(retracement low, 78.6% level) − 0.1 ATR. **Targets:** T1 = H (retest), T2 = 127.2% extension.

### POS_5EMA (v1.0)

* **Alert candle:** SELL = candle entirely above EMA5 (low > EMA5). BUY = candle entirely below (high < EMA5).
* **Trigger:** the **next** candle breaks the alert low (SELL) or high (BUY). The alert expires after that one candle, unless the next candle is itself a new alert candle, which replaces it.
* **Signal:** at the close of the trigger candle (`CONFIRMED_TRIGGER`). Skipped if that candle closed back beyond the stop, or if the alert range is outside 0.25–3 × ATR14.
* **Stop:** the alert candle's opposite extreme. **Targets:** 2R and 3R. Optional EMA50 trend filter (off).

### RSI2_MEAN_REVERSION (v1.0)

* **Long:** RSI(2) of the **previous** candle ≤ 10, the current candle closes up (close > open and close > previous close), and close > EMA200. Short is the mirror image (≥ 90, below EMA200).
* **Exits:** stop = 1.5 × ATR14. Mean exit when close crosses SMA(5). Time stop after 12 candles. 3-candle cooldown.

### LIQUIDITY_SWEEP (v1.0)

* **Levels:** confirmed swing highs/lows (5 left / 3 right), equal highs/lows (within 0.1 ATR), Asia (00–07), London (07–13) and New York (13–21 UTC) session highs/lows, previous UTC-day high/low. Previous-candle levels are optional (off). Each level has an id, source, creation time and status (active, swept, broken, expired; max age 288 bars).
* **Bullish sweep:** low < level − 0.05 ATR, and penetration ≤ 1.0 ATR (deeper is treated as a breakout). Then either the same candle closes back above with lower wick / range ≥ 0.3, or the next candle closes back above as a bullish candle.
* **Sweep vs breakout:** if price **closes** beyond the level and does not close back within 1 candle, the level is marked *broken* and no reversal signal fires.
* **Stop:** sweep extreme ∓ 0.1 ATR. **Targets:** 1.5R and 3R (or nearest opposing levels with `target_mode: "liquidity"`). 6-candle cooldown per direction. When several levels are swept at once, the most significant is used (previous day > session > equal > swing).
* **Optional filters (off):** EMA200 trend, relative volume (only when the feed has volume; XAU/USD has none).
* These are price-reference levels. The bot does not see real orders.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `TWELVE_DATA_API_KEY` | yes | Market data |
| `BOT_TOKEN` | yes | Telegram bot token |
| `CHAT_ID` | yes | Chat that receives signals (and the only chat allowed to use commands) |
| `WEBHOOK_SECRET` | recommended | Required `"secret"` field for `POST /webhook` |
| `ADMIN_TOKEN` | recommended | Required `?token=` for `GET /scanner/test-alert` |
| `ENABLED_STRATEGIES` / `DISABLED_STRATEGIES` | no | Comma-separated strategy ids |
| `STRATEGY_PARAMS` | no | JSON overrides, e.g. `{"LIQUIDITY_SWEEP": {"trend_filter": true}}` |
| `TELEGRAM_COMMANDS_ENABLED` | no | default `true` |
| `SCAN_DELAY_SECONDS`, `MAX_SIGNAL_AGE_SECONDS`, `OUTPUT_SIZE`, `DAILY_CREDIT_BUDGET`, `STATE_DB_PATH` | no | See `.env.example` |

Example: keep only the Supertrends and the liquidity sweep:
`ENABLED_STRATEGIES=SUPERTREND_10_3,SUPERTREND_20_3,LIQUIDITY_SWEEP`

**API budget:** one request per asset per candle is 576 credits/day, within the
free plan's 800/day. The old 60-second polling used 2,880/day. Gold is not
fetched from Saturday until Sunday 21:00 UTC.

## Local setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # fill in real values; .env is git-ignored
python -m uvicorn app.main:app --reload
```

## Tests

```bash
python -m pytest -q
```

Tests are offline and cover candle validation, every strategy (including no-look-ahead
prefix-invariance checks), dedup across restarts, staleness, failure isolation,
the Telegram retry policy, secret redaction, the health endpoint, auth, config, and backtest fills.

## Backtesting

```bash
python scripts/fetch_history.py --source twelvedata --symbol XAU/USD --bars 20000   # 4 credits
python scripts/fetch_history.py --source binance --symbol BTCUSDT --days 180
python scripts/run_backtest.py data/twelvedata_XAUUSD_5m.csv --symbol XAU/USD --cost 0.02 --optimize
```

Methodology: 60/20/20 chronological train/validation/test split. Entries at the
candle close. If stop and target are both hit inside one candle, it is counted as
a stop. Gaps fill at the open. Round-trip costs are deducted, with 0× and 2× cost
sensitivity reported. The optimiser uses a small fixed grid; a combination must pass
train gates and is chosen on validation only. Results are never auto-applied to
live settings. See [docs/BACKTEST_RESULTS.md](docs/BACKTEST_RESULTS.md).

## Telegram

* **Signals** include instrument, strategy, setup, levels, entry, stop, targets,
  R:R, the candle close time in UTC and IST, the reason, the signal id, and a disclaimer.
  Fields that a strategy does not calculate are omitted.
* **Commands** (only from `CHAT_ID`): `/status`, `/strategies`, `/test`
  (a connectivity message that is clearly not a signal), `/help`.
* **Delivery:** a 429 waits for `retry_after`. 5xx and connection errors use backoff, up to 3 attempts. 4xx errors are not retried. A **read timeout is not retried** (Telegram may already have delivered the message), and it is recorded as `UNCERTAIN`.

## Health and diagnostics

* `GET /health`: scanner task, last loop and last successful scan, latest candle per
  asset, provider errors, credits, Telegram delivery status, and signal counts by status.
  It returns **503 only if the scanner is dead or stalled**, so Render restarts it.
  Provider or Telegram trouble shows as `"degraded"` with a 200.
* `GET /scanner/status`: per-strategy status and the 20 most recent signals with
  delivery status, attempts, errors and latency.

**Diagnosing alerts**

* *Missing:* check `/health`. A sleeping service means the free plan spun down (see below); a provider error may mean the credit budget was exceeded.
  `STALE_SKIPPED` means the signal was older than `MAX_SIGNAL_AGE_SECONDS`.
* *Duplicate:* every signal has an id and a unique setup key. A duplicate usually means two instances are running.
* *Delayed:* `latency_ms` in `/scanner/status` is the time from candle close to Telegram acceptance.

## Deployment (Render)

1. Environment: `TWELVE_DATA_API_KEY`, `BOT_TOKEN`, `CHAT_ID` (already set), plus
   `WEBHOOK_SECRET` and `ADMIN_TOKEN` (recommended).
2. Health check path: `/health`. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
3. Push to `main` (auto-deploy). After deploy, check `/health` shows `"status": "ok"`,
   then send `/status` to the bot.

**Free plan limitation:** free web services sleep after 15 minutes without inbound
HTTP traffic, and the in-process scanner stops with them. Free instances also lose
their disk on every restart, so dedup falls back to the startup baseline. The bot
then never re-sends after a restart, but can miss a signal on the restart candle.
Supported options:

* **Paid web service** (e.g. Starter): always on. This is the recommended option.
* **Paid background worker**: possible, but `/health` and the webhook would need a separate web service.
* An external uptime monitor that calls `/health` every few minutes keeps a free instance
  awake. This is ordinary inbound traffic, but check Render's current terms, and note that the
  750 free instance-hours per month cover just one always-on service.

The bot itself contains no keep-alive or self-ping code.

## Known limitations

* 5-minute backtests cover about 10 weeks for Gold (Twelve Data) and 6 months for BTC (Binance BTCUSDT as a proxy). Regime coverage is limited.
* Twelve Data does not document whether bars are revised after close. The 8 s delay and the validator reduce the risk but cannot eliminate it.
* XAU/USD has no volume, so volume filters are skipped for gold.
* Session times are fixed in UTC and do not shift with daylight saving.

## Security

Secrets are read only from the environment, and `.env` is git-ignored. API keys and bot tokens
are redacted from logs and errors. Webhook and test endpoints support shared-secret auth,
and Telegram commands are accepted only from `CHAT_ID`.
