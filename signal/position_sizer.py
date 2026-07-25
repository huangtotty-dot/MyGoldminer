# coding=utf-8
"""
signal/position_sizer.py — 动态仓位管理器

简化版：移除虚拟交易/ETF 等依赖，聚焦华工科技个股回测。
移植自 E:\06_T\position_sizer.py
"""

from typing import Dict, Any, Optional

from config.params import PARAMS, STOCK_PARAMS


class PositionSizer:
    def __init__(self, params: dict = None):
        self.params = params or PARAMS

    def _effective_params(self, code: str) -> dict:
        p = dict(self.params)
        sp = STOCK_PARAMS.get(code, {})
        p.update(sp)
        return p

    def calc_sell_qty(self, code: str, holding: dict,
                      sig_score: float, threshold: float,
                      used_sells: int = 0) -> int:
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t <= 0:
            return 0
        p = self._effective_params(code)
        strength = sig_score - threshold
        min_unit = p.get("stock_min_trade_unit", 100)

        if used_sells == 0:
            if strength >= 10:
                pct = p.get("stock_qty_strong_pct", 0.40)
            elif strength >= 5:
                pct = p.get("stock_qty_base_pct", 0.30)
            else:
                pct = p.get("stock_qty_weak_pct", 0.20)
        elif used_sells == 1:
            pct = 0.40 if strength >= 5 else 0.20
        else:
            pct = 0.15

        qty = int(total_t * pct)
        qty = max(min_unit, (qty // min_unit) * min_unit)

        # 可用仓位
        available = int(holding.get("available", total_t) or total_t)
        qty = min(qty, available)
        return qty

    def calc_buy_qty(self, code: str, holding: dict,
                     sig_score: float, threshold: float) -> int:
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t <= 0:
            return 0
        p = self._effective_params(code)
        strength = sig_score - threshold
        min_unit = p.get("stock_min_trade_unit", 100)

        if strength >= 10:
            pct = p.get("stock_first_add_strong_pct", 0.30)
        elif strength >= 5:
            pct = p.get("stock_first_add_pct", 0.20)
        else:
            pct = p.get("stock_first_add_weak_pct", 0.10)

        hold_qty = int(holding.get("qty", 0) or 0)
        max_buyable = max(0, total_t - hold_qty)
        if max_buyable <= 0:
            return 0
        qty = int(total_t * pct)
        qty = min(qty, max_buyable)
        qty = max(min_unit, (qty // min_unit) * min_unit)
        return qty
