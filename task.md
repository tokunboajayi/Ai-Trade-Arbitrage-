- [x] 1. Setup Solana Environment
  - [x] Install `solana` and `solders` python packages.
  - [x] Add `SOLANA_RPC_URL` and `JUPITER_API_URL` to `.env`.
- [x] 2. Build `src/markets/solana/connector.py`
  - [x] Initialize AsyncClient with Helius RPC.
  - [x] Implement Jupiter v6 quote API wrapper.
  - [x] Implement Jupiter v6 swap API wrapper & transaction signing.
- [x] 3. Build `src/markets/solana/rugcheck.py`
  - [x] API wrapper for `api.rugcheck.xyz`.
  - [x] Function to score safety based on token report.
- [x] 4. Build `src/agents/meme_sniper.py`
  - [x] Inherit from `BaseAgent`.
  - [x] Implement `run_cycle` to fetch new pairs, run rug check, and execute swap.
  - [x] Implement paper mode in Solana Connector (`src/markets/solana/connector.py`)
- [x] Refactor Meme Sniper agent to use proposals (`src/agents/meme_sniper.py`)
- [x] Refactor Orchestrator to instantiate state, route executions, and pass state (`src/core/orchestrator.py`).
  - [x] Initialize `SolanaConnector`.

# Phase 2 Tasks

## 1. Live SOL Price Feed
- [x] Create `src/markets/solana/price_feed.py` with CoinGecko cached fetcher
- [x] Update `orchestrator.py` to use live SOL price
- [x] Update `meme_sniper.py` to use live SOL price from state

## 2. CrossArbFinder Implementation
- [x] Implement event fetching & fuzzy-matching in `cross_arb.py`
- [x] Implement order book spread calculation
- [x] Pass connectors to `CrossArbFinder` from orchestrator
- [x] Handle arb proposals in `execute_trade()`

## 3. KillSwitch Integration
- [x] Call `kill_switch.check()` each iteration in `_run_loop()`
- [x] Start kill switch monitoring thread in `initialize()`
- [x] Register shutdown callback
- [x] Call `check_position()` for open meme positions

## 4. Meme Position Time-Based Exit
- [x] Add hold-time check in `_check_exit_conditions()`

## 5. Enriched Telegram `/status`
- [x] Pass `PortfolioState` reference to `TelegramNotifier`
- [x] Enrich `/status` handler with live portfolio data

## 6. Live Web Dashboard
- [x] Create `src/dashboard/server.py` with aiohttp REST endpoints
- [x] Create `src/dashboard/static/index.html` premium dark-mode UI
- [x] Start dashboard server from orchestrator
- [x] Add `PAPER_TRADE=true` to `.env` if missing

## 7. Verification
- [x] Run bot in paper mode — zero errors
- [x] Dashboard loads at localhost:8080
- [x] Telegram `/status` returns enriched data

# Phase 3 Tasks: Directional Agents
## 1. Orchestrator Updates
- [x] Update `execute_trade` to handle `kalshi` and `polymarket`
- [x] Record standalone trades in the DB as `directional`
- [x] Add Telegram notifications for directional trades
- [x] Instantiate `PredictionDirectionalAgent` in Orchestrator

## 2. Agent Implementation
- [x] Create `src/agents/prediction_directional.py`
- [x] Connect to Kalshi and Polymarket connectors
- [x] Implement High Conviction Trend Following logic (Price > 0.85)

## 3. Verification
- [x] Run bot in paper mode
- [x] Verify directional proposals are generated and executed
