# Project Context & Memory

## Stack & Architecture
- **Repository**: [ParthYendhe0679/-BTC-Gold-Parth-5min](https://github.com/ParthYendhe0679/-BTC-Gold-Parth-5min)
- **Framework**: Python 3.10+, FastAPI, Uvicorn
- **Strategy**: Dual Supertrend ((10, 3) and (20, 3)) on 5-minute closed candles for BTC/USD and XAU/USD (Gold).
- **Data Source**: Twelve Data API
- **Alert Channel**: Telegram Bot API
- **Deployment**: Render (`render.yaml`)

## Key Files
- [`app/main.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/main.py): FastAPI app initialization and lifecycle management.
- [`app/scanner.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/scanner.py): Autonomous background scanning loop.
- [`app/strategy.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/strategy.py): Supertrend calculation and Pine Script math.
- [`app/data_fetcher.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/data_fetcher.py): Twelve Data REST API client.
- [`app/telegram.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/telegram.py): Telegram alert formatting & dispatch.
- [`app/config.py`](file:///c:/Users/yendh/OneDrive/Desktop/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/app/config.py): Environment settings (`TWELVE_DATA_API_KEY`, `BOT_TOKEN`, `CHAT_ID`).

## Lessons & Preferences
- Initial checkout completed on branch `main`.
- Audit D1-D13 addressed: rate limiting / credit management, persistent state across restarts, secret leak prevention in exception logs, HTML parse mode for Telegram, and webhook token authentication.
- Modular strategies added under `app/strategies/` with full backtesting harness.
