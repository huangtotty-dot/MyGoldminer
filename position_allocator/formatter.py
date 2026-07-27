# coding=utf-8
"""formatter.py — 格式化工具"""

import math


class ResultFormatter:
    @staticmethod
    def format_currency(amount: float) -> str:
        return f"¥{amount:,.0f}"

    @staticmethod
    def format_pct(value: float) -> str:
        return f"{value:.1%}"

    @staticmethod
    def round_to_lot(shares: float, lot_size: int = 100) -> int:
        if shares <= 0:
            return 0
        return max(0, int(math.floor(shares / lot_size)) * lot_size)

    @staticmethod
    def round_to_lot_ceil(shares: float, lot_size: int = 100) -> int:
        if shares <= 0:
            return 0
        return max(0, int(math.ceil(shares / lot_size)) * lot_size)

    @staticmethod
    def format_range(min_val: int, max_val: int) -> str:
        return f"{min_val} ~ {max_val}股"

    @staticmethod
    def fmt_range(min_val: int, max_val: int) -> tuple:
        return min_val, max_val
