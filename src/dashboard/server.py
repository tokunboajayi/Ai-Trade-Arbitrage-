"""
Live Dashboard Server — Real-time portfolio monitoring via web UI.

Serves a premium dark-mode dashboard at http://localhost:8080 with:
- REST API endpoints for status, trades, and snapshots.
- Static HTML/JS/CSS single-page app.
- Auto-refresh every 10 seconds.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite
from aiohttp import web

from src.core.state import PortfolioState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardServer:
    """Lightweight aiohttp dashboard serving REST API + static HTML."""

    def __init__(self, db_path: Path, state: PortfolioState, port: int = 8080):
        self.db_path = db_path
        self.state = state
        self.port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """Start the dashboard web server."""
        self._app = web.Application()
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/trades", self._handle_trades)
        self._app.router.add_get("/api/snapshots", self._handle_snapshots)
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_static("/static", STATIC_DIR, show_index=False)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Dashboard server started at http://localhost:{self.port}")

    async def stop(self) -> None:
        """Stop the dashboard server."""
        if self._runner:
            await self._runner.cleanup()
        logger.info("Dashboard server stopped.")

    # ---- API Handlers ----

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the main dashboard HTML."""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        return web.Response(text="Dashboard not found", status=404)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Return current portfolio state as JSON."""
        s = self.state
        data = {
            "total_balance": s.total_balance,
            "peak_balance": s.peak_balance,
            "drawdown_pct": s.drawdown_from_peak_pct,
            "today_pnl": s.today_pnl,
            "today_pnl_pct": s.today_pnl_pct,
            "kalshi_balance": s.kalshi_balance,
            "polymarket_balance": s.polymarket_balance,
            "meme_balance": s.meme_balance,
            "reserve_balance": s.reserve_balance,
            "total_trades": s.total_trades,
            "win_rate": s.win_rate,
            "total_realized_pnl": s.total_realized_pnl,
            "phase": s.current_phase,
            "open_positions": [
                {
                    "id": p.id[:8],
                    "symbol": p.symbol,
                    "market": p.market if isinstance(p.market, str) else p.market.value,
                    "side": p.side if isinstance(p.side, str) else p.side.value,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "quantity": p.quantity,
                    "pnl_pct": p.pnl_pct,
                    "hold_time_min": p.hold_time_seconds / 60,
                }
                for p in s.open_positions
            ],
            "timestamp": time.time(),
        }
        return web.json_response(data)

    async def _handle_trades(self, request: web.Request) -> web.Response:
        """Return recent trades from the database."""
        limit = int(request.query.get("limit", "50"))
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    """
                    SELECT id, market, strategy, symbol, side, entry_price,
                           exit_price, quantity, pnl, pnl_pct, exit_reason,
                           status, hold_time_seconds, opened_at, closed_at
                    FROM trades
                    ORDER BY opened_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                columns = [desc[0] for desc in cursor.description]
                rows = await cursor.fetchall()
                trades = [dict(zip(columns, row)) for row in rows]
            return web.json_response(trades)
        except Exception as e:
            logger.error(f"Dashboard trades query failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_snapshots(self, request: web.Request) -> web.Response:
        """Return portfolio snapshots for charting."""
        limit = int(request.query.get("limit", "500"))
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    """
                    SELECT total_balance, peak_balance, drawdown_pct,
                           kalshi_balance, polymarket_balance, meme_balance,
                           reserve_balance, total_pnl, phase, created_at
                    FROM portfolio_snapshots
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                columns = [desc[0] for desc in cursor.description]
                rows = await cursor.fetchall()
                snapshots = [dict(zip(columns, row)) for row in rows]
            # Reverse so oldest first for charting
            snapshots.reverse()
            return web.json_response(snapshots)
        except Exception as e:
            logger.error(f"Dashboard snapshots query failed: {e}")
            return web.json_response({"error": str(e)}, status=500)
