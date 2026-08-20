"""
Telegram Bot Integration

This module handles sending trade alerts and receiving critical commands
(like the Kill Switch) directly via Telegram.
It uses standard long-polling so no webhook server is required.
"""

import asyncio
import logging
from typing import Optional, Callable

import aiohttp

from src.core.config import TelegramConfig
from src.core.kill_switch import KillSwitch
from src.core.state import PortfolioState

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: TelegramConfig, kill_switch: KillSwitch,
                 state: Optional[PortfolioState] = None):
        self.config = config
        self.kill_switch = kill_switch
        self.state = state
        self.api_url = f"https://api.telegram.org/bot{self.config.bot_token}"
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._last_update_id = 0
        
        # We only accept commands from the exact chat ID defined in config
        self.allowed_chat_id = str(self.config.chat_id)

    def set_state(self, state: PortfolioState) -> None:
        """Set or update the portfolio state reference."""
        self.state = state

    async def start(self) -> None:
        """Start the background task to listen for commands."""
        if not self.config.enabled:
            logger.info("Telegram bot is disabled in config.")
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_updates())
        
        await self.send_message("🟢 <b>AI Hedge Fund Orchestrator Started</b>\nAll systems online. Monitoring markets...", parse_mode="HTML")
        logger.info("Telegram notifier started.")

    async def stop(self) -> None:
        """Stop polling and send a shutdown message."""
        if not self._running:
            return
            
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
                
        if self.config.enabled:
            await self.send_message("🔴 <b>AI Hedge Fund Offline</b>\nOrchestrator has shut down.", parse_mode="HTML")
        logger.info("Telegram notifier stopped.")

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the owner."""
        if not self.config.enabled:
            return False

        payload = {
            "chat_id": self.allowed_chat_id,
            "text": text,
            "parse_mode": parse_mode
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.api_url}/sendMessage", json=payload) as response:
                    if response.status != 200:
                        error_data = await response.text()
                        logger.error(f"Failed to send Telegram message: {error_data}")
                        return False
                    return True
        except Exception as e:
            logger.error(f"Telegram network error: {e}")
            return False

    async def _poll_updates(self) -> None:
        """Continuously poll Telegram for new commands (Long Polling)."""
        while self._running:
            try:
                # Long polling: timeout=30 means the request hangs for 30s until a message arrives
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message"]
                }
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{self.api_url}/getUpdates", params=params, timeout=35) as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            for update in data.get("result", []):
                                self._last_update_id = update["update_id"]
                                
                                if "message" in update and "text" in update["message"]:
                                    await self._handle_command(update["message"])
                        else:
                            await asyncio.sleep(5) # Backoff on error
                            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error polling Telegram: {e}")
                await asyncio.sleep(5)

    async def _handle_command(self, message: dict) -> None:
        """Process incoming text messages."""
        chat_id = str(message.get("chat", {}).get("id"))
        text = message.get("text", "").strip()

        # SECURITY: Ignore messages from anyone else
        if chat_id != self.allowed_chat_id:
            logger.warning(f"Unauthorized Telegram access attempt from chat_id {chat_id}: {text}")
            return

        logger.info(f"Received Telegram command: {text}")

        if text == "/kill":
            await self.send_message("🛑 <b>KILL COMMAND RECEIVED</b>\nTriggering hard stop. Liquidating positions if possible...")
            self.kill_switch.trigger_manual_kill("Telegram manual kill command")
            
        elif text == "/status":
            await self._send_status()
            
        elif text == "/pause":
            await self.send_message("⏸️ <b>Pause command received</b>. (Feature pending implementation).")
            
        elif text == "/help" or text == "/start":
            msg = (
                "🤖 <b>AI Hedge Fund Control Panel</b>\n\n"
                "Available Commands:\n"
                "<code>/status</code> - Check system health and PnL\n"
                "<code>/pause</code> - Stop new trades, maintain current positions\n"
                "<code>/kill</code> - Emergency halt all trading immediately"
            )
            await self.send_message(msg)
        else:
            await self.send_message("Unknown command. Type <code>/help</code> for options.")

    async def _send_status(self) -> None:
        """Send an enriched status message with live portfolio data."""
        if not self.state:
            await self.send_message("📊 <b>System Status</b>\nNo state data available yet.")
            return

        s = self.state
        kill_state = "🔴 TRIGGERED" if self.kill_switch._manual_kill else "🟢 ACTIVE"
        
        # Build open positions summary
        pos_lines = []
        for pos in s.open_positions[:5]:  # Show max 5
            roi = pos.pnl_pct
            emoji = "📈" if roi >= 0 else "📉"
            hold_min = pos.hold_time_seconds / 60
            pos_lines.append(
                f"  {emoji} <code>{pos.symbol[:8]}...</code> "
                f"ROI: {roi:+.1f}% | {hold_min:.0f}m"
            )
        
        positions_str = "\n".join(pos_lines) if pos_lines else "  None"
        
        msg = (
            f"📊 <b>Portfolio Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: <b>${s.total_balance:.2f}</b>\n"
            f"📈 Peak: ${s.peak_balance:.2f}\n"
            f"📉 Drawdown: {s.drawdown_from_peak_pct:.1f}%\n"
            f"📅 Today PnL: <b>${s.today_pnl:+.2f}</b> ({s.today_pnl_pct:+.1f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 <b>Desk Balances</b>\n"
            f"  Kalshi: ${s.kalshi_balance:.2f}\n"
            f"  Polymarket: ${s.polymarket_balance:.2f}\n"
            f"  Meme: ${s.meme_balance:.2f}\n"
            f"  Reserve: ${s.reserve_balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Performance</b>\n"
            f"  Trades: {s.total_trades} | "
            f"Win Rate: {s.win_rate * 100:.0f}%\n"
            f"  Realized PnL: ${s.total_realized_pnl:+.4f}\n"
            f"  Phase: <b>{s.current_phase.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Open Positions ({len(s.open_positions)})</b>\n"
            f"{positions_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ Kill Switch: {kill_state}\n"
        )
        await self.send_message(msg)
