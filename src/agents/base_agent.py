"""
Base Agent — Abstract foundation for all agents in the system.

Every agent (scanner, executor, risk manager, etc.) inherits from this.
Provides lifecycle management, performance tracking, and the termination
interface that the orchestrator uses to kill/replace failing agents.

Design principles:
- Each agent has exactly ONE responsibility
- Each agent tracks its own performance metrics
- Each agent can be terminated and replaced independently
- Config is explicit — no hidden state
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.config import AgentStatus, AppConfig, Market
from src.core.state import AgentState, PortfolioState
from src.learning.trade_logger import TradeLogger

logger = logging.getLogger(__name__)


@dataclass
class AgentPerformance:
    """Snapshot of agent performance metrics."""
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_hold_time_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_pnl": self.total_pnl,
            "avg_pnl": self.avg_pnl,
            "best_trade_pnl": self.best_trade_pnl,
            "worst_trade_pnl": self.worst_trade_pnl,
            "win_rate": self.win_rate,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "avg_hold_time_seconds": self.avg_hold_time_seconds,
        }


class BaseAgent(ABC):
    """
    Abstract base class for all agents.
    
    Lifecycle:
    1. __init__: Create with config
    2. initialize(): Async setup (connections, etc.)
    3. run_cycle(): Called by orchestrator each loop iteration
    4. terminate(): Graceful shutdown
    
    The orchestrator calls evaluate() to check if this agent should be
    terminated and replaced.
    """

    def __init__(
        self,
        name: str,
        version: int = 1,
        market: Optional[Market] = None,
        config: Optional[dict] = None,
        inherited_knowledge: Optional[list] = None,
    ):
        self.id = str(uuid.uuid4())
        self.name = name
        self.version = version
        self.market = market
        self.status = AgentStatus.SPAWNING
        self.agent_config = config or {}
        self.inherited_knowledge = inherited_knowledge or []

        # Performance tracking
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_pnl = 0.0
        self.pnl_history: list[float] = []  # For Sharpe calculation
        self.allocated_capital = 0.0
        self.peak_capital = 0.0

        # Timestamps
        self.created_at = time.time()
        self.last_run_at: Optional[float] = None
        self.last_trade_at: Optional[float] = None

        # Error tracking
        self.consecutive_errors = 0
        self.max_consecutive_errors = 5  # Auto-pause after this many

        self._logger = logging.getLogger(f"agent.{name}_v{version}")

    # ---- Abstract methods (must be implemented) ----

    @abstractmethod
    async def initialize(self, app_config: AppConfig, trade_logger: TradeLogger) -> None:
        """Set up connections, load state. Called once on startup."""
        ...

    @abstractmethod
    async def run_cycle(self, state: PortfolioState) -> list[dict[str, Any]]:
        """
        Execute one cycle of the agent's logic.
        
        Returns a list of actions to take, e.g.:
        [
            {"action": "open_position", "market": "kalshi", "symbol": "...", ...},
            {"action": "close_position", "position_id": "...", ...},
            {"action": "signal", "type": "arb_opportunity", "data": {...}},
        ]
        
        The orchestrator processes these actions through the execution pipeline.
        """
        ...

    @abstractmethod
    async def terminate(self) -> None:
        """Graceful shutdown. Close connections, save state."""
        ...

    # ---- Lifecycle methods ----

    async def start(self, app_config: AppConfig, trade_logger: TradeLogger) -> None:
        """Initialize and mark as active."""
        try:
            await self.initialize(app_config, trade_logger)
            self.status = AgentStatus.ACTIVE
            self._logger.info(f"Agent started: {self.full_name}")
        except Exception as e:
            self.status = AgentStatus.TERMINATED
            self._logger.error(f"Agent failed to start: {e}")
            raise

    async def safe_run_cycle(self, state: PortfolioState) -> list[dict[str, Any]]:
        """Run cycle with error handling. Pauses agent after too many errors."""
        if self.status != AgentStatus.ACTIVE:
            return []

        try:
            self.last_run_at = time.time()
            actions = await self.run_cycle(state)
            self.consecutive_errors = 0  # Reset on success
            return actions

        except Exception as e:
            self.consecutive_errors += 1
            self._logger.error(
                f"Cycle error ({self.consecutive_errors}/{self.max_consecutive_errors}): {e}"
            )

            if self.consecutive_errors >= self.max_consecutive_errors:
                self.status = AgentStatus.PAUSED
                self._logger.warning(
                    f"Agent paused after {self.consecutive_errors} consecutive errors"
                )

            return []

    async def stop(self) -> None:
        """Terminate and mark as terminated."""
        try:
            await self.terminate()
        except Exception as e:
            self._logger.error(f"Error during termination: {e}")
        finally:
            self.status = AgentStatus.TERMINATED
            self._logger.info(f"Agent terminated: {self.full_name}")

    # ---- Performance tracking ----

    def record_trade(self, pnl: float) -> None:
        """Record a trade outcome for performance tracking."""
        self.trade_count += 1
        self.total_pnl += pnl
        self.pnl_history.append(pnl)
        self.last_trade_at = time.time()

        if pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

    def get_performance(self) -> AgentPerformance:
        """Calculate current performance metrics."""
        perf = AgentPerformance(
            trade_count=self.trade_count,
            win_count=self.win_count,
            loss_count=self.loss_count,
            total_pnl=self.total_pnl,
        )

        if self.trade_count > 0:
            perf.win_rate = self.win_count / self.trade_count
            perf.avg_pnl = self.total_pnl / self.trade_count

        if self.pnl_history:
            perf.best_trade_pnl = max(self.pnl_history)
            perf.worst_trade_pnl = min(self.pnl_history)
            perf.sharpe_ratio = self._calculate_sharpe()

        if self.allocated_capital > 0:
            peak = max(
                self.allocated_capital,
                self.allocated_capital + max(
                    (sum(self.pnl_history[:i+1]) for i in range(len(self.pnl_history))),
                    default=0,
                )
            )
            trough = self.allocated_capital + min(
                (sum(self.pnl_history[:i+1]) for i in range(len(self.pnl_history))),
                default=0,
            )
            if peak > 0:
                perf.max_drawdown_pct = ((peak - trough) / peak) * 100

        return perf

    def _calculate_sharpe(self, risk_free_rate: float = 0.0) -> float:
        """Calculate Sharpe ratio from PnL history."""
        if len(self.pnl_history) < 2:
            return 0.0

        import statistics
        mean_pnl = statistics.mean(self.pnl_history)
        std_pnl = statistics.stdev(self.pnl_history)

        if std_pnl == 0:
            return 0.0

        return (mean_pnl - risk_free_rate) / std_pnl

    def set_allocation(self, capital: float) -> None:
        """Set the capital allocated to this agent."""
        self.allocated_capital = capital
        if capital > self.peak_capital:
            self.peak_capital = capital

    # ---- State export ----

    def to_agent_state(self) -> AgentState:
        """Export to AgentState for portfolio tracking."""
        return AgentState(
            id=self.id,
            name=self.full_name,
            version=self.version,
            status=self.status,
            market=self.market,
            allocated_capital=self.allocated_capital,
            total_pnl=self.total_pnl,
            trade_count=self.trade_count,
            win_count=self.win_count,
            loss_count=self.loss_count,
            created_at=self.created_at,
            last_trade_at=self.last_trade_at,
            config=self.agent_config,
            inherited_knowledge=self.inherited_knowledge,
        )

    @property
    def full_name(self) -> str:
        return f"{self.name}_v{self.version}"

    @property
    def is_active(self) -> bool:
        return self.status == AgentStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} {self.full_name} "
            f"status={self.status.value} trades={self.trade_count} "
            f"pnl=${self.total_pnl:.4f}>"
        )
