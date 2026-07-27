# coding=utf-8
"""allocator.py — 资金分配策略"""

from typing import Dict, List
from .models import StockHolding
from .config import RISK_WEIGHTS, SECTOR_LIMITS


class EqualWeightAllocator:
    @staticmethod
    def allocate(active_amount: float, holdings: List[StockHolding]) -> Dict[str, float]:
        n = len(holdings)
        if n == 0:
            return {}
        per_stock = active_amount / n
        return {h.stock_code: per_stock for h in holdings}


class RiskWeightedAllocator:
    @staticmethod
    def allocate(active_amount: float, holdings: List[StockHolding]) -> Dict[str, float]:
        total_weight = sum(RISK_WEIGHTS.get(h.risk_level, 1.0) for h in holdings)
        if total_weight <= 0:
            return EqualWeightAllocator.allocate(active_amount, holdings)
        result = {}
        for h in holdings:
            w = RISK_WEIGHTS.get(h.risk_level, 1.0)
            result[h.stock_code] = active_amount * w / total_weight
        return result


class SectorConstrainedAllocator:
    @staticmethod
    def allocate(active_amount: float, holdings: List[StockHolding]) -> Dict[str, float]:
        base = EqualWeightAllocator.allocate(active_amount, holdings)
        sector_totals: Dict[str, float] = {}
        for h in holdings:
            sec = h.sector or "默认"
            limit = SECTOR_LIMITS.get(sec, SECTOR_LIMITS.get("默认", 0.25))
            sector_totals[sec] = sector_totals.get(sec, 0) + base.get(h.stock_code, 0)
        for sec, amt in sector_totals.items():
            limit = SECTOR_LIMITS.get(sec, 0.25)
            max_amt = active_amount * limit
            if amt > max_amt:
                scale = max_amt / amt
                for h in holdings:
                    if (h.sector or "默认") == sec:
                        base[h.stock_code] *= scale
        return base


def get_allocator(strategy: str = "equal_weight"):
    return {
        "equal_weight": EqualWeightAllocator(),
        "risk_weighted": RiskWeightedAllocator(),
        "sector_constrained": SectorConstrainedAllocator(),
    }.get(strategy, EqualWeightAllocator())
