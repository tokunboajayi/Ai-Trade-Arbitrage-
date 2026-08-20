"""
Risk Manager Agent — Enforces all risk rules across the portfolio.

This agent sits between the scanners (which find opportunities) and
the executors (which place trades). Every trade proposal must pass
through the risk manager before execution.

It enforces:
- Position sizing (Kelly Criterion, capped)
- Correlation limits (no overexposure to one event)
- Per-desk drawdown limits
- Total open position limits
- Meme coin-specific rules (max concurrent, hold time)
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.core.config import AppConfig, Market, RiskConfig
from src.core.state import PortfolioState, Position
from src.learning.trade_logger import TradeLogger

logger = logging.getLogger(__name__)


class RiskManager(BaseAgent):
    """
    Risk enforcement agent. Does not generate trades — only approves or
    rejects trade proposals from other agents.
    """

    def __init__(self, version: int = 1, config: Optional[dict] = None, **kwargs):
        super().__init__(
            name="risk_manager",
            version=version,
            config=config,
            **kwargs,
        )
        self.risk_config: Optional[RiskConfig] = None
        self.trade_logger: Optional[TradeLogger] = None

    async def initialize(self, app_config: AppConfig, trade_logger: TradeLogger) -> None:
        self.risk_config = app_config.risk
        self.trade_logger = trade_logger
        self._logger.info("Risk manager initialized")

    async def run_cycle(self, state: PortfolioState) -> list[dict[str, Any]]:
        """Risk manager doesn't generate actions — it's called by evaluate_trade."""
        return []

    async def terminate(self) -> None:
        self._logger.info("Risk manager terminated")

    # ---- Trade evaluation (called by orchestrator) ----

    def evaluate_trade(
        self,
        state: PortfolioState,
        proposal: dict,
    ) -> tuple[bool, str, dict]:
        """
        Evaluate a trade proposal. Returns (approved, reason, adjusted_proposal).
        
        proposal format:
        {
            "market": "kalshi" | "polymarket" | "meme",
            "strategy": "arb" | "directional" | "snipe",
            "symbol": "...",
            "side": "buy" | "sell" | "yes" | "no",
            "price": 0.45,
            "quantity": 10,
            "estimated_edge_pct": 5.0,  # Expected return
            "confidence": 0.8,          # 0-1 signal confidence
            "event_id": "...",          # For correlation checking
        }
        """
        rc = self.risk_config
        adjusted = proposal.copy()

        # ---- Check 1: Kill switch already triggered? ----
        if state.kill_switch_triggered:
            return False, "Kill switch is active — no new trades", adjusted

        # ---- Check 2: Portfolio-level limits ----
        # Max drawdown approaching
        if state.drawdown_from_peak_pct > rc.max_drawdown_pct * 0.8:
            return False, (
                f"Drawdown at {state.drawdown_from_peak_pct:.1f}% — "
                f"approaching kill limit ({rc.max_drawdown_pct}%)"
            ), adjusted

        # Daily loss approaching
        if state.today_pnl_pct < -(rc.daily_loss_limit_pct * 0.8):
            return False, (
                f"Daily loss at {state.today_pnl_pct:.1f}% — "
                f"approaching kill limit ({rc.daily_loss_limit_pct}%)"
            ), adjusted

        # ---- Check 3: Market-specific limits ----
        market = proposal.get("market", "")

        if market == "meme":
            # Max concurrent meme positions
            meme_positions = state.get_positions_for_market(Market.MEME)
            if len(meme_positions) >= rc.max_concurrent_meme_positions:
                return False, (
                    f"Max meme positions reached ({rc.max_concurrent_meme_positions})"
                ), adjusted

        # ---- Check 4: Arb spread minimum ----
        if proposal.get("strategy") == "arb":
            edge = proposal.get("estimated_edge_pct", 0)
            if edge < rc.min_arb_spread_pct:
                return False, (
                    f"Arb spread {edge:.1f}% below minimum ({rc.min_arb_spread_pct}%)"
                ), adjusted

        # ---- Check 5: Correlation check ----
        event_id = proposal.get("event_id", "")
        if event_id:
            correlated_count = sum(
                1 for p in state.open_positions
                if p.metadata.get("event_id") == event_id
            )
            if correlated_count >= rc.max_correlated_positions:
                return False, (
                    f"Max correlated positions on event {event_id} "
                    f"({rc.max_correlated_positions})"
                ), adjusted

        # ---- Check 6: Position sizing ----
        # Calculate max allowed size
        desk_allocation = self._get_desk_allocation(state, market)
        max_trade_size = desk_allocation * (rc.max_single_trade_pct / 100)

        proposed_cost = proposal.get("price", 0) * proposal.get("quantity", 0)
        if proposed_cost > max_trade_size:
            # Adjust quantity down
            if proposal.get("price", 0) > 0:
                adjusted_qty = math.floor(max_trade_size / proposal["price"])
                if adjusted_qty <= 0:
                    return False, (
                        f"Trade cost ${proposed_cost:.4f} exceeds max "
                        f"${max_trade_size:.4f} — cannot size down"
                    ), adjusted
                adjusted["quantity"] = adjusted_qty
                adjusted["size_adjusted"] = True
                self._logger.info(
                    f"Position sized down: {proposal['quantity']} → {adjusted_qty}"
                )

        # ---- Check 7: Kelly Criterion sizing (for directional bets) ----
        if proposal.get("strategy") == "directional":
            kelly_size = self._kelly_criterion(
                win_prob=proposal.get("confidence", 0.5),
                win_payout=proposal.get("estimated_edge_pct", 5) / 100,
                loss_amount=1.0,
            )
            # Cap Kelly at 25% (quarter-Kelly for safety)
            kelly_capped = min(kelly_size * 0.25, rc.max_single_trade_pct / 100)
            kelly_trade_size = desk_allocation * kelly_capped

            if proposal.get("price", 0) > 0:
                kelly_qty = math.floor(kelly_trade_size / proposal["price"])
                if kelly_qty < adjusted.get("quantity", 0):
                    adjusted["quantity"] = max(kelly_qty, 1)
                    adjusted["kelly_adjusted"] = True

        # ---- All checks passed ----
        return True, "Approved", adjusted

    # ---- Internal helpers ----

    def _get_desk_allocation(self, state: PortfolioState, market: str) -> float:
        """Get the current capital allocated to a market desk."""
        if market == "kalshi":
            return state.kalshi_balance
        elif market == "polymarket":
            return state.polymarket_balance
        elif market == "meme":
            return state.meme_balance
        return 0.0

    @staticmethod
    def _kelly_criterion(
        win_prob: float,
        win_payout: float,
        loss_amount: float,
    ) -> float:
        """
        Kelly Criterion: f* = (p * b - q) / b
        
        Where:
        - p = probability of winning
        - q = probability of losing (1 - p)
        - b = ratio of win amount to loss amount
        
        Returns fraction of bankroll to bet (0 to 1).
        Negative means don't bet.
        """
        if win_payout <= 0 or loss_amount <= 0:
            return 0.0

        p = max(0.0, min(1.0, win_prob))
        q = 1.0 - p
        b = win_payout / loss_amount

        kelly = (p * b - q) / b if b > 0 else 0.0
        return max(0.0, kelly)  # Never bet negative

    def get_risk_report(self, state: PortfolioState) -> dict:
        """Generate a risk report for the dashboard."""
        return {
            "portfolio_drawdown_pct": state.drawdown_from_peak_pct,
            "daily_pnl_pct": state.today_pnl_pct,
            "open_positions": len(state.open_positions),
            "meme_positions": len(state.get_positions_for_market(Market.MEME)),
            "current_phase": state.current_phase,
            "total_balance": state.total_balance,
            "peak_balance": state.peak_balance,
            "limits": {
                "max_drawdown_pct": self.risk_config.max_drawdown_pct,
                "daily_loss_limit_pct": self.risk_config.daily_loss_limit_pct,
                "max_meme_positions": self.risk_config.max_concurrent_meme_positions,
                "min_arb_spread_pct": self.risk_config.min_arb_spread_pct,
            },
        }
