# coding=utf-8
"""
analysis/market_regime.py — 个股市场状态识别（简化版）

移植自 E:\06_T\market_regime.py
保留核心枚举和简化检测逻辑，移除 exec 依赖。
"""

from enum import Enum
from typing import Optional, Tuple


class MarketRegime(Enum):
    NORMAL = "normal"
    HEAVY_SELL = "heavy_sell"
    DISTRIBUTION = "distribution"
    MORNING_SURGE = "morning_surge"
    RECOVERY = "recovery"
    BREAKOUT = "breakout"


def regime_name(regime) -> str:
    return {
        MarketRegime.NORMAL: "正常",
        MarketRegime.HEAVY_SELL: "主力重压",
        MarketRegime.DISTRIBUTION: "主力出货",
        MarketRegime.MORNING_SURGE: "早盘冲高",
        MarketRegime.RECOVERY: "触底回升",
        MarketRegime.BREAKOUT: "突破",
    }.get(regime if isinstance(regime, MarketRegime) else MarketRegime.NORMAL, "未知")


def should_clear_all(regime) -> bool:
    return regime in (MarketRegime.HEAVY_SELL, MarketRegime.DISTRIBUTION)


def should_reduce(regime) -> bool:
    return regime == MarketRegime.HEAVY_SELL


def detect_regime(code: str, date: str, **kwargs) -> Tuple[MarketRegime, str]:
    """简化版：默认返回 NORMAL"""
    return MarketRegime.NORMAL, "简化模式：始终返回正常"
