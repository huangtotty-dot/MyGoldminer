# coding=utf-8
"""models.py — 数据模型"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class UserConfig:
    total_capital: float              # 资金总量（元）
    base_position_ratio: float = 0.55
    active_position_ratio: float = 0.25
    cash_reserve_ratio: float = 0.20
    min_trade_unit: int = 100
    allocation_strategy: str = "equal_weight"  # equal_weight | risk_weighted | sector_constrained

    def __post_init__(self):
        total = self.base_position_ratio + self.active_position_ratio + self.cash_reserve_ratio
        if abs(total - 1.0) > 0.01:
            s = total or 1.0
            self.base_position_ratio /= s
            self.active_position_ratio /= s
            self.cash_reserve_ratio /= s


@dataclass
class StockHolding:
    stock_code: str                   # 如 "000988.SZ"
    stock_name: str                   # 如 "华工科技"
    current_price: float              # 最新价格
    existing_shares: int = 0          # 已有持仓
    sector: str = ""                  # 所属板块
    risk_level: str = "medium"        # low / medium / high
    volatility: float = 0.0           # 波动率（可选）


@dataclass
class MarketScenario:
    name: str
    base_adjustment: float
    active_adjustment: float
    cash_adjustment: float
    buy_intensity: float
    range_multiplier: tuple = (0.5, 1.2)


@dataclass
class AllocationResult:
    scenario_name: str
    total_capital: float
    base_amount: float
    active_amount: float
    cash_amount: float
    base_pct: float
    active_pct: float
    cash_pct: float
    buy_intensity: float
    stock_details: List["StockAllocation"] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class StockAllocation:
    stock_code: str
    stock_name: str
    current_price: float
    sector: str
    risk_level: str
    existing_shares: int
    base_shares: int
    active_shares: int
    total_shares: int
    additional_shares: int
    buy_range_min: int
    buy_range_max: int
    buy_range_str: str
    note: str = ""
