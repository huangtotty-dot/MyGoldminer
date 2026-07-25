# coding=utf-8
"""
data/indicators.py — 技术指标计算 + Signal 数据类

移植自 E:\06_T\data_fetcher.py（add_indicators / resample / Signal）
不依赖 gm.api，纯 pandas/numpy 计算。
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional

from config.params import PARAMS


@dataclass
class Signal:
    code: str
    name: str
    action: str
    price: float
    score: float
    reasons: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)
    indicators: Dict[str, float] = field(default_factory=dict)
    factors: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.now)
    cycle_id: str = ""
    cycle_action_count: int = 0
    hold_qty: int = 0


def clean_code(code: str) -> str:
    """去除前缀，返回纯数字代码"""
    return code.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为1分钟K线计算全套技术指标

    输入列: time, open, high, low, close, volume[, amount]
    输出列: 全部输入列 + rsi, bb_up, bb_dn, bb_pct, macd, macd_signal, macd_hist,
            ema_fast, ema_slow, ema_spread, vwap, vwap_dev, vwap_dev_atr,
            day_amplitude, range_pos, vol_ma10, vol_ratio, mom5,
            upper_shadow, lower_shadow
    """
    if df.empty or len(df) < 2:
        return df
    c = df["close"]
    p = PARAMS

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(p["rsi_period"], min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(p["rsi_period"], min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    ma = c.rolling(p["bb_period"], min_periods=1).mean()
    sd = c.rolling(p["bb_period"], min_periods=1).std()
    df["bb_up"] = ma + p["bb_std"] * sd
    df["bb_dn"] = ma - p["bb_std"] * sd
    band_width = (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_dn"]) / band_width

    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = exp1 - exp2
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2

    df["ema_fast"] = c.ewm(span=p["ema_fast_period"], adjust=False).mean()
    df["ema_slow"] = c.ewm(span=p["ema_slow_period"], adjust=False).mean()
    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"].replace(0, np.nan)

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = tp * df["volume"]
    time_text = df["time"].astype(str).str.strip()
    parsed_time = pd.to_datetime(time_text, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    if parsed_time.isna().all():
        parsed_hms = pd.to_datetime(time_text, format="%H:%M:%S", errors="coerce")
        if parsed_hms.notna().all():
            parsed_time = pd.Timestamp.now().normalize() + (parsed_hms - parsed_hms.dt.normalize())
        else:
            parsed_hm = pd.to_datetime(time_text, format="%H:%M", errors="coerce")
            if parsed_hm.notna().all():
                parsed_time = pd.Timestamp.now().normalize() + (parsed_hm - parsed_hm.dt.normalize())
    df["date"] = parsed_time.dt.date

    if "amount" in df.columns and df["amount"].notna().sum() > 0:
        df["vwap"] = df.groupby("date")["amount"].cumsum() / (df.groupby("date")["volume"].cumsum() * 100.0)
    else:
        df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"].replace(0, np.nan)
    _atr_14 = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
    df["vwap_dev_atr"] = df["vwap_dev"] / (_atr_14 / df["close"]).replace(0, np.nan)

    day_high = df.groupby("date")["high"].transform("max")
    day_low = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (day_high - day_low) / day_low.replace(0, np.nan)
    df["range_pos"] = (c - day_low) / (day_high - day_low + 1e-9)

    last_date = df["date"].iloc[-1]
    prev_data = df[df["date"] < last_date]
    df["prev_high"] = prev_data["high"].max() if not prev_data.empty else df["high"].rolling(120).max()

    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)

    k_length = df["high"] - df["low"] + 1e-5
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / k_length
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / k_length

    return df


def resample_to_15min(df: pd.DataFrame) -> pd.DataFrame:
    """1分钟K线 → 15分钟K线聚合"""
    if df.empty or len(df) < 15:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_15m"] = df["time"].dt.floor("15min")
    agg = df.groupby("time_15m").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_15m": "time"})
    return agg


def resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
    """1分钟K线 → 5分钟K线聚合"""
    if df.empty or len(df) < 5:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_5m"] = df["time"].dt.floor("5min")
    agg = df.groupby("time_5m").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_5m": "time"})
    return agg
