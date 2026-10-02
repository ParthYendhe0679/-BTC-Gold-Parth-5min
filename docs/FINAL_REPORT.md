# Final Report — BTC/Gold-Parth 5min v3.0.0

Date: 2026-10-03. Related documents: [AUDIT.md](AUDIT.md) (initial audit) ·
[BACKTEST_RESULTS.md](BACKTEST_RESULTS.md) (full tables) · [../README.md](../README.md) (rules, setup, deployment).

## A. Initial audit

The full audit is in [AUDIT.md](AUDIT.md), with 16 defects (D1–D16). The root causes were:

| # | Problem | Evidence |
|---|---|---|
| D1 | Polling every 60 s = 2,880 Twelve Data credits/day vs the free limit of 800/day. On a free key the bot went silent about 7 h into each UTC day. | Code + Twelve Data plan limits |
| D2 | Render free plan spins down after 15 min without traffic, and the in-process scanner stops with it. | Render docs |
| D3 | No `timezone` parameter: XAU/USD came back in **UTC+10**, so gold timestamps were 10 h wrong. | Verified live with the real key |
| D4/D5 | Weak closed-candle handling. Alerts were "state differs from last sent" instead of a flip on a specific candle, which allowed late or mislabelled alerts. | Code |
| D6 | API key and bot token leaked into logs through exception URLs. | Code |
| D7–D10 | HTTP status always logged as "?"; legacy Markdown broke on `_`/`*`; permanent errors were retried; read timeouts were retried (risking duplicates); 429 `retry_after` was ignored. | Code |
| D11 | `/webhook` and `/scanner/test-alert` were unauthenticated. | Code |
| D12–D16 | Scans not aligned to candle close, `/` always reported "running", `render.yaml` missing the API key, deprecated asyncio calls, README describing files and endpoints that didn't exist. | Code |

Verified provider behavior: both BTC/USD and XAU/USD responses **include the still-forming candle**.

## B. Files changed

| File | Status | Why |
|---|---|---|
| `app/candles.py` | new | Candle validation (forming, future, duplicate, malformed, inconsistent) |
| `app/data_fetcher.py` | rewritten | `timezone=UTC`, classified retries, credit tracking, redaction |
| `app/strategies/base.py` | new | Common `Signal` schema, indicators, shared indicator cache |
| `app/strategies/legacy.py` | new | Supertrend (10,3), (20,3) and EMA 5 wrappers (original math) |
| `app/strategies/fib_golden_zone.py`, `pos_5ema.py`, `rsi2.py`, `liquidity_sweep.py` | new | The four new strategies |
| `app/strategies/__init__.py` | new | Registry and engine (failure isolation, agreement annotation) |
| `app/state.py` | new | SQLite dedup, cursors, delivery log and latency |
| `app/scanner.py` | rewritten | Bar-aligned scheduler, overlap lock, baseline, staleness, delivery queue |
| `app/telegram.py` | rewritten | HTML format with escaping, retry policy, test message that is not a signal |
| `app/telegram_commands.py` | new | `/start /help /status /strategies /test`, accepted only from `CHAT_ID` |
| `app/main.py` | rewritten | `/health`, task supervision, admin token |
| `app/webhook.py`, `app/models.py` | modified | Optional `WEBHOOK_SECRET` |
| `app/config.py` | modified | Strategy enable/disable, parameter overrides, new settings |
| `app/utils.py` | modified | `redact()` and time helpers |
| `app/backtest.py`, `scripts/fetch_history.py`, `scripts/run_backtest.py` | new | Backtesting and bounded optimisation |
| `tests/*` (80 tests), `pytest.ini`, `requirements-dev.txt` | new | Test suite |
| `render.yaml` | modified | `/health` check, all env vars declared, `--workers 1` |
| `README.md`, `.env.example`, `.gitignore`, `docs/*`, `reports/*` | new/modified | Documentation and results |

`app/strategy.py` is **unchanged**: the original Supertrend and EMA math is reused as-is.

## C. Repairs to the existing strategies

* Supertrend (10,3), Supertrend (20,3) and EMA 5: the math and parameters are **unchanged** (tests check this).
* Behavior fix: a signal is now the direction flip on a specific closed candle, and the
  alert carries that candle's time and price.
* Their alerts now also show the Supertrend line as a trailing stop.

## D. The four new strategies

Exact rules are in the README. In summary:

* **FIB_GOLDEN_ZONE:** the swing is confirmed 5 bars after the pivot (no repainting). The impulse must be ≥ 3 × ATR. Entry needs a touch of the 50–61.8% zone plus a confirmation candle that closes above the previous high (or below the previous low for SELL). Invalidation: close beyond 78.6%, break of the swing origin, or 48 bars. T1 = swing retest, T2 = 127.2%.
* **POS_5EMA** (inspired by public descriptions, not a reproduction): an alert candle fully above or below EMA5, triggered when the next candle breaks it. The signal fires at the close of the trigger candle. Stop = the alert candle's opposite extreme. Targets 2R and 3R. ATR quality filter.
* **RSI2_MEAN_REVERSION:** RSI(2) ≤ 10 or ≥ 90 on the previous candle, a turn candle, and the EMA200 trend filter. Exits: stop at 1.5 × ATR, close crossing SMA5, or 12 bars.
* **LIQUIDITY_SWEEP:** levels from swings, equal highs/lows, sessions and the previous day. Price must penetrate 0.05–1.0 ATR and reclaim the level on the same or next candle with a rejection. A close beyond the level without a reclaim marks it broken (a breakout, so no signal). Stop beyond the sweep extreme. Targets 1.5R and 3R.

## E. Telegram

* A delivery queue sends immediately after the scan, so a send never blocks evaluation.
* HTML messages with escaping; IST and UTC timestamps; only calculated fields are shown.
* Retries: 429 waits for `retry_after`, 5xx and connection errors use backoff, 4xx are not retried, and a read timeout becomes `UNCERTAIN` without a retry.
* **Measured:** Telegram send 1.18 s; strategy compute 61–69 ms per asset; about 10 s from candle close to alert (previously 0–60 s).
* No language rewrite (C++/Rust/Go): compute is under 1% of the total latency.

## F. Render reliability

* Service: web, **free plan**, 1 worker. Health check path `/health` (in `render.yaml`).
  If the service was created in the dashboard, set this there too.
* Lifecycle: scanner and command tasks start in the lifespan and are cancelled on shutdown. There is an in-process single-instance guard, and a scan that takes longer than 240 s is cancelled.
* `/health` returns 503 only when the scanner is dead or stalled. Provider problems report `degraded` with a 200.
* **Remaining limitation:** the free plan spins down after 15 minutes idle, and its disk is wiped on restart. For true 24/7 operation use a paid Starter instance. No keep-alive hack was added.

## G. Test results

* Before the changes: **no tests existed** (`pytest` collected 0).
* After: `python -m pytest -q` → **80 passed, 0 failed**.
* Coverage: candle validation, all 7 strategies including no-look-ahead (prefix-invariance) checks, dedup across restarts, staleness, failure isolation, the Telegram retry policy, secret redaction, `/health`, authentication, config, and backtest fills.
* Live local run with the real keys: both assets scanned, startup baseline worked, and **no secrets** appeared in the logs.

## H. Backtest results

Full tables are in [BACKTEST_RESULTS.md](BACKTEST_RESULTS.md).

| Data | Period | Cost | Result |
|---|---|---|---|
| XAU/USD (Twelve Data) | 2026-07-25 → 10-02, 20,000 bars | 0.02% | Only Supertrend (20,3) is positive: PF 1.04, test PF 1.09, 76 trades. This is noise-level, not a proven edge. All others are negative. |
| BTC/USD (Twelve Data) | same | 0.10% | All negative |
| BTCUSDT (Binance proxy) | 2026-04-05 → 10-02, 51,840 bars | 0.10% | All negative. Before costs, everything is roughly breakeven. |

The optimiser found no parameter set that improved test results, so the defaults are kept. There is **no 90–99% win rate** anywhere.

## I. Deployment steps

1. Render → Environment: keep `TWELVE_DATA_API_KEY`, `BOT_TOKEN` and `CHAT_ID`. Add `WEBHOOK_SECRET` and `ADMIN_TOKEN`.
2. Settings → Health Check Path = `/health`.
3. `git push` to `main` (auto-deploy).
4. Check that `/health` shows `"status": "ok"`, then send `/status` to the bot.
5. Optional: `DISABLED_STRATEGIES=EMA5_CROSS,POS_5EMA` to cut noise.

## J. Remaining risks

* The free plan sleeps, and dedup is not durable on its wiped disk.
* The backtest samples are short (about 10 weeks for Gold), and BTC 6-month data is a Binance proxy.
* Whether Twelve Data revises a bar after it closes is not documented.
* Session times are fixed in UTC and ignore daylight saving.
* **The bot token and API key were shared in a screenshot: rotate them.**

## K. Final status

| Item | Status |
|---|---|
| Implemented | ✅ |
| Tested locally (80 tests + live run) | ✅ |
| Committed / pushed to GitHub | ❌ waiting for approval |
| Deployed to Render | ❌ |
| Verified in production | ❌ |
