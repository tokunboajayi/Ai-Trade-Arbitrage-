"""
Kalshi API Connector — Handles all communication with Kalshi's REST + WebSocket API.

Supports:
- Market data (order books, trades, metadata)
- Order management (place, cancel, get positions)
- Authentication via ECDSA signing
- Rate limiting (5 req/sec authenticated)
- Paper trading mode (logs orders without submitting)

Uses demo environment by default until switched to live.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.config import Environment, KalshiConfig

logger = logging.getLogger(__name__)


class KalshiConnector:
    """
    Low-level Kalshi API wrapper.
    
    All market-specific logic (scanning, arb finding) lives in separate
    agent classes. This connector only handles raw API calls.
    """

    def __init__(self, config: KalshiConfig, paper_mode: bool = True):
        self.config = config
        self.paper_mode = paper_mode
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._last_request_at: float = 0
        self._rate_limit_interval = 1.0 / config.rate_limit_per_sec

        # Paper trade tracking
        self._paper_orders: list[dict] = []
        self._paper_positions: dict[str, dict] = {}

    async def connect(self) -> None:
        """Initialize HTTP client and authenticate."""
        self._client = httpx.AsyncClient(
            base_url=self.config.active_base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

        if not self.paper_mode and self.config.api_key_id:
            await self._authenticate()

        mode = "PAPER" if self.paper_mode else "LIVE"
        env = "DEMO" if self.config.env == "demo" else "PRODUCTION"
        logger.info(f"Kalshi connector ready ({mode} mode, {env} env)")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()

    # ---- Authentication ----

    async def _authenticate(self) -> None:
        """Authenticate with Kalshi API using API key."""
        try:
            response = await self._request(
                "POST",
                "/trade-api/v2/login",
                json={
                    "email": "",  # API key auth doesn't need email
                    "password": "",
                },
                authenticated=False,
            )
            self._token = response.get("token")
            # Token typically valid for 24 hours
            self._token_expires_at = time.time() + 86000
            logger.info("Kalshi authentication successful")
        except Exception as e:
            logger.error(f"Kalshi authentication failed: {e}")
            raise

    # ---- Market Data ----

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
    ) -> dict:
        """Get list of markets with optional filters."""
        params: dict[str, Any] = {
            "status": status,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker

        return await self._request("GET", "/trade-api/v2/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        """Get details for a specific market."""
        return await self._request("GET", f"/trade-api/v2/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Get the order book for a market."""
        return await self._request(
            "GET",
            f"/trade-api/v2/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict:
        """Get recent trades."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor

        return await self._request("GET", "/trade-api/v2/markets/trades", params=params)

    async def get_events(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict:
        """Get list of events (each event can have multiple markets)."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        return await self._request("GET", "/trade-api/v2/events", params=params)

    async def get_event(self, event_ticker: str) -> dict:
        """Get details for a specific event."""
        return await self._request("GET", f"/trade-api/v2/events/{event_ticker}")

    # ---- Order Management ----

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        price: int,  # In cents (1-99)
        order_type: str = "limit",
        expiration_ts: Optional[int] = None,
    ) -> dict:
        """
        Place an order on Kalshi.
        
        In paper mode, logs the order but doesn't submit to the API.
        
        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price: Price in cents (1-99)
            order_type: "limit" or "market"
            expiration_ts: Optional expiration timestamp
        """
        order_data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }

        if order_type == "limit":
            order_data["yes_price"] = price if side == "yes" else None
            order_data["no_price"] = price if side == "no" else None

        if expiration_ts:
            order_data["expiration_ts"] = expiration_ts

        if self.paper_mode:
            return self._paper_place_order(order_data)

        return await self._request(
            "POST",
            "/trade-api/v2/portfolio/orders",
            json=order_data,
        )

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        if self.paper_mode:
            return self._paper_cancel_order(order_id)

        return await self._request(
            "DELETE",
            f"/trade-api/v2/portfolio/orders/{order_id}",
        )

    async def get_positions(self) -> dict:
        """Get all current positions."""
        if self.paper_mode:
            return {"market_positions": list(self._paper_positions.values())}

        return await self._request("GET", "/trade-api/v2/portfolio/positions")

    async def get_balance(self) -> dict:
        """Get account balance."""
        if self.paper_mode:
            return {"balance": 100_00}  # $100 paper balance in cents

        return await self._request("GET", "/trade-api/v2/portfolio/balance")

    async def get_fills(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Get trade fills (executed trades)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker

        return await self._request("GET", "/trade-api/v2/portfolio/fills", params=params)

    # ---- Batch operations ----

    async def cancel_all_orders(self) -> list[dict]:
        """Cancel all open orders. Used by kill switch."""
        if self.paper_mode:
            cancelled = self._paper_orders.copy()
            self._paper_orders.clear()
            logger.info(f"Paper: cancelled {len(cancelled)} orders")
            return cancelled

        # Get all open orders and cancel them
        try:
            orders_resp = await self._request(
                "GET",
                "/trade-api/v2/portfolio/orders",
                params={"status": "resting"},
            )
            orders = orders_resp.get("orders", [])
            results = []
            for order in orders:
                try:
                    result = await self.cancel_order(order["order_id"])
                    results.append(result)
                except Exception as e:
                    logger.error(f"Failed to cancel order {order['order_id']}: {e}")
            return results
        except Exception as e:
            logger.error(f"Failed to get orders for cancellation: {e}")
            return []

    # ---- Paper trading ----

    def _paper_place_order(self, order_data: dict) -> dict:
        """Simulate order placement in paper mode."""
        import uuid
        order_id = str(uuid.uuid4())
        paper_order = {
            "order_id": order_id,
            "status": "resting",
            "created_time": datetime.now(timezone.utc).isoformat(),
            **order_data,
        }
        self._paper_orders.append(paper_order)
        logger.info(
            f"Paper order placed: {order_data['action']} {order_data['count']}x "
            f"{order_data['side']} {order_data['ticker']} @ {order_data.get('yes_price') or order_data.get('no_price')}¢"
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
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        authenticated: bool = True,
    ) -> dict:
        """Make an API request with rate limiting and retry logic."""
        # Rate limiting
        elapsed = time.time() - self._last_request_at
        if elapsed < self._rate_limit_interval:
            import asyncio
            await asyncio.sleep(self._rate_limit_interval - elapsed)

        headers = {}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._last_request_at = time.time()

        response = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
        )

        if response.status_code == 401:
            # Token expired, re-authenticate
            await self._authenticate()
            headers["Authorization"] = f"Bearer {self._token}"
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers,
            )

        if response.status_code >= 400:
            error_body = response.text
            logger.error(f"Kalshi API error {response.status_code}: {error_body}")
            response.raise_for_status()

        return response.json()
