"""
Capital Allocator — Decides how to distribute funds across desks.

Uses a phase-based system:
- Seed ($1-$10): 80% arb, 20% meme
- Growth ($10-$100): 50% arb, 30% directional, 20% meme
- Scale ($100+): 40% arb, 30% directional, 20% meme, 10% reserve

Rebalancing happens hourly based on rolling Sharpe ratio of each desk.
Better-performing desks get more capital. Losing desks get starved.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.core.config import AllocationConfig, AppConfig, Market
from src.core.state import PortfolioState
from src.learning.trade_logger import TradeLogger

logger = logging.getLogger(__name__)


class CapitalAllocator(BaseAgent):
    """
    Allocates capital across trading desks based on phase and performance.
    Runs every rebalance interval (default: 1 hour).
    """

    def __init__(self, version: int = 1, config: Optional[dict] = None, **kwargs):
        super().__init__(
            name="capital_allocator",
            version=version,
            config=config,
            **kwargs,
        )
        self.alloc_config: Optional[AllocationConfig] = None
        self.trade_logger: Optional[TradeLogger] = None
        self._last_rebalance = 0.0

    async def initialize(self, app_config: AppConfig, trade_logger: TradeLogger) -> None:
        self.alloc_config = app_config.allocation
        self.trade_logger = trade_logger
        self._logger.info("Capital allocator initialized")

    async def run_cycle(self, state: PortfolioState) -> list[dict[str, Any]]:
        """Check if rebalancing is needed and emit rebalance actions."""
        now = time.time()
        interval = self.alloc_config.rebalance_interval_hours * 3600

        if now - self._last_rebalance < interval:
            return []  # Not time yet

        return await self.rebalance(state)

    async def terminate(self) -> None:
        self._logger.info("Capital allocator terminated")

    # ---- Rebalancing ----

    async def rebalance(self, state: PortfolioState) -> list[dict[str, Any]]:
        """
        Calculate target allocations and emit rebalance actions.
        Returns list of allocation change actions for the orchestrator.
        """
        self._last_rebalance = time.time()
        state.last_rebalance_at = self._last_rebalance

        phase = state.current_phase
        total = state.total_balance
        actions = []

        # Get base allocation for current phase
        targets = self._get_phase_targets(phase)

        # Adjust based on performance (if we have enough data)
        if state.total_trades >= 20:
            targets = await self._adjust_by_performance(targets, state)

        # Calculate target balances
        target_kalshi = total * targets.get("arb", 0) / 100
        target_poly = 0.0  # Arb is split between Kalshi and Polymarket
        target_meme = total * targets.get("meme", 0) / 100
        target_reserve = total * targets.get("reserve", 0) / 100

        # For arb, split evenly between Kalshi and Polymarket
        arb_total = total * targets.get("arb", 0) / 100
        directional_total = total * targets.get("directional", 0) / 100
        target_kalshi = (arb_total + directional_total) / 2
        target_poly = (arb_total + directional_total) / 2

        # Emit rebalance actions
        actions.append({
            "action": "rebalance",
            "allocations": {
                "kalshi": round(target_kalshi, 6),
                "polymarket": round(target_poly, 6),
                "meme": round(target_meme, 6),
                "reserve": round(target_reserve, 6),
            },
            "phase": phase,
            "total_balance": total,
            "targets_pct": targets,
        })

        # Apply to state
        state.kalshi_balance = round(target_kalshi, 6)
        state.polymarket_balance = round(target_poly, 6)
        state.meme_balance = round(target_meme, 6)
        state.reserve_balance = round(target_reserve, 6)

        self._logger.info(
            f"Rebalanced (phase={phase}): "
            f"Kalshi=${target_kalshi:.4f} Poly=${target_poly:.4f} "
            f"Meme=${target_meme:.4f} Reserve=${target_reserve:.4f}"
        )

        return actions

    def _get_phase_targets(self, phase: str) -> dict[str, float]:
        """Get base allocation percentages for the current phase."""
        ac = self.alloc_config

        if phase == "seed":
            return {
                "arb": ac.seed_arb_pct,
                "directional": 0,
                "meme": ac.seed_meme_pct,
                "reserve": 0,
            }
        elif phase == "growth":
            return {
                "arb": ac.growth_arb_pct,
                "directional": ac.growth_directional_pct,
                "meme": ac.growth_meme_pct,
                "reserve": 0,
            }
        else:  # scale
            return {
                "arb": ac.scale_arb_pct,
                "directional": ac.scale_directional_pct,
                "meme": ac.scale_meme_pct,
                "reserve": ac.scale_reserve_pct,
            }

    async def _adjust_by_performance(
        self,
        base_targets: dict[str, float],
        state: PortfolioState,
    ) -> dict[str, float]:
        """
        Adjust allocations based on rolling Sharpe ratio of each strategy.
        Better-performing strategies get up to 50% more allocation.
        Losing strategies get up to 50% less.
        """
        if not self.trade_logger:
            return base_targets

        adjusted = base_targets.copy()

        # Get performance by strategy
        strategies = ["arb", "directional", "snipe"]
        strategy_map = {"arb": "arb", "directional": "directional", "snipe": "meme"}
        performances = {}

        for strat in strategies:
            perf = await self.trade_logger.get_strategy_performance(
                strategy=strat,
                window_days=self.alloc_config.rebalance_sharpe_window_days,
            )
            total_pnl = perf.get("total_pnl") or 0
            trade_count = perf.get("trade_count") or 0
            performances[strat] = {
                "total_pnl": total_pnl,
                "trade_count": trade_count,
                "avg_pnl_pct": perf.get("avg_pnl_pct") or 0,
            }

        # Calculate adjustment factors
        total_pnl_all = sum(p["total_pnl"] for p in performances.values())

        if total_pnl_all == 0:
            return base_targets  # No data to adjust on

        for strat, perf in performances.items():
            desk_key = strategy_map.get(strat, strat)
            if desk_key not in adjusted or adjusted[desk_key] == 0:
                continue

            if perf["trade_count"] < 5:
                continue  # Not enough data

            # Scale factor: -50% to +50% based on relative performance
            if perf["total_pnl"] > 0:
                # Winning desk: boost up to 50%
                scale = min(1.5, 1.0 + (perf["avg_pnl_pct"] / 20))
            else:
                # Losing desk: reduce up to 50%
                scale = max(0.5, 1.0 + (perf["avg_pnl_pct"] / 20))

            adjusted[desk_key] = adjusted[desk_key] * scale

        # Normalize to 100%
        total = sum(adjusted.values())
        if total > 0:
            for key in adjusted:
                adjusted[key] = (adjusted[key] / total) * 100

        return adjusted
