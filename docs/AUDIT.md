# Initial Audit — BTC/Gold-Parth 5min (commit `53c6e03`)

Audit performed on 2026-10-03 against the local checkout, which was verified to be
identical to `origin/main`. Every finding below references code as it existed
**before** this change set.

## 1. Architecture (as found)

Single Render **free-plan web service** (`render.yaml`), one Uvicorn process:

```
FastAPI lifespan ──► asyncio task run_scanner()  (app/scanner.py)
   every 60 s, for each asset (BTC/USD, XAU/USD):
     fetch_candles()  (app/data_fetcher.py, blocking requests in thread pool, outputsize=200)
       └─► candles_to_dataframe()  (UTC→IST)
       └─► drop rows where open_time + 5 min > now   ("closed candle" filter)
       └─► for each strategy in settings.STRATEGIES:
             calculate_supertrend / calculate_ema_strategy  (app/strategy.py)
             compare last-row direction with in-memory _last_signals
             send_scanner_alert()  (app/telegram.py, Markdown, blocking + time.sleep retries)
POST /webhook  (TradingView → Telegram passthrough, no authentication)
GET  /  (static "running"), GET /scanner/status, GET /scanner/test-alert
```

No tests, no persistence (all state in process memory), no `.env.example`
(although the README tells users to copy one), no `/health` route (although the
README documents one).

## 2. Strategies found

| Name (config) | Type | Parameters | Signal rule |
|---|---|---|---|
| Supertrend (10,3) | supertrend | ATR 10 (Wilder RMA), mult 3.0 | Alert when the last closed bar's direction differs from the last alerted direction |
| Supertrend (20,3) | supertrend | ATR 20, mult 3.0 | same |
| EMA 5 | ema | EMA 5 of close | Alert when close-vs-EMA side differs from last alerted side |

The README and project memory call the two Supertrends the core strategies. The
EMA 5 strategy was added in the latest commit. All three are retained.

The Supertrend math matches Pine's `ta.supertrend` after warm-up. During warm-up,
band values differ because zeros are used where Pine has `na`. That is harmless
with 200+ bars, so the math is preserved unchanged.

## 3. Confirmed defects

| # | Severity | Location | Defect |
|---|---|---|---|
| D1 | **Critical** | `config.py:37`, `scanner.py:323` | 2 Twelve Data requests every 60 s = **2,880 credits/day**. The free Basic plan allows **800/day** (8/min), so on a free key the quota runs out about 6.7 h into each UTC day and the bot goes silent for the rest of the day. |
| D2 | **Critical** | `render.yaml` (`plan: free`) | Render free web services **spin down after 15 min without inbound HTTP traffic**. The scanner is an in-process asyncio task, so it stops whenever the service sleeps. It resumes only when someone hits the URL, and then silently re-baselines without alerting. |
| D3 | High | `data_fetcher.py:52` | No `timezone` parameter is sent. Twelve Data's default is the **exchange** timezone, but `candles_to_dataframe` assumes UTC. If a symbol's exchange tz is not UTC, the closed-candle filter is wrong by hours: it either drops every bar (falling back to D4) or keeps a forming bar. |
| D4 | High | `scanner.py:249-250` | If the time filter removes every row, the code falls back to `df_base.iloc[:-1]`, which uses unvalidated data. Future-dated, duplicate, and malformed (high<low, NaN) rows are never rejected. |
| D5 | High | `scanner.py:174-197` | Signals are "state differs from last *sent* state", not "flip on this candle". After a failed send or missed scans, a later candle can trigger a late alert labelled with the wrong candle and price. After a restart, the first scan swallows a flip on the current bar. |
| D6 | High | `data_fetcher.py:113`, `telegram.py:65` | Exception messages are logged verbatim. `requests` connection errors include the full URL, so the **Twelve Data `apikey` and the Telegram bot token leak into logs**. |
| D7 | Medium | `data_fetcher.py:119`, `telegram.py:67` | `exc.response.status_code if exc.response else "?"` — `requests.Response.__bool__` returns `resp.ok`, which is False for every 4xx/5xx, so the HTTP status is **always logged as "?"**. |
| D8 | Medium | `telegram.py:43` | Legacy `Markdown` parse mode with unescaped dynamic text (webhook fields, strategy names). Any `_`, `*` or `` ` `` causes a 400 "can't parse entities", which is then retried pointlessly. |
| D9 | Medium | `telegram.py:47-75` | Retries every failure, including permanent 400/401/403 and uncertain read timeouts (which can duplicate a message). Ignores Telegram's 429 `retry_after`. |
| D10 | Medium | `data_fetcher.py:74-78` | Retries permanent errors (bad key / bad symbol). On a 429 it sleeps up to 45 s inside a thread-pool thread, which stalls the whole scan. |
| D11 | Medium | `webhook.py` | `/webhook` is unauthenticated: anyone who finds the URL can post arbitrary text to the Telegram chat. `GET /scanner/test-alert` is also unauthenticated and is a GET, so link previews and crawlers can trigger it. |
| D12 | Medium | `scanner.py` | The scan is not aligned to candle closes: 60 s polling means alerts arrive 0–60 s after close, and one asset's fetch retries (up to ~30 s) delay the other asset. |
| D13 | Low | `main.py` | `/` always returns "running", even if the scanner task has died. The `scanner_state["running"]` flag is only cleared on cancellation. |
| D14 | Low | `render.yaml` | `TWELVE_DATA_API_KEY` is not declared in `envVars` (it must have been added by hand in the dashboard). |
| D15 | Low | `scanner.py` | `asyncio.get_event_loop()` is used inside coroutines (deprecated). Audit output uses `print` instead of logging. |
| D16 | Low | README | Documents a `/health` endpoint and a `.env.example` file that do not exist. |

### Live verification (2026-10-02 18:57:32 UTC, real API key)

* BTC/USD and XAU/USD responses **both include the still-forming 18:55 bar**, so
  forming-bar filtering is required.
* Without `timezone`, XAU/USD returned `2026-10-03 04:55:00` for the bar that is
  `2026-10-02 18:55:00` UTC. That is **UTC+10 exchange time**. The old code treated
  every gold bar as 10 h in the future, emptied `df_closed`, and hit the D4
  fallback. Gold alert timestamps were therefore off by 10 h. **D3 is confirmed.**
* The `Api-Credits-Left` response header is the **per-minute** remaining count.

## 4. Market-data / candle handling

* The Twelve Data `datetime` is the bar **open** time (per the official docs), so
  `open + 5 min <= now` is the correct closed test, provided the timezone is known (D3).
* Whether Twelve Data includes the forming bar is not documented. The validator
  must therefore handle both cases, rather than blindly dropping the last row.
* XAU/USD is closed on weekends. The scanner kept polling it, which wastes credits.

## 5. Telegram

Synchronous sends block a thread-pool thread with `time.sleep` back-off. There is
no delivery record, no latency measurement, and no duplicate protection when a
retry is uncertain (D8, D9).

## 6. Render reliability

Free plan: the service sleeps after 15 min idle, 750 instance-hours per month
per workspace, an ephemeral filesystem (lost on every deploy, restart, or
spin-down), and no background workers on the free tier. Supported always-on
options are a paid web service (Starter) or a paid background worker.

## 7. Tests

None.

## 8. Prioritised plan

1. Candle validation layer (UTC request, closed/future/duplicate/malformed rejection), with tests.
2. Bar-aligned scheduler within the credit budget (D1, D12), with an overlap guard, heartbeat, and supervisor.
3. Shared strategy engine with a common `Signal` schema. Wrap the three retained
   strategies with unchanged math and switch to candle-level flip detection (D5).
4. Four new strategies.
5. SQLite dedup and delivery log, with a startup baseline rule.
6. Telegram: HTML + escaping, classified retries, 429 handling, no retry on uncertain
   timeouts, delivery queue, latency measurement, secret redaction (D6–D9), and
   authenticated commands.
7. `/health`, webhook and admin secrets, render.yaml fixes, docs.
8. Backtester, history fetcher, bounded optimiser, and reports.
