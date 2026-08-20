"""
Meme Sniper Agent — Finds and snipes newly listed Solana meme coins.

Pipeline:
1. Scan DexScreener for newly trending Solana tokens.
2. Run RugCheck safety analysis.
3. Get Jupiter swap quote.
4. Return a buy proposal to the orchestrator.
5. Monitor open position ROI and return sell proposals on TP/SL/time limit.
"""

import asyncio
import logging
import httpx
import time
from typing import Optional, Dict, Any

from src.agents.base_agent import BaseAgent
from src.markets.solana.connector import SolanaConnector
from src.markets.solana.rugcheck import RugCheckAPI
from src.markets.solana.price_feed import get_sol_price_usd
from src.core.config import AppConfig, RiskConfig
from src.core.state import PortfolioState

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
TRADE_SIZE_SOL = 0.003  # ~$0.45
TRADE_SIZE_LAMPORTS = int(TRADE_SIZE_SOL * 1e9)
SLIPPAGE_BPS = 1000  # 10%


class MemeSniper(BaseAgent):
    def __init__(self, connector: SolanaConnector, version: int = 1, config: Optional[dict] = None):
        super().__init__(name="meme_sniper", version=version, config=config)
        self.connector = connector
        self.rugcheck = RugCheckAPI()
        self.risk_config: Optional[RiskConfig] = None

    async def initialize(self, app_config: AppConfig, trade_logger) -> None:
        """Called before the agent starts running."""
        self.risk_config = app_config.risk

    async def run_cycle(self, state: PortfolioState) -> list[dict]:
        """Main sniper loop."""
        # 1. Look for active position in the PortfolioState
        active_position = None
        if state:
            for pos in state.open_positions:
                if pos.agent_id == self.id and pos.market == "meme":
                    active_position = pos
                    break

        if active_position:
            # We have a position, check TP/SL/time limit
            exit_proposal = await self._check_exit_conditions(active_position)
            return [exit_proposal] if exit_proposal else []

        # 2. Look for new opportunities
        logger.info("[MemeSniper] Scanning for new tokens...")
        target_mint = await self._find_target()

        if not target_mint:
            return []

        logger.info(f"[MemeSniper] Found target: {target_mint}. Running RugCheck...")
        is_safe, reason = await self.rugcheck.is_safe(target_mint)

        if not is_safe:
            logger.warning(f"[MemeSniper] Token {target_mint} failed safety check: {reason}")
            return []

        logger.info(f"[MemeSniper] Token {target_mint} is SAFE. Requesting Jupiter quote...")

        quote = await self.connector.get_quote(
            input_mint=WSOL_MINT,
            output_mint=target_mint,
            amount_lamports=TRADE_SIZE_LAMPORTS,
            slippage_bps=SLIPPAGE_BPS
        )

        if not quote:
            logger.warning("[MemeSniper] No quote available.")
            return []

        # Calculate approx entry price from quote in USD
        sol_price = await get_sol_price_usd()
        out_amount = int(quote.get("outAmount", 0))
        price = 0.0
        if out_amount > 0:
            price = (TRADE_SIZE_SOL * sol_price) / out_amount  # USD per raw token

        logger.info(f"[MemeSniper] Proposing BUY swap for {TRADE_SIZE_SOL} SOL (SOL=${sol_price:.2f})...")

        proposal = {
            "action": "open_position",
            "market": "meme",
            "strategy": "snipe",
            "symbol": target_mint,
            "side": "buy",
            "price": price,
            "quantity": out_amount,
            "agent_id": self.id,
            "estimated_edge_pct": 100.0,  # Targets 2x / 100% ROI
            "confidence": 0.8,
            "metadata": {
                "quote": quote
            }
        }
        return [proposal]

    async def _check_exit_conditions(self, active_position) -> Optional[dict]:
        """Monitor price and return a close proposal if TP/SL/time triggers."""
        sol_price = await get_sol_price_usd()

        # --- Time-based exit ---
        max_hold_sec = 4.0 * 3600  # default 4 hours
        if self.risk_config:
            max_hold_sec = self.risk_config.max_meme_hold_hours * 3600

        if active_position.hold_time_seconds > max_hold_sec:
            logger.info(
                f"[MemeSniper] TIME LIMIT reached on {active_position.symbol}: "
                f"{active_position.hold_time_seconds / 3600:.1f}h > "
                f"{max_hold_sec / 3600:.1f}h. Force closing."
            )
            # Get sell quote for time-based exit
            quote = await self.connector.get_quote(
                input_mint=active_position.symbol,
                output_mint=WSOL_MINT,
                amount_lamports=int(active_position.quantity),
                slippage_bps=SLIPPAGE_BPS
            )
            if not quote:
                return None

            current_sol_value = int(quote.get("outAmount", 0))
            exit_price = ((current_sol_value / 1e9) * sol_price) / active_position.quantity if active_position.quantity > 0 else 0

            return {
                "action": "close_position",
                "market": "meme",
                "strategy": "snipe",
                "symbol": active_position.symbol,
                "side": "sell",
                "price": exit_price,
                "quantity": active_position.quantity,
                "position_id": active_position.id,
                "agent_id": self.id,
                "exit_reason": "time_limit",
                "metadata": {"quote": quote}
            }

        # --- Price-based exit (TP/SL) ---
        quote = await self.connector.get_quote(
            input_mint=active_position.symbol,
            output_mint=WSOL_MINT,
            amount_lamports=int(active_position.quantity),
            slippage_bps=SLIPPAGE_BPS
        )
        if not quote:
            return None

        current_sol_value = int(quote.get("outAmount", 0))
        current_usd_value = (current_sol_value / 1e9) * sol_price
        cost_basis_usd = active_position.entry_price * active_position.quantity

        roi = (current_usd_value - cost_basis_usd) / cost_basis_usd if cost_basis_usd > 0 else 0.0
        logger.info(f"[MemeSniper] Current ROI on {active_position.symbol}: {roi * 100:.2f}%")

        # Take Profit: +100%, Stop Loss: -30%
        stop_loss_pct = -0.30
        if self.risk_config:
            stop_loss_pct = -(self.risk_config.meme_stop_loss_pct / 100.0)

        if roi >= 1.0 or roi <= stop_loss_pct:
            action_type = "take_profit" if roi >= 1.0 else "stop_loss"
            logger.info(f"[MemeSniper] Triggering {action_type.upper()} at {roi * 100:.2f}%")

            exit_price = ((current_sol_value / 1e9) * sol_price) / active_position.quantity if active_position.quantity > 0 else 0

            proposal = {
                "action": "close_position",
                "market": "meme",
                "strategy": "snipe",
                "symbol": active_position.symbol,
                "side": "sell",
                "price": exit_price,
                "quantity": active_position.quantity,
                "position_id": active_position.id,
                "agent_id": self.id,
                "exit_reason": action_type,
                "metadata": {"quote": quote}
            }
            return proposal
        return None

    async def _find_target(self) -> Optional[str]:
        """Use DexScreener to find a trending new token."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
                if resp.status_code == 200:
                    profiles = resp.json()
                    for p in profiles:
                        if p.get("chainId") == "solana":
                            return p.get("tokenAddress")
        except Exception as e:
            logger.error(f"Failed to fetch DexScreener: {e}")
        return None

    async def terminate(self) -> None:
        """Called when the agent is stopped."""
        await self.rugcheck.close()
        self.status = 3  # TERMINATED
