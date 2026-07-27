# coding=utf-8
"""calculator.py — 核心计算引擎"""

import logging
from typing import Dict, List, Tuple
from .models import UserConfig, StockHolding, MarketScenario, AllocationResult, StockAllocation
from .validator import ConstraintValidator
from .allocator import get_allocator
from .formatter import ResultFormatter
from .config import SCENARIOS as DEFAULT_SCENARIOS

log = logging.getLogger("position_allocator")


class PositionCalculator:
    def __init__(self, config: UserConfig):
        self.config = config
        self.validator = ConstraintValidator()
        self.fmt = ResultFormatter()
        self.allocator = get_allocator(config.allocation_strategy)

    def calculate_scenario(self, scenario: MarketScenario, holdings: List[StockHolding]) -> AllocationResult:
        C = self.config.total_capital
        b = self.config.base_position_ratio
        a = self.config.active_position_ratio
        c = self.config.cash_reserve_ratio

        # 应用场景调整
        B1 = C * b * (1 + scenario.base_adjustment)
        A1 = C * a * (1 + scenario.active_adjustment)
        C1 = C * c * (1 + scenario.cash_adjustment)

        # 现金底线校验
        B1, A1, C1, notes = self.validator.enforce_cash_floor(C1, A1, B1, C)

        # 归一化
        B, A, CASH = self.validator.normalize(B1, A1, C1, C)

        # 底仓等权分配
        n = len(holdings)
        base_per_stock = B / n if n > 0 else 0

        # 活动仓按策略分配
        active_map = self.allocator.allocate(A, holdings)

        # 计算每只股票
        details = []
        for h in holdings:
            ap = active_map.get(h.stock_code, A / n if n > 0 else 0)
            active_per_stock = ap * scenario.buy_intensity

            # 底仓股数
            base_s = self.fmt.round_to_lot(base_per_stock / h.current_price) if h.current_price > 0 else 0
            # 活动仓股数
            active_s = self.fmt.round_to_lot(active_per_stock / h.current_price) if h.current_price > 0 else 0
            total_s = base_s + active_s

            # 增仓
            add_s = max(0, total_s - h.existing_shares)

            # 范围
            m1, m2 = scenario.range_multiplier
            r_min = self.fmt.round_to_lot(total_s * m1)
            r_max = self.fmt.round_to_lot_ceil(total_s * m2)

            detail = StockAllocation(
                stock_code=h.stock_code,
                stock_name=h.stock_name,
                current_price=h.current_price,
                sector=h.sector,
                risk_level=h.risk_level,
                existing_shares=h.existing_shares,
                base_shares=base_s,
                active_shares=active_s,
                total_shares=total_s,
                additional_shares=add_s,
                buy_range_min=r_min,
                buy_range_max=r_max,
                buy_range_str=self.fmt.format_range(r_min, r_max),
                note=self._note_for(add_s, h),
            )
            details.append(detail)

        return AllocationResult(
            scenario_name=scenario.name,
            total_capital=C,
            base_amount=B, active_amount=A, cash_amount=CASH,
            base_pct=B / C, active_pct=A / C, cash_pct=CASH / C,
            buy_intensity=scenario.buy_intensity,
            stock_details=details, notes=notes,
        )

    def calculate_all(self, holdings: List[StockHolding],
                      scenarios: dict = None) -> Dict[str, AllocationResult]:
        if scenarios is None:
            scenarios = DEFAULT_SCENARIOS
        results = {}
        for key, params in scenarios.items():
            sc = MarketScenario(name=params.get("name", key), **{k: v for k, v in params.items() if k != "name"})
            results[key] = self.calculate_scenario(sc, holdings)
        return results

    @staticmethod
    def _apply_adjustments(C: float, b: float, a: float, c: float,
                           sc: MarketScenario) -> Tuple[float, float, float]:
        return (
            C * b * (1 + sc.base_adjustment),
            C * a * (1 + sc.active_adjustment),
            C * c * (1 + sc.cash_adjustment),
        )

    @staticmethod
    def _note_for(add_shares: int, holding: StockHolding) -> str:
        if add_shares < 0:
            return "⚠ 超配, 建议减仓"
        if add_shares == 0:
            return "✓ 仓位刚好"
        if holding.risk_level == "high":
            return "高风险, 控制仓位"
        return ""
