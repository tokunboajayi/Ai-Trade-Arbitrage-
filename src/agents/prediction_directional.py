import asyncio
import time
import logging
from typing import Dict, List, Any

from src.agents.base_agent import BaseAgent
from src.core.state import PortfolioState


class PredictionDirectionalAgent(BaseAgent):
    """
    Agent that scans prediction markets (Kalshi & Polymarket) for directional trades.
    Strategy: High Conviction Trend Following. 
    Looks for markets where the probability (price) of an outcome is very high 
    (e.g., > 85%) but still offers a slight edge before resolving.
    """
    
    def __init__(self, config: dict, kalshi_connector, polymarket_connector):
        super().__init__(
            name="prediction_directional",
            version=1,
            config=config
        )
        self.kalshi = kalshi_connector
        self.polymarket = polymarket_connector
        self._logger = logging.getLogger(__name__)
        
        self.min_prob = 0.85
        self.max_prob = 0.95
        
        # Avoid spamming the same trades
        self._last_trade_time: Dict[str, float] = {}
        self.trade_cooldown = 3600  # 1 hour per symbol

    async def initialize(self, app_config, trade_logger) -> None:
        self._logger.info("PredictionDirectionalAgent initialized.")

    async def run_cycle(self, state: PortfolioState) -> List[dict]:
        proposals = []
        
        try:
            # 1. Scan Kalshi
            k_proposals = await self._scan_kalshi()
            proposals.extend(k_proposals)
            
            # 2. Scan Polymarket
            p_proposals = await self._scan_polymarket()
            proposals.extend(p_proposals)
            
        except Exception as e:
            self._logger.error(f"PredictionDirectionalAgent failed iteration: {e}")
            
        return proposals

    async def _scan_kalshi(self) -> List[dict]:
        proposals = []
        try:
            resp = await self.kalshi.get_events(status="open", limit=30)
            events = resp.get("events", [])
            for event in events:
                markets = event.get("markets", [])
                for market in markets:
                    ticker = market.get("ticker", "")
                    if not ticker:
                        continue
                        
                    # Respect cooldown
                    if time.time() - self._last_trade_time.get(ticker, 0) < self.trade_cooldown:
                        continue
                        
                    ob = await self.kalshi.get_market_orderbook(ticker)
                    yes_asks = ob.get("yes", [])
                    if yes_asks:
                        best_yes_cents = yes_asks[0][0]
                        best_yes_price = best_yes_cents / 100.0
                        
                        if self.min_prob <= best_yes_price <= self.max_prob:
                            proposals.append({
                                "action": "open_position",
                                "market": "kalshi",
                                "strategy": "directional",
                                "symbol": ticker,
                                "side": "yes",
                                "price": best_yes_price,
                                "quantity": 10, # Base quantity
                                "agent_id": self.id,
                                "metadata": {
                                    "title": event.get("title", ""),
                                    "conviction": best_yes_price
                                }
                            })
                            self._last_trade_time[ticker] = time.time()
        except Exception as e:
            self._logger.error(f"Failed to scan Kalshi: {e}")
            
        return proposals

    async def _scan_polymarket(self) -> List[dict]:
        proposals = []
        try:
            resp = await self.polymarket.get_markets(active=True, limit=30)
            markets = resp.get("data", []) if isinstance(resp, dict) else resp
            
            for market in markets:
                condition_id = market.get("condition_id", "")
                tokens = market.get("tokens", [])
                if not condition_id or len(tokens) < 2:
                    continue
                    
                yes_token = tokens[0].get("token_id", "")
                
                # Cooldown
                if time.time() - self._last_trade_time.get(yes_token, 0) < self.trade_cooldown:
                    continue
                    
                # We can't fetch generic orderbooks easily without passing token_id.
                # In demo mode, we just simulate checking the price for Poly since Gamma API price isn't live.
                # We'll skip deep Polymarket orderbook scans for this simplistic implementation
                # unless we specifically query the CLOB.
                pass
                
        except Exception as e:
            self._logger.error(f"Failed to scan Polymarket: {e}")
            
        return proposals

    async def terminate(self) -> None:
        self._logger.info("PredictionDirectionalAgent terminated.")
