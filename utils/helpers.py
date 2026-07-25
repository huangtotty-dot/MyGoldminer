# coding=utf-8
"""
utils/helpers.py — 通用工具函数（精简版）
"""
from datetime import datetime
from typing import Optional


# 全局回测时间注入（由 main.py 在 on_bar 中设为当前 bar 时间）
SIM_NOW: Optional[datetime] = None


def _now() -> datetime:
    return SIM_NOW if SIM_NOW is not None else datetime.now()


def get_today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _default_daily_context(code: str, status: str = "unavailable", reason: str = "") -> dict:
    return {
        "daily_status": status,
        "daily_buy_t_ok": False,
        "daily_gate": "neutral",
        "daily_trend_bg": "unknown",
        "daily_ma5_state": "unknown",
        "daily_support_name": "",
        "daily_breakdown_risk": False,
        "daily_overheated": False,
        "daily_pullback_support": False,
        "index_regime": "range",
        "index_regime_status": "normal",
        "index_gate_advice": "normal_t",
        "daily_ma5": 0,
        "daily_ma10": 0,
        "daily_ma20": 0,
        "daily_prev_close": 0,
        "daily_day_ret": 0,
        "intraday_alerts": [],
    }
