"""
Configuration system for the AI Hedge Fund.
All settings are validated via Pydantic. Loaded from YAML + .env.
No magic — every parameter is explicit and typed.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Environment(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Market(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    MEME = "meme"


class AgentStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    TERMINATED = "terminated"
    SPAWNING = "spawning"


# ---------------------------------------------------------------------------
# Risk Configuration (Non-overridable hard limits)
# ---------------------------------------------------------------------------

class RiskConfig(BaseModel):
    """Hard-coded risk parameters. The kill switch enforces these."""

    # Portfolio-level
    max_drawdown_pct: float = Field(50.0, description="Kill all if portfolio drops this % from peak")
    daily_loss_limit_pct: float = Field(20.0, description="Kill all if daily loss exceeds this %")
    heartbeat_timeout_sec: int = Field(60, description="Kill all if orchestrator unresponsive for this long")

    # Per-trade
    max_single_trade_pct: float = Field(25.0, description="Max % of desk allocation per single trade")
    min_arb_spread_pct: float = Field(4.0, description="Minimum spread after fees to execute arb")

    # Meme-specific
    max_meme_hold_hours: float = Field(4.0, description="Force-sell meme coins after this many hours")
    max_concurrent_meme_positions: int = Field(3, description="Max simultaneous meme coin positions")
    meme_take_profit_at_2x_pct: float = Field(50.0, description="% of position to sell at 2x")
    meme_take_profit_at_5x_pct: float = Field(25.0, description="% of position to sell at 5x")
    meme_stop_loss_pct: float = Field(50.0, description="Hard stop-loss % from entry")
    meme_trailing_stop_pct: float = Field(30.0, description="Trailing stop % from peak")

    # Correlation
    max_correlated_positions: int = Field(2, description="Max positions on same underlying event")


# ---------------------------------------------------------------------------
# Capital Allocation
# ---------------------------------------------------------------------------

class AllocationConfig(BaseModel):
    """How capital is split across desks. Adjusts as portfolio grows."""

    # Phase 1: $1-$10
    seed_arb_pct: float = 80.0
    seed_meme_pct: float = 20.0

    # Phase 2: $10-$100
    growth_arb_pct: float = 50.0
    growth_directional_pct: float = 30.0
    growth_meme_pct: float = 20.0

    # Phase 3: $100+
    scale_arb_pct: float = 40.0
    scale_directional_pct: float = 30.0
    scale_meme_pct: float = 20.0
    scale_reserve_pct: float = 10.0

    # Thresholds
    seed_to_growth_threshold: float = 10.0  # USD
    growth_to_scale_threshold: float = 100.0  # USD

    # Rebalancing
    rebalance_interval_hours: int = 1
    rebalance_sharpe_window_days: int = 14


# ---------------------------------------------------------------------------
# Agent Configuration
# ---------------------------------------------------------------------------

class AgentTerminationConfig(BaseModel):
    """Rules for when to kill an underperforming agent."""

    max_drawdown_pct: float = Field(20.0, description="Terminate if agent loses > X% of allocation")
    min_win_rate: float = Field(0.30, description="Terminate if win rate below this after min trades")
    min_trades_for_evaluation: int = Field(20, description="Don't evaluate until this many trades")
    min_sharpe_ratio: float = Field(0.0, description="Terminate if Sharpe below this after min trades")
    min_trades_for_sharpe: int = Field(30, description="Don't check Sharpe until this many trades")
    evaluation_window_days: int = Field(7, description="Look-back window for performance metrics")


# ---------------------------------------------------------------------------
# Market-Specific Configs
# ---------------------------------------------------------------------------

class KalshiConfig(BaseModel):
    api_key_id: str = ""
    private_key_path: str = "./keys/kalshi_private.pem"
    base_url: str = "https://api.elections.kalshi.com"
    demo_base_url: str = "https://demo-api.kalshi.co"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    demo_ws_url: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    env: str = "demo"
    rate_limit_per_sec: int = 5

    @property
    def active_base_url(self) -> str:
        return self.demo_base_url if self.env == "demo" else self.base_url

    @property
    def active_ws_url(self) -> str:
        return self.demo_ws_url if self.env == "demo" else self.ws_url


class PolymarketConfig(BaseModel):
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    env: str = "test"
    rate_limit_per_sec: int = 10


class SolanaConfig(BaseModel):
    private_key: str = ""
    rpc_url: str = "https://api.devnet.solana.com"
    ws_url: str = ""
    jupiter_api_url: str = "https://quote-api.jup.ag/v6"
    rugcheck_api_url: str = "https://api.rugcheck.xyz/v1"
    env: str = "devnet"
    # Meme coin detection
    min_bonding_curve_pct: float = 20.0  # Skip tokens < 20% bonding curve filled
    max_dev_wallet_pct: float = 5.0  # Skip if dev holds > 5%
    min_social_mentions: int = 50  # Minimum social signal


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False


class LLMConfig(BaseModel):
    provider: str = "gemini"  # gemini | openai | anthropic | ollama
    api_key: str = ""
    base_url: Optional[str] = None  # e.g., http://localhost:11434 for Ollama
    model: str = "gemini-2.5-flash"
    max_tokens: int = 4096
    temperature: float = 0.3  # Low temp for analysis, not creative writing


# ---------------------------------------------------------------------------
# Master Config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    """Root configuration. Everything flows from here."""

    environment: Environment = Environment.PAPER
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    # Sub-configs
    risk: RiskConfig = RiskConfig()
    allocation: AllocationConfig = AllocationConfig()
    agent_termination: AgentTerminationConfig = AgentTerminationConfig()
    kalshi: KalshiConfig = KalshiConfig()
    polymarket: PolymarketConfig = PolymarketConfig()
    solana: SolanaConfig = SolanaConfig()
    telegram: TelegramConfig = TelegramConfig()
    llm: LLMConfig = LLMConfig()

    # Orchestrator
    main_loop_interval_sec: float = 10.0
    learning_interval_trades: int = 50
    learning_interval_hours: int = 24

    @field_validator("data_dir")
    @classmethod
    def ensure_data_dir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v


def load_config() -> AppConfig:
    """Load config from environment variables and defaults.
    
    Priority: .env file → environment variables → defaults
    """
    from dotenv import load_dotenv
    load_dotenv()

    # Map env vars to config
    kalshi_cfg = KalshiConfig(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem"),
        env=os.getenv("KALSHI_ENV", "demo"),
    )

    polymarket_cfg = PolymarketConfig(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        api_key=os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        env=os.getenv("POLYMARKET_ENV", "test"),
    )

    solana_cfg = SolanaConfig(
        private_key=os.getenv("SOLANA_PRIVATE_KEY", ""),
        rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com"),
        ws_url=os.getenv("SOLANA_RPC_WS_URL", ""),
        env=os.getenv("SOLANA_ENV", "devnet"),
    )

    telegram_cfg = TelegramConfig(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        enabled=bool(os.getenv("TELEGRAM_BOT_TOKEN")),
    )

    llm_cfg = LLMConfig(
        provider=os.getenv("LLM_PROVIDER", "gemini"),
        api_key=os.getenv("LLM_API_KEY", os.getenv("GEMINI_API_KEY", "")),
        base_url=os.getenv("LLM_BASE_URL"),
        model=os.getenv("LLM_MODEL", "gemini-2.5-flash"),
    )

    paper_trade = os.getenv("PAPER_TRADE", "true").lower() == "true"

    return AppConfig(
        environment=Environment.PAPER if paper_trade else Environment.LIVE,
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        kalshi=kalshi_cfg,
        polymarket=polymarket_cfg,
        solana=solana_cfg,
        telegram=telegram_cfg,
        llm=llm_cfg,
    )
