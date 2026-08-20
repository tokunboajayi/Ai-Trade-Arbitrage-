# AI Hedge Fund

A fully autonomous, multi-agent algorithmic trading system that bridges the gap between high-frequency DeFi (Solana meme coins) and prediction markets (Kalshi and Polymarket).

## Overview

The AI Hedge Fund dynamically allocates capital across three distinct strategies based on portfolio growth phases:
1. **Meme Coin Sniping (Solana)**: High risk/reward fast trades utilizing Jupiter API, DexScreener, and RugCheck.
2. **Prediction Market Cross-Arbitrage**: Finding risk-free spreads between Kalshi and Polymarket events.
3. **Prediction Market Directional Betting**: High-conviction trend following on open prediction markets.

### Key Features
- **Dynamic Capital Allocation**: Shifts from aggressive (seed phase) to conservative (scale phase) automatically.
- **Strict Risk Management**: Multi-layered kill switches, trailing stops, max drawdown limits, and time-based exits.
- **Live Web Dashboard**: Premium dark-mode glassmorphism dashboard serving real-time PnL, equity curves, and open positions.
- **Telegram Control Panel**: Live heartbeat monitoring, rich HTML status reports, and manual kill switch commands.

## Architecture

- **Orchestrator (`src/core/orchestrator.py`)**: The central brain running the event loop.
- **Capital Allocator (`src/core/capital_allocator.py`)**: Manages the phase-based distribution of capital.
- **Risk Manager & Kill Switch**: Deterministic logic protecting against out-of-band volatility.
- **Trade Logger (`src/learning/trade_logger.py`)**: SQLite backend for all historical trade recording.

## Installation

1. Install Python 3.12+
2. Install the required dependencies using `uv` or `pip`:
   ```bash
   pip install -r requirements.txt
   # OR using pyproject.toml
   pip install -e .
   ```
3. Copy `.env.example` to `.env` and fill in your keys:
   ```env
   SOLANA_PRIVATE_KEY="your_base58_key"
   SOLANA_RPC_URL="your_rpc"
   TELEGRAM_BOT_TOKEN="your_token"
   TELEGRAM_CHAT_ID="your_chat_id"
   PAPER_TRADE=true # Set to false for live trading
   ```

## Usage

To start the bot:
```bash
python main.py
```

### Dashboard
Once running, the live dashboard is available at:
**http://localhost:8080**

### Telegram Commands
- `/status` - Returns a full HTML report of your portfolio balance, open positions, and desk allocations.
- `/kill` - Emergency shutdown, liquidates all positions and stops the bot.

## Disclaimer
This software is for educational purposes only. Trading cryptocurrency and prediction markets carries a high level of risk. Do not trade with money you cannot afford to lose. Use at your own risk.
