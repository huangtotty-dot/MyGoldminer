# coding=utf-8
"""validator.py — 约束校验"""

import math
from typing import Tuple, List
from .config import CASH_HARD_FLOOR


class ConstraintValidator:
    @staticmethod
    def validate_cash_floor(cash: float, total: float) -> bool:
        return cash >= total * CASH_HARD_FLOOR - 0.01

    @staticmethod
    def validate_sum(base: float, active: float, cash: float, total: float) -> bool:
        return abs(base + active + cash - total) < 0.01

    @staticmethod
    def validate_price(price: float) -> bool:
        return price > 0

    @staticmethod
    def enforce_cash_floor(cash: float, active: float, base: float, total: float) -> Tuple[float, float, float, List[str]]:
        notes = []
        floor = total * CASH_HARD_FLOOR
        if cash < floor:
            notes.append(f"现金{cash:.0f}低于底线{floor:.0f}, 从活动仓扣除差额")
            deficit = floor - cash
            cash = floor
            active -= deficit
            if active < 0:
                notes.append(f"活动仓不足, 从底仓补扣{abs(active):.0f}")
                base += active  # active is negative
                active = 0
        return base, active, cash, notes

    @staticmethod
    def normalize(base: float, active: float, cash: float, total: float) -> Tuple[float, float, float]:
        s = base + active + cash
        if abs(s - total) > 0.01 and s > 0:
            scale = total / s
            return base * scale, active * scale, cash * scale
        return base, active, cash
