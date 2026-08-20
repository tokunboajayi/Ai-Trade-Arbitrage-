"""
The AI Hedge Fund Orchestrator.

This is the main brain of the system. It:
1. Initializes all core components (Config, Safety, Risk, Allocation, DB).
2. Spawns and manages market connectors.
3. Manages the lifecycle of Trading Agents.
4. Runs the central event loop, ensuring the KillSwitch heartbeat is ticked.
5. Starts the live web dashboard.
"""

import os
import asyncio
import logging
from typing import Dict, List, Optional

from src.core.config import AppConfig, load_config, Environment, Market
from src.core.kill_switch import KillSwitch, KillAction
from src.agents.risk_manager import RiskManager
from src.core.capital_allocator import CapitalAllocator
from src.learning.trade_logger import TradeLogger
from src.agents.base_agent import BaseAgent
from src.core.state import PortfolioState, Position, Side
from src.markets.solana.price_feed import get_sol_price_usd

# Connectors
from src.markets.kalshi.connector import KalshiConnector
from src.markets.polymarket.connector import PolymarketConnector

# Notifications
from src.notifications.telegram_bot import TelegramNotifier

# Dashboard
from src.dashboard.server import DashboardServer

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self):
        # 1. Load Configuration
        self.config = load_config()
        self.paper_mode = self.config.environment == Environment.PAPER
        
        # 2. Initialize Database Logging
        self.trade_logger = TradeLogger(self.config.data_dir)
        
        # 3. State Management
        self.state = PortfolioState()
        
        self.kill_switch = KillSwitch(self.config.risk)
        
        self.risk_manager = RiskManager(self.config.risk)
        self.capital_allocator = CapitalAllocator(self.config.allocation)
        
        # 4. Notifications (pass state reference for enriched /status)
        self.telegram = TelegramNotifier(self.config.telegram, self.kill_switch, state=self.state)
        
        # 5. Market Connectors
        self.kalshi = KalshiConnector(self.config.kalshi, paper_mode=self.paper_mode)
        self.polymarket = PolymarketConnector(self.config.polymarket, paper_mode=True)
        
        # Initialize Solana Connector (supports paper mode)
        from src.markets.solana.connector import SolanaConnector
        self.solana = SolanaConnector(
            private_key=os.getenv("SOLANA_PRIVATE_KEY"),
            rpc_url=os.getenv("SOLANA_RPC_URL"),
            jupiter_url=os.getenv("JUPITER_API_URL"),
            paper_mode=self.paper_mode
        )
        
        # 6. Agent Management
        from src.markets.cross_arb import CrossArbFinder
        from src.agents.meme_sniper import MemeSniper
        from src.agents.prediction_directional import PredictionDirectionalAgent
        
        self.agents: List[BaseAgent] = [
            CrossArbFinder(
                config={},
                kalshi_connector=self.kalshi,
                polymarket_connector=self.polymarket,
            ),
            MemeSniper(connector=self.solana, config={}),
            PredictionDirectionalAgent(
                config={},
                kalshi_connector=self.kalshi,
                polymarket_connector=self.polymarket
            )
        ]
        
        # 7. Dashboard
        self.dashboard = DashboardServer(
            db_path=self.config.data_dir / "trades.db",
            state=self.state,
            port=8080,
        )
        
        self._running = False
        self._main_task: Optional[asyncio.Task] = None

        # Track current SOL price for use across the loop
        self._sol_price_usd: float = 150.0

    async def initialize(self) -> None:
        """Connect to markets and prepare the environment."""
        logger.info("Initializing AI Hedge Fund Orchestrator...")
        
        # Start DB
        await self.trade_logger.initialize()
        
        # Start Notifications
        await self.telegram.start()
        
        # Connect to markets
        await asyncio.gather(
            self.kalshi.connect(),
            self.polymarket.connect()
        )
        
        # Fetch live SOL price
        self._sol_price_usd = await get_sol_price_usd()
        logger.info(f"SOL price: ${self._sol_price_usd:.2f}")
        
        # Fetch initial balances and populate state
        try:
            kalshi_bal_resp = await self.kalshi.get_balance()
            kalshi_val = kalshi_bal_resp.get("balance", 0) / 100.0
            
            poly_bal_resp = await self.polymarket.get_balance()
            poly_val = poly_bal_resp.get("balance", 0.0)
            
            sol_bal = await self.solana.get_balance()
            sol_val = sol_bal * self._sol_price_usd
            
            self.state.kalshi_balance = kalshi_val
            self.state.polymarket_balance = poly_val
            self.state.meme_balance = sol_val
            self.state.total_balance = kalshi_val + poly_val + sol_val
            self.state.peak_balance = self.state.total_balance
            self.state.today_start_balance = self.state.total_balance
            
            logger.info(f"Balances loaded: Kalshi=${kalshi_val:.2f}, Polymarket=${poly_val:.2f}, Solana=${sol_val:.2f} (Total: ${self.state.total_balance:.2f})")
        except Exception as e:
            logger.error(f"Failed to fetch initial balances: {e}")
            
        # Initialize allocator and risk manager
        await self.capital_allocator.initialize(self.config, self.trade_logger)
        await self.risk_manager.initialize(self.config, self.trade_logger)
        
        # Perform initial rebalance
        await self.capital_allocator.rebalance(self.state)
        
        # Start KillSwitch monitoring thread
        self.kill_switch.register_callback(self._on_kill_signal)
        self.kill_switch.start_monitoring(self._get_kill_switch_state)
        
        # Start all registered agents
        for agent in self.agents:
            await agent.start(self.config, self.trade_logger)
            
        # Start Dashboard
        try:
            await self.dashboard.start()
        except Exception as e:
            logger.error(f"Dashboard failed to start: {e}")
            
        logger.info("Market connectors and agents initialized.")

    async def shutdown(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Shutting down Orchestrator...")
        self._running = False
        
        # Stop KillSwitch monitoring
        self.kill_switch.stop_monitoring()
        
        # Cancel all active agents
        for agent in self.agents:
            await agent.stop()
            
        # Cancel all open orders in paper or live (if supported)
        await asyncio.gather(
            self.kalshi.cancel_all_orders(),
            self.polymarket.cancel_all_orders(),
            return_exceptions=True
        )
        
        # Disconnect markets
        await asyncio.gather(
            self.kalshi.disconnect(),
            self.polymarket.disconnect(),
            self.solana.close(),
            return_exceptions=True
        )
        
        # Stop Dashboard
        await self.dashboard.stop()
        
        # Stop Notifications
        await self.telegram.stop()
        
        # Close DB
        await self.trade_logger.close()
        logger.info("Shutdown complete.")

    def _get_kill_switch_state(self):
        """Return state tuple for the kill switch monitoring thread."""
        return (
            self.state.total_balance,
            self.state.peak_balance,
            self.state.today_start_balance,
            self.state.last_heartbeat_at,
        )

    def _on_kill_signal(self, signal):
        """Callback fired by the kill switch thread when it triggers."""
        logger.critical(f"Kill switch callback fired: {signal.reason}")
        self.state.kill_switch_triggered = True
        self.state.kill_switch_reason = signal.reason

    async def _run_loop(self) -> None:
        """The main continuous loop."""
        self._running = True
        logger.info("Starting Orchestrator main loop.")
        
        try:
            while self._running:
                # 1. Update Portfolio Heartbeat
                self.state.record_heartbeat()
                
                # 2. Run KillSwitch checks (deterministic, no LLM)
                kill_signal = self.kill_switch.check(
                    total_balance=self.state.total_balance,
                    peak_balance=self.state.peak_balance,
                    today_start_balance=self.state.today_start_balance,
                    last_heartbeat=self.state.last_heartbeat_at,
                )
                if kill_signal.action == KillAction.KILL_ALL:
                    logger.critical(f"KILL SWITCH TRIGGERED: {kill_signal.reason}")
                    self.state.kill_switch_triggered = True
                    self.state.kill_switch_reason = kill_signal.reason
                    await self.telegram.send_message(
                        f"🛑 <b>KILL SWITCH TRIGGERED</b>\n"
                        f"Reason: {kill_signal.reason}\n"
                        f"Shutting down all operations.",
                        parse_mode="HTML"
                    )
                    await self.shutdown()
                    break

                # 2b. Check individual meme positions via kill switch
                for pos in list(self.state.open_positions):
                    if pos.market == Market.MEME or (hasattr(pos.market, 'value') and pos.market.value == "meme"):
                        pos_signal = self.kill_switch.check_position(
                            entry_price=pos.entry_price,
                            current_price=pos.current_price,
                            peak_price=pos.current_price,  # Use current as peak for now
                            hold_time_seconds=pos.hold_time_seconds,
                            market="meme",
                        )
                        if pos_signal.action == KillAction.CLOSE_POSITION:
                            logger.warning(f"KillSwitch forcing close on {pos.symbol}: {pos_signal.reason}")

                # 3. Update SOL price
                self._sol_price_usd = await get_sol_price_usd()

                # 4. Update Capital Allocations
                try:
                    await self.capital_allocator.run_cycle(self.state)
                except Exception as e:
                    logger.error(f"Capital allocator crashed: {e}")

                # 5. Run Active Agents
                for agent in self.agents:
                    if not agent.is_active:
                        logger.warning(f"Agent {agent.name} is inactive.")
                        continue
                    
                    try:
                        proposals = await agent.run_cycle(self.state) 
                        
                        for proposal in proposals:
                            approved, reason, adjusted = self.risk_manager.evaluate_trade(self.state, proposal)
                            if approved:
                                logger.info(f"Trade approved: {adjusted}")
                                await self.execute_trade(agent, adjusted)
                            else:
                                logger.info(f"Trade rejected by Risk Manager: {reason}")
                    except Exception as e:
                        logger.error(f"Agent {agent.name} crashed during cycle: {e}")

                # 6. Log portfolio snapshot
                try:
                    await self.trade_logger.log_portfolio_snapshot(
                        total_balance=self.state.total_balance,
                        peak_balance=self.state.peak_balance,
                        drawdown_pct=self.state.drawdown_from_peak_pct,
                        kalshi_balance=self.state.kalshi_balance,
                        polymarket_balance=self.state.polymarket_balance,
                        meme_balance=self.state.meme_balance,
                        reserve_balance=self.state.reserve_balance,
                        open_positions=len(self.state.open_positions),
                        total_trades=self.state.total_trades,
                        win_rate=self.state.win_rate,
                        total_pnl=self.state.total_realized_pnl,
                        phase=self.state.current_phase
                    )
                except Exception as e:
                    logger.error(f"Failed to log portfolio snapshot: {e}")

                # Sleep until next loop iteration
                await asyncio.sleep(self.config.main_loop_interval_sec)
                
        except asyncio.CancelledError:
            logger.info("Orchestrator loop cancelled.")
        except Exception as e:
            logger.error(f"Orchestrator main loop crashed: {e}")
            self.kill_switch.trigger_manual_kill(f"Orchestrator crash: {e}")
            await self.shutdown()
        finally:
            self._running = False

    async def execute_trade(self, agent: BaseAgent, proposal: dict) -> None:
        """Route trade proposal to proper connector and execute it."""
        action = proposal.get("action")
        market = proposal.get("market")
        symbol = proposal.get("symbol")
        side = proposal.get("side")
        price = proposal.get("price", 0.0)
        quantity = proposal.get("quantity", 0.0)
        
        logger.info(f"Executing trade: {action} {side} {quantity} {symbol} on {market}")
        
        import time
        if market == "meme":
            if action == "open_position":
                quote = proposal.get("metadata", {}).get("quote")
                if not quote:
                    logger.error("Cannot execute Solana trade: missing quote metadata.")
                    return
                    
                tx_sig = await self.solana.execute_swap(quote)
                if tx_sig:
                    # Log trade in DB and get trade_id
                    trade_id = await self.trade_logger.log_trade_opened(
                        market="meme",
                        strategy=proposal.get("strategy", "snipe"),
                        symbol=symbol,
                        side="buy",
                        entry_price=price,
                        quantity=quantity,
                        agent_id=agent.id,
                        agent_name=agent.name,
                        agent_version=agent.version,
                        signal_data=proposal.get("metadata"),
                    )
                    
                    # Create Position with trade_id as position.id
                    pos = Position(
                        id=trade_id,
                        market=Market.MEME,
                        symbol=symbol,
                        side=Side.BUY,
                        entry_price=price,
                        quantity=quantity,
                        current_price=price,
                        opened_at=time.time(),
                        agent_id=agent.id,
                        strategy=proposal.get("strategy", "snipe"),
                        metadata={"quote": quote, "tx_sig": tx_sig}
                    )
                    self.state.add_position(pos)
                    
                    # Calculate SOL spent and update state balances
                    sol_spent = (int(quote.get("inAmount", 0)) / 1e9)
                    sol_spent_usd = sol_spent * self._sol_price_usd
                    self.state.meme_balance -= sol_spent_usd
                    self.state.total_balance = self.state.kalshi_balance + self.state.polymarket_balance + self.state.meme_balance
                    self.state.update_peak()
                    
                    # Send alert
                    await self.telegram.send_message(
                        f"🟢 <b>Solana Meme Position Opened</b>\n"
                        f"Token: <code>{symbol}</code>\n"
                        f"Amount: {quantity / 1e9:.2f} tokens\n"
                        f"Cost: {sol_spent:.4f} SOL (~${sol_spent_usd:.2f})\n"
                        f"Tx: <code>{tx_sig[:8]}...</code>",
                        parse_mode="HTML"
                    )
                    
            elif action == "close_position":
                quote = proposal.get("metadata", {}).get("quote")
                if not quote:
                    logger.error("Cannot execute Solana trade: missing quote metadata.")
                    return
                    
                tx_sig = await self.solana.execute_swap(quote)
                if tx_sig:
                    closed = self.state.close_position(proposal.get("position_id"), price)
                    if closed:
                        # Log closed trade in DB
                        await self.trade_logger.log_trade_closed(
                            trade_id=closed.id,
                            exit_price=price,
                            exit_reason=proposal.get("exit_reason", "take_profit"),
                        )
                        
                        # Add SOL received back to balances
                        sol_received = (int(quote.get("outAmount", 0)) / 1e9)
                        sol_received_usd = sol_received * self._sol_price_usd
                        self.state.meme_balance += sol_received_usd
                        
                        # Update total balance
                        self.state.total_balance = self.state.kalshi_balance + self.state.polymarket_balance + self.state.meme_balance
                        self.state.update_peak()
                        
                        # Record trade performance on agent
                        pnl = closed.realized_pnl
                        agent.record_trade(pnl)
                        
                        # Send alert
                        reason_str = proposal.get("exit_reason", "TP/SL").replace("_", " ").upper()
                        await self.telegram.send_message(
                            f"🔴 <b>Solana Meme Position Closed</b>\n"
                            f"Token: <code>{symbol}</code>\n"
                            f"Exit Reason: {reason_str}\n"
                            f"PnL: {closed.realized_pnl:+.4f} USD\n"
                            f"Tx: <code>{tx_sig[:8]}...</code>",
                            parse_mode="HTML"
                        )

        elif market == "arb":
            # Two-legged arbitrage execution
            direction = proposal.get("metadata", {}).get("direction", "")
            pair = proposal.get("metadata", {}).get("pair", {})
            contracts = proposal.get("quantity", 0)
            leg1_price = proposal.get("metadata", {}).get("leg1_price", 0)
            leg2_price = proposal.get("metadata", {}).get("leg2_price", 0)
            
            if contracts <= 0:
                return
            
            kalshi_ticker = pair.get("kalshi_ticker", "")
            
            try:
                if direction == "kalshi_yes_poly_no":
                    # Leg 1: Buy YES on Kalshi
                    kalshi_price_cents = int(leg1_price * 100)
                    await self.kalshi.place_order(
                        ticker=kalshi_ticker,
                        side="yes",
                        action="buy",
                        count=contracts,
                        price=kalshi_price_cents,
                    )
                    # Leg 2: Buy NO on Polymarket
                    poly_no_token = pair.get("poly_no_token", "")
                    if poly_no_token:
                        await self.polymarket.place_order(
                            token_id=poly_no_token,
                            side="BUY",
                            size=contracts,
                            price=leg2_price,
                        )
                    
                elif direction == "kalshi_no_poly_yes":
                    # Leg 1: Buy NO on Kalshi
                    kalshi_price_cents = int(leg1_price * 100)
                    await self.kalshi.place_order(
                        ticker=kalshi_ticker,
                        side="no",
                        action="buy",
                        count=contracts,
                        price=kalshi_price_cents,
                    )
                    # Leg 2: Buy YES on Polymarket
                    poly_yes_token = pair.get("poly_yes_token", "")
                    if poly_yes_token:
                        await self.polymarket.place_order(
                            token_id=poly_yes_token,
                            side="BUY",
                            size=contracts,
                            price=leg2_price,
                        )
                
                # Log arb trade
                total_cost = (leg1_price + leg2_price) * contracts
                trade_id = await self.trade_logger.log_trade_opened(
                    market="arb",
                    strategy="arb",
                    symbol=kalshi_ticker,
                    side="buy",
                    entry_price=leg1_price + leg2_price,
                    quantity=contracts,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    agent_version=agent.version,
                    signal_data=proposal.get("metadata"),
                )
                
                spread_pct = proposal.get("metadata", {}).get("spread_pct", 0)
                await self.telegram.send_message(
                    f"🎯 <b>Arbitrage Trade Executed</b>\n"
                    f"Event: <code>{pair.get('title', kalshi_ticker)[:50]}</code>\n"
                    f"Direction: {direction}\n"
                    f"Contracts: {contracts}\n"
                    f"Spread: {spread_pct:.1f}%\n"
                    f"Total Cost: ${total_cost:.2f}",
                    parse_mode="HTML"
                )
                
            except Exception as e:
                logger.error(f"Arb execution failed: {e}")

        elif market in ["kalshi", "polymarket"]:
            # Single-leg prediction market directional trade
            try:
                if market == "kalshi":
                    price_cents = int(price * 100)
                    await self.kalshi.place_order(
                        ticker=symbol,
                        side=side,
                        action="buy",
                        count=int(quantity),
                        price=price_cents,
                    )
                else:
                    await self.polymarket.place_order(
                        token_id=symbol,
                        side=side.upper(),
                        size=quantity,
                        price=price,
                    )
                
                total_cost = price * quantity
                
                # Log trade
                trade_id = await self.trade_logger.log_trade_opened(
                    market=market,
                    strategy="directional",
                    symbol=symbol,
                    side=side,
                    entry_price=price,
                    quantity=quantity,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    agent_version=agent.version,
                    signal_data=proposal.get("metadata"),
                )
                
                # Update simple state balances based on execution
                if market == "kalshi":
                    self.state.kalshi_balance -= total_cost
                else:
                    self.state.polymarket_balance -= total_cost
                self.state.total_balance = self.state.kalshi_balance + self.state.polymarket_balance + self.state.meme_balance
                self.state.update_peak()
                
                # Send alert
                m_title = proposal.get("metadata", {}).get("title", symbol)
                await self.telegram.send_message(
                    f"🧭 <b>Directional Trade Executed ({market.capitalize()})</b>\n"
                    f"Event: <code>{m_title[:60]}</code>\n"
                    f"Side: {side.upper()}\n"
                    f"Contracts: {quantity}\n"
                    f"Price: ${price:.2f}\n"
                    f"Cost: ${total_cost:.2f}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Directional {market} execution failed: {e}")

    async def start(self) -> None:
        """Initialize and start the main loop in the background."""
        await self.initialize()
        self._running = True
        self._main_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the main loop and shut down."""
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        await self.shutdown()
