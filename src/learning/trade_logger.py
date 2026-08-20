"""
Trade Logger — Records every trade, position change, and system event to SQLite.

This is the foundation of the learning loop. Without complete trade logs,
the system cannot learn from its mistakes or improve its strategies.

Every trade gets logged with full context: signal data, market conditions,
agent version, strategy used, entry/exit prices, PnL, and timing.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Database schema
SCHEMA = """
-- Trades: every completed trade
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL,           -- kalshi | polymarket | meme
    strategy TEXT NOT NULL,         -- arb | directional | snipe
    symbol TEXT NOT NULL,           -- ticker / token address / event slug
    side TEXT NOT NULL,             -- buy | sell | yes | no
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity REAL NOT NULL,
    cost_basis REAL NOT NULL,
    pnl REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    hold_time_seconds REAL DEFAULT 0,
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    agent_version INTEGER DEFAULT 1,
    signal_data TEXT,              -- JSON: what triggered the trade
    market_conditions TEXT,        -- JSON: state of the market at entry
    exit_reason TEXT,              -- take_profit | stop_loss | trailing_stop | time_limit | manual | arb_close
    status TEXT DEFAULT 'open',   -- open | closed | cancelled
    opened_at REAL NOT NULL,
    closed_at REAL,
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- Agent history: tracks agent lifecycle
CREATE TABLE IF NOT EXISTS agent_history (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    event TEXT NOT NULL,            -- spawned | terminated | paused | resumed
    reason TEXT,
    config TEXT,                    -- JSON: agent config at time of event
    performance_snapshot TEXT,      -- JSON: metrics at time of event
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- Portfolio snapshots: periodic state captures
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id TEXT PRIMARY KEY,
    total_balance REAL NOT NULL,
    peak_balance REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    kalshi_balance REAL DEFAULT 0,
    polymarket_balance REAL DEFAULT 0,
    meme_balance REAL DEFAULT 0,
    reserve_balance REAL DEFAULT 0,
    open_position_count INTEGER DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    phase TEXT DEFAULT 'seed',
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- System events: kill switch triggers, errors, restarts
CREATE TABLE IF NOT EXISTS system_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,       -- kill_switch | error | restart | config_change | rebalance
    severity TEXT DEFAULT 'info',   -- info | warning | error | critical
    message TEXT NOT NULL,
    data TEXT,                      -- JSON: additional context
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- Learning outcomes: what the strategy evolver learned
CREATE TABLE IF NOT EXISTS learning_outcomes (
    id TEXT PRIMARY KEY,
    analysis_type TEXT NOT NULL,    -- trade_review | parameter_update | agent_evolution
    findings TEXT NOT NULL,         -- JSON: what was learned
    actions_taken TEXT,             -- JSON: what was changed
    affected_agents TEXT,           -- JSON: list of agent IDs affected
    trade_window_start REAL,
    trade_window_end REAL,
    created_at REAL DEFAULT (strftime('%s', 'now'))
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_agent_history_agent ON agent_history(agent_id);
CREATE INDEX IF NOT EXISTS idx_system_events_type ON system_events(event_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON portfolio_snapshots(created_at);
"""


class TradeLogger:
    """
    Async SQLite trade logger. Every trade, every event, every decision — logged.
    
    Usage:
        logger = TradeLogger(Path("./data"))
        await logger.initialize()
        await logger.log_trade_opened(...)
        await logger.log_trade_closed(...)
    """

    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "trades.db"
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create database and tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        # Enable WAL mode for crash safety and concurrent reads
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info(f"Trade database initialized at {self.db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---- Trade logging ----

    async def log_trade_opened(
        self,
        market: str,
        strategy: str,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        agent_id: str,
        agent_name: str,
        agent_version: int = 1,
        signal_data: Optional[dict] = None,
        market_conditions: Optional[dict] = None,
    ) -> str:
        """Log a new trade. Returns the trade ID."""
        trade_id = str(uuid.uuid4())
        cost_basis = entry_price * quantity

        await self._db.execute(
            """
            INSERT INTO trades (
                id, market, strategy, symbol, side, entry_price, quantity,
                cost_basis, agent_id, agent_name, agent_version,
                signal_data, market_conditions, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                trade_id, market, strategy, symbol, side, entry_price, quantity,
                cost_basis, agent_id, agent_name, agent_version,
                json.dumps(signal_data or {}),
                json.dumps(market_conditions or {}),
                time.time(),
            ),
        )
        await self._db.commit()
        logger.info(
            f"Trade opened: {trade_id[:8]} | {market}/{strategy} | "
            f"{side} {quantity} @ ${entry_price:.4f}"
        )
        return trade_id

    async def log_trade_closed(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        fees: float = 0.0,
    ) -> dict:
        """Close a trade and calculate PnL. Returns trade summary."""
        # Get the trade
        cursor = await self._db.execute(
            "SELECT entry_price, quantity, cost_basis, opened_at FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Trade {trade_id} not found")

        entry_price, quantity, cost_basis, opened_at = row
        exit_value = exit_price * quantity
        pnl = exit_value - cost_basis - fees
        pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0
        hold_time = time.time() - opened_at

        await self._db.execute(
            """
            UPDATE trades SET
                exit_price = ?, pnl = ?, pnl_pct = ?, fees = ?,
                hold_time_seconds = ?, exit_reason = ?, status = 'closed',
                closed_at = ?
            WHERE id = ?
            """,
            (exit_price, pnl, pnl_pct, fees, hold_time, exit_reason, time.time(), trade_id),
        )
        await self._db.commit()

        summary = {
            "trade_id": trade_id,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "fees": fees,
            "hold_time_seconds": hold_time,
            "exit_reason": exit_reason,
        }
        logger.info(
            f"Trade closed: {trade_id[:8]} | "
            f"PnL: ${pnl:.4f} ({pnl_pct:+.1f}%) | "
            f"Reason: {exit_reason}"
        )
        return summary

    # ---- Agent logging ----

    async def log_agent_event(
        self,
        agent_id: str,
        agent_name: str,
        version: int,
        event: str,
        reason: str = "",
        config: Optional[dict] = None,
        performance: Optional[dict] = None,
    ) -> None:
        """Log an agent lifecycle event."""
        await self._db.execute(
            """
            INSERT INTO agent_history (
                id, agent_id, agent_name, version, event, reason,
                config, performance_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), agent_id, agent_name, version, event, reason,
                json.dumps(config or {}),
                json.dumps(performance or {}),
            ),
        )
        await self._db.commit()
        logger.info(f"Agent event: {agent_name}_v{version} → {event} ({reason})")

    # ---- Portfolio snapshots ----

    async def log_portfolio_snapshot(
        self,
        total_balance: float,
        peak_balance: float,
        drawdown_pct: float,
        kalshi_balance: float = 0,
        polymarket_balance: float = 0,
        meme_balance: float = 0,
        reserve_balance: float = 0,
        open_positions: int = 0,
        total_trades: int = 0,
        win_rate: float = 0,
        total_pnl: float = 0,
        phase: str = "seed",
    ) -> None:
        """Take a portfolio snapshot for historical tracking."""
        await self._db.execute(
            """
            INSERT INTO portfolio_snapshots (
                id, total_balance, peak_balance, drawdown_pct,
                kalshi_balance, polymarket_balance, meme_balance, reserve_balance,
                open_position_count, total_trades, win_rate, total_pnl, phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), total_balance, peak_balance, drawdown_pct,
                kalshi_balance, polymarket_balance, meme_balance, reserve_balance,
                open_positions, total_trades, win_rate, total_pnl, phase,
            ),
        )
        await self._db.commit()

    # ---- System events ----

    async def log_system_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        data: Optional[dict] = None,
    ) -> None:
        """Log a system event (kill switch, error, restart, etc.)."""
        await self._db.execute(
            """
            INSERT INTO system_events (id, event_type, severity, message, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), event_type, severity, message, json.dumps(data or {})),
        )
        await self._db.commit()
        log_func = getattr(logger, severity, logger.info)
        log_func(f"System event [{event_type}]: {message}")

    # ---- Learning outcomes ----

    async def log_learning_outcome(
        self,
        analysis_type: str,
        findings: dict,
        actions_taken: Optional[dict] = None,
        affected_agents: Optional[list] = None,
        trade_window: Optional[tuple[float, float]] = None,
    ) -> None:
        """Log what the learning engine discovered and changed."""
        await self._db.execute(
            """
            INSERT INTO learning_outcomes (
                id, analysis_type, findings, actions_taken, affected_agents,
                trade_window_start, trade_window_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                analysis_type,
                json.dumps(findings),
                json.dumps(actions_taken or {}),
                json.dumps(affected_agents or []),
                trade_window[0] if trade_window else None,
                trade_window[1] if trade_window else None,
            ),
        )
        await self._db.commit()

    # ---- Query methods (for learning engine) ----

    async def get_trades(
        self,
        market: Optional[str] = None,
        strategy: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: str = "closed",
        since: Optional[float] = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Query trades with filters. Used by the learning engine."""
        query = "SELECT * FROM trades WHERE status = ?"
        params: list[Any] = [status]

        if market:
            query += " AND market = ?"
            params.append(market)
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if since:
            query += " AND opened_at > ?"
            params.append(since)

        query += " ORDER BY opened_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self._db.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    async def get_agent_performance(
        self,
        agent_id: str,
        window_days: int = 7,
    ) -> dict:
        """Get aggregated performance metrics for an agent."""
        since = time.time() - (window_days * 86400)
        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(pnl) as avg_pnl,
                AVG(pnl_pct) as avg_pnl_pct,
                MAX(pnl) as best_trade,
                MIN(pnl) as worst_trade,
                AVG(hold_time_seconds) as avg_hold_time
            FROM trades
            WHERE agent_id = ? AND status = 'closed' AND opened_at > ?
            """,
            (agent_id, since),
        )
        row = await cursor.fetchone()
        columns = [desc[0] for desc in cursor.description]
        result = dict(zip(columns, row))

        # Calculate win rate
        tc = result.get("trade_count", 0) or 0
        w = result.get("wins", 0) or 0
        result["win_rate"] = w / tc if tc > 0 else 0

        return result

    async def get_strategy_performance(
        self,
        strategy: str,
        window_days: int = 14,
    ) -> dict:
        """Get aggregated performance for a strategy type."""
        since = time.time() - (window_days * 86400)
        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(pnl) as total_pnl,
                AVG(pnl_pct) as avg_pnl_pct,
                AVG(hold_time_seconds) as avg_hold_time
            FROM trades
            WHERE strategy = ? AND status = 'closed' AND opened_at > ?
            """,
            (strategy, since),
        )
        row = await cursor.fetchone()
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
