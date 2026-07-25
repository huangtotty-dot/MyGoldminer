# coding=utf-8
"""
analysis/index_regime.py — 大盘态势判定模块 V2.2.4（精简回测版）

核心逻辑移植自 E:\06_T\index_regime.py（2738行 → 精简版）
数据获取改用 gm.api（通过 GM_INDEX_CACHE 注入，完全移除 akshare）

在 main.py 的 init() 中，先用 gm.api history_n() 预取全回测区间的
上证指数日线数据，存入 GM_INDEX_CACHE。本模块的 _ir_fetch_index_daily()
从中读取，不再需要 HTTP/akshare 调用。

不可用的功能（QVIX、涨跌家数、E5涨跌停池）自动降级（degraded），
不影响趋势维度（占60%权重）的正常评分。
"""

import os
import math
import time
import logging
import numpy as np
import pandas as pd
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from config.params import INDEX_REGIME_PARAMS

# ===== 日志 =====
_ir_log = logging.getLogger("index_regime")
_ir_log.setLevel(logging.INFO)

# ===== 数据缓存（由 main.py 注入） =====
# 格式: {"sh000001": DataFrame(date, open, high, low, close, volume)}
GM_INDEX_CACHE: Dict[str, pd.DataFrame] = {}
# 标记是否已注入 gm.api 数据（避免在 init() 前被调用）
GM_DATA_READY = False

# ============================================================================
# 参数
# ============================================================================

IR_DEFAULT_PARAMS = dict(INDEX_REGIME_PARAMS)


def _ir_params() -> Dict[str, Any]:
    return dict(IR_DEFAULT_PARAMS)


def _ir_state_dir(p: Dict[str, Any]) -> str:
    env = os.environ.get("IR_STATE_DIR")
    if env:
        return env
    return ""


# ============================================================================
# 状态枚举
# ============================================================================

class IndexRegime(Enum):
    UNI_DOWN = "uni_down"
    RANGE = "range"
    UNI_UP = "uni_up"


_IR_REGIME_NAMES = {
    IndexRegime.UNI_DOWN: "单边下行",
    IndexRegime.RANGE: "横盘震荡",
    IndexRegime.UNI_UP: "单边上涨",
}

_IR_POSITION_FACTORS = {
    IndexRegime.UNI_DOWN: 0.6,
    IndexRegime.RANGE: 1.0,
    IndexRegime.UNI_UP: 1.1,
}

_IR_MODES = ("eod", "morning", "tail")


def index_regime_name(regime) -> str:
    try:
        r = regime if isinstance(regime, IndexRegime) else IndexRegime(str(regime))
    except Exception:
        return "未知"
    return _IR_REGIME_NAMES.get(r, "未知")


def get_regime_position_factor(regime) -> float:
    try:
        r = regime if isinstance(regime, IndexRegime) else IndexRegime(str(regime))
    except Exception:
        return 1.0
    return _IR_POSITION_FACTORS.get(r, 1.0)


# ============================================================================
# 数据获取（覆盖原 akshare/HTTP 调用，改由 gm.api 缓存注入）
# ============================================================================

def _ir_fetch_index_daily(symbol: str, end_date: str, count: int,
                           p: Dict[str, Any]) -> Tuple[Optional[pd.DataFrame], str]:
    """从 GM_INDEX_CACHE 读取大盘日线数据（由 main.py 在 init() 中预填充）。

    原版通过腾讯 HTTP API + akshare 获取，该版本完全依赖 gm.api 数据注入。
    """
    if not GM_DATA_READY:
        _ir_log.warning("[index_regime] GM_DATA_READY=False，跳过指数日线读取")
        return None, "gm_data_not_ready"
    code_clean = symbol.replace("sh", "SHSE.").replace("sz", "SZSE.")
    df = GM_INDEX_CACHE.get(code_clean)
    if df is None:
        # 也尝试直接用 symbol 作为 key
        df = GM_INDEX_CACHE.get(symbol)
    if df is None or df.empty:
        _ir_log.warning(f"[index_regime] 缓存中无 {symbol}({code_clean}) 数据")
        return None, "gm_cache_miss"
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d") if "date" in df.columns else df.index
    df = df[df["date"] <= end_date].reset_index(drop=True)
    return df, "gm_api"


def _ir_fetch_qvix(end_date: str, p: Dict[str, Any]) -> Tuple[Optional[pd.Series], str]:
    """gm.api 无期权数据，降级"""
    return None, "unavailable"


def _ir_fetch_spot_breadth(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """gm.api 无实时全市场数据，降级"""
    return None


def _ir_fetch_e5_limit_pool(end_date: str, p: Dict[str, Any]) -> Tuple[Optional[pd.DataFrame], str]:
    return None, "unavailable"


# ============================================================================
# 小工具
# ============================================================================

_IR_MEM_CACHE: Dict[str, Tuple[float, IndexRegime, float, Dict[str, Any]]] = {}


def _ir_f(x, nd: int = 4) -> Optional[float]:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except Exception:
        return None


def _ir_sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _ir_clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ir_now() -> datetime:
    return datetime.now()


# ============================================================================
# Hurst 指数
# ============================================================================

def _ir_hurst_rs_one(closes: np.ndarray) -> Optional[float]:
    rets = np.diff(np.log(np.asarray(closes, dtype=float)))
    n = rets.size
    if n < 60 or np.isnan(rets).any():
        return None
    lags = [10, 15, 20, 30, 40, 60]
    xs, ys = [], []
    for lag in lags:
        k = n // lag
        if k < 2:
            continue
        rs_list = []
        for i in range(k):
            seg = rets[i * lag:(i + 1) * lag]
            z = np.cumsum(seg - seg.mean())
            r = float(z.max() - z.min())
            s = float(seg.std(ddof=1))
            if s > 0 and r > 0:
                rs_list.append(r / s)
        if rs_list:
            xs.append(math.log(lag))
            ys.append(math.log(float(np.mean(rs_list))))
    if len(xs) < 3:
        return None
    h = float(np.polyfit(np.array(xs), np.array(ys), 1)[0])
    return _ir_clip(h, 0.0, 1.0)


def _ir_hurst_bar(close: pd.Series, window: int, smooth: int) -> Tuple[Optional[float], Optional[float]]:
    need = window + smooth - 1
    if len(close) < need:
        return None, None
    arr = close.values.astype(float)
    hs = []
    tail = smooth + 1
    for end in range(len(arr) - tail + 1, len(arr) + 1):
        h = _ir_hurst_rs_one(arr[end - window:end])
        if h is not None:
            hs.append(h)
    if not hs:
        return None, None
    h_bar = float(np.mean(hs[-smooth:]))
    return h_bar, hs[-1]


# ============================================================================
# streak 特征
# ============================================================================

def _ir_streak_features(df: pd.DataFrame, p: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "streak": None, "cross20": None,
                           "vol_ratio": None, "pos20": None,
                           "ma5": None, "ma10": None, "ma20": None, "ma60": None,
                           "ma5_slope_pct": None, "ma5_slope_up": False, "ma5_slope_down": False,
                           "full_above_ma5": False, "full_below_ma5": False,
                           "full_above_ma5_days": None, "full_below_ma5_days": None,
                           "close_below_ma60": False, "close_above_ma60": False,
                           "close": None, "above_ma5_days": None, "below_ma5_days": None,
                           "above_ma20_days": None, "below_ma20_days": None,
                           "above_ma60_days": None, "below_ma60_days": None,
                           "up_days": None, "down_days": None,
                           "touch_ma20": False, "touch_ma60": False,
                           "break_ma20": False, "break_ma60": False}
    try:
        n = len(df)
        if n < 10:
            return out
        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        ma5 = close.rolling(5, min_periods=5).mean()
        ma10 = close.rolling(10, min_periods=10).mean()
        ma20 = close.rolling(20, min_periods=20).mean()
        ma60 = close.rolling(60, min_periods=60).mean()
        diff = ma5 - ma10
        sign = np.sign(diff.values.astype(float))
        streaks = np.zeros(n, dtype=int)
        cur = 0
        for i, x in enumerate(sign):
            if np.isnan(x) or x == 0:
                cur = 0
            elif x > 0:
                cur = cur + 1 if cur > 0 else 1
            else:
                cur = cur - 1 if cur < 0 else -1
            streaks[i] = cur
        out["streak"] = int(streaks[-1])
        out["ma5"] = _ir_f(ma5.iloc[-1], 2)
        out["ma10"] = _ir_f(ma10.iloc[-1], 2)
        out["close"] = _ir_f(close.iloc[-1], 2)
        if n >= 6 and not np.isnan(ma5.iloc[-2]) and ma5.iloc[-2] != 0:
            ma5_slope_pct = (float(ma5.iloc[-1]) / float(ma5.iloc[-2]) - 1.0) * 100.0
            out["ma5_slope_pct"] = _ir_f(ma5_slope_pct, 4)
            eps = float(p.get("ma5_slope_eps_pct", 0.0)) if isinstance(p, dict) else 0.0
            out["ma5_slope_up"] = bool(ma5_slope_pct > eps)
            out["ma5_slope_down"] = bool(ma5_slope_pct < -eps)
        out["full_above_ma5"] = bool(n >= 5 and float(low.iloc[-1]) > float(ma5.iloc[-1]))
        out["full_below_ma5"] = bool(n >= 5 and float(high.iloc[-1]) < float(ma5.iloc[-1]))
        if n >= 20:
            out["ma20"] = _ir_f(ma20.iloc[-1], 2)
            if n >= 60:
                out["ma60"] = _ir_f(ma60.iloc[-1], 2)
            s_ser = pd.Series(sign, index=df.index)
            cross = ((s_ser * s_ser.shift(1)) < 0).astype(float)
            cross[s_ser.isna() | s_ser.shift(1).isna()] = np.nan
            c20 = cross.rolling(20, min_periods=20).sum().iloc[-1]
            out["cross20"] = None if (isinstance(c20, float) and math.isnan(c20)) else int(c20)
            v5 = vol.rolling(5, min_periods=5).mean().iloc[-1]
            v20 = vol.rolling(20, min_periods=20).mean().iloc[-1]
            if not (np.isnan(v5) or np.isnan(v20) or v20 <= 0):
                out["vol_ratio"] = _ir_f(v5 / v20, 4)
            hh20 = high.rolling(20, min_periods=20).max().iloc[-1]
            ll20 = low.rolling(20, min_periods=20).min().iloc[-1]
            if not (np.isnan(hh20) or np.isnan(ll20) or hh20 <= ll20):
                out["pos20"] = _ir_f((close.iloc[-1] - ll20) / (hh20 - ll20) * 100.0, 2)

            def _tail_run(series: pd.Series, cond) -> int:
                cnt = 0
                for v in reversed(series.astype(float).tolist()):
                    if cond(v):
                        cnt += 1
                    else:
                        break
                return cnt

            if n >= 60:
                out["above_ma5_days"] = _tail_run(close >= ma5, lambda x: bool(x))
                out["below_ma5_days"] = _tail_run(close < ma5, lambda x: bool(x))
                out["above_ma20_days"] = _tail_run(close >= ma20, lambda x: bool(x))
                out["below_ma20_days"] = _tail_run(close < ma20, lambda x: bool(x))
                out["above_ma60_days"] = _tail_run(close >= ma60, lambda x: bool(x))
                out["below_ma60_days"] = _tail_run(close < ma60, lambda x: bool(x))
                out["full_above_ma5_days"] = _tail_run(low > ma5, lambda x: bool(x))
                out["full_below_ma5_days"] = _tail_run(high < ma5, lambda x: bool(x))
                out["up_days"] = _tail_run(close > close.shift(1), lambda x: bool(x))
                out["down_days"] = _tail_run(close < close.shift(1), lambda x: bool(x))
                prev_ma20 = float(ma20.iloc[-2]) if n >= 21 and not np.isnan(ma20.iloc[-2]) else np.nan
                prev_ma60 = float(ma60.iloc[-2]) if n >= 61 and not np.isnan(ma60.iloc[-2]) else np.nan
                if not np.isnan(prev_ma20):
                    out["touch_ma20"] = bool(float(low.iloc[-1]) <= prev_ma20 <= float(high.iloc[-1]))
                    out["break_ma20"] = bool(float(close.iloc[-1]) < float(ma20.iloc[-1])
                                              and float(close.iloc[-2]) >= prev_ma20)
                if not np.isnan(prev_ma60):
                    out["touch_ma60"] = bool(float(low.iloc[-1]) <= prev_ma60 <= float(high.iloc[-1]))
                    out["break_ma60"] = bool(float(close.iloc[-1]) < float(ma60.iloc[-1])
                                              and float(close.iloc[-2]) >= prev_ma60)
        if n >= 60:
            out["close_below_ma60"] = bool(float(close.iloc[-1]) < float(ma60.iloc[-1]))
            out["close_above_ma60"] = bool(float(close.iloc[-1]) > float(ma60.iloc[-1]))
        out["ok"] = True
    except Exception as e:
        _ir_log.info(f"[index_regime] streak 特征计算失败: {type(e).__name__}: {e}")
    return out


def _ir_streak_curve_value(k: int, p: Dict[str, Any]) -> float:
    curve = p.get("streak_curve", ((1, 8.0), (3, 16.0), (5, 24.0), (8, 32.0), (10, 36.0), (13, 40.0)))
    cap = float(p.get("streak_cap", 40.0))
    k_abs = abs(k)
    val = 0.0
    for days, score in curve:
        if k_abs >= days:
            val = float(score)
        else:
            break
    return _ir_clip(val if k > 0 else -val, -cap, cap)


# ============================================================================
# K-day 关键日评估
# ============================================================================

def _ir_kday_eval(df: pd.DataFrame, feat: Dict[str, Any],
                   prev_regime: IndexRegime, prev_anchor: Optional[Dict],
                   prev_kup: Optional[Dict], p: Dict[str, Any]) -> Tuple[Dict, Optional[Dict], Optional[Dict], Dict]:
    """简版 K-day 评估"""
    key_day = {"type": None}
    n = len(df)
    if n < 2 or not feat.get("ok"):
        return key_day, None, None, {}

    close = df["close"].astype(float)
    pct = float(close.iloc[-1]) / float(close.iloc[-2]) - 1 if float(close.iloc[-2]) > 0 else 0
    k_up_pct = float(p.get("k_up_pct", 1.0)) / 100.0
    k_down_pct = float(p.get("k_down_pct", 1.0)) / 100.0

    if pct >= k_up_pct:
        key_day["type"] = "k_up"
        key_day["pct"] = pct
    elif pct <= -k_down_pct:
        key_day["type"] = "k_down"
        key_day["pct"] = pct

    return key_day, None, None, {}


# ============================================================================
# SHARP 指标锐化
# ============================================================================

def _ir_sharp_eval(df: pd.DataFrame, feat: Dict[str, Any],
                    prev_sharp: Optional[Dict], prev_regime_val: str,
                    p: Dict[str, Any]) -> Tuple[Dict, float, bool]:
    """精简版 SHARP 评估"""
    sharp = {"sharp_s": 0.0, "triggered": False, "up_raw": 0.0, "dn_raw": 0.0}
    if not feat.get("ok"):
        return sharp, 0.0, False
    n = len(df)
    if n < 6:
        return sharp, 0.0, False

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    _open = df["open"].astype(float)
    prev_close_s = close.shift(1)
    ma5 = close.rolling(5, min_periods=5).mean()
    ma10 = close.rolling(10, min_periods=10).mean()
    sharp_full = float(p.get("sharp_full", 22))
    sharp_map_max = float(p.get("sharp_map_max", 40))
    decay = float(p.get("sharp_decay", 0.5))
    trigger = float(p.get("sharp_trigger", 32))

    last = n - 1
    c = float(close.iloc[last])
    h = float(high.iloc[last])
    pc = float(prev_close_s.iloc[last]) if not np.isnan(prev_close_s.iloc[last]) else c
    ma5_v = float(ma5.iloc[last]) if not np.isnan(ma5.iloc[last]) else c
    ma10_v = float(ma10.iloc[last]) if not np.isnan(ma10.iloc[last]) else c
    gap_pct = (float(_open.iloc[last]) / pc - 1) if pc > 0 else 0

    # 波动突破侧
    up_score = 0
    dn_score = 0
    if n >= 6:
        max5 = float(high.iloc[last - 5:last].max() if last >= 5 else 0)
        min5 = float(df["low"].iloc[last - 5:last].min() if last >= 5 else 0)
        if h > max5 and max5 > 0:
            up_score += int(p.get("sharp_bo5_high", 5))
            if c > max5:
                up_score += int(p.get("sharp_bo5_close", 2))
            if gap_pct > 0:
                up_score += int(p.get("sharp_gap", 2))
    # 均线侧
    if c > ma5_v:
        up_score += int(p.get("sharp_ma5", 4))
        if c > ma10_v:
            up_score += int(p.get("sharp_ma10", 4))
    if c < ma5_v:
        dn_score += int(p.get("sharp_ma5", 4))
        if c < ma10_v:
            dn_score += int(p.get("sharp_ma10", 4))
    # 量能侧
    vr = feat.get("vol_ratio")
    if vr is not None:
        vr15 = float(p.get("sharp_vol_15", 1.5))
        vr12 = float(p.get("sharp_vol_12", 1.2))
        if vr >= vr15:
            up_score += int(p.get("sharp_vol_hi_score", 5))
            dn_score += int(p.get("sharp_vol_hi_score", 5))
        elif vr >= vr12:
            up_score += int(p.get("sharp_vol_lo_score", 3))
            dn_score += int(p.get("sharp_vol_lo_score", 3))

    # 空头对称
    if n >= 6:
        min5 = float(df["low"].iloc[last - 5:last].min() if last >= 5 else 0)
        l = float(df["low"].iloc[last])
        if l < min5 and min5 > 0:
            dn_score += int(p.get("sharp_bo5_high", 5))
            if c < min5:
                dn_score += int(p.get("sharp_bo5_close", 2))
            if gap_pct < 0:
                dn_score += int(p.get("sharp_gap", 2))

    up_raw = min(up_score, int(sharp_full))
    dn_raw = min(dn_score, int(sharp_full))
    sharp["up_raw"] = up_raw
    sharp["dn_raw"] = dn_raw

    if up_raw > 5 and dn_raw > 5:
        net = up_raw - dn_raw
    else:
        net = up_raw if up_raw > dn_raw else -dn_raw

    sharp_s = net / sharp_full * sharp_map_max
    sharp_s = _ir_clip(sharp_s, -sharp_map_max, sharp_map_max)
    sharp["sharp_s"] = sharp_s
    sharp["triggered"] = abs(sharp_s) >= trigger

    # 衰减
    carry = 0.0
    if prev_sharp and prev_sharp.get("triggered"):
        prev_carry = float(prev_sharp.get("carry", abs(prev_sharp.get("sharp_s", 0))))
        if prev_carry > 0.5:
            carry = prev_carry * decay
            sharp["carry"] = carry
    sharp_add = (sharp_s if sharp["triggered"] else 0) + carry

    return sharp, carry, sharp["triggered"]


# ============================================================================
# 趋势/环境评分
# ============================================================================

def _ir_calc_adx(df: pd.DataFrame, p: Dict[str, Any]) -> Optional[float]:
    n = len(df)
    if n < int(p.get("adx_len", 14)) + 2:
        return None
    period = int(p.get("adx_len", 14))
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    up = high - high.shift(1)
    dn = low.shift(1) - low
    plus_dm = ((up > dn) & (up > 0)).astype(float) * up
    minus_dm = ((dn > up) & (dn > 0)).astype(float) * dn
    atr = tr.rolling(period).mean()
    pdi = 100 * plus_dm.rolling(period).sum() / atr.replace(0, np.nan)
    ndi = 100 * minus_dm.rolling(period).sum() / atr.replace(0, np.nan)
    dx = (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan) * 100
    adx_val = dx.rolling(period).mean().iloc[-1]
    return _ir_f(adx_val) if not (isinstance(adx_val, float) and math.isnan(adx_val)) else None


def _ir_score_trend(df: pd.DataFrame, feat: Dict[str, Any], p: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """趋势维度评分（60%权重）"""
    detail: Dict[str, Any] = {"streak_score": None, "structure_score": None,
                              "adx_score": None, "reg_r2_score": None,
                              "er_score": None, "aroon_score": None, "trend_total": 0.0}
    if not feat.get("ok"):
        return 0.0, detail
    total = 0.0
    w_streak = float(p.get("w_ma_streak", 0.35))
    w_adx = float(p.get("w_adx", 0.18))
    w_reg_r2 = float(p.get("w_reg_r2", 0.17))
    w_er = float(p.get("w_er", 0.08))
    w_aroon = float(p.get("w_aroon", 0.07))
    w_structure = float(p.get("w_structure", 0.15))

    # streak
    streak = feat.get("streak")
    if streak is not None and streak != 0:
        streak_score = _ir_streak_curve_value(streak, p)
    else:
        streak_score = 0.0
    detail["streak_score"] = _ir_f(streak_score)
    total += streak_score * w_streak

    # ADX
    adx = _ir_calc_adx(df, p)
    if adx is not None:
        adx_score = _ir_clip((float(adx) - 20) / 20 * 10, -10, 10)
    else:
        adx_score = 0.0
    detail["adx_score"] = _ir_f(adx_score)
    total += adx_score * w_adx

    # structure (simplified)
    ma5 = feat.get("ma5")
    ma20 = feat.get("ma20")
    structure_score = 0.0
    if ma5 is not None and ma20 is not None and ma20 > 0:
        close_val = feat.get("close", 0)
        if close_val:
            if close_val > ma5:
                structure_score += 5
            if close_val > ma20:
                structure_score += 3
            if feat.get("full_above_ma5"):
                structure_score += 4
    detail["structure_score"] = _ir_f(structure_score)
    total += structure_score * w_structure

    detail["trend_total"] = _ir_f(total)
    return total, detail


def _ir_score_env(df: pd.DataFrame, feat: Dict[str, Any], p: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """环境维度评分（40%权重）—— 降解版：仅量能可用，其余降级"""
    detail: Dict[str, Any] = {"breadth_score": None, "nhnl_score": None,
                              "volume_score": None, "qvix_score": None, "env_total": 0.0}
    total = 0.0
    w_vol = float(p.get("w_volume", 0.25))
    vr = feat.get("vol_ratio")
    vol_score = 0.0
    if vr is not None:
        if vr >= float(p.get("vol_ratio_high", 1.2)):
            vol_score = 5.0
        elif vr <= float(p.get("vol_ratio_low", 0.8)):
            vol_score = -3.0
    detail["volume_score"] = _ir_f(vol_score)
    total += vol_score * w_vol

    detail["breadth_score"] = None
    detail["nhnl_score"] = None
    detail["qvix_score"] = None
    detail["env_total"] = _ir_f(total)
    return total, detail


# ============================================================================
# 状态机
# ============================================================================

def _ir_step_regime(prev_regime: IndexRegime, s_today: float, s_prev: Optional[float],
                     days_in_regime: int, p: Dict[str, Any],
                     key_day: Dict, sharp_triggered: bool,
                     sharp_add: float, feat: Dict[str, Any]) -> Tuple[IndexRegime, int, bool]:
    """磁滞状态机：在 range/uni_up/uni_down 间切换"""
    enter = float(p.get("enter_threshold", 25))
    exit_t = float(p.get("exit_threshold", 15))
    enter_days = int(p.get("enter_confirm_days", 2))
    k_boost = int(p.get("k_boost", 9))
    enter_abs = abs(s_today)
    new_regime = prev_regime
    new_days = days_in_regime
    switched = False

    # K-day 强制跃迁
    k_type = key_day.get("type") if isinstance(key_day, dict) else None
    if k_type == "k_up" and prev_regime in (IndexRegime.RANGE, IndexRegime.UNI_DOWN):
        new_regime = IndexRegime.UNI_UP
        new_days = k_boost
        switched = True
    elif k_type == "k_down" and prev_regime in (IndexRegime.RANGE, IndexRegime.UNI_UP):
        new_regime = IndexRegime.UNI_DOWN
        new_days = -k_boost
        switched = True

    if not switched:
        if prev_regime in (IndexRegime.UNI_UP, IndexRegime.UNI_DOWN):
            if enter_abs < exit_t:
                new_regime = IndexRegime.RANGE
                new_days = 0
                switched = True
            else:
                new_days = days_in_regime + (1 if s_today > 0 else -1)
        else:
            if enter_abs >= enter:
                ns = days_in_regime + 1 if (s_today > 0 and prev_regime != IndexRegime.UNI_DOWN) else \
                     (days_in_regime - 1 if s_today < 0 else days_in_regime)
                if ns >= enter_days and s_today > 0:
                    new_regime = IndexRegime.UNI_UP
                    new_days = 1
                    switched = True
                elif ns <= -enter_days and s_today < 0:
                    new_regime = IndexRegime.UNI_DOWN
                    new_days = -1
                    switched = True
                else:
                    new_days = ns
            else:
                new_days = 0

    return new_regime, new_days, switched


# ============================================================================
# 引擎主类
# ============================================================================

class _IndexRegimeEngine:
    """大盘态势判定引擎（回测版）"""

    def detect(self, as_of: Optional[str] = None, force: bool = False,
               mode: str = "eod") -> Tuple[IndexRegime, float, Dict[str, Any]]:
        p = _ir_params()
        target = (as_of or _ir_now().strftime("%Y-%m-%d"))[:10]
        mode = str(mode or "eod").lower()

        # 内存缓存
        cache_key = f"{mode}:{target}"
        if not force and cache_key in _IR_MEM_CACHE:
            ts, r, s, ctx = _IR_MEM_CACHE[cache_key]
            if (time.time() - ts) < float(p.get("score_cache_ttl", 1800)):
                return r, s, ctx

        try:
            regime, score, ctx = self._detect_inner(target, p, mode)
        except Exception as e:
            _ir_log.warning(f"[index_regime] detect 异常: {e}")
            regime, score = IndexRegime.RANGE, 0.0
            ctx = {"regime": regime.value, "score": 0.0, "degraded": ["internal_error"],
                   "gate_advice": "normal_t", "mode": mode}

        _IR_MEM_CACHE[cache_key] = (time.time(), regime, score, ctx)
        return regime, score, ctx

    def _detect_inner(self, target: str, p: Dict[str, Any],
                      mode: str = "eod") -> Tuple[IndexRegime, float, Dict[str, Any]]:
        degraded: List[str] = []

        # 指数日线
        df, px_src = _ir_fetch_index_daily(p["index_symbol_sh"], target,
                                            int(p["kline_count_sh"]), p)
        if df is None or len(df) == 0:
            ctx = {"date": target, "regime": IndexRegime.RANGE.value,
                   "score": 0.0, "degraded": ["指数日线不可用"], "gate_advice": "normal_t"}
            return IndexRegime.RANGE, 0.0, ctx

        df = df[df["date"] <= target].reset_index(drop=True)
        if len(df) == 0:
            ctx = {"date": target, "regime": IndexRegime.RANGE.value,
                   "score": 0.0, "degraded": ["无指数日线"], "gate_advice": "normal_t"}
            return IndexRegime.RANGE, 0.0, ctx

        date_str = str(df["date"].iloc[-1])
        close = df["close"].astype(float)

        feat = _ir_streak_features(df, p)
        key_day, _, _, _ = _ir_kday_eval(df, feat, IndexRegime.RANGE, None, None, p)

        # 趋势评分
        trend_s, trend_detail = _ir_score_trend(df, feat, p)
        # 环境评分（降级版）
        env_s, env_detail = _ir_score_env(df, feat, p)
        # Hurst
        h_bar, _ = _ir_hurst_bar(close, int(p.get("hurst_window", 120)),
                                  int(p.get("hurst_smooth", 20)))
        # SHARP
        sharp, sharp_carry, sharp_trig = _ir_sharp_eval(df, feat, None, "range", p)
        sharp_add = (sharp.get("sharp_s", 0) if sharp_trig else 0) + sharp_carry

        # 综合分
        w_trend = float(p.get("trend_weight", 0.60))
        w_env = float(p.get("env_weight", 0.40))
        s_raw = trend_s * w_trend + env_s * w_env

        # Hurst 修正
        hurst_mult = 1.0
        if h_bar is not None:
            if h_bar > 0.6:
                hurst_mult = 1.2
            elif h_bar < 0.4:
                hurst_mult = 0.8
        s = s_raw * hurst_mult

        # SHARP 叠加
        s = s + sharp_add

        # 衰竭
        exhaust_flag = False
        if abs(s) > float(p.get("enter_threshold", 25)) * 1.5:
            exhaust_flag = True
            s = s * float(p.get("exhaust_factor", 0.7))

        # EMA 平滑
        s_prev = None

        # 状态机
        prev_regime = IndexRegime.RANGE
        days_in_regime = 0
        regime, days_in_regime, switched = _ir_step_regime(
            prev_regime, s, s_prev, days_in_regime, p,
            key_day, sharp_trig, sharp_add, feat)

        # 门控建议
        gate_advice = "normal_t"
        if regime == IndexRegime.UNI_UP:
            gate_advice = "trend_up_hold"
        elif regime == IndexRegime.UNI_DOWN:
            gate_advice = "defensive_t"

        ctx = {
            "date": date_str,
            "regime": regime.value,
            "regime_name": index_regime_name(regime),
            "score": _ir_f(s, 2) or 0.0,
            "score_raw": _ir_f(s_raw, 2) or 0.0,
            "trend_score": _ir_f(trend_s, 2) or 0.0,
            "env_score": _ir_f(env_s, 2) or 0.0,
            "hurst_mult": _ir_f(hurst_mult, 4) or 1.0,
            "exhaust_flag": exhaust_flag,
            "days_in_regime": days_in_regime,
            "switched": switched,
            "detail": {
                "streak": feat.get("streak"),
                "trend": trend_detail,
                "env": env_detail,
                "sharp_s": _ir_f(sharp.get("sharp_s")),
                "sharp_triggered": sharp_trig,
            },
            "degraded": degraded,
            "gate_advice": gate_advice,
            "mode": mode,
        }
        return regime, s, ctx

    @staticmethod
    def _degenerate_ctx(target: str, reason: str, mode: str) -> Dict[str, Any]:
        return {"date": target, "regime": IndexRegime.RANGE.value,
                "score": 0.0, "degraded": [reason],
                "gate_advice": "normal_t", "mode": mode}


# 全局单例
_IR_ENGINE = _IndexRegimeEngine()


def detect_index_regime(as_of: str = None, force: bool = False,
                        mode: str = "eod") -> Tuple[IndexRegime, float, Dict[str, Any]]:
    """公开入口：返回 (regime, score, context_dict)"""
    return _IR_ENGINE.detect(as_of, force, mode)
