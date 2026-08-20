"""
SQLite Database wrapper for logging trades and persisting state.
Uses WAL mode for high concurrency.
"""

import sqlite3
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)

class TradeLogger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        # Create directory if it doesn't exist
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        logger.info(f"Connected to database at {self.db_path}")

    def _init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                market TEXT,
                symbol TEXT,
                side TEXT,
                entry_price REAL,
                quantity REAL,
                exit_price REAL,
                realized_pnl REAL,
                opened_at REAL,
                closed_at REAL,
                agent_id TEXT,
                strategy TEXT,
                metadata TEXT
            )
        ''')
        self.conn.commit()

    def log_trade(self, trade_data: dict):
        if not self.conn:
            return
            
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO trades 
            (id, market, symbol, side, entry_price, quantity, exit_price, realized_pnl, opened_at, closed_at, agent_id, strategy, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade_data.get('id'),
            trade_data.get('market'),
            trade_data.get('symbol'),
            trade_data.get('side'),
            trade_data.get('entry_price', 0),
            trade_data.get('quantity', 0),
            trade_data.get('exit_price', 0),
            trade_data.get('realized_pnl', 0),
            trade_data.get('opened_at', 0),
            trade_data.get('closed_at', 0),
            trade_data.get('agent_id'),
            trade_data.get('strategy'),
            json.dumps(trade_data.get('metadata', {}))
        ))
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed.")
