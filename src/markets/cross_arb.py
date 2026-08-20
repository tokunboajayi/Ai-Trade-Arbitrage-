"""
Cross-Platform Arbitrage Finder Agent

Scans Kalshi and Polymarket for overlapping binary-outcome markets.
If an arbitrage opportunity is found (spread > config threshold),
it proposes a two-legged trade to the Risk Manager.

Matching logic:
  1. Fetch active events from both platforms.
  2. Fuzzy-match event titles to find overlapping markets.
  3. For each match, fetch order books and calculate arb spread.
  4. A binary arb exists when: best_ask_yes(A) + best_ask_no(B) < 1.0
     (buy YES on one, NO on the other for less than $1 total → guaranteed $1 payout).
"""

import asyncio
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Optional

from src.agents.base_agent import BaseAgent
from src.core.config import AppConfig, Market
from src.core.state import PortfolioState

logger = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Strip punctuation, lowercase, and remove common filler words for matching."""
    title = title.lower()
    title = re.sub(r"[^a-z0-9 ]", " ", title)
    # Remove common filler
    for word in ("will", "the", "by", "in", "on", "a", "an", "of", "be", "to"):
        title = re.sub(rf"\b{word}\b", "", title)
    return " ".join(title.split())


def _similarity(a: str, b: str) -> float:
    """Return 0-1 similarity score between two normalized strings."""
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


class CrossArbFinder(BaseAgent):
    """
    Arbitrage scanner between Kalshi and Polymarket.
    """

    def __init__(self, version: int = 1, config: Optional[dict] = None,
                 kalshi_connector=None, polymarket_connector=None, **kwargs):
        super().__init__(
            name="cross_arb_finder",
            version=version,
            config=config,
            **kwargs,
        )
        self.min_spread_pct: float = 4.0
        self.scan_interval_sec: float = 60.0
        self.match_threshold: float = 0.70  # Minimum title similarity to consider a match
        self.kalshi = kalshi_connector
        self.polymarket = polymarket_connector

        # Cache matched events so we don't re-scan every cycle
        self._matched_pairs: list[dict] = []
        self._last_match_scan: float = 0.0
        self._match_scan_interval: float = 300.0  # Re-scan matching every 5 min

    async def initialize(self, app_config: AppConfig, trade_logger: Any) -> None:
        self.min_spread_pct = app_config.risk.min_arb_spread_pct
        self._logger.info(f"CrossArbFinder initialized. Target spread: >{self.min_spread_pct}%")

    async def run_cycle(self, state: PortfolioState) -> list[dict[str, Any]]:
        """
        Executes one scan cycle:
        1. Refresh event matching if stale.
        2. For each matched pair, fetch order books.
        3. Calculate arb spread and emit proposals if profitable.
        """
        proposals = []

        if not self.kalshi or not self.polymarket:
            self._logger.debug("CrossArb: connectors not set, skipping.")
            return proposals

        # Step 1: Refresh matched event pairs periodically
        import time
        now = time.time()
        if now - self._last_match_scan > self._match_scan_interval:
            await self._refresh_matched_pairs()
            self._last_match_scan = now

        if not self._matched_pairs:
            self._logger.debug("CrossArb: no matched pairs found.")
            return proposals

        # Step 2: Scan each matched pair for arb
        for pair in self._matched_pairs:
            try:
                arb_proposal = await self._check_pair(pair, state)
                if arb_proposal:
                    proposals.append(arb_proposal)
            except Exception as e:
                self._logger.error(f"Error checking pair {pair.get('title', '?')}: {e}")

        return proposals

    async def _refresh_matched_pairs(self) -> None:
        """Fetch events from both platforms and fuzzy-match by title."""
        self._logger.info("CrossArb: Refreshing event matching...")
        self._matched_pairs = []

        try:
            # Fetch Kalshi events
            kalshi_resp = await self.kalshi.get_events(status="open", limit=100)
            kalshi_events = kalshi_resp.get("events", [])

            # Fetch Polymarket markets
            poly_markets = await self.polymarket.get_markets(active=True, limit=100)
            if not isinstance(poly_markets, list):
                poly_markets = poly_markets.get("data", []) if isinstance(poly_markets, dict) else []

        except Exception as e:
            self._logger.error(f"Failed to fetch events for matching: {e}")
            return

        # Build matching
        for k_event in kalshi_events:
            k_title = k_event.get("title", "")
            k_ticker = k_event.get("event_ticker", "")
            k_markets = k_event.get("markets", [])

            if not k_title or not k_markets:
                continue

            for p_market in poly_markets:
                p_title = p_market.get("question", p_market.get("title", ""))
                p_condition_id = p_market.get("condition_id", "")

                if not p_title:
                    continue

                sim = _similarity(k_title, p_title)
                if sim >= self.match_threshold:
                    # Pick the first Kalshi market ticker for this event
                    k_market_ticker = k_markets[0].get("ticker", "") if k_markets else ""

                    # Get Polymarket token IDs
                    p_tokens = p_market.get("tokens", [])
                    p_yes_token = ""
                    p_no_token = ""
                    for tok in p_tokens:
                        if tok.get("outcome", "").lower() == "yes":
                            p_yes_token = tok.get("token_id", "")
                        elif tok.get("outcome", "").lower() == "no":
                            p_no_token = tok.get("token_id", "")

                    pair = {
                        "title": k_title,
                        "similarity": sim,
                        "kalshi_ticker": k_market_ticker,
                        "kalshi_event_ticker": k_ticker,
                        "poly_condition_id": p_condition_id,
                        "poly_yes_token": p_yes_token,
                        "poly_no_token": p_no_token,
                    }
                    self._matched_pairs.append(pair)
                    self._logger.info(
                        f"CrossArb matched: \"{k_title}\" ↔ \"{p_title}\" "
                        f"(sim={sim:.2f})"
                    )

        self._logger.info(f"CrossArb: {len(self._matched_pairs)} matched pairs found.")

    async def _check_pair(self, pair: dict, state: PortfolioState) -> Optional[dict]:
        """
        Check a matched pair for arb opportunity.

        Binary arb logic:
        - If Kalshi YES ask + Polymarket NO ask < $1.00 → arb exists.
          Buy YES on Kalshi, buy NO on Polymarket.
          Guaranteed $1 payout minus costs.
        - Also check the reverse: Kalshi NO ask + Polymarket YES ask < $1.00.
        """
        k_ticker = pair.get("kalshi_ticker", "")
        p_yes_token = pair.get("poly_yes_token", "")
        p_no_token = pair.get("poly_no_token", "")

        if not k_ticker:
            return None

        try:
            # Fetch Kalshi order book
            k_book = await self.kalshi.get_orderbook(k_ticker, depth=1)
        except Exception as e:
            self._logger.debug(f"Failed to get Kalshi orderbook for {k_ticker}: {e}")
            return None

        # Parse Kalshi best ask (in cents)
        k_yes_asks = k_book.get("yes", k_book.get("orderbook", {}).get("yes", []))
        k_no_asks = k_book.get("no", k_book.get("orderbook", {}).get("no", []))

        k_yes_best_ask = None
        k_no_best_ask = None

        if k_yes_asks and isinstance(k_yes_asks, list) and len(k_yes_asks) > 0:
            # Asks are usually [[price, qty], ...] sorted ascending
            if isinstance(k_yes_asks[0], list):
                k_yes_best_ask = k_yes_asks[0][0] / 100.0  # cents → dollars
            elif isinstance(k_yes_asks[0], dict):
                k_yes_best_ask = k_yes_asks[0].get("price", 0) / 100.0

        if k_no_asks and isinstance(k_no_asks, list) and len(k_no_asks) > 0:
            if isinstance(k_no_asks[0], list):
                k_no_best_ask = k_no_asks[0][0] / 100.0
            elif isinstance(k_no_asks[0], dict):
                k_no_best_ask = k_no_asks[0].get("price", 0) / 100.0

        # Fetch Polymarket order books for YES and NO tokens
        p_yes_best_ask = None
        p_no_best_ask = None

        if p_yes_token:
            try:
                p_yes_book = await self.polymarket.get_orderbook(p_yes_token)
                p_asks = p_yes_book.get("asks", [])
                if p_asks and len(p_asks) > 0:
                    p_yes_best_ask = float(p_asks[0].get("price", 0))
            except Exception:
                pass

        if p_no_token:
            try:
                p_no_book = await self.polymarket.get_orderbook(p_no_token)
                p_asks = p_no_book.get("asks", [])
                if p_asks and len(p_asks) > 0:
                    p_no_best_ask = float(p_asks[0].get("price", 0))
            except Exception:
                pass

        # Check for arb: Kalshi YES + Poly NO < 1.0
        if k_yes_best_ask is not None and p_no_best_ask is not None:
            total_cost = k_yes_best_ask + p_no_best_ask
            spread_pct = (1.0 - total_cost) / 1.0 * 100 if total_cost < 1.0 else 0
            if spread_pct > self.min_spread_pct:
                self._logger.info(
                    f"🎯 ARB FOUND: \"{pair['title']}\" | "
                    f"Kalshi YES @{k_yes_best_ask:.2f} + Poly NO @{p_no_best_ask:.2f} "
                    f"= {total_cost:.2f} | Spread: {spread_pct:.1f}%"
                )
                return self._create_arb_proposal(
                    pair, "kalshi_yes_poly_no",
                    k_yes_best_ask, p_no_best_ask, spread_pct, state
                )

        # Check reverse: Kalshi NO + Poly YES < 1.0
        if k_no_best_ask is not None and p_yes_best_ask is not None:
            total_cost = k_no_best_ask + p_yes_best_ask
            spread_pct = (1.0 - total_cost) / 1.0 * 100 if total_cost < 1.0 else 0
            if spread_pct > self.min_spread_pct:
                self._logger.info(
                    f"🎯 ARB FOUND: \"{pair['title']}\" | "
                    f"Kalshi NO @{k_no_best_ask:.2f} + Poly YES @{p_yes_best_ask:.2f} "
                    f"= {total_cost:.2f} | Spread: {spread_pct:.1f}%"
                )
                return self._create_arb_proposal(
                    pair, "kalshi_no_poly_yes",
                    k_no_best_ask, p_yes_best_ask, spread_pct, state
                )

        return None

    def _create_arb_proposal(
        self, pair: dict, direction: str,
        leg1_price: float, leg2_price: float,
        spread_pct: float, state: PortfolioState
    ) -> dict:
        """Create a two-legged arb trade proposal."""
        # Size: use the smaller of kalshi or polymarket desk allocation
        desk_size = min(state.kalshi_balance, state.polymarket_balance)
        # Risk 10% of the smaller desk per arb
        trade_size_usd = desk_size * 0.10
        total_cost_per_contract = leg1_price + leg2_price
        contracts = int(trade_size_usd / total_cost_per_contract) if total_cost_per_contract > 0 else 0

        if contracts <= 0:
            return None

        return {
            "action": "open_position",
            "market": "arb",
            "strategy": "arb",
            "symbol": pair.get("kalshi_ticker", ""),
            "side": "buy",
            "price": total_cost_per_contract,
            "quantity": contracts,
            "estimated_edge_pct": spread_pct,
            "confidence": min(pair.get("similarity", 0.7), 0.95),
            "event_id": pair.get("kalshi_event_ticker", ""),
            "metadata": {
                "direction": direction,
                "pair": pair,
                "leg1_price": leg1_price,
                "leg2_price": leg2_price,
                "spread_pct": spread_pct,
            }
        }

    async def terminate(self) -> None:
        self._logger.info("CrossArbFinder terminated")
