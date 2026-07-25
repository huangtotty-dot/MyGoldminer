# coding=utf-8
"""
main.py — 掘金量化策略入口（v1.1.0 WIP）
"""

from __future__ import print_function, absolute_import, division
from gm.api import *
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
import os
import sys
import json

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from config.params import PARAMS, STOCK_PARAMS
from data.indicators import add_indicators, clean_code
from signal.engine import SignalEngine, SIM_NOW as SE_SIM_NOW
from signal.position_sizer import PositionSizer
from utils.helpers import SIM_NOW, _now, get_today_str, _default_daily_context

# ── 标的 ──
STOCKS = {"000988": "SZSE.000988"}
STOCK_NAMES = {"000988": "华工科技"}
REVERSE_MAP = {v: k for k, v in STOCKS.items()}
COMMISSION = PARAMS["commission_rate"]
MIN_BARS = 25
BASE_POSITION_PCT = 0.50           # 底仓占用资金比例
T1_AUTO_UNLOCK_HOUR = 9
T1_AUTO_UNLOCK_MINUTE = 31
INITIAL_CASH = 200000


def _raw_code(symbol: str) -> str:
    return symbol.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


def _build_bar_df(context, code: str, gm_symbol: str) -> pd.DataFrame:
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    df = add_indicators(df)
    return df


def _calc_base_qty(cash: float, price: float) -> int:
    """按资金反推算底仓股数（100 的倍数）"""
    if price <= 0:
        return 0
    raw_qty = cash * BASE_POSITION_PCT / price
    qty = int(raw_qty // 100) * 100
    return max(qty, 100)


def _get_holding(context, code: str, gm_symbol: str) -> dict:
    """多源持仓读取，按优先级：

    1. context.manual_position（即时缓存）
    2. context.account().positions()（优先，每30分钟对账）
    3. context.executed_orders（回退）
    """
    default = {"name": STOCK_NAMES.get(code, code), "qty": 0,
               "available": 0, "t_qty": 0, "cost": 0,
               "type": "stock", "pre_close": 0}
    now = _now()

    # 1. 每30分钟跟 gm.api positions 对账一次
    reconcile_interval = 1800
    last_rec = getattr(context, "_last_position_reconcile", None)
    if last_rec is None or (now - last_rec).total_seconds() > reconcile_interval:
        context._last_position_reconcile = now
        try:
            pos = context.account().positions(symbol=gm_symbol, side=PositionSide_Long)
            if pos and len(pos) > 0:
                p = pos[0]
                gm_pos = {
                    "name": STOCK_NAMES.get(code, code),
                    "qty": int(p.volume),
                    "available": int(p.available),
                    "t_qty": int(p.volume),
                    "cost": float(p.vwap or 0),
                    "type": "stock",
                    "pre_close": float(p.vwap or context.latest_pre_close.get(code, 0)),
                }
                mp = context.manual_position.get(gm_symbol)
                if mp and abs(int(mp.get("qty", 0)) - int(p.volume)) > 0:
                    context.manual_position[gm_symbol] = gm_pos
                return gm_pos
        except Exception:
            pass

    # 2. manual_position
    mp = context.manual_position.get(gm_symbol)
    if mp and int(mp.get("qty", 0) or 0) > 0:
        return mp

    # 3. executed_orders
    if gm_symbol in context.executed_orders:
        return context.executed_orders[gm_symbol]
    return default


# ── 交易日线上下文构建 ──

def _refresh_daily_ctx(context, code: str, gm_symbol: str, now: datetime) -> dict:
    """用 history_n 拉标的日线，计算 daily_ctx 供信号引擎用。

    每日 09:31 触发一次（或首次调用时），结果缓存到 context.daily_ctx_cache。
    关键：显式传 end_time=昨日收盘，避免当日未收盘数据污染。
    """
    today_str = now.strftime("%Y-%m-%d")
    if getattr(context, "_daily_ctx_date", None) == today_str and hasattr(context, "daily_ctx_cache"):
        return context.daily_ctx_cache

    # 取 120 个交易日日线
    try:
        end_of_yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d") + " 15:00:00"
        daily = history_n(symbol=gm_symbol, frequency="1d", count=120,
                          fields="eob,open,high,low,close,volume",
                          fill_missing="Previous", adjust=ADJUST_PREV,
                          end_time=end_of_yesterday)
    except Exception:
        daily = None

    ctx = dict(_default_daily_context(code))

    if daily is not None and len(daily) >= 10:
        df = pd.DataFrame(daily)
        c = df["close"].astype(float)
        ctx["daily_ma5"] = float(c.rolling(5).mean().iloc[-1]) if len(c) >= 5 else 0
        ctx["daily_ma10"] = float(c.rolling(10).mean().iloc[-1]) if len(c) >= 10 else 0
        ctx["daily_ma20"] = float(c.rolling(20).mean().iloc[-1]) if len(c) >= 20 else 0
        prev_close = float(c.iloc[-1]) if len(c) > 0 else 0
        ctx["daily_prev_close"] = prev_close
        ctx["daily_status"] = "ok"
        ctx["daily_buy_t_ok"] = True

        # 破位/过热简化判断
        if len(c) >= 20:
            ma20 = c.rolling(20).mean().iloc[-1]
            if prev_close < ma20 * 0.985:
                ctx["daily_breakdown_risk"] = True
            if prev_close > ma20 * 1.08:
                ctx["daily_overheated"] = True
            ctx["daily_ma5_state"] = "above_ma5_trend" if prev_close > ctx["daily_ma5"] else "near_ma5_chop"

        # 将 latest_pre_close 暴露给 _get_holding
        context.latest_pre_close[code] = prev_close
    else:
        context.latest_pre_close[code] = 0
        ctx["daily_status"] = "unavailable"

    context.daily_ctx_cache = ctx
    context._daily_ctx_date = today_str
    return ctx


# ── 审计 JSONL ──

_AUDIT_LOG_PATH = os.path.join(PROJECT_DIR, "gmcache", "backtrace.jsonl")
_audit_file = None

def _audit_write(rec: dict):
    """追加一条决策审计记录"""
    global _audit_file
    try:
        if _audit_file is None:
            os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
            _audit_file = open(_AUDIT_LOG_PATH, "a", encoding="utf-8")
        _audit_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _audit_file.flush()
    except Exception:
        pass

def _audit_close():
    global _audit_file
    if _audit_file:
        _audit_file.close()
        _audit_file = None


# ==================== 策略生命周期 ====================

def init(context):
    context.bar_cache = {}
    context.executed_orders = {}
    context.engine = SignalEngine()
    context.daily_buy_count = {}
    context.daily_sell_count = {}
    context.daily_trade_price = {}
    context.last_index_regime = "range"
    context.last_index_score = 0.0
    context.manual_position = {}
    context.latest_pre_close = {}
    context._base_ordered = set()
    context._base_settled = set()
    context.cur_date = None
    context.total_trade_cost = 0.0
    context.total_trade_count = 0
    context.rejected_order_count = 0
    context.audit_records = []
    context.sizer = PositionSizer(params=PARAMS)

    # 预取分钟数据
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
            print(f"[init] 历史分钟数据预取失败 {sym}: {e}")

    # 预取上证指数日线
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
            print(f"[init] 警告: history_n 未返回上证指数日线")
    except Exception as e:
        print(f"[init] 大盘日线预取失败: {e}")
        ir.GM_DATA_READY = False

    symbols = list(STOCKS.values())
    subscribe(symbols=symbols, frequency="60s", count=240,
              fields="symbol,eob,open,high,low,close,volume,amount")
    print(f"[init] 策略初始化完成: {len(symbols)} 只标的")


def on_bar(context, bars):
    now = context.now if hasattr(context, "now") else datetime.now()
    import utils.helpers as uh
    uh.SIM_NOW = now
    import signal.engine as se
    se.SIM_NOW = now

    t = now.time()
    today = now.date()

    # ── D1: 按日重置 ──
    if context.cur_date is None or context.cur_date != today:
        context.cur_date = today
        context.daily_buy_count.clear()
        context.daily_sell_count.clear()
        context.daily_trade_price.clear()
        context.engine._check_date_reset()
        _audit_write({"event": "date_reset", "date": str(today)})

    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    # ── D4: 大盘态势（每交易日一次） ──
    if today != getattr(context, "_last_ir_date", None):
        context._last_ir_date = today
        try:
            import analysis.index_regime as ir
            if ir.GM_DATA_READY:
                # 每个交易日重新拉指数日线（回测时钟下自动对齐）
                try:
                    idx_data = history_n(symbol="SHSE.000001", frequency="1d", count=900,
                                        fields="eob,open,high,low,close,volume",
                                        fill_missing="Previous")
                    if idx_data is not None and len(idx_data) > 10:
                        rows = []
                        for bar in idx_data:
                            rows.append({
                                "date": str(bar["eob"])[:10],
                                "open": float(bar["open"]),
                                "high": float(bar["high"]),
                                "low": float(bar["low"]),
                                "close": float(bar["close"]),
                                "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                            })
                        df_idx = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
                        ir.GM_INDEX_CACHE["SHSE.000001"] = df_idx
                        ir.GM_DATA_READY = True
                except Exception as e:
                    print(f"[ir] 指数日线刷新失败: {e}")

                ir_regime, ir_score, ir_ctx = ir.detect_index_regime(
                    as_of=now.strftime("%Y-%m-%d"), force=True, mode="eod")
                context.last_index_regime = ir_regime.value if hasattr(ir_regime, "value") else str(ir_regime)
                context.last_index_score = float(ir_score)
                degraded = ir_ctx.get("degraded", [])
                if degraded:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f} degraded={degraded}")
                else:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f}")
        except Exception as e:
            print(f"[ir] 大盘态势判定失败: {e}")

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

        # ── D6: VWAP 单位验证（首次 bar 打一行） ──
        if not getattr(context, "_vwap_checked", False) and row["amount"] > 0 and row["volume"] > 0:
            ratio = row["amount"] / row["volume"]
            print(f"[D6] amount/volume={ratio:.2f} close={cp:.2f} → VWAP单位比={ratio/cp:.4f}")
            if ratio / cp < 0.1:
                print(f"[D6] 结论: volume单位为股，需移除×100")
            context._vwap_checked = True

        # ── D2: 底仓（按资金反推） ──
        if code not in context._base_ordered and code not in context._base_settled:
            # 尚未下单，计算底仓
            # 初始资金用初始化参数
            base_qty = _calc_base_qty(INITIAL_CASH, cp)
            if base_qty < 100:
                base_qty = 100
            try:
                order_volume(symbol=gm_sym, volume=base_qty,
                             side=OrderSide_Buy,
                             order_type=OrderType_Market,
                             position_effect=PositionEffect_Open)
                context._base_ordered.add(code)
                print(f"[{now:%H:%M:%S}] BASE {code} 下单 {base_qty}股@{cp:.2f} (资金=200000, 价格={cp:.2f})")
                _audit_write({"event": "base_order", "code": code, "qty": base_qty, "price": cp, "time": str(now)})
            except Exception as e:
                print(f"[{now:%H:%M:%S}] BASE {code} 下单失败: {e}")
            return

        if code not in context._base_settled and code in context._base_ordered:
            # 已下单未成交，跳过
            return

        # ── 日线上下文刷新（每日首根有效 bar） ──
        daily_ctx = _refresh_daily_ctx(context, code, gm_sym, now)
        # 注入指数态势
        daily_ctx["index_circuit_state"] = "clear" if context.last_index_regime == "uni_down" else "normal"
        daily_ctx["index_gate_advice"] = "defensive_t" if context.last_index_regime == "uni_down" else "normal_t"

        # 持仓读取
        holding = _get_holding(context, code, gm_sym)
        pos_qty = int(holding.get("qty", 0) or 0)
        if pos_qty <= 0:
            return

        # T+1 结转
        if now.hour == T1_AUTO_UNLOCK_HOUR and now.minute == T1_AUTO_UNLOCK_MINUTE:
            mp = context.manual_position.get(gm_sym)
            if mp:
                mp["available"] = mp.get("qty", 0)
                mp["t_qty"] = mp.get("qty", 0)

        # 补上 today_ret
        prev_close = float(daily_ctx.get("daily_prev_close", 0) or 0)
        if prev_close > 0:
            daily_ctx["daily_day_ret"] = (cp - prev_close) / prev_close

        # ── 信号引擎 ──
        try:
            buy_score, sell_score, sig = context.engine.evaluate(
                code, STOCK_NAMES.get(code, code), df, holding, daily_ctx)
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {code} evaluate err: {e}")
            continue

        # ── D5: uni_down 熔断（硬性防线，不再依赖 RiskManager 链路） ──
        if context.last_index_regime == "uni_down" and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── D5: 尾盘回转（14:50-15:00，先于 PANIC_SELL 检查） ──
        is_tail = now.hour == 14 and now.minute >= 50
        if is_tail and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── D5: 深度亏损 → PANIC_SELL ──
        feats_cache = getattr(context.engine, "_last_feats", {}).get(code, {})
        if feats_cache.get("is_deep_loss") and sell_score < 40:
            sell_score = 40
            sig = None  # 重新生成 panic sell
            from data.indicators import Signal
            sig = Signal(code=code, name=STOCK_NAMES.get(code, code),
                         action="PANIC_SELL", price=cp, score=65.0,
                         reasons=["深度亏损恐慌卖出", f"profit_pct={feats_cache.get('profit_pct', 0):.2%}"])

        if sig is None:
            _audit_write({
                "event": "no_signal", "code": code, "time": str(now),
                "buy_score": buy_score, "sell_score": sell_score,
                "pos_qty": pos_qty, "price": cp,
                "index_regime": context.last_index_regime,
            })
            continue

        # ── D5: 卖出地板保护 ──
        stock_params = STOCK_PARAMS.get(code, {})
        max_sells = stock_params.get("max_sell_times_per_stock", 3)
        max_buys = stock_params.get("max_buy_times_per_stock", 3)

        if sig.action in ("SELL_HIGH", "PANIC_SELL"):
            sell_floor_ratio = float(PARAMS.get("sell_floor_ratio", 0.5))
            # base_qty 跟踪（首次有持仓时记录）
            base_ref = getattr(context, f"_base_ref_{code}", pos_qty)
            setattr(context, f"_base_ref_{code}", base_ref)
            min_hold = int(base_ref * sell_floor_ratio)
            if pos_qty - 100 < min_hold:
                print(f"[{now:%H:%M:%S}] SELL {code} SKIP: 地板保护 base_ref={base_ref} min_hold={min_hold} pos_qty={pos_qty}")
                continue

        # ── D1: 引擎冷却/计数 ──
        threshold = stock_params.get("notify_sell_threshold", 65) if sig.action in ("SELL_HIGH", "PANIC_SELL") else \
                    stock_params.get("notify_buy_threshold", 43)

        if sig.score < threshold:
            continue

        # 执行交易
        if sig.action in ("BUY_LOW", "ADD_POS"):
            bc = context.daily_buy_count.get(code, 0)
            if bc >= max_buys:
                continue
            qty = context.sizer.calc_buy_qty(code, holding, sig.score, threshold)
            if qty < 100:
                qty = 300  # fallback
            try:
                order_volume(symbol=gm_sym, volume=qty,
                             side=OrderSide_Buy,
                             order_type=OrderType_Market,
                             position_effect=PositionEffect_Open)
                context.daily_buy_count[code] = bc + 1
                context.daily_trade_price[code] = cp
                context.total_trade_count += 1
                # 手动跟踪买入（T+1: 只加 qty，不加 available）
                old = context.manual_position.get(gm_sym, {"qty": 0, "available": 0, "cost": cp, "t_qty": 0})
                old_q = int(old.get("qty", 0))
                old_c = float(old.get("cost", cp))
                new_q = old_q + qty
                new_c = (old_c * old_q + cp * qty) / new_q if new_q > 0 else cp
                context.manual_position[gm_sym] = dict(old, **{"qty": new_q, "t_qty": new_q, "cost": new_c})
                context.engine.buy_count_per_stock[code] = context.daily_buy_count.get(code, 0)
                print(f"[{now:%H:%M:%S}] BUY {code} {qty}@{cp:.2f} score={sig.score:.0f} regime={context.last_index_regime}")
                _audit_write({"event": "buy", "code": code, "qty": qty, "price": cp, "score": sig.score,
                              "time": str(now), "regime": context.last_index_regime,
                              "pos_after_buy": new_q, "buy_count": bc + 1})
            except Exception as e:
                print(f"[{now:%H:%M:%S}] BUY {code} 失败: {e}")

        elif sig.action in ("SELL_HIGH", "PANIC_SELL"):
            sc = context.daily_sell_count.get(code, 0)
            if sc >= max_sells:
                continue
            qty = context.sizer.calc_sell_qty(code, holding, sig.score, threshold, used_sells=sc)
            if qty < 100:
                qty = min(300, pos_qty)
            qty = min(qty, pos_qty)
            if qty < 100:
                continue
            try:
                order_volume(symbol=gm_sym, volume=qty,
                             side=OrderSide_Sell,
                             order_type=OrderType_Market,
                             position_effect=PositionEffect_Close)
                context.daily_sell_count[code] = sc + 1
                context.total_trade_count += 1
                new_pos = max(0, pos_qty - qty)
                if gm_sym in context.manual_position:
                    context.manual_position[gm_sym]["qty"] = new_pos
                    context.manual_position[gm_sym]["available"] = new_pos
                    context.manual_position[gm_sym]["t_qty"] = new_pos
                context.engine.sell_count_per_stock[code] = context.daily_sell_count.get(code, 0)
                print(f"[{now:%H:%M:%S}] SELL {code} {qty}@{cp:.2f} score={sig.score:.0f} regime={context.last_index_regime}")
                _audit_write({"event": "sell", "code": code, "qty": qty, "price": cp, "score": sig.score,
                              "time": str(now), "regime": context.last_index_regime,
                              "pos_after_sell": new_pos, "sell_count": sc + 1})
            except Exception as e:
                print(f"[{now:%H:%M:%S}] SELL {code} 失败: {e}")


def on_order_status(context, order):
    symbol = order["symbol"]
    status = order["status"]
    volume = order["volume"]
    price = order["price"]
    side = order["side"]

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
            # 底仓确认
            if code in context._base_ordered and code not in context._base_settled:
                context._base_settled.add(code)
                context.manual_position[symbol] = dict(context.executed_orders[symbol])
                print(f"[BASE] {code} 底仓成交 {volume}股@{price:.2f}")
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
    elif status in (4, 5, 6):  # 拒单/撤单/部分成交撤单
        context.rejected_order_count = getattr(context, "rejected_order_count", 0) + 1
        print(f"[ORDER] {symbol} 被拒 status={status}")


def on_backtest_finished(context, indicator):
    _audit_close()
    print("*" * 50)
    print("回测已完成")
    if isinstance(indicator, dict):
        for k, v in sorted(indicator.items()):
            try:
                print(f"  {k}: {v}")
            except Exception:
                print(f"  {k}: {v}")
    print(f"  手动统计: 成交笔数={getattr(context, 'total_trade_count', 0)}")
    print(f"  拒单笔数={getattr(context, 'rejected_order_count', 0)}")
    print("*" * 50)


if __name__ == "__main__":
    backtest_start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")  # 90天 ~4-7min
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
