import os
import base64
import asyncio
import httpx
import logging
import base58
from typing import Optional, Dict, Any
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

logger = logging.getLogger(__name__)

class SolanaConnector:
    def __init__(self, private_key: str, rpc_url: str, jupiter_url: str, paper_mode: bool = True):
        self.rpc_url = rpc_url
        self.jupiter_url = jupiter_url
        self.paper_mode = paper_mode
        self.client = AsyncClient(rpc_url)
        
        # Parse private key
        try:
            secret = base58.b58decode(private_key)
            self.keypair = Keypair.from_bytes(secret)
            self.pubkey = str(self.keypair.pubkey())
        except Exception as e:
            logger.error(f"Failed to load Solana keypair: {e}")
            raise
            
        self.session = httpx.AsyncClient(timeout=30.0)
        
    async def get_balance(self) -> float:
        """Get SOL balance."""
        if self.paper_mode:
            return 10.0
        try:
            res = await self.client.get_balance(self.keypair.pubkey())
            return res.value / 1e9
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0

    async def get_quote(self, input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int = 1000) -> Optional[Dict]:
        """Get swap quote from Jupiter. Default slippage is 10% (1000 bps)"""
        url = f"{self.jupiter_url}/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "true" # Safer for memes
        }
        try:
            resp = await self.session.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Jupiter quote failed: {resp.text}")
        except Exception as e:
            logger.error(f"Quote error: {e}")
        return None

    async def execute_swap(self, quote_response: Dict) -> Optional[str]:
        """Execute the swap using Jupiter quote."""
        if self.paper_mode:
            import uuid
            tx_sig = f"simulated_solana_tx_{uuid.uuid4().hex}"
            logger.info(f"[SolanaConnector - Paper] Simulated swap execution: {tx_sig}")
            return tx_sig
            
        url = f"{self.jupiter_url}/swap"
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto"
        }
        
        try:
            # 1. Get transaction from Jupiter
            resp = await self.session.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Failed to get swap tx: {resp.text}")
                return None
                
            data = resp.json()
            swap_tx = data.get("swapTransaction")
            if not swap_tx:
                return None
                
            # 2. Deserialize
            raw_tx = base64.b64decode(swap_tx)
            tx = VersionedTransaction.from_bytes(raw_tx)
            
            # 3. Sign
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            
            # 4. Broadcast
            logger.info("Broadcasting swap transaction...")
            result = await self.client.send_raw_transaction(
                bytes(signed_tx)
            )
            
            tx_sig = result.value
            logger.info(f"Swap executed! Signature: {tx_sig}")
            return str(tx_sig)
            
        except Exception as e:
            logger.error(f"Swap execution failed: {e}")
            return None
            
    async def close(self):
        await self.session.aclose()
        await self.client.close()
