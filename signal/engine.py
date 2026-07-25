# coding=utf-8
"""
signal/engine.py — 信号评分引擎

移植自 E:\06_T\signal_engine.py
- 移除 exec/globals 依赖，改用标准 import
- 时间由外部注入 SIM_NOW
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

from config.params import PARAMS, STOCK_PARAMS
from data.indicators import Signal, clean_code


# ===== 时间注入（回测时由 main.py 设为当前 K 线时间） =====
SIM_NOW: Optional[datetime] = None

def _engine_now() -> datetime:
    return SIM_NOW if SIM_NOW is not None else datetime.now()


# ===== 工具函数 =====

def _sp_param(code: str, key: str, default=None):
    """个股专属参数 > 全局 PARAMS > default"""
    v = STOCK_PARAMS.get(code, {}).get(key)
    if v is not None:
        return v
    return PARAMS.get(key, default)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


# ===== FACTOR_WEIGHTS =====
FACTOR_WEIGHTS = {
    "vwap_buy_atr_mult": -1.5,
    "vwap_sell_atr_mult": 1.2,
    "rsi_oversold_atr_adj": True,
    "buy_score_atr_smooth": 50,
    "sell_score_atr_smooth": 50,
    "trend_strength_atr_mult": 2.0,
    "stop_loss_atr_mult": 2.5,
    "take_profit_atr_mult": 3.0,
    "min_score_continuous": True,
    "factor_weight_vwap": 0.20,
    "factor_weight_rsi": 0.12,
    "factor_weight_macd": 0.08,
    "factor_weight_volume": 0.08,
    "factor_weight_position": 0.08,
    "factor_weight_ema": 0.04,
    "factor_weight_pattern": 0.20,
    "factor_weight_index_regime": 0.20,
    "factor_weight_time": 0.00,
    "max_score_raw": 100,
}


# ===== ScoringEngine =====

class ScoringEngine:
    """因子打分引擎"""

    @staticmethod
    def _sigmoid(x: float, center: float = 0, slope: float = 1) -> float:
        z = -slope * (x - center)
        if z > 100:
            return 0.0
        if z < -100:
            return 1.0
        return 1.0 / (1.0 + np.exp(z))

    @staticmethod
    def score_vwap_buy(feats: dict) -> tuple:
        ratio = feats.get("vwap_dev_atr_ratio", 0)
        raw = ScoringEngine._sigmoid(-ratio, center=0.5, slope=2.0)
        return raw, [{"指标": "VWAP偏离(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_buy(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngine._sigmoid(35 - rsi, center=3, slope=0.5)
        return raw, [{"指标": "RSI超卖", "当前": f"{rsi:.1f}", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_sell(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngine._sigmoid(rsi - 78, center=3, slope=0.5)
        return raw, [{"指标": "RSI超买", "当前": f"{rsi:.1f}", "强度": round(raw, 3)}]

    @staticmethod
    def score_macd_buy(feats: dict) -> tuple:
        mh = feats.get("macd_hist", 0)
        pmh = feats.get("prev_macd_hist", 0)
        if mh < 0 and mh > pmh:
            ratio = min(1.0, abs(mh) / max(abs(pmh), 0.001))
            return ratio, [{"指标": "MACD负区拐头", "当前": f"{mh:.4f}↑", "强度": round(ratio, 3)}]
        return 0.0, []

    @staticmethod
    def score_macd_sell(feats: dict) -> tuple:
        mh = feats.get("macd_hist", 0)
        pmh = feats.get("prev_macd_hist", 0)
        if mh > 0 and mh < pmh:
            ratio = min(1.0, mh / max(mh - pmh, 0.001))
            return ratio, [{"指标": "MACD正区萎缩", "当前": f"{mh:.4f}↓", "强度": round(ratio, 3)}]
        return 0.0, []

    @staticmethod
    def score_vwap_sell(feats: dict) -> tuple:
        price = feats.get("price", 0)
        vwap = feats.get("vwap", 0)
        atr = max(feats.get("atr", 0.02), 0.002)
        if vwap <= 0 or price <= 0:
            return 0.0, []
        ratio = (price - vwap) / vwap / atr
        raw = ScoringEngine._sigmoid(ratio, center=0.5, slope=1.5)
        return raw, [{"指标": "VWAP溢价(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_lower_shadow(feats: dict) -> tuple:
        ls = feats.get("lower_shadow", 0)
        raw = ScoringEngine._sigmoid(ls, center=0.3, slope=8.0)
        if raw > 0.05:
            return raw, [{"指标": "长下影", "当前": f"{ls:.2f}", "强度": round(raw, 3)}]
        return 0.0, []

    @staticmethod
    def score_ema_improve(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0)
        pes = feats.get("prev_ema_spread", 0)
        delta = es - pes
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        if raw > 0.05:
            return raw, [{"指标": "EMA转强", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}]
        return 0.0, []

    @staticmethod
    def score_ema_weaken(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0)
        pes = feats.get("prev_ema_spread", 0)
        delta = pes - es
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        if raw > 0.05:
            return raw, [{"指标": "EMA转弱", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}]
        return 0.0, []

    @staticmethod
    def score_volume(feats: dict) -> tuple:
        vr = feats.get("vol_ratio", 1.0)
        raw = ScoringEngine._sigmoid(vr, center=1.2, slope=4.0)
        if raw > 0.05:
            return raw, [{"指标": "量能确认", "当前": f"{vr:.2f}", "强度": round(raw, 3)}]
        return 0.0, []

    @staticmethod
    def score_upper_shadow(feats: dict) -> tuple:
        us = feats.get("upper_shadow", 0)
        raw = ScoringEngine._sigmoid(us, center=0.4, slope=6.0)
        if raw > 0.05:
            return raw, [{"指标": "长上影", "当前": f"{us:.2f}", "强度": round(raw, 3)}]
        return 0.0, []

    @staticmethod
    def _weighted_factor_score(raw: float, weight_key: str, w_mult: float = 1.0,
                                 p: dict = None) -> float:
        w = (p or FACTOR_WEIGHTS).get(weight_key, 0.10)
        return raw * 100 * w * w_mult

    @staticmethod
    def score_index_regime(feats: dict, side: str = "buy") -> float:
        regime = feats.get("index_regime", "range")
        if regime == "uni_down":
            return 1.0 if side == "sell" else 0.0
        if regime == "uni_up":
            return 0.2 if side == "sell" else 1.0
        return 0.5

    @staticmethod
    def calc_buy_score(feats: dict, p: dict = None) -> tuple:
        details = []
        score = 0.0
        raw, d = ScoringEngine.score_vwap_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_vwap", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_rsi_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_rsi", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_macd_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_macd", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_volume(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_volume", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_lower_shadow(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_pattern", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_ema_improve(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_ema", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        s_regime = ScoringEngine.score_index_regime(feats, "buy")
        s = ScoringEngine._weighted_factor_score(s_regime, "factor_weight_index_regime", p=p)
        score += s
        details.append({"指标": "大盘态势", "当前": feats.get("index_regime", "range"), "加分": round(s, 1)})
        score = min(score, FACTOR_WEIGHTS.get("max_score_raw", 100))
        return round(score, 1), details

    @staticmethod
    def calc_sell_score(feats: dict, p: dict = None) -> tuple:
        details = []
        score = 0.0
        raw, d = ScoringEngine.score_vwap_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_vwap", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_rsi_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_rsi", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_macd_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_macd", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_volume(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_volume", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_upper_shadow(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_pattern", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_ema_weaken(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_ema", p=p)
        score += s
        d and details.append(d[0] | {"加分": round(s, 1)})
        s_regime = ScoringEngine.score_index_regime(feats, "sell")
        s = ScoringEngine._weighted_factor_score(s_regime, "factor_weight_index_regime", p=p)
        score += s
        details.append({"指标": "大盘态势", "当前": feats.get("index_regime", "range"), "加分": round(s, 1)})
        score = min(score, FACTOR_WEIGHTS.get("max_score_raw", 100))
        return round(score, 1), details


# ===== RiskManager =====

class RiskManager:
    """一票否决守门员"""

    @staticmethod
    def check_all(feats: dict) -> dict:
        result = {"blocked": False, "reason": "", "buy_block": [], "sell_block": []}
        if not feats:
            result["blocked"] = True
            result["reason"] = "无特征数据"
            return result
        if feats.get("day_amplitude", 0) < 0.002 and feats.get("t_val", 0) > 1000:
            result["sell_block"].append("dead_water")
        if feats.get("daily_breakdown_risk"):
            result["buy_block"].append("daily_breakdown_risk")
        if feats.get("is_strong_uptrend"):
            result["sell_block"].append("strong_uptrend")
        if feats.get("is_gap_down_no_reversal"):
            result["buy_block"].append("gap_down_no_reversal")
        if feats.get("daily_overheated"):
            result["buy_block"].append("daily_overheated")
        index_regime = feats.get("index_regime", "range")
        if index_regime == "uni_down":
            result["buy_block"].append("index_uni_down_clearance")
        for alert in (feats.get("intraday_alerts") or []):
            if alert.get("tag") in ("I1", "I4"):
                result["buy_block"].append(f"intraday_panic_{alert.get('tag')}")
        return result


# ===== FeatureExtractor =====

class FeatureExtractor:
    """单次调用提取全部客观特征"""

    @staticmethod
    def extract_all(code: str, name: str, df, holding: dict,
                    daily_ctx: dict, cached_5m_df=None,
                    cached_15m_df=None) -> dict:
        feats = {}
        if df.empty or len(df) < 5:
            return feats
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        _dt = df.index if hasattr(last, "time") else pd.Timestamp.now()
        if "time" in last and hasattr(last["time"], "hour"):
            _dt = pd.to_datetime(last["time"])
        feats["t_val"] = _dt.hour * 100 + _dt.minute if hasattr(_dt, "hour") else 0
        feats["current_minute"] = _dt.hour * 60 + _dt.minute if hasattr(_dt, "hour") else 0
        feats["is_etf"] = holding.get("type") == "etf"
        price = float(last.get("close", 0))
        vwap = float(last.get("vwap", 0) or 0)
        feats["price"] = price
        feats["vwap"] = vwap
        feats["day_amplitude"] = float(last.get("day_amplitude", 0) or 0)
        feats["rsi"] = float(last.get("rsi", 50) or 50)
        feats["bb_pct"] = float(last.get("bb_pct", 0.5) or 0.5)
        feats["macd_hist"] = float(last.get("macd_hist", 0) or 0)
        feats["prev_macd_hist"] = float(prev.get("macd_hist", 0) or 0)
        feats["ema_spread"] = float(last.get("ema_spread", 0) or 0)
        feats["prev_ema_spread"] = float(prev.get("ema_spread", 0) or 0)
        feats["range_pos"] = float(last.get("range_pos", 0.5) or 0.5)
        feats["vol_ratio"] = float(last.get("vol_ratio", 1.0) or 1.0)
        feats["mom5"] = float(last.get("mom5", 0) or 0)
        feats["lower_shadow"] = float(last.get("lower_shadow", 0) or 0)
        feats["upper_shadow"] = float(last.get("upper_shadow", 0) or 0)
        if len(df) >= 14:
            atr_v = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
            feats["atr"] = float(atr_v.iloc[-1] / price) if price > 0 else 0.02
        else:
            feats["atr"] = 0.02
        atr = max(feats["atr"], 0.002)
        feats["buy_profit_space"] = (vwap - price) / price if price > 0 else 0.0
        feats["sell_profit_space"] = (price - vwap) / vwap if vwap else 0.0
        feats["vwap_dev_atr_ratio"] = feats["buy_profit_space"] / atr if atr > 0 else 0
        today_df = df[df["date"] == last["date"]]
        today_open = float(today_df.iloc[0]["open"]) if not today_df.empty else price
        pre_close = float(holding.get("pre_close", today_open) or today_open)
        feats["today_open"] = today_open
        feats["pre_close"] = pre_close
        feats["today_ret"] = (price - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["open_gap"] = (today_open - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["prev_high"] = float(last.get("prev_high", 0) or price)
        feats["is_strong_trend"] = (feats["today_ret"] > 2 * atr) and (price >= feats["prev_high"] * 0.99) and (feats["vol_ratio"] > 1.2)
        feats["is_strong_pullback"] = feats["is_strong_trend"] and abs((price - vwap) / vwap) < 0.5 * atr if vwap else False
        cost = float(holding.get("cost", 0) or 0)
        feats["hold_qty"] = int(holding.get("t_qty") or holding.get("qty") or 0)
        feats["profit_pct"] = (price - cost) / cost if cost > 0 else 0
        feats["is_deep_loss"] = cost > 0 and feats["profit_pct"] < -5 * atr
        dc = daily_ctx if isinstance(daily_ctx, dict) else {}
        for k in ["daily_status", "daily_gate", "daily_trend_bg", "daily_ma5_state",
                   "daily_support_name", "index_regime"]:
            feats[k] = dc.get(k, "unknown")
        for n in [5, 10, 20, 30, 60, 120]:
            feats[f"daily_ma{n}"] = float(dc.get(f"daily_ma{n}", 0) or 0)
        feats["daily_ma5_slope"] = float(dc.get("daily_ma5_slope", 0) or 0)
        feats["daily_above_ma5"] = feats["daily_ma5"] > 0 and price >= feats["daily_ma5"]
        feats["daily_buy_t_ok"] = dc.get("daily_status") == "ok" and feats["daily_ma5"] > 0 and feats["daily_ma5_state"] in {"near_ma5_chop", "above_ma5_trend"}
        feats["daily_breakdown_risk"] = bool(dc.get("daily_breakdown_risk", False))
        feats["daily_overheated"] = bool(dc.get("daily_overheated", False))
        feats["daily_pullback_support"] = bool(dc.get("daily_pullback_support", False))
        feats["benchmark_gate"] = dc.get("benchmark_gate", "neutral")
        feats["intraday_alerts"] = dc.get("intraday_alerts", [])
        for k in ["index_regime_status", "index_circuit_state", "index_gate_advice", "index_temp_bucket"]:
            feats[k] = dc.get(k, "normal")
        if cached_15m_df is not None and not cached_15m_df.empty:
            _f15 = FeatureExtractor.extract_15min_features(cached_15m_df, price, vwap, atr=atr)
            for k, v in _f15.items():
                feats[f"f15_{k}"] = v
        if cached_5m_df is not None and not cached_5m_df.empty:
            _f5 = FeatureExtractor.extract_5min_features(cached_5m_df, price, vwap, atr=atr)
            for k, v in _f5.items():
                feats[f"f5_{k}"] = v
        feats["is_strong_uptrend"] = False
        if not feats.get("is_etf") and len(df) >= 20 and price > 0:
            c5 = df["close"].tail(5).mean()
            c10 = df["close"].tail(10).mean()
            c20 = df["close"].tail(20).mean()
            ma_ok = c5 >= c10 * 0.995 and c10 >= c20 * 0.995
            day_low = float(df["low"].iloc[:len(df)].min())
            rebound = (price - day_low) / day_low if day_low > 0 else 0
            feats["is_strong_uptrend"] = ma_ok and rebound > 3 * atr and price > vwap * 1.005
        feats["is_double_top"] = False
        if len(df) >= 10:
            high_sofar = float(df["high"].max()) if not df.empty else price
            peak_gap = (high_sofar - price) / high_sofar if high_sofar > 0 else 0
        return feats

    @staticmethod
    def extract_15min_features(df, price, vwap, atr=0.02):
        """15分钟线特征"""
        feats = {}
        if df is None or df.empty or len(df) < 3:
            return feats
        c15 = df["close"]
        rsi_delta = c15.diff()
        g = rsi_delta.clip(lower=0).rolling(6, min_periods=1).mean()
        l = (-rsi_delta).clip(upper=0).rolling(6, min_periods=1).mean()
        rs = g / l.replace(0, np.nan)
        feats["rsi"] = float(100 - 100 / (1 + rs).iloc[-1]) if rs.notna().any() else 50
        return feats

    @staticmethod
    def extract_5min_features(df, price, vwap, atr=0.02):
        """5分钟线特征"""
        feats = {}
        if df is None or df.empty or len(df) < 3:
            return feats
        return feats


# ===== SignalEngine =====

class SignalEngine:
    def __init__(self, factor_weights: dict = None):
        self.buy_cooldown: Dict[str, datetime] = {}
        self.sell_cooldown: Dict[str, datetime] = {}
        self.buy_count_per_stock: Dict[str, int] = {}
        self.sell_count_per_stock: Dict[str, int] = {}
        self.state_reset_date = _engine_now().strftime("%Y-%m-%d")
        self.t_cycle_start_time: Dict[str, datetime] = {}
        self.last_signal_state: Dict[str, Dict[str, Any]] = {}
        self.last_trade_state: Dict[str, Dict[str, Any]] = {}
        self.cycle_count: Dict[str, int] = {}
        self.cycle_direction: Dict[str, str] = {}
        self.post_sell_block_until: Dict[str, datetime] = {}
        self.awaiting_buyback: Dict[str, Dict[str, Any]] = {}
        self.pending_sells: Dict[str, Dict[str, Any]] = {}
        self.peak_tracker: Dict[str, Dict[str, Any]] = {}
        self.daily_realized_loss_monitor = 0.0
        self.diagnostics: Dict[str, Dict[str, Any]] = {}
        self.scenario_factor_state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.morning_alert_state: Dict[str, Dict[str, Any]] = {}
        self.last_decision: Dict[str, Dict[str, Any]] = {}
        self.factor_weights = factor_weights or FACTOR_WEIGHTS
        self.signals: List[Signal] = []
        self._last_feats: Dict[str, Dict[str, Any]] = {}

    def _get_params(self, code: str) -> dict:
        p = dict(PARAMS)
        sp = STOCK_PARAMS.get(code, {})
        p.update(sp)
        return p

    def _check_date_reset(self):
        now = _engine_now().date()
        if now != datetime.strptime(self.state_reset_date, "%Y-%m-%d").date():
            for k in ["buy_cooldown", "sell_cooldown", "buy_count_per_stock",
                       "sell_count_per_stock", "cycle_count", "post_sell_block_until",
                       "awaiting_buyback", "pending_sells", "peak_tracker",
                       "scenario_factor_state", "morning_alert_state", "cycle_direction"]:
                getattr(self, k).clear()
            self.cycle_direction.clear()
            self.diagnostics.clear()
            self.last_decision.clear()
            self.last_signal_state.clear()
            self.last_trade_state.clear()
            self.state_reset_date = now.strftime("%Y-%m-%d")

    def evaluate(self, code, name, df, holding, daily_ctx=None) -> tuple:
        """主入口：返回 (buy_score, sell_score, Signal|None)"""
        self._check_date_reset()
        now = _engine_now()
        p = self._get_params(code)
        daily_ctx = daily_ctx or {}
        feats = FeatureExtractor.extract_all(code, name, df, holding, daily_ctx)
        if not feats:
            return 0.0, 0.0, None

        self._last_feats[code] = feats
        buy_score, buy_details = ScoringEngine.calc_buy_score(feats, self.factor_weights)
        sell_score, sell_details = ScoringEngine.calc_sell_score(feats, self.factor_weights)
        risk = RiskManager.check_all(feats)
        sig = None

        if risk.get("blocked"):
            self.last_decision[code] = {"action": "HOLD", "reason": risk.get("reason", "blocked")}
            return buy_score, sell_score, None

        t_val = feats.get("t_val", 0)
        sell_blocks = risk.get("sell_block", [])
        buy_blocks = risk.get("buy_block", [])

        # 卖出判定
        sell_threshold = float(p.get("notify_sell_threshold", 65))
        sell_allowed = not sell_blocks
        sell_cooldown_ok = code not in self.sell_cooldown or now >= self.sell_cooldown.get(code, now)
        max_sells = int(p.get("max_sell_times_per_stock", 3))
        sell_count_ok = self.sell_count_per_stock.get(code, 0) < max_sells

        if sell_score >= sell_threshold and sell_allowed and sell_cooldown_ok and sell_count_ok:
            action = "SELL_HIGH"
            sig = Signal(code=code, name=name, action=action,
                         price=feats.get("price", 0), score=sell_score,
                         reasons=["评分达标"],
                         indicators={"vwap": feats.get("vwap", 0),
                                     "market_state": "normal",
                                     "today_ret": feats.get("today_ret", 0)},
                         details=sell_details)

        # 买入判定（卖出优先）
        if sig is None:
            buy_threshold = float(p.get("notify_buy_threshold", 68))
            if t_val >= 1000:
                buy_threshold = float(p.get("notify_buy_threshold", 68))
            buy_allowed = not buy_blocks
            buy_cooldown_ok = code not in self.buy_cooldown or now >= self.buy_cooldown.get(code, now)
            max_buys = int(p.get("max_buy_times_per_stock", 3))
            buy_count_ok = self.buy_count_per_stock.get(code, 0) < max_buys

            if buy_score >= buy_threshold and buy_allowed and buy_cooldown_ok and buy_count_ok:
                action = "BUY_LOW"
                sig = Signal(code=code, name=name, action=action,
                             price=feats.get("price", 0), score=buy_score,
                             reasons=["评分达标"],
                             indicators={"vwap": feats.get("vwap", 0),
                                         "market_state": "normal",
                                         "today_ret": feats.get("today_ret", 0)},
                             details=buy_details)

        if sig:
            self.signals.append(sig)

        self.last_decision[code] = {
            "action": sig.action if sig else "HOLD",
            "reason": "信号触发" if sig else "无信号",
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_blocks": buy_blocks,
            "sell_blocks": sell_blocks,
        }
        return buy_score, sell_score, sig

    def record_signal(self, code, action, price, score):
        self.last_signal_state[code] = {
            "action": action, "price": price, "score": score,
            "time": _engine_now(),
        }

    def record_trade_action(self, code, action, qty=0, price=0.0):
        now = _engine_now()
        p = self._get_params(code)
        self.last_trade_state[code] = {
            "action": action, "qty": qty, "price": price, "time": now,
        }
        if action in ("SELL_HIGH", "PANIC_SELL"):
            cd = int(p.get("cooldown_minutes", 30))
            self.sell_cooldown[code] = now + timedelta(minutes=cd)
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
        elif action in ("BUY_LOW", "ADD_POS"):
            cd = int(p.get("cooldown_minutes", 30))
            self.buy_cooldown[code] = now + timedelta(minutes=cd)
            self.buy_count_per_stock[code] = self.buy_count_per_stock.get(code, 0) + 1
