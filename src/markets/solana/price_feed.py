"""
Live SOL Price Feed — Cached CoinGecko price fetcher.

Fetches SOL/USD price from CoinGecko's free API with a 30-second cache
to avoid rate limits. Falls back to a static price if the API is unreachable.
"""

import time
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Module-level cache
_cached_price: float = 150.0
_cached_at: float = 0.0
_cache_ttl: float = 30.0  # seconds

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
FALLBACK_PRICE = 150.0


async def get_sol_price_usd() -> float:
    """
    Get the current SOL/USD price with caching.
    
    Returns cached value if fresh (< 30s old).
    Falls back to FALLBACK_PRICE if CoinGecko is unreachable.
    """
    global _cached_price, _cached_at

    now = time.time()
    if now - _cached_at < _cache_ttl:
        return _cached_price

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                COINGECKO_URL,
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            if resp.status_code == 200:
                data = resp.json()
                price = data.get("solana", {}).get("usd")
                if price and price > 0:
                    _cached_price = float(price)
                    _cached_at = now
                    logger.debug(f"SOL price updated: ${_cached_price:.2f}")
                    return _cached_price

        logger.warning("CoinGecko returned unexpected data, using cached price")
    except Exception as e:
        logger.warning(f"CoinGecko price fetch failed: {e}. Using cached: ${_cached_price:.2f}")

    # Update timestamp even on failure to avoid hammering
    _cached_at = now
    return _cached_price
