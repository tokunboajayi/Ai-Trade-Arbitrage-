import httpx
import logging
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

class RugCheckAPI:
    def __init__(self):
        self.base_url = "https://api.rugcheck.xyz/v1/tokens"
        self.session = httpx.AsyncClient(timeout=10.0)
        
    async def get_report(self, mint: str) -> Dict[str, Any]:
        """Fetch token report from RugCheck."""
        try:
            resp = await self.session.get(f"{self.base_url}/{mint}/report/summary")
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception as e:
            logger.error(f"RugCheck failed for {mint}: {e}")
            return {}
            
    async def is_safe(self, mint: str) -> Tuple[bool, str]:
        """
        Determine if token is safe to trade.
        Returns (is_safe, reason)
        """
        report = await self.get_report(mint)
        if not report:
            return False, "Failed to get RugCheck report"
            
        risks = report.get("risks", [])
        score = report.get("score", 100000)
        
        # We want a very low score for safety.
        if score > 5000:
            return False, f"Risk score too high: {score}"
            
        # Check specific critical risks
        critical_risks = ["Mint Authority", "Freeze Authority", "High ownership"]
        
        for risk in risks:
            name = risk.get("name", "")
            level = risk.get("level", "info")
            if level == "danger":
                if any(c.lower() in name.lower() for c in critical_risks):
                    return False, f"Critical risk found: {name}"
                
        return True, "Token appears safe"
        
    async def close(self):
        await self.session.aclose()
