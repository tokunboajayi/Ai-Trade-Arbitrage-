"""
AI Hedge Fund - Main Entry Point

This script starts the entire system:
1. Loads environment variables
2. Initializes the Orchestrator
3. Starts the main event loop
"""

import asyncio
import logging
import sys

from src.core.orchestrator import Orchestrator

# Basic logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("main")


async def main():
    logger.info("Booting up AI Hedge Fund...")
    
    orchestrator = Orchestrator()
    
    try:
        # Start background tasks (including Telegram polling)
        await orchestrator.start()
        
        # Keep the main process alive
        while orchestrator._running:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}")
    finally:
        await orchestrator.stop()
        logger.info("System gracefully terminated.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
