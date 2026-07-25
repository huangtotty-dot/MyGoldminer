# coding=utf-8
"""
main.py — 掘金量化策略入口（V2.0 融合版）

架构：
  从 E:\06_T 移植信号引擎和风控模块，使用标准 Python import。
  大盘态势模块通过 gm.api 预取数据注入，完全移除 akshare 依赖。

融合模块：
  - config/params.py: 全部参数
  - data/indicators.py: 技术指标计算
  - analysis/index_regime.py: 大盘态势判定（gm.api 数据）
  - analysis/market_regime.py: 个股市场状态
  - signal/engine.py: 信号评分引擎
  - signal/position_sizer.py: 动态仓位
  - utils/helpers.py: 工具函数
"""

from __future__ import print_function, absolute_import, division
from gm.api import *
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
import os
import sys

# ── 确保项目根在 sys.path ──
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from config.params import PARAMS, STOCK_PARAMS
from data.indicators import add_indicators, clean_code
from signal.engine import SignalEngine, SIM_NOW as SE_SIM_NOW
from signal.position_sizer import PositionSizer
from utils.helpers import SIM_NOW, _now, get_today_str, _default_daily_context

# ── 标的映射 ──
# 华工科技: 深交所 SZSE.000988
STOCKS = {
    "000988": "SZSE.000988",
}
STOCK_NAMES = {"000988": "华工科技"}
REVERSE_MAP = {v: k for k, v in STOCKS.items()}

COMMISSION = PARAMS["commission_rate"]
MIN_BARS = 25
TRADE_QTY = 300  # 每次回转交易数量


def _raw_code(symbol: str) -> str:
    return symbol.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


def _build_bar_df(context, code: str, gm_symbol: str) -> pd.DataFrame:
    """从累积 bar 构建完整技术指标 DataFrame"""
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    df = add_indicators(df)
    return df


def _get_holding(context, code: str, gm_symbol: str) -> dict:
    """手动跟踪持仓（gm.api get_position 对 SZSE 回测不可靠）

    策略：
      1. context.manual_position（即时跟踪，下完单就更新）
      2. context.account().positions()（优先，仅当有数据时）
      3. context.executed_orders（on_order_status 回调更新）
    """
    default = {"name": STOCK_NAMES.get(code, code), "qty": 0,
               "available": 0, "t_qty": 0, "cost": 0,
               "type": "stock", "pre_close": 0}
    # 1. 手动跟踪（最可靠，立即更新）
    mp = context.manual_position.get(gm_symbol)
    if mp and int(mp.get("qty", 0) or 0) > 0:
        return mp
    # 2. gm.api 持仓接口
    try:
        pos = context.account().positions(symbol=gm_symbol, side=PositionSide_Long)
        if pos and len(pos) > 0:
            p = pos[0]
            return {
                "name": STOCK_NAMES.get(code, code),
                "qty": int(p.volume),
                "available": int(p.available),
                "t_qty": int(p.volume),
                "cost": float(p.vwap or 0),
                "type": "stock",
                "pre_close": float(p.vwap or 0),
            }
    except Exception:
        pass
    # 3. 已成交订单
    if gm_symbol in context.executed_orders:
        return context.executed_orders[gm_symbol]
    return default


# ── 缓存上下文构建 ──

def _build_daily_ctx(index_regime_value: str = "range", score: float = 0.0) -> dict:
    """构建 daily_ctx（供 SignalEngine.evaluate 使用）"""
    return {
        "daily_status": "ok",
        "daily_buy_t_ok": True,
        "daily_gate": "neutral",
        "daily_trend_bg": "unknown",
        "daily_ma5_state": "above_ma5_trend",
        "daily_support_name": "",
        "daily_breakdown_risk": False,
        "daily_overheated": False,
        "daily_pullback_support": False,
        "index_regime": index_regime_value,
        "index_regime_status": "normal",
        "index_circuit_state": "normal",
        "index_gate_advice": "normal_t",
        "index_pos_factor": 1.0,
        "daily_ma5": 0,
        "daily_ma10": 0,
        "daily_ma20": 0,
        "daily_prev_close": 0,
        "daily_day_ret": 0,
        "intraday_alerts": [],
        "daily_context_version": "gm_backtest_v2",
    }


# ==================== 策略生命周期 ====================

def init(context):
    """策略初始化

    1. 订阅标的分钟数据
    2. 预取上证指数日线（用于大盘态势判定）
    3. 初始化引擎
    """
    context.bar_cache = {}  # gm_symbol -> list of bar dicts
    context.executed_orders = {}  # gm_symbol -> holding dict
    context.engine = SignalEngine()
    context.daily_buy_count = {}
    context.daily_sell_count = {}
    context.daily_trade_price = {}
    context.last_index_regime = "range"
    context.last_index_score = 0.0
    # 手动跟踪持仓（绕开 gm.api get_position() SZSE 兼容问题）
    context.manual_position = {}  # gm_symbol -> {"qty": int, "cost": float, ...}

    # 预取回测全量历史分钟数据（240根 = 4小时，够一天）
    for code, sym in STOCKS.items():
        try:
            his = history_n(symbol=sym, frequency="60s", count=240,
                           fields="symbol,eob,open,high,low,close,volume,amount",
                           fill_missing="Previous", adjust=ADJUST_PREV)
            if his is not None and len(his) > 0:
                rows = []
                for bar in his:
                    rows.append({
                        "time": str(bar["eob"]),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                        "amount": float(bar["amount"]) if bar["amount"] is not None else 0,
                    })
                context.bar_cache[sym] = rows
        except Exception as e:
            print(f"[init] 历史数据预取失败 {sym}: {e}")

    # 预取上证指数日线 → 注入大盘态势模块
    import analysis.index_regime as ir
    try:
        idx_data = history_n(symbol="SHSE.000001", frequency="1d", count=900,
                            fields="eob,open,high,low,close,volume",
                            fill_missing="Previous")
        if idx_data is not None and len(idx_data) > 10:
            rows = []
            for bar in idx_data:
                dt = str(bar["eob"])[:10]
                rows.append({
                    "date": dt,
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                })
            df_idx = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            ir.GM_INDEX_CACHE["SHSE.000001"] = df_idx
            ir.GM_DATA_READY = True
            print(f"[init] 大盘日线已缓存: {len(df_idx)} 行, {df_idx['date'].iloc[0]} ~ {df_idx['date'].iloc[-1]}")
        else:
            print(f"[init] 警告: history_n 未返回上证指数日线数据")
    except Exception as e:
        print(f"[init] 大盘日线预取失败: {e}")
        ir.GM_DATA_READY = False

    # 订阅实时分钟数据
    symbols = list(STOCKS.values())
    subscribe(symbols=symbols, frequency="60s", count=240,
              fields="symbol,eob,open,high,low,close,volume,amount")
    print(f"[init] 策略初始化完成: {len(symbols)} 只标的, 上证指数大盘态势已启用")


def on_bar(context, bars):
    """每分钟 bar 回调：信号引擎 + 交易执行"""
    # 注入回测时间到引擎和工具模块
    now = context.now if hasattr(context, "now") else datetime.now()
    import utils.helpers as uh
    uh.SIM_NOW = now
    import signal.engine as se
    se.SIM_NOW = now

    t = now.time()
    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    # 每5分钟更新一次大盘态势（避免每次 on_bar 都算）
    _minute_key = now.hour * 100 + now.minute
    if not hasattr(context, "_last_ir_minute") or context._last_ir_minute != _minute_key:
        context._last_ir_minute = _minute_key
        try:
            import analysis.index_regime as ir
            if ir.GM_DATA_READY:
                ir_regime, ir_score, ir_ctx = ir.detect_index_regime(
                    as_of=now.strftime("%Y-%m-%d"), mode="eod")
                context.last_index_regime = ir_regime.value if hasattr(ir_regime, "value") else str(ir_regime)
                context.last_index_score = float(ir_score)
        except Exception as e:
            pass

    for bar in bars:
        gm_sym = str(bar["symbol"])
        code = _raw_code(gm_sym)
        if code not in STOCKS:
            continue

        # 累积 bar
        row = {
            "time": str(bar["eob"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
            "amount": float(bar["amount"]) if bar["amount"] is not None else 0,
        }
        context.bar_cache.setdefault(gm_sym, []).append(row)
        if len(context.bar_cache[gm_sym]) > 480:
            context.bar_cache[gm_sym] = context.bar_cache[gm_sym][-480:]

        df = _build_bar_df(context, code, gm_sym)
        if df.empty:
            continue

        cp = float(bar["close"])

        # ---- 建底仓（首日首根有效 bar 执行，仅一次） ----
        settle_key = f"{code}_base_settled"
        if not hasattr(context, "_base_settled"):
            context._base_settled = {}
        if not context._base_settled.get(code):
            context._base_settled[code] = True
            total_shares = 5000
            try:
                order_volume(symbol=gm_sym, volume=total_shares,
                             side=OrderSide_Buy,
                             order_type=OrderType_Market,
                             position_effect=PositionEffect_Open)
                print(f"[{now:%H:%M:%S}] BASE {code} 建底仓 {total_shares}股@{cp:.2f}")
                # 立即更新手动持仓
                context.manual_position[gm_sym] = {
                    "name": STOCK_NAMES.get(code, code),
                    "qty": total_shares,
                    "available": total_shares,
                    "t_qty": total_shares,
                    "cost": cp,
                    "type": "stock",
                    "pre_close": cp,
                }
            except Exception as e:
                print(f"[{now:%H:%M:%S}] BASE {code} 建底仓失败: {e}")
            return  # 建仓后跳过本次信号处理

        # 构建持仓信息（手动跟踪）
        holding = _get_holding(context, code, gm_sym)
        pos_qty = int(holding.get("qty", 0) or 0)
        if pos_qty <= 0:
            # 底仓已下单但尚未成交，跳过本 bar
            return

        # 构建 daily_ctx
        daily_ctx = _build_daily_ctx(context.last_index_regime, context.last_index_score)

        # 信号评分
        try:
            buy_score, sell_score, sig = context.engine.evaluate(
                code, STOCK_NAMES.get(code, code), df, holding, daily_ctx)
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {code} evaluate err: {e}")
            continue

        if sig is None:
            continue

        # 阈值过滤
        stock_params = STOCK_PARAMS.get(code, {})
        if sig.action in ("BUY_LOW", "ADD_POS"):
            thresh = stock_params.get("notify_buy_threshold", PARAMS.get("notify_buy_threshold", 68))
        elif t >= dtime(10, 0):
            thresh = stock_params.get("notify_sell_threshold", PARAMS.get("notify_sell_threshold", 65))
        else:
            thresh = stock_params.get("notify_sell_early_threshold", PARAMS.get("notify_sell_early_threshold", 75))

        if sig.score < thresh:
            continue

        # 执行交易
        if sig.action in ("BUY_LOW", "ADD_POS"):
            bc = context.daily_buy_count.get(code, 0)
            max_buys = stock_params.get("max_buy_times_per_stock", 3)
            if bc >= max_buys:
                continue
            # 计算买入量
            qty = TRADE_QTY  # 简化：固定300股
            order_volume(symbol=gm_sym, volume=qty,
                         side=OrderSide_Buy,
                         order_type=OrderType_Market,
                         position_effect=PositionEffect_Open)
            context.daily_buy_count[code] = bc + 1
            context.daily_trade_price[code] = cp
            # 手动跟踪
            old = context.manual_position.get(gm_sym, {"qty": 0, "cost": cp})
            old_q = int(old.get("qty", 0))
            old_c = float(old.get("cost", cp))
            new_q = old_q + qty
            new_c = (old_c * old_q + cp * qty) / new_q if new_q > 0 else cp
            context.manual_position[gm_sym] = dict(old, **{"qty": new_q, "available": new_q, "t_qty": new_q, "cost": new_c})
            print(f"[{now:%H:%M:%S}] BUY {code} {qty}@{cp:.2f} 评分={sig.score:.0f} 指数={context.last_index_regime}")

        elif sig.action in ("SELL_HIGH", "PANIC_SELL"):
            sc = context.daily_sell_count.get(code, 0)
            max_sells = stock_params.get("max_sell_times_per_stock", 3)
            if sc >= max_sells:
                continue
            pos_qty = int(holding.get("qty", 0) or 0)
            if pos_qty < 100:
                continue
            qty = min(TRADE_QTY, pos_qty)
            order_volume(symbol=gm_sym, volume=qty,
                         side=OrderSide_Sell,
                         order_type=OrderType_Market,
                         position_effect=PositionEffect_Close)
            context.daily_sell_count[code] = sc + 1
            # 手动跟踪
            new_pos = max(0, pos_qty - qty)
            if gm_sym in context.manual_position:
                context.manual_position[gm_sym]["qty"] = new_pos
                context.manual_position[gm_sym]["available"] = new_pos
                context.manual_position[gm_sym]["t_qty"] = new_pos
            print(f"[{now:%H:%M:%S}] SELL {code} {qty}@{cp:.2f} 评分={sig.score:.0f} 指数={context.last_index_regime}")


def on_order_status(context, order):
    """订单状态回调，跟踪已成交订单的持仓"""
    symbol = order["symbol"]
    status = order["status"]
    volume = order["volume"]
    price = order["price"]
    side = order["side"]
    effect = order["position_effect"]
    order_type = order["order_type"]

    if status == 3:  # 全部成交
        code = _raw_code(symbol)
        if side == 1:  # 买入
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0, "cost": price})
            old_qty = int(old.get("qty", 0))
            old_cost = float(old.get("cost", price))
            new_qty = old_qty + volume
            new_cost = (old_cost * old_qty + price * volume) / new_qty if new_qty > 0 else price
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                "available": new_qty,
                "t_qty": new_qty,
                "cost": new_cost,
                "type": "stock",
                "pre_close": price,
            }
        elif side == 2:  # 卖出
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0})
            old_qty = int(old.get("qty", 0))
            new_qty = max(0, old_qty - volume)
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                "available": new_qty,
                "t_qty": new_qty,
                "cost": price,
                "type": "stock",
                "pre_close": price,
            }


def on_backtest_finished(context, indicator):
    print("*" * 50)
    print("回测已完成")
    if isinstance(indicator, dict):
        for k, v in sorted(indicator.items()):
            try:
                print(f"  {k}: {v}")
            except Exception:
                print(f"  {k}: {v}")
    else:
        print(f"  indicator: {indicator}")
    print("*" * 50)


if __name__ == "__main__":
    backtest_start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    backtest_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
        filename="main.py",
        mode=MODE_BACKTEST,
        token="480a6c84b0f43417ffcc9c15162dd7256ca9c3b0",
        backtest_start_time=backtest_start,
        backtest_end_time=backtest_end,
        backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=200000,
        backtest_commission_ratio=COMMISSION,
        backtest_slippage_ratio=0.0001,
        backtest_match_mode=1)
