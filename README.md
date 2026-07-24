# 📈 BTC/Gold-Parth 5min

An autonomous, production-ready **FastAPI** multi-asset signal scanner that continuously monitors **Bitcoin (BTC/USD)** and **Gold (XAU/USD)** on the 5-minute chart using dual **Supertrend** strategies: **(10,3)** and **(20,3)**. It dynamically evaluates completed closed candles via the **Twelve Data API** and instantly dispatches Telegram alerts whenever a trend crossover is confirmed.

---

## ✨ Features

- **24/7 Autonomous Scanner**: Runs continuously in an `asyncio` background loop without requiring TradingView webhooks.
- **Dual Supertrend Strategies**: Simultaneously calculates:
  - **Supertrend (10,3)** — ATR Period = 10, Multiplier = 3.0
  - **Supertrend (20,3)** — ATR Period = 20, Multiplier = 3.0
- **Dynamic Candle Close Verification**: Evaluates Supertrend strictly on completed closed candles, eliminating unclosed bar noise and false alerts.
- **Event-Based Signal Deduplication**: Tracks unique `(symbol, strategy, candle_timestamp)` keys so every trend transition fires **exactly once**.
- **Pine Script Accurate Math**: Wilder's RMA ATR + Pine Script band clamping + exact trend switching logic matching TradingView `ta.supertrend()`.
- **Instant Telegram Alerts**: Sends rich formatted Markdown notifications with asset name, timeframe, strategy variant, entry price, trend, and IST timestamp.
- **Dual Notification Pathways**: Supports both internal background scanner alerts and incoming TradingView webhook signals (`POST /webhook`).
- **Testing Endpoint**: `GET /scanner/test-alert` sends an instant test notification to verify Telegram bot connectivity.

---

## 🛠️ Technologies Used

- **Framework**: Python 3.10+ / FastAPI
- **Server**: Uvicorn (ASGI)
- **Data Source**: Twelve Data REST API
- **Data Processing**: Pandas, NumPy
- **HTTP Client**: Requests
- **Environment**: Python-Dotenv, Pydantic
- **Alert Channel**: Telegram Bot API

---

## 🔑 Environment Variables

The application requires three environment variables configured in a `.env` file:

| Variable | Required | Description | Example |
| :--- | :--- | :--- | :--- |
| `TWELVE_DATA_API_KEY` | **Yes** | Your API key from [Twelve Data](https://twelvedata.com) | `abc123xyz...` |
| `BOT_TOKEN` | **Yes** | Telegram Bot token from [@BotFather](https://t.me/BotFather) | `123456:ABC-DEF...` |
| `CHAT_ID` | **Yes** | Telegram chat ID or channel ID | `987654321` |

Copy `.env.example` to create your local `.env`:

```bash
cp .env.example .env
```

---

## 💻 Local Setup & Installation

### 1. Clone the Repository
```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd Bot
```

### 2. Create and Activate Virtual Environment
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the root directory:
```env
TWELVE_DATA_API_KEY=YOUR_TWELVE_DATA_API_KEY
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
CHAT_ID=YOUR_TELEGRAM_CHAT_ID
```

### 5. Run the Server
```bash
# Windows (PowerShell)
$env:PYTHONUTF8=1; python -m uvicorn app.main:app --reload

# Linux / macOS
python3 -m uvicorn app.main:app --reload
```

The application will start on `http://127.0.0.1:8000`.

---

## 📡 API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Root health check and project status. |
| `GET` | `/health` | Application status check for hosting platforms. |
| `GET` | `/scanner/status` | Current state of the background signal scanner. |
| `GET` | `/scanner/test-alert` | Triggers an immediate test alert to Telegram. |
| `POST` | `/webhook` | Receives incoming TradingView webhook alerts. |
| `GET` | `/docs` | Interactive Swagger API documentation. |

---

## ☁️ Deployment (Render)

The project includes a pre-configured `render.yaml` for 1-click deployment on **Render**:

1. Push your repository to GitHub.
2. Log in to [Render Dashboard](https://dashboard.render.com).
3. Click **New +** → **Blueprint**.
4. Connect your GitHub repository.
5. Enter your environment variables (`TWELVE_DATA_API_KEY`, `BOT_TOKEN`, `CHAT_ID`) when prompted.
6. Render will automatically build and deploy the web service using `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## 🐙 GitHub Setup Instructions

Execute the following commands in your terminal to initialize and push to GitHub:

```bash
git init
git add .
git commit -m "Initial commit - BTC/Gold-Parth 5min"
git branch -M main
git remote add origin <MY_GITHUB_REPOSITORY_URL>
git push -u origin main
```

---

## 🔒 Security Notice

The `.gitignore` file is configured to strictly exclude `.env`, `.venv`, and `*.log` files. Your private API keys and Telegram bot credentials will **NEVER** be committed or exposed to GitHub.
