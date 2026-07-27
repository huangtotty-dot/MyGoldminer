# coding=utf-8
"""config.py — 全局常量与场景预设"""

# ── 默认比例 ──
DEFAULT_BASE_RATIO = 0.55
DEFAULT_ACTIVE_RATIO = 0.25
DEFAULT_CASH_RATIO = 0.20
CASH_HARD_FLOOR = 0.20

# ── 最小交易单位 ──
MIN_TRADE_UNIT = 100

# ── 风险等级权重 ──
RISK_WEIGHTS = {"low": 1.5, "medium": 1.0, "high": 0.5}

# ── 板块上限 ──
SECTOR_LIMITS = {
    "半导体": 0.30, "存储芯片": 0.20, "先进封装": 0.15,
    "光刻机": 0.15, "锂电池": 0.20, "AI_PCB": 0.15,
    "白酒": 0.30, "默认": 0.25,
}

# ── 场景预设 ──
SCENARIOS = {
    "下行": {
        "base_adjustment": -0.05,
        "active_adjustment": -0.10,
        "cash_adjustment": 0.15,
        "buy_intensity": 0.2,
        "range_multiplier": (0.3, 0.8),
    },
    "震荡": {
        "base_adjustment": 0.0,
        "active_adjustment": 0.0,
        "cash_adjustment": 0.0,
        "buy_intensity": 0.6,
        "range_multiplier": (0.5, 1.2),
    },
    "上行": {
        "base_adjustment": 0.05,
        "active_adjustment": 0.10,
        "cash_adjustment": -0.05,
        "buy_intensity": 0.9,
        "range_multiplier": (0.8, 1.5),
    },
}
