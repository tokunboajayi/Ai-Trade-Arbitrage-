"""
Kill Switch — The single most important component in the system.

This is DETERMINISTIC CODE. No LLM, no AI, no ambiguity.
It runs on its own thread, checks every 5 seconds, and cannot be overridden
by any agent. It is the ONLY component that can override the orchestrator.

Rules:
1. If portfolio drops > max_drawdown_pct from peak → KILL ALL
2. If daily loss exceeds daily_loss_limit_pct → KILL ALL
3. If orchestrator heartbeat stops for > heartbeat_timeout_sec → KILL ALL
4. If manual kill is triggered (Telegram/dashboard) → KILL ALL
5. If any single position exceeds its hard stop → CLOSE THAT POSITION

KILL ALL means:
- Cancel all open orders on all platforms
- Close all open positions at market price
- Halt all agents
- Send emergency alert
- Log everything
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from src.core.config import RiskConfig

logger = logging.getLogger(__name__)


class KillAction(str, Enum):
    CONTINUE = "continue"
    KILL_ALL = "kill_all"
    CLOSE_POSITION = "close_position"


@dataclass
class KillSignal:
    """Signal emitted when kill switch triggers."""
    action: KillAction
    reason: str
    timestamp: float = field(default_factory=time.time)
    position_id: Optional[str] = None  # Only for CLOSE_POSITION


class KillSwitch:
    """
    Deterministic kill switch. No LLM. No AI. Pure logic.
    
    This class monitors portfolio state and triggers emergency actions
    when risk limits are breached. It runs independently of the main
    orchestrator loop on its own thread.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._manual_kill = False
        self._manual_kill_reason = ""
        self._enabled = True
        self._check_interval = 5.0  # seconds
        self._callbacks: list[Callable] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Tracking
        self.checks_performed = 0
        self.kills_triggered = 0
        self.last_check_at = 0.0
        self.signals: list[KillSignal] = []

    # ---- Public API ----

    def register_callback(self, callback: Callable[[KillSignal], None]) -> None:
        """Register a function to call when kill switch triggers.
        
        The orchestrator and each market connector register their
        emergency shutdown functions here.
        """
        self._callbacks.append(callback)

    def trigger_manual_kill(self, reason: str = "Manual kill triggered") -> None:
        """Human-triggered emergency stop. Called from Telegram or dashboard."""
        with self._lock:
            self._manual_kill = True
            self._manual_kill_reason = reason
        logger.critical(f"MANUAL KILL TRIGGERED: {reason}")

    def reset_manual_kill(self) -> None:
        """Reset manual kill flag. Only used after human review."""
        with self._lock:
            self._manual_kill = False
            self._manual_kill_reason = ""

    def enable(self) -> None:
        self._enabled = True
        logger.info("Kill switch ENABLED")

    def disable(self) -> None:
        """Disable kill switch. USE WITH EXTREME CAUTION."""
        self._enabled = False
        logger.warning("Kill switch DISABLED — system is unprotected")

    # ---- Core check logic ----

    def check(
        self,
        total_balance: float,
        peak_balance: float,
        today_start_balance: float,
        last_heartbeat: float,
    ) -> KillSignal:
        """
        Run all kill switch checks. Returns CONTINUE or KILL_ALL.
        
        This method is called every 5 seconds by the kill switch thread,
        AND at the start of every orchestrator loop iteration.
        """
        self.checks_performed += 1
        self.last_check_at = time.time()

        if not self._enabled:
            return KillSignal(action=KillAction.CONTINUE, reason="Kill switch disabled")

        # Check 1: Manual kill
        with self._lock:
            if self._manual_kill:
                signal = KillSignal(
                    action=KillAction.KILL_ALL,
                    reason=f"Manual kill: {self._manual_kill_reason}",
                )
                self._record_and_fire(signal)
                return signal

        # Check 2: Max drawdown from peak
        if peak_balance > 0:
            drawdown_pct = ((peak_balance - total_balance) / peak_balance) * 100
            if drawdown_pct > self.config.max_drawdown_pct:
                signal = KillSignal(
                    action=KillAction.KILL_ALL,
                    reason=(
                        f"Max drawdown exceeded: {drawdown_pct:.1f}% "
                        f"(limit: {self.config.max_drawdown_pct}%). "
                        f"Balance: ${total_balance:.4f}, Peak: ${peak_balance:.4f}"
                    ),
                )
                self._record_and_fire(signal)
                return signal

        # Check 3: Daily loss limit
        if today_start_balance > 0:
            daily_loss_pct = (
                (today_start_balance - total_balance) / today_start_balance
            ) * 100
            if daily_loss_pct > self.config.daily_loss_limit_pct:
                signal = KillSignal(
                    action=KillAction.KILL_ALL,
                    reason=(
                        f"Daily loss limit hit: {daily_loss_pct:.1f}% "
                        f"(limit: {self.config.daily_loss_limit_pct}%). "
                        f"Today start: ${today_start_balance:.4f}, "
                        f"Current: ${total_balance:.4f}"
                    ),
                )
                self._record_and_fire(signal)
                return signal

        # Check 4: Heartbeat timeout
        heartbeat_age = time.time() - last_heartbeat
        if heartbeat_age > self.config.heartbeat_timeout_sec:
            signal = KillSignal(
                action=KillAction.KILL_ALL,
                reason=(
                    f"Orchestrator heartbeat timeout: {heartbeat_age:.0f}s "
                    f"(limit: {self.config.heartbeat_timeout_sec}s)"
                ),
            )
            self._record_and_fire(signal)
            return signal

        # Check 5: Balance went to zero or negative
        if total_balance <= 0:
            signal = KillSignal(
                action=KillAction.KILL_ALL,
                reason=f"Balance is ${total_balance:.4f} — zero or negative",
            )
            self._record_and_fire(signal)
            return signal

        return KillSignal(action=KillAction.CONTINUE, reason="All checks passed")

    def check_position(
        self,
        entry_price: float,
        current_price: float,
        peak_price: float,
        hold_time_seconds: float,
        market: str,
    ) -> KillSignal:
        """
        Check if a single position should be force-closed.
        Called by the exit manager for each open position.
        """
        if entry_price <= 0:
            return KillSignal(action=KillAction.CONTINUE, reason="No entry price")

        # Hard stop-loss
        loss_pct = ((entry_price - current_price) / entry_price) * 100
        if loss_pct > self.config.meme_stop_loss_pct and market == "meme":
            return KillSignal(
                action=KillAction.CLOSE_POSITION,
                reason=f"Hard stop-loss: down {loss_pct:.1f}% (limit: {self.config.meme_stop_loss_pct}%)",
            )

        # Trailing stop (meme coins)
        if peak_price > 0 and market == "meme":
            drop_from_peak_pct = ((peak_price - current_price) / peak_price) * 100
            if drop_from_peak_pct > self.config.meme_trailing_stop_pct:
                return KillSignal(
                    action=KillAction.CLOSE_POSITION,
                    reason=(
                        f"Trailing stop: {drop_from_peak_pct:.1f}% from peak "
                        f"(limit: {self.config.meme_trailing_stop_pct}%)"
                    ),
                )

        # Time-based exit (meme coins)
        max_hold_sec = self.config.max_meme_hold_hours * 3600
        if hold_time_seconds > max_hold_sec and market == "meme":
            return KillSignal(
                action=KillAction.CLOSE_POSITION,
                reason=(
                    f"Max hold time: {hold_time_seconds / 3600:.1f}h "
                    f"(limit: {self.config.max_meme_hold_hours}h)"
                ),
            )

        return KillSignal(action=KillAction.CONTINUE, reason="Position OK")

    # ---- Background monitoring thread ----

    def start_monitoring(self, state_getter: Callable) -> None:
        """
        Start the kill switch monitoring thread.
        
        state_getter: a callable that returns (total_balance, peak_balance,
                       today_start_balance, last_heartbeat)
        """
        self._running = True
        self._state_getter = state_getter
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="KillSwitchThread",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Kill switch monitoring started (interval: {self._check_interval}s)"
        )

    def stop_monitoring(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Kill switch monitoring stopped")

    def _monitor_loop(self) -> None:
        """Background loop that checks portfolio state every N seconds."""
        while self._running:
            try:
                state = self._state_getter()
                signal = self.check(
                    total_balance=state[0],
                    peak_balance=state[1],
                    today_start_balance=state[2],
                    last_heartbeat=state[3],
                )
                if signal.action == KillAction.KILL_ALL:
                    logger.critical(f"KILL SWITCH TRIGGERED: {signal.reason}")
                    # Don't break — callbacks handle the shutdown
            except Exception as e:
                logger.error(f"Kill switch check error: {e}")
                # If we can't even check, that's dangerous — trigger kill
                signal = KillSignal(
                    action=KillAction.KILL_ALL,
                    reason=f"Kill switch self-check failed: {e}",
                )
                self._record_and_fire(signal)

            time.sleep(self._check_interval)

    # ---- Internal ----

    def _record_and_fire(self, signal: KillSignal) -> None:
        """Record the signal and fire all registered callbacks."""
        self.signals.append(signal)
        self.kills_triggered += 1
        logger.critical(f"KILL SIGNAL: {signal.action.value} — {signal.reason}")

        for callback in self._callbacks:
            try:
                callback(signal)
            except Exception as e:
                logger.error(f"Kill switch callback failed: {e}")

    def get_status(self) -> dict:
        """Return current kill switch status for dashboard/monitoring."""
        return {
            "enabled": self._enabled,
            "manual_kill_active": self._manual_kill,
            "checks_performed": self.checks_performed,
            "kills_triggered": self.kills_triggered,
            "last_check_at": self.last_check_at,
            "monitoring_active": self._running,
            "recent_signals": [
                {
                    "action": s.action.value,
                    "reason": s.reason,
                    "timestamp": s.timestamp,
                }
                for s in self.signals[-10:]  # Last 10 signals
            ],
        }
