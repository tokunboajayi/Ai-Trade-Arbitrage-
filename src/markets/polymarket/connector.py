"""
Polymarket API Connector — Handles all communication with Polymarket's Gamma/CLOB API.

Supports:
- Market data (order books, trades, metadata)
- Order management (place, cancel, get positions)
- Paper trading mode (logs orders without submitting)

Note: Live trading on Polymarket requires Web3 signing (L1 -> L2).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import PolymarketConfig

logger = logging.getLogger(__name__)


class PolymarketConnector:
    """
    Low-level Polymarket API wrapper for CLOB / Gamma API.
    """

    def __init__(self, config: PolymarketConfig, paper_mode: bool = True):
        self.config = config
        self.paper_mode = paper_mode
        self._gamma_client: Optional[httpx.AsyncClient] = None
        self._clob_client: Optional[httpx.AsyncClient] = None
        self._last_request_at: float = 0
        self._rate_limit_interval = 1.0 / config.rate_limit_per_sec

        # Paper trade tracking
        self._paper_orders: list[dict] = []
        self._paper_positions: dict[str, dict] = {}
        self._paper_balance: float = 100.0  # Start with 100 USDC in paper mode

    async def connect(self) -> None:
        """Initialize HTTP clients."""
        self._gamma_client = httpx.AsyncClient(
            base_url=self.config.gamma_url,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )
        self._clob_client = httpx.AsyncClient(
            base_url=self.config.clob_url,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

        mode = "PAPER" if self.paper_mode else "LIVE"
        logger.info(f"Polymarket connector ready ({mode} mode)")

    async def disconnect(self) -> None:
        if self._gamma_client:
            await self._gamma_client.aclose()
        if self._clob_client:
            await self._clob_client.aclose()

    # ---- Market Data (Gamma API) ----

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get list of markets from Gamma API."""
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
        }
        return await self._request(self._gamma_client, "GET", "/markets", params=params)

    async def get_market(self, condition_id: str) -> dict:
        """Get details for a specific market."""
        return await self._request(self._gamma_client, "GET", f"/markets/{condition_id}")

    # ---- Orderbook Data (CLOB API) ----

    async def get_orderbook(self, token_id: str) -> dict:
        """Get the order book for a specific token ID."""
        return await self._request(
            self._clob_client,
            "GET",
            "/book",
            params={"token_id": token_id},
        )

    # ---- Order Management (CLOB API) ----

    async def place_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        size: float,
        price: float,
        order_type: str = "FOK",  # FOK, GTC, GTD
    ) -> dict:
        """Place an order."""
        order_data = {
            "token_id": token_id,
            "side": side.upper(),
            "size": size,
            "price": price,
            "type": order_type,
        }

        if self.paper_mode:
            return self._paper_place_order(order_data)

        # In a real environment, this requires signing the order via Web3.
        # This is a placeholder for the actual CLOB signing logic.
        logger.warning("Live Polymarket ordering requires L1 signature implementation. Falling back to paper.")
        return self._paper_place_order(order_data)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        if self.paper_mode:
            return self._paper_cancel_order(order_id)

        # Requires signing
        logger.warning("Live Polymarket cancel requires L1 signature implementation. Falling back to paper.")
        return self._paper_cancel_order(order_id)

    async def cancel_all_orders(self) -> list[dict]:
        """Cancel all open orders. Used by kill switch."""
        if self.paper_mode:
            cancelled = self._paper_orders.copy()
            self._paper_orders.clear()
            logger.info(f"Paper: cancelled {len(cancelled)} Polymarket orders")
            return cancelled
        
        logger.warning("Live Polymarket cancel_all requires L1 signature implementation.")
        return []

    async def get_balance(self) -> dict:
        """Get USDC balance on Polygon."""
        if self.paper_mode:
            return {"balance": self._paper_balance}

        # Real implementation would query the USDC contract on Polygon via RPC
        return {"balance": 0.0}

    # ---- Paper trading ----

    def _paper_place_order(self, order_data: dict) -> dict:
        """Simulate order placement in paper mode."""
        order_id = str(uuid.uuid4())
        paper_order = {
            "order_id": order_id,
            "status": "resting",
            "created_time": datetime.now(timezone.utc).isoformat(),
            **order_data,
        }
        self._paper_orders.append(paper_order)
        logger.info(
            f"Paper order placed: {order_data['side']} {order_data['size']}x "
            f"Token {order_data['token_id']} @ ${order_data.get('price')}"
        )
        return {"order": paper_order}

    def _paper_cancel_order(self, order_id: str) -> dict:
        """Simulate order cancellation in paper mode."""
        self._paper_orders = [o for o in self._paper_orders if o["order_id"] != order_id]
        return {"order_id": order_id, "status": "cancelled"}

    # ---- HTTP request helper ----

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        """Make an API request with rate limiting and retry logic."""
        elapsed = time.time() - self._last_request_at
        if elapsed < self._rate_limit_interval:
            import asyncio
            await asyncio.sleep(self._rate_limit_interval - elapsed)

        self._last_request_at = time.time()

        response = await client.request(
            method,
            path,
            params=params,
            json=json,
        )

        if response.status_code >= 400:
            error_body = response.text
            logger.error(f"Polymarket API error {response.status_code}: {error_body}")
            response.raise_for_status()

        return response.json()
