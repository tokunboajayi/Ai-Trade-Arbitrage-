# AI Hedge Fund: Comprehensive Technical Documentation (Deep Dive)

This document provides a highly granular, low-level technical breakdown of the entire AI Hedge Fund project. It covers class structures, method signatures, database schemas, trading logic, mathematical formulas, and the explicit event loop workflow.

---

## 1. System Architecture Overview

The system is a multi-agent, asynchronous Python application built on the `asyncio` event loop. It operates across two primary domains:
- **Solana DeFi (Meme Coins)**: Extremely volatile, high-frequency token sniping.
- **Prediction Markets (Kalshi & Polymarket)**: Lower-frequency, event-driven arbitrage and directional betting.

### The "Hub and Spoke" Model
- **Hub**: The `Orchestrator` (`src/core/orchestrator.py`).
- **Spokes**: The Agents (`CrossArbFinder`, `MemeSniper`, `PredictionDirectionalAgent`).
- **Shared Memory**: The `PortfolioState` (`src/core/state.py`), a mutable state object passed by reference to all spokes during the event loop.

---

## 2. Core Operational Modules (`src/core/`)

### 2.1 The Orchestrator (`orchestrator.py`)
The Orchestrator is the central engine. It manages the `PortfolioState`, instantiates all connectors and agents, and controls the execution loop.

**Initialization Phase (`initialize()`):**
1. **DB Setup**: Calls `TradeLogger.initialize()` to ensure the SQLite file and tables exist.
2. **Notification Setup**: Starts the `TelegramNotifier` background polling task.
3. **Market Connections**: Calls `.connect()` on `KalshiConnector` and `PolymarketConnector`.
4. **State Seeding**: Fetches the live SOL/USD price via `get_sol_price_usd()`. Fetches actual REST API balances from Kalshi, Polymarket, and the local Solana wallet, computing the `total_balance` and `peak_balance`.
5. **Component Initialization**: Initializes `CapitalAllocator` and `RiskManager` with the loaded configuration.
6. **Background Threads**: Registers the shutdown callback and starts the `KillSwitch` daemon on a separate thread.
7. **Agent Startup**: Calls `.start()` on all instantiated agents.
8. **Dashboard Startup**: Spawns the `DashboardServer` on port `8080`.

**The Event Loop (`_run_loop()`):**
Runs every `main_loop_interval_sec` (default: 5s).
1. Updates `state.last_heartbeat_at`.
2. Evaluates global risk via `kill_switch.check()`. If `KILL_ALL` is returned, immediately calls `shutdown()`.
3. Evaluates open meme positions via `kill_switch.check_position()`. If a position has exceeded `max_meme_hold_hours`, it flags it for forced closure.
4. Refreshes the `SOL/USD` spot price.
5. Calls `capital_allocator.run_cycle(state)` to adjust available desk capital based on current phase.
6. Iterates over all active `agents`:
   - Calls `await agent.run_cycle(state)`.
   - Iterates over returned `proposals` (dictionaries).
   - Passes each proposal through `risk_manager.evaluate_trade(state, proposal)`.
   - If approved, routes to `execute_trade(agent, proposal)`.
7. Calls `trade_logger.log_portfolio_snapshot()` to record the current equity curve data point.

### 2.2 Portfolio State (`state.py`)
Manages the live financial state in memory. 

**State Variables:**
- `total_balance` (float): The combined USD value of all assets.
- `peak_balance` (float): The highest `total_balance` recorded (high-water mark).
- `today_start_balance` (float): The balance at the start of the current trading day.
- `kalshi_balance`, `polymarket_balance`, `meme_balance`, `reserve_balance` (float): Individual desk allocations.
- `open_positions` (Dict[str, Position]): A dictionary of currently active trades.
- `current_phase` (str): `seed`, `growth`, or `scale`.

**The `Position` Dataclass:**
Stores immutable and mutable data for a live trade:
- `id` (str): Database trade ID.
- `market` (Market): Enum (`MEME`, `KALSHI`, `POLYMARKET`, `ARB`).
- `symbol` (str): The token mint or market ticker.
- `side` (Side): `BUY` or `SELL`.
- `entry_price` (float): USD entry.
- `current_price` (float): Live USD value.
- `quantity` (float): Number of tokens/contracts.
- `opened_at` (float): Unix timestamp.
- `metadata` (Dict): Transaction signatures, Jupiter quotes, etc.

### 2.3 Risk Manager (`risk_manager.py`)
Acts as the firewall between Agent proposals and Orchestrator execution.

**Validation Checks (`evaluate_trade`):**
1. **Drawdown Check**: Rejects all trades if `state.drawdown_from_peak_pct > max_drawdown_pct`.
2. **Market State Check**: Rejects trades if `kill_switch_triggered` is true.
3. **Trade Size Check**: Ensures the `quantity * price` does not exceed `max_single_trade_pct` of the total portfolio.
4. **Agent Concurrency Check**: Prevents agents from holding more than `max_concurrent_meme_positions`.
5. **Kelly Criterion Sizing**: If the strategy is `directional`, it adjusts the requested trade size based on the perceived edge to prevent over-betting on high-conviction trades.

### 2.4 Capital Allocator (`capital_allocator.py`)
Distributes capital dynamically across the `arb`, `directional`, and `meme` strategies.

**Phase Thresholds:**
- **Seed Phase** (`total_balance < seed_to_growth_threshold`): Aggressive portfolio building. 80% Arb (risk-free yield), 20% Meme (high variance asymmetric upside).
- **Growth Phase** (`total_balance < growth_to_scale_threshold`): 50% Arb, 30% Directional, 20% Meme.
- **Scale Phase** (`total_balance >= growth_to_scale_threshold`): Wealth preservation mode. 40% Arb, 30% Directional, 20% Meme, 10% Reserve (cash drag).

### 2.5 Kill Switch (`kill_switch.py`)
A highly specialized daemon that runs on an isolated `threading.Thread`.
- **Purpose**: To protect the system from infinite loops or frozen asyncio tasks.
- **Mechanism**: The Orchestrator registers a callback (`_on_kill_signal`) and provides a state getter function. The Kill Switch polls this getter every 5 seconds.
- **Global Triggers**: 
  - `Heartbeat Timeout`: If `time.time() - state.last_heartbeat_at > 60` seconds, the main loop is frozen. Triggers `KILL_ALL`.
  - `Max Drawdown`: If `(peak_balance - total_balance) / peak_balance > max_drawdown_pct`. Triggers `KILL_ALL`.
- **Position Triggers**:
  - `Stale Position`: If an individual meme position is held for `> max_meme_hold_hours`, it emits a `CLOSE_POSITION` signal to force-liquidate.

---

## 3. Market Connectors (`src/markets/`)

### 3.1 Solana Connector (`solana/connector.py` & `price_feed.py`)
- **RPC Communication**: Connects to the Helius Solana RPC node.
- **Jupiter V1 API**: 
  - `get_quote()`: Fetches optimal routing pathways for Token A -> Token B.
  - `execute_swap()`: Submits the transaction payload, signs it using `base58` decoded `SOLANA_PRIVATE_KEY`, and broadcasts it to the network.
- **Price Feed**: Implements `get_sol_price_usd()` which queries `api.coingecko.com/api/v3/simple/price`. Features an in-memory TTL cache to prevent API ratelimiting.

### 3.2 Kalshi Connector (`kalshi/connector.py`)
- **Base URL**: `https://trading-api.kalshi.com/trade-api/v2/` (or `demo-api.kalshi.co` in paper mode).
- **Endpoints**:
  - `GET /events`: Retrieves a list of active event dictionaries.
  - `GET /markets/{ticker}/orderbook`: Fetches the Level 2 orderbook (bids/asks in cents).
  - `POST /portfolio/orders`: Submits `buy`/`sell` limit orders.

### 3.3 Polymarket Connector (`polymarket/connector.py`)
- **Gamma API**: `https://gamma-api.polymarket.com/`
  - `GET /markets?active=true`: Fetches human-readable titles, condition IDs, and YES/NO token IDs.
- **CLOB API**: Interacts with the Central Limit Order Book to place simulated or real trades on the Polygon network.

### 3.4 Cross-Arb Engine (`cross_arb.py`)
This class merges the logic of Kalshi and Polymarket into a cohesive engine.
- **Event Matching**: 
  - Iterates through Kalshi events and Polymarket questions.
  - Uses `difflib.SequenceMatcher(None, k_title, p_title).ratio()`.
  - If similarity > `0.70`, it binds the two markets.
- **Spread Calculation**:
  - Fetches the Kalshi Orderbook and takes the best `YES` ask.
  - Fetches the Polymarket price for the corresponding `NO` token.
  - Formula: `Spread = 1.00 - (Kalshi_YES_Price + Polymarket_NO_Price)`.
  - If `Spread > 4.0%` (0.04), it emits a two-legged `arb` proposal.

---

## 4. Trading Agents (`src/agents/`)

### 4.1 Base Agent (`base_agent.py`)
An abstract base class (ABC) that enforces the agent contract.
- Tracks `AgentPerformance` dataclass (trade count, win rate, PnL, max drawdown).
- Requires implementation of `initialize()`, `run_cycle()`, and `terminate()`.

### 4.2 Meme Sniper (`meme_sniper.py`)
- **Target**: DexScreener API (`/token-profiles/latest/v1`).
- **Safety Checks**: Queries RugCheck API (`https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary`). Discards tokens labeled as "danger", honeypots, or highly centralized.
- **Execution**: If safe, requests a Jupiter quote for `0.003 SOL` and emits an `open_position` proposal to the Orchestrator.
- **Position Management**: Every cycle, it evaluates its open positions. If ROI < `-50%` (Stop Loss) or > `+50%` (Take Profit), it emits a `close_position` proposal.

### 4.3 Prediction Directional Agent (`prediction_directional.py`)
- **Strategy**: High Conviction Trend Following.
- Iterates over the top 30 Kalshi and Polymarket events.
- Evaluates the orderbooks. If the `YES` contract is heavily favored (price between `$0.85` and `$0.95`), the market believes the event is practically guaranteed.
- Emits a single-leg `directional` trade to capture the remaining 5% - 15% yield before market resolution.
- Enforces a `3600` second (1 hour) cooldown per symbol to avoid spamming orders on the same market.

---

## 5. Web Dashboard & Persistence

### 5.1 Dashboard Server (`src/dashboard/server.py`)
Built with `aiohttp.web`. Exposes a Read-Only API connecting to the local database.
- **Database Architecture**: SQLite connection initialized with `PRAGMA journal_mode=WAL` (Write-Ahead Logging). This is absolutely critical, as it allows the dashboard to perform non-blocking reads while the Orchestrator writes snapshots simultaneously.
- **Endpoints**:
  - `/api/status`: Returns JSON serialization of `PortfolioState`.
  - `/api/trades`: Returns the last 50 historical trades from the `trades` table.
  - `/api/snapshots`: Returns the last 100 historical equity curve data points from the `portfolio_snapshots` table.

### 5.2 Dashboard Frontend (`src/dashboard/static/index.html`)
- Built with Vanilla HTML/CSS/JS (no heavy frontend frameworks required).
- Implements a premium aesthetic using CSS variables, glassmorphism (`backdrop-filter`), and deep dark mode colors (`#0a0e17`).
- Integrates `Chart.js` for an interactive, responsive Equity Curve line chart.
- Uses `setInterval` to fetch new data from the API endpoints every 10 seconds, updating DOM elements dynamically.

### 5.3 Trade Logger (`src/learning/trade_logger.py`)
Manages the SQL schema.
- **Table `trades`**: `id`, `market`, `strategy`, `symbol`, `side`, `entry_price`, `exit_price`, `quantity`, `pnl`, `status`, `opened_at`, `closed_at`.
- **Table `portfolio_snapshots`**: `timestamp`, `total_balance`, `peak_balance`, `drawdown_pct`, `kalshi_balance`, `polymarket_balance`, `meme_balance`, `reserve_balance`, `open_positions`, `total_trades`, `win_rate`.

---

## 6. Telegram Control Panel (`src/notifications/telegram_bot.py`)

A critical interface for remote management, built with standard long-polling (or HTTP polling).
- Initialized with `PortfolioState` reference to provide real-time reporting without DB queries.
- **`/status`**: Formats the live portfolio state into a rich HTML message. Displays exact desk allocations, Phase status, total PnL, Drawdown, and a summarized table of current open positions and their respective ROI.
- **`/kill`**: When executed, sets the global KillSwitch manual override to `True`. The Orchestrator's next event loop will catch this override, initiate the `KILL_ALL` protocol, cancel open orders, and shut down gracefully.

---

## 7. Execution and Environment Options

**`.env` Configuration File:**
```env
# Solana RPC and Wallet setup
SOLANA_PRIVATE_KEY="base58_encoded_key"
SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
JUPITER_API_URL="https://api.jup.ag/swap/v1"

# Telegram Setup
TELEGRAM_BOT_TOKEN="bot_father_token"
TELEGRAM_CHAT_ID="your_telegram_id"

# Safety Setup
PAPER_TRADE=true  # If true, executes simulated orders. If false, signs and broadcasts real transactions.
```

**Starting the Bot:**
To run the system, simply activate your virtual environment and execute the entry point:
```bash
python main.py
```
This single command spins up the Orchestrator, Database, Agents, Telegram Bot, and Dashboard Web Server simultaneously via asyncio tasks.
