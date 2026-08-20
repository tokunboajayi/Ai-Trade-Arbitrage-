"""
Portfolio state management.
Tracks all positions, balances, agent states, and portfolio metrics.
Persisted to SQLite. Loaded on startup for crash recovery.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.core.config import AgentStatus, Market


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    YES = "yes"
    NO = "no"


@dataclass
class Position:
    """A single open position across any market."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    market: Market = Market.KALSHI
    symbol: str = ""  # ticker / token address / event slug
    side: Side = Side.BUY
    entry_price: float = 0.0
    quantity: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: float = field(default_factory=time.time)
    agent_id: str = ""
    strategy: str = ""  # arb | directional | snipe
    metadata: dict = field(default_factory=dict)  # event_id, token_info, etc.

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity

    @property
    def current_value(self) -> float:
        return self.current_price * self.quantity

    @property
    def hold_time_seconds(self) -> float:
        return time.time() - self.opened_at

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return ((self.current_value - self.cost_basis) / self.cost_basis) * 100


# ---------------------------------------------------------------------------
# Agent state tracking
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Runtime state of a single agent."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    version: int = 1
    status: AgentStatus = AgentStatus.ACTIVE
    market: Optional[Market] = None
    allocated_capital: float = 0.0
    total_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_trade_at: Optional[float] = None
    config: dict = field(default_factory=dict)  # Agent-specific parameters
    inherited_knowledge: list = field(default_factory=list)  # Lessons from dead agents

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.win_count / self.trade_count

    @property
    def avg_pnl_per_trade(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.total_pnl / self.trade_count


# ---------------------------------------------------------------------------
# Portfolio state (the whole picture)
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    """
    Master state object. Single source of truth for the entire system.
    Checkpointed to SQLite after every trade cycle.
    """

    # Balances
    total_balance: float = 1.0  # Starting with $1
    peak_balance: float = 1.0
    today_start_balance: float = 1.0

    # Per-desk balances
    kalshi_balance: float = 0.0
    polymarket_balance: float = 0.0
    meme_balance: float = 0.0
    reserve_balance: float = 0.0

    # Positions
    open_positions: list[Position] = field(default_factory=list)

    # Agents
    active_agents: list[AgentState] = field(default_factory=list)
    terminated_agents: list[AgentState] = field(default_factory=list)

    # Counters
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_realized_pnl: float = 0.0
    trades_since_last_learn: int = 0

    # Timestamps
    started_at: float = field(default_factory=time.time)
    last_checkpoint_at: float = field(default_factory=time.time)
    last_rebalance_at: float = 0.0
    last_learn_at: float = 0.0
    last_heartbeat_at: float = field(default_factory=time.time)

    # Kill switch state
    kill_switch_triggered: bool = False
    kill_switch_reason: str = ""

    # ---- Computed properties ----

    @property
    def drawdown_from_peak_pct(self) -> float:
        if self.peak_balance == 0:
            return 0.0
        return ((self.peak_balance - self.total_balance) / self.peak_balance) * 100

    @property
    def today_pnl(self) -> float:
        return self.total_balance - self.today_start_balance

    @property
    def today_pnl_pct(self) -> float:
        if self.today_start_balance == 0:
            return 0.0
        return (self.today_pnl / self.today_start_balance) * 100

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_wins / self.total_trades

    @property
    def positions_by_market(self) -> dict[Market, list[Position]]:
        result: dict[Market, list[Position]] = {}
        for pos in self.open_positions:
            result.setdefault(pos.market, []).append(pos)
        return result

    @property
    def current_phase(self) -> str:
        """Determine which capital allocation phase we're in."""
        if self.total_balance < 10.0:
            return "seed"
        elif self.total_balance < 100.0:
            return "growth"
        else:
            return "scale"

    # ---- State mutations ----

    def update_peak(self) -> None:
        if self.total_balance > self.peak_balance:
            self.peak_balance = self.total_balance

    def record_heartbeat(self) -> None:
        self.last_heartbeat_at = time.time()

    def add_position(self, position: Position) -> None:
        self.open_positions.append(position)

    def close_position(self, position_id: str, exit_price: float) -> Optional[Position]:
        for i, pos in enumerate(self.open_positions):
            if pos.id == position_id:
                pos.current_price = exit_price
                pos.realized_pnl = pos.current_value - pos.cost_basis
                closed = self.open_positions.pop(i)

                # Update portfolio stats
                self.total_trades += 1
                self.trades_since_last_learn += 1
                self.total_realized_pnl += closed.realized_pnl

                if closed.realized_pnl > 0:
                    self.total_wins += 1
                else:
                    self.total_losses += 1

                self.total_balance += closed.realized_pnl
                self.update_peak()
                return closed
        return None

    def get_positions_for_market(self, market: Market) -> list[Position]:
        return [p for p in self.open_positions if p.market == market]

    def get_agent(self, agent_id: str) -> Optional[AgentState]:
        for agent in self.active_agents:
            if agent.id == agent_id:
                return agent
        return None
