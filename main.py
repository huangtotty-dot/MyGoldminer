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
from signals.engine import SignalEngine, SIM_NOW as SE_SIM_NOW
from signals.position_sizer import PositionSizer
from utils.helpers import SIM_NOW, _now, get_today_str, _default_daily_context
from gm_bridge.writer import (
    write_signal, write_order, write_fill, write_reject, write_risk,
    write_heartbeat, check_kill_switch,
)

# ── 标的 ──
# 代码 → gm_symbol 映射
STOCKS = {
    "000988": "SZSE.000988",
    "600481": "SHSE.600481",
    "600176": "SHSE.600176",
    "603667": "SHSE.603667",
}
STOCK_NAMES = {
    "000988": "华工科技",
    "600481": "双良节能",
    "600176": "中国巨石",
    "603667": "五洲新春",
}
REVERSE_MAP = {v: k for k, v in STOCKS.items()}

# ── 镜像持仓（与实盘账户一致） ──
# 模拟盘建仓时按此表中的股数/成本下单
MIRROR_HOLDINGS = {
    # 2026-07-28 owner决策(N2): 事故超配减仓后新基线
    # 2026-07-29 F7返工: MIRROR语义=目标底仓。000988维持500, 缺口200由_base_topup_qty择时回补
    "000988": {"qty": 500,  "cost": 0},
    "600481": {"qty": 1400, "cost": 0},
    "600176": {"qty": 500,  "cost": 0},
    "603667": {"qty": 800,  "cost": 0},
    # 588170 ETF 已移除：T+0机制/最小单位与策略不兼容，首日仅观察
}

COMMISSION = PARAMS["commission_rate"]
MIN_BARS = 25
T1_AUTO_UNLOCK_HOUR = 9
T1_AUTO_UNLOCK_MINUTE = 31
# 镜像持仓总市值约 123,000（500×100.6 + 1400×3.9 + 500×37.6 + 800×50.9，按2026-07-28收盘）
# 按市值×1.5 配置模拟盘资金（留足做T现金水位）
INITIAL_CASH = 150000
MAX_BASE_RETRY = 3


# ═══════════════════════════════════════════
# T4 卖出通道仲裁器
# ═══════════════════════════════════════════
# 优先级: P1 PANIC > P2 TRAIL > P3 TREND_EXIT > P4 TARGET > P5 SELL_HIGH > P6 TAIL
# 当前: P1/P2/P5/P6 四通道，P3/P4 为 Phase B 预留
#   - P1/P2 豁免 sizer 分批与日卖出计数（止血/保护不受节流）
#   - P5/P6 占用计数
def _sell_arbiter(context, code, sig, pos_qty, cp, now, holding, threshold,
                  stock_params, gm_sym):
    """执行卖出：地板检查 + sizer + 下单 + 持仓更新 + 审计。
    返回 True=已执行, False=被拦截/跳过。"""
    sc = context.daily_sell_count.get(code, 0)
    max_sells = stock_params.get("max_sell_times_per_stock", 3)

    # F9: 在途卖单守卫（2026-07-31 PANIC 0.33秒内连发4单、5单超可用持仓事故）
    # 成交回报到达前冷却未生效（P0-2设计），在途期间禁止再发任何卖单
    _inflight = int(getattr(context, "_inflight_sell", {}).get(gm_sym, 0) or 0)
    if _inflight >= 100:
        _audit_write({"event": "inflight_skip", "code": code, "action": sig.action,
                      "inflight": _inflight, "time": str(now)})
        return False

    # 地板检查
    base_ref = getattr(context, f"_base_ref_{code}", pos_qty)
    setattr(context, f"_base_ref_{code}", base_ref)
    _is_protection = sig.action in ("PANIC_SELL", "TRAIL_SELL", "TREND_EXIT")
    sell_floor_ratio = 0.0 if _is_protection else float(PARAMS.get("sell_floor_ratio", 0.5))
    min_hold = int(base_ref * sell_floor_ratio)
    if pos_qty - 100 < min_hold:
        try:
            write_risk(str(now), "floor_protection",
                       f"base_ref={base_ref} min_hold={min_hold} pos_qty={pos_qty} action={sig.action}", code=code)
        except Exception: pass
        if sig.action == "PANIC_SELL":
            context.engine.record_trade_action(code, "PANIC_SELL", 0, cp)
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "floor",
                      "base_ref": base_ref, "min_hold": min_hold, "pos_qty": pos_qty, "time": str(now)})
        return False

    # 阈值检查
    if sig.score < threshold:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "threshold",
                      "threshold": threshold, "time": str(now)})
        return False

    # 日计数检查（保护类卖出豁免）
    if not _is_protection and sc >= max_sells:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "daily_count",
                      "sc": sc, "max_sells": max_sells, "time": str(now)})
        return False

    # sizer 计算卖出量
    qty = context.sizer.calc_sell_qty(code, holding, sig.score, threshold, used_sells=sc)
    if qty < 100:
        qty = min(300, pos_qty)
    # F13: TREND_EXIT 量封顶到超 base_ref 部分——设计语义"只卖利润仓/超额仓，
    # 底仓本体永不触发"(WP-F8)，sizer 定量可能越界(WP-B回放包fix4实证:
    # pos600/base_ref500/excess100 却卖出200→底仓被啃100)
    if sig.action == "TREND_EXIT":
        qty = min(qty, max(0, pos_qty - base_ref))
    # N25-3: T+1 可用量检查 — 区分 None(缺key兜底) 与 0(合法当日全锁)
    _avail_raw = holding.get("available")
    _avail = pos_qty if _avail_raw is None else int(_avail_raw)
    qty = min(qty, pos_qty, _avail)
    # F9: 扣除在途量，委托总量不得超可用持仓
    qty = min(qty, max(0, min(pos_qty, _avail) - _inflight))
    if qty < 100:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "qty",
                      "qty": qty, "pos_qty": pos_qty, "time": str(now)})
        return False

    # 下单 + 持仓更新 + 审计
    try:
        write_order(str(now), code, "SELL", qty, cp)
    except Exception: pass
    try:
        order_volume(symbol=gm_sym, volume=qty,
                     side=OrderSide_Sell,
                     order_type=OrderType_Market,
                     position_effect=PositionEffect_Close)
        # F9: 登记在途量（fill/reject 回调释放）
        if not hasattr(context, "_inflight_sell") or context._inflight_sell is None:
            context._inflight_sell = {}
        context._inflight_sell[gm_sym] = _inflight + qty
        # F9: 下单即计虚冷却——成交回调 record_trade_action 再确认；
        # 拒单不补冷却属保守可接受（当日不再卖，次日 D1 重置）
        try:
            if not hasattr(context.engine, "sell_cooldown") or context.engine.sell_cooldown is None:
                context.engine.sell_cooldown = {}
            _cd = int(context.engine._get_params(code).get("cooldown_minutes", 30))
        except Exception:
            _cd = 30
        context.engine.sell_cooldown[code] = now + timedelta(minutes=_cd)
        context.daily_sell_count[code] = sc + 1
        context.total_trade_count += 1
        new_pos = max(0, pos_qty - qty)
        if gm_sym in context.manual_position:
            context.manual_position[gm_sym]["qty"] = new_pos
            context.manual_position[gm_sym]["available"] = new_pos
            context.manual_position[gm_sym]["t_qty"] = new_pos
        context.engine.sell_count_per_stock[code] = context.daily_sell_count.get(code, 0)
        print(f"[{now:%H:%M:%S}] SELL {code} {qty}@{cp:.2f} score={sig.score:.0f} regime={context.last_index_regime}")
        # N28: 挂接通道信息，成交回调写入action/score
        if not hasattr(context, "_pending_sell_action"):
            context._pending_sell_action = {}
        context._pending_sell_action[gm_sym] = (sig.action, sig.score)
        return True
    except Exception as e:
        print(f"[{now:%H:%M:%S}] SELL {code} 失败: {e}")
        try: write_risk(str(now), "order_failed", f"SELL {qty}@{cp:.2f} err={e}", code=code)
        except Exception: pass
        return False


def _raw_code(symbol: str) -> str:
    return symbol.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


def _dedup_bar(context, gm_sym: str, eob: str) -> bool:
    """F9: 同 eob 重复 bar 判定（True=重复应跳过）。

    2026-07-31 模拟盘同秒 4 次重复投递 000988 bar，导致 PANIC 连发 4 单；
    同时防止 bar_cache 重复累积。"""
    _eob_map = getattr(context, "_last_bar_eob", None)
    if _eob_map is None:
        _eob_map = {}
        context._last_bar_eob = _eob_map
    if _eob_map.get(gm_sym) == eob:
        return True
    _eob_map[gm_sym] = eob
    return False


def _maybe_clear_audit_log(context):
    """D8/F10: 仅回测模式清空审计文件；模拟盘(MODE_LIVE)保留追加。

    2026-07-31 上午段 backtrace 被 13:09 重启清空——回测设计误伤模拟盘审计。"""
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if _is_live:
        return False
    try:
        _audit_close()  # 先释放句柄，否则 Windows 下 remove 失败
        if os.path.exists(_AUDIT_LOG_PATH):
            os.remove(_AUDIT_LOG_PATH)
            return True
    except Exception:
        pass
    return False


def _build_bar_df(context, code: str, gm_symbol: str) -> pd.DataFrame:
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    df = add_indicators(df)
    return df



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
    # F2: 模拟盘模式启用对账，回测模式跳过
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    _skip_reconcile = not _is_live
    if not _skip_reconcile and (last_rec is None or (now - last_rec).total_seconds() > reconcile_interval):
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
                    _my_cost = mp.get("cost", gm_pos["cost"])
                    context.manual_position[gm_symbol] = gm_pos
                    context.manual_position[gm_symbol]["cost"] = _my_cost
                    _audit_write({"event": "reconcile_fix", "code": code, "time": str(now),
                                  "old_qty": mp.get("qty"), "new_qty": gm_pos["qty"]})
                # ①-1: manual_position cost=0时用gm vwap修正(市价单price=0兜底)
                if mp and float(mp.get("cost", 0) or 0) <= 0 and float(gm_pos.get("cost", 0) or 0) > 0:
                    mp["cost"] = gm_pos["cost"]
                    _audit_write({"event": "cost_fix", "code": code, "time": str(now),
                                  "cost": gm_pos["cost"]})
                # qty 一致时返回 manual_position（我们跟踪的成本），不返回 gm_pos
                # gm_pos 的 vwap 可能含前复权调整，与真实买入成本不一致
                if mp and int(mp.get("qty", 0) or 0) > 0:
                    return mp
            else:
                # F11: 终端已无持仓而台账仍有余量 → 向下对账归零
                # (2026-07-31 PANIC清仓000988后 心跳仍报300股：空仓查询返回空列表
                # 走不到向上对账分支，台账残影永远不自愈)
                mp = context.manual_position.get(gm_symbol)
                if mp and int(mp.get("qty", 0) or 0) > 0:
                    _old_q = int(mp.get("qty", 0))
                    mp["qty"] = 0
                    mp["available"] = 0
                    mp["t_qty"] = 0
                    _audit_write({"event": "reconcile_fix", "code": code, "time": str(now),
                                  "old_qty": _old_q, "new_qty": 0})
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
    _cache_key = f"{today_str}|{code}"
    if not hasattr(context, "_daily_ctx_cache_map"):
        context._daily_ctx_cache_map = {}
    if _cache_key in context._daily_ctx_cache_map:
        return context._daily_ctx_cache_map[_cache_key]

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
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        ctx["daily_ma5"] = float(c.rolling(5).mean().iloc[-1]) if len(c) >= 5 else 0
        ctx["daily_ma10"] = float(c.rolling(10).mean().iloc[-1]) if len(c) >= 10 else 0
        ctx["daily_ma20"] = float(c.rolling(20).mean().iloc[-1]) if len(c) >= 20 else 0
        # N4: 日线 ATR（用于 PANIC_SELL）
        if len(c) >= 14:
            tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
            ctx["daily_atr"] = float(tr.rolling(14).mean().iloc[-1] / c.iloc[-1]) if c.iloc[-1] > 0 else 0.02
        else:
            ctx["daily_atr"] = 0.02
        # M2: 做T门槛指标（供标的池准入检查）
        if len(c) >= 20 and "volume" in df.columns:
            v = df["volume"].astype(float)
            ctx["_m2_amp20"] = float((tr.rolling(20).mean().iloc[-1] / c.iloc[-1])) if c.iloc[-1] > 0 else 0
            ctx["_m2_amount20"] = float((v * c).rolling(20).mean().iloc[-1]) if len(v) >= 20 else 0
            ctx["_m2_lot_value"] = float(c.iloc[-1] * 100)
            # 门槛判定（AMP<3% 或 AMT<2亿 或 单手<2000 → 标记仅观察）
            _pass = (ctx["_m2_amp20"] >= 0.03 and ctx["_m2_amount20"] >= 200000000
                     and ctx["_m2_lot_value"] >= 2000)  # TODO(PhaseD): 寻优定值
            ctx["_m2_pool_pass"] = _pass
            if not _pass:
                ctx["daily_status"] = "pool_gate_fail"
        prev_close = float(c.iloc[-1]) if len(c) > 0 else 0
        ctx["daily_prev_close"] = prev_close
        ctx.setdefault("daily_status", "ok")  # F7: 不覆盖 pool_gate_fail
        ctx["daily_buy_t_ok"] = True

        # 破位/过热简化判断 + R1 个股趋势状态
        if len(c) >= 20:
            ma20 = c.rolling(20).mean().iloc[-1]
            if prev_close < ma20 * 0.985:
                ctx["daily_breakdown_risk"] = True
            if prev_close > ma20 * 1.08:
                ctx["daily_overheated"] = True
            ctx["daily_ma5_state"] = "above_ma5_trend" if prev_close > ctx["daily_ma5"] else "near_ma5_chop"
            # R1: 个股趋势状态（供趋势熔断 G3 消费）
            _ma5_val = ctx["daily_ma5"]
            _ma5_slope = float(c.rolling(5).mean().diff().iloc[-1]) if len(c) >= 6 else 0
            if ctx.get("daily_breakdown_risk"):
                ctx["_stock_trend_state"] = "TREND_BREAKDOWN"
            elif prev_close < _ma5_val and _ma5_slope < 0:
                ctx["_stock_trend_state"] = "TREND_DOWN"
            elif prev_close > _ma5_val and _ma5_slope > 0:
                ctx["_stock_trend_state"] = "TREND_UP"
            else:
                ctx["_stock_trend_state"] = "TREND_RANGE"

        # 将 latest_pre_close 暴露给 _get_holding
        context.latest_pre_close[code] = prev_close
    else:
        context.latest_pre_close[code] = 0
        ctx["daily_status"] = "unavailable"

    context._daily_ctx_cache_map[_cache_key] = ctx
    return ctx


# ── 审计 JSONL ──

_AUDIT_LOG_PATH = os.path.join(PROJECT_DIR, "gmcache", "backtrace.jsonl")
_audit_file = None
_AUDIT_RUN_ID = ""

def _audit_write(rec: dict):
    rec['_run_id'] = _AUDIT_RUN_ID
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

def _reconcile_positions_at_init(context):
    """F1: 启动全量持仓对账（2026-07-28 日复盘 P0）

    重启后 _base_ordered/_base_settled 为纯内存空集，若不从账户拉取真实持仓，
    已持有标的会被重发底仓单（2026-07-28 600481 三轮重复建仓至 5600 股事故）。
    本函数在 init 末尾执行：
      1. 逐票查询 account().positions()，有持仓则灌入 executed_orders/manual_position
      2. 已持仓标的直接入 _base_settled（跳过重发底仓单）并设 _base_ref_
      3. 每票写 reconcile_init 审计事件
    仅 MODE_LIVE 执行；回测模式跳过。
    """
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return
    for code, sym in STOCKS.items():
        try:
            pos = context.account().positions(symbol=sym, side=PositionSide_Long)
            if not pos or len(pos) == 0:
                continue
            p = pos[0]
            vol = int(p.volume)
            if vol <= 0:
                continue
            _cost = float(p.vwap or 0)
            context.executed_orders[sym] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": vol,
                "available": int(p.available),
                "t_qty": vol,
                "cost": _cost,
                "type": "stock",
                "pre_close": _cost,
            }
            context.manual_position[sym] = dict(context.executed_orders[sym])
            context._base_settled.add(code)
            # F7: _base_ref_ 语义=目标底仓(镜像表)，非实际持仓——缺口由 _base_topup_qty 择时回补
            setattr(context, f'_base_ref_{code}',
                    int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or vol))
            _audit_write({"event": "reconcile_init", "code": code, "qty": vol,
                          "available": int(p.available), "cost": _cost,
                          "time": str(datetime.now())})
            print(f"[INIT] {code} {STOCK_NAMES.get(code, code)} 持仓对账: "
                  f"{vol}股 可用{int(p.available)} 成本{_cost:.2f}")
        except Exception as e:
            print(f"[INIT] {code} 持仓对账失败: {e}")


def _base_topup_qty(context, code, gm_sym):
    """F7: 底仓择时回补量（2026-07-29 owner定调：基线维持，缺口择时买回）

    语义: MIRROR_HOLDINGS 是目标底仓而非现状快照。已建仓标的实际持仓低于目标
    100 股以上时，返回回补量(向下100取整)，由 on_bar 底仓块走既有 M2/趋势闸
    择时买入；否则返回 0。

    反绞肉门控（本函数内，on_bar 的 M2/趋势闸仍照常叠加）:
      - 当日已有任意卖出成交(如PANIC止损) → 当日不反补，防恐慌-回补来回打脸
      - 指数 uni_down → 不补（防御日不加重敞口）
    """
    if code not in getattr(context, "_base_settled", set()):
        return 0  # 未建仓标的走原建仓路径
    _mirror = int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or 0)
    if _mirror <= 0:
        return 0
    _held = int(context.manual_position.get(gm_sym, {}).get("qty", 0) or 0)
    _short = ((_mirror - _held) // 100) * 100
    if _short < 100:
        return 0
    if context.daily_sell_count.get(code, 0):
        return 0
    if getattr(context, "last_index_regime", "range") == "uni_down":
        return 0
    return _short


def init(context):
    global _AUDIT_RUN_ID
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    # D8/F10: 仅回测模式清空审计文件（模拟盘重启不丢当日段）
    _maybe_clear_audit_log(context)
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
    context._inflight_sell = {}   # F9: 在途卖单台账 {gm_sym: qty}
    context._last_bar_eob = {}    # F9: 重复bar去重 {gm_sym: eob}
    context.cur_date = None
    context._daily_ctx_cache_map = {}
    context.total_trade_cost = 0.0
    context.total_trade_count = 0
    context.rejected_order_count = 0
    context.audit_records = []
    context.sizer = PositionSizer(params=PARAMS)

    # F1: 启动全量持仓对账——已持仓标的入 _base_settled，防止重启重发底仓单
    _reconcile_positions_at_init(context)

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
    # 确保事件桥目录存在
    from gm_bridge.writer import BRIDGE_DIR
    os.makedirs(BRIDGE_DIR, exist_ok=True)
    print(f"[init] 事件桥: {BRIDGE_DIR}")
    print(f"[init] 策略初始化完成: {len(symbols)} 只标的")


def on_bar(context, bars):
    now = context.now if hasattr(context, "now") else datetime.now()
    import utils.helpers as uh
    uh.SIM_NOW = now
    import signals.engine as se
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

    # ── KILL_SWITCH 检查 ──
    _killed = check_kill_switch()

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

    # ── 心跳（每分钟写一次；仅模拟盘/实盘，回测跳过省I/O——纯监控产物不参与决策） ──
    try:
        _hb_live = context.mode == MODE_LIVE
    except Exception:
        _hb_live = False
    if _hb_live:
        # F11: 心跳持仓改走 _get_holding 多源对账（含终端空仓向下同步），
        # 不再裸读 manual_position（0731 心跳报000988=300 实际=0 事故）
        _hb_positions = {}
        for _hc, _hs in STOCKS.items():
            try:
                _h = _get_holding(context, _hc, _hs)
            except Exception:
                continue
            if int(_h.get("qty", 0) or 0) > 0:
                _hb_positions[_hs] = {"qty": int(_h.get("qty", 0)),
                                      "cost": float(_h.get("cost", 0) or 0)}
        # ①-3: 实时读取可用现金
        _hb_cash = INITIAL_CASH
        try:
            _acct = context.account()
            _c = getattr(_acct, 'cash', None)
            if _c is not None:
                _c = _c() if callable(_c) else _c
                if isinstance(_c, dict):
                    _hb_cash = float(_c.get('available', _c.get('total', INITIAL_CASH)))
                else:
                    _hb_cash = float(_c)
        except Exception:
            pass
        write_heartbeat(
            time_str=str(now), bar=f"{now:%H:%M}",
            positions=_hb_positions,
            cash=_hb_cash,
            index_regime=context.last_index_regime,
            index_score=context.last_index_score,
        )

    for bar in bars:
        gm_sym = str(bar["symbol"])
        code = _raw_code(gm_sym)
        if code not in STOCKS:
            continue

        # F9: 同 eob 重复 bar 去重（2026-07-31 模拟盘同秒 4 次重复投递
        # 导致 PANIC 连发 4 单；同时防止 bar_cache 重复累积）
        if _dedup_bar(context, gm_sym, str(bar["eob"])):
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

        # ── D2: 底仓（按镜像持仓表逐股建仓 + F7择时回补缺口） ──
        _topup_qty = _base_topup_qty(context, code, gm_sym)
        if code not in context._base_ordered and (code not in context._base_settled or _topup_qty >= 100):
            mirror = MIRROR_HOLDINGS.get(code, {})
            base_qty = mirror.get("qty", 0) if code not in context._base_settled else _topup_qty
            _is_topup = code in context._base_settled  # F8: 已持仓标的走回补路径——闸门拦截不得中断持仓信号评估(0730盲区事故)
            if base_qty < 100:
                print(f"[{now:%H:%M:%S}] BASE {code} 跳过: MIRROR_HOLDINGS 中无此标的或 qty<100")
                context._base_settled.add(code)
                if not _is_topup:
                    return
            # M2: 做T门槛检查（底仓建仓前置）
            _dc = _refresh_daily_ctx(context, code, gm_sym, now)
            # R1/A3: 底仓过趋势闸——TREND_BREAKDOWN 延迟到次日（F5: 回退61a19e6激进模式）
            _trend = _dc.get("_stock_trend_state", "TREND_RANGE")
            _topup_blocked = False
            if _trend == "TREND_BREAKDOWN":
                _defer_key = f'_base_deferred_{code}'
                if getattr(context, _defer_key, '') != now.strftime("%Y-%m-%d"):
                    setattr(context, _defer_key, now.strftime("%Y-%m-%d"))
                    print(f'[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} TREND_BREAKDOWN→延迟建仓')
                    try: write_risk(str(now), "base_deferred", f"_stock_trend_state={_trend}", code=code)
                    except: pass
                if not _is_topup:
                    return
                _topup_blocked = True  # F8: 回补被趋势闸拦截，但持仓信号评估照常落地
            # 默认 False: 数据不足时保守不放行（F5: 恢复M2门槛）
            if not _topup_blocked and not _dc.get("_m2_pool_pass", False):
                print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} 门槛未过→仅观察 "
                      f"(amp={_dc.get('_m2_amp20',0):.1%} amt={_dc.get('_m2_amount20',0)/1e8:.1f}亿 "
                      f"lot={_dc.get('_m2_lot_value',0):.0f}元)")
                try: write_risk(str(now), "pool_gate", f"amp={_dc.get('_m2_amp20',0):.1%} 仅观察", code=code)
                except: pass
                if not _is_topup:
                    context._base_settled.add(code)
                    return
                _topup_blocked = True  # F8: 回补被M2闸拦截，信号评估照常
            if not _topup_blocked:
                try:
                    try:
                        write_order(str(now), code, "BUY", base_qty, cp, order_id="base")
                    except Exception:
                        pass
                    order_volume(symbol=gm_sym, volume=base_qty,
                                 side=OrderSide_Buy,
                                 order_type=OrderType_Market,
                                 position_effect=PositionEffect_Open)
                    context._base_ordered.add(code)
                    print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} 下单 {base_qty}股@{cp:.2f}")
                    _audit_write({"event": "base_order", "code": code, "qty": base_qty, "price": cp, "time": str(now)})
                except Exception as e:
                    print(f"[{now:%H:%M:%S}] BASE {code} 下单失败: {e}")
                    try:
                        write_risk(str(now), "order_failed", f"BASE BUY {base_qty}@{cp:.2f} err={e}", code=code)
                    except Exception:
                        pass
                return
            # F8: _topup_blocked=True 时不下单，继续走下方信号评估流程

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

        # N6: 开盘5分钟买入隔离
        morning_no_buy = now.hour == 9 and now.minute <= 35

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
        # 修正 profit_pct: engine 读 DataFrame 最后一行可能不是当前 bar
        _holding_cost = float(holding.get("cost", 0) or 0)
        _last_f = context.engine._last_feats.get(code, {})
        if _last_f and _holding_cost > 0:
            _last_f["price"] = cp
            _last_f["profit_pct"] = (cp - _holding_cost) / _holding_cost
            _last_f["vwap"] = cp
            _daily_atr = float(daily_ctx.get("daily_atr", 0.02) or 0.02)
            _panic_trigger = max(-5 * _daily_atr, -0.12)
            _last_f["is_deep_loss"] = _holding_cost > 0 and _last_f["profit_pct"] < _panic_trigger
            _last_f["panic_trigger"] = _panic_trigger

        # ── D5/G2: uni_down 熔断 ──
        if context.last_index_regime == "uni_down" and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── R1/G3: 个股趋势熔断（一票一闸） ──
        _trend = daily_ctx.get("_stock_trend_state", "TREND_RANGE")
        if _trend == "TREND_BREAKDOWN" and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None
            try: write_risk(str(now), "stock_trend_gate", f"{_trend} 禁买", code=code)
            except: pass
        elif _trend == "TREND_DOWN" and sig and sig.action == "ADD_POS":
            sig = None

        # ── D5: 尾盘回转（14:50-15:00，先于 PANIC_SELL 检查） ──
        is_tail = now.hour == 14 and now.minute >= 50
        if is_tail and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── B1/T1: TRAIL_SELL 移动止盈 ──
        # TODO(PhaseD): 寻优 ACT_LINE/k/MIN_BACK/MAX_BACK
        feats_cache = getattr(context.engine, "_last_feats", {}).get(code, {})
        _panic_on_cooldown = (code in context.engine.sell_cooldown
                              and now < context.engine.sell_cooldown.get(code, now))
        _profit = feats_cache.get("profit_pct", 0) if feats_cache else 0
        _trail_state = "INACTIVE"
        _trail_peak = 0.0
        if gm_sym in context.manual_position:
            _trail_state = context.manual_position[gm_sym].get("_trail_state", "INACTIVE")
            _trail_peak = context.manual_position[gm_sym].get("_trail_peak", 0.0)
        # 激活: 浮盈 > +8%
        if _trail_state == "INACTIVE" and _profit > 0.08:
            _trail_state = "ARMED"
            _trail_peak = max(cp, _trail_peak)
        # 跟踪: 更新峰值
        if _trail_state == "ARMED":
            _trail_peak = max(_trail_peak, cp)
            # N23/B1': TRAIL k×ATR带界 — 防止回撤阈值自解除
            _daily_atr = daily_ctx.get("daily_atr", 0.02) or 0.02
            _back = max(0.03, min(1.5 * _daily_atr, 0.08))  # TODO(PhaseD): MIN_BACK/k/MAX_BACK
            _drawdown = (_trail_peak - cp) / _trail_peak if _trail_peak > 0 else 0
            if _drawdown > _back and not _panic_on_cooldown:
                from data.indicators import Signal
                sig = Signal(code=code, name=STOCK_NAMES.get(code, code),
                             action="TRAIL_SELL", price=cp, score=80.0,
                             reasons=[f"移动止盈: peak={_trail_peak:.2f} dd={_drawdown:.1%}"])
                _trail_state = "COOLED"
            context.manual_position[gm_sym]["_trail_state"] = _trail_state
            context.manual_position[gm_sym]["_trail_peak"] = _trail_peak
        # 复位: 全仓清空
        if pos_qty <= 0 and gm_sym in context.manual_position:
            context.manual_position[gm_sym]["_trail_state"] = "INACTIVE"
            context.manual_position[gm_sym]["_trail_peak"] = 0.0

        # ── D5/N4: 深度亏损 → PANIC_SELL ──
        # R3/B5: 开盘卖出缓冲 — 09:35前 SELL_HIGH 延后
        if sig and sig.action in ("SELL_HIGH", "TARGET_SELL") and now.hour == 9 and now.minute <= 35:
            sig = None
            _audit_write({"event": "morning_sell_blocked", "code": code, "time": str(now),
                          "action": "SELL_HIGH", "reason": "开盘缓冲延后"})

        # B2/T3: 趋势破坏止盈 TREND_EXIT
        _profit = feats_cache.get("profit_pct", 0)
        _base_ref = getattr(context, f'_base_ref_{code}', 0) or pos_qty
        # N20: 不得覆盖更高优先级信号(P1 PANIC/P2 TRAIL)
        if ((sig is None or sig.action not in ("PANIC_SELL", "TRAIL_SELL"))
                and _profit > 0 and not _panic_on_cooldown
                and daily_ctx.get("_stock_trend_state") in ("TREND_DOWN", "TREND_BREAKDOWN")):
            from data.indicators import Signal as _Sig
            _excess = max(0, pos_qty - _base_ref) if _base_ref else 0
            if _excess >= 100:
                sig = _Sig(code=code, name=STOCK_NAMES.get(code, code),
                           action="TREND_EXIT", price=cp, score=78.0,
                           reasons=[f"趋势破坏止盈: profit={_profit:.1%} trend={daily_ctx.get('_stock_trend_state')}"])

        # B4/T2: 分批目标止盈 TARGET_SELL
        # TODO(PhaseD): 寻优分档 L1/L2/L3 及批次比例
        if (_profit > 0.10 and not _panic_on_cooldown
                # N20: 不得覆盖更高优先级信号(P1/P2/P3)
                and not (sig and sig.action in ("PANIC_SELL", "TRAIL_SELL", "TREND_EXIT"))):
            _filled = context.manual_position.get(gm_sym, {}).get("_target_filled_l1", False) if gm_sym in context.manual_position else False
            if not _filled:
                from data.indicators import Signal as _Sig
                sig = _Sig(code=code, name=STOCK_NAMES.get(code, code),
                           action="TARGET_SELL", price=cp, score=75.0,
                           reasons=[f"目标止盈L1: profit={_profit:.1%}"])
                if gm_sym in context.manual_position:
                    context.manual_position[gm_sym]["_target_filled_l1"] = True
        # 复位: 全仓清空时重置目标位图
        if pos_qty <= 0 and gm_sym in context.manual_position:
            context.manual_position[gm_sym]["_target_filled_l1"] = False

        if feats_cache.get("is_deep_loss") and not _panic_on_cooldown:
            from data.indicators import Signal
            sig = Signal(code=code, name=STOCK_NAMES.get(code, code),
                         action="PANIC_SELL", price=cp, score=75.0,
                         reasons=["深度亏损恐慌卖出", f"profit_pct={feats_cache.get('profit_pct', 0):.2%}"])
            print(f"[{now:%H:%M:%S}] PANIC_SELL {code} {feats_cache.get('hold_qty', 0)}@{cp:.2f} "
                  f"profit_pct={feats_cache.get('profit_pct', 0):.2%}")

        # ── D5-c: 尾盘回转（14:50-15:00），超底仓部分强制卖出归位 ──
        if is_tail and pos_qty > getattr(context, '_base_ref_' + code, pos_qty) and not _panic_on_cooldown:
            if sig is None or sig.action not in ('SELL_HIGH', 'PANIC_SELL'):
                target = getattr(context, '_base_ref_' + code, pos_qty)
                excess = pos_qty - target
                if excess >= 100:
                    qty = (excess // 100) * 100
                    # F14: T+1可用量封顶 + 在途扣除（与仲裁器同口径）
                    _avail_raw = holding.get("available")
                    _avail = pos_qty if _avail_raw is None else int(_avail_raw)
                    _tif = int(getattr(context, "_inflight_sell", {}).get(gm_sym, 0) or 0)
                    qty = min(qty, max(0, min(pos_qty, _avail) - _tif))
                    if qty < 100:
                        continue
                    try:
                        write_order(str(now), code, "SELL", qty, cp, order_id="tail")
                    except Exception:
                        pass
                    try:
                        order_volume(symbol=gm_sym, volume=qty,
                                     side=OrderSide_Sell,
                                     order_type=OrderType_Market,
                                     position_effect=PositionEffect_Close)
                    except Exception as e:
                        print(f'[{now:%H:%M:%S}] TAIL {code} 下单失败: {e}')
                        try:
                            write_risk(str(now), "order_failed", f"TAIL SELL {qty}@{cp:.2f} err={e}", code=code)
                        except Exception:
                            pass
                        continue
                    if not hasattr(context, "_inflight_sell") or context._inflight_sell is None:
                        context._inflight_sell = {}
                    context._inflight_sell[gm_sym] = _tif + qty
                    # F14: tail下单即计虚冷却，拒单后不再重试（与仲裁器同模式）
                    try:
                        if not hasattr(context.engine, "sell_cooldown") or context.engine.sell_cooldown is None:
                            context.engine.sell_cooldown = {}
                        _cd = int(context.engine._get_params(code).get("cooldown_minutes", 30))
                    except Exception:
                        _cd = 30
                    context.engine.sell_cooldown[code] = now + timedelta(minutes=_cd)
                    # 立即更新 manual_position 防下一分钟重复触发（仅下单成功后；拒单由 status=8 分支回滚）
                    if gm_sym in context.manual_position:
                        new_pos = pos_qty - qty
                        context.manual_position[gm_sym]["qty"] = new_pos
                        context.manual_position[gm_sym]["available"] = new_pos
                        context.manual_position[gm_sym]["t_qty"] = new_pos
                    print(f'[{now:%H:%M:%S}] TAIL {code} 尾盘归位 {qty}股 (pos={pos_qty}→{pos_qty-qty} target={target})')
                    if not hasattr(context, "_pending_sell_action"):
                        context._pending_sell_action = {}
                    context._pending_sell_action[gm_sym] = ("TAIL", 0)
                    context.total_trade_count += 1
                    context.daily_sell_count[code] = context.daily_sell_count.get(code, 0) + 1
                    continue

        # ── N6: 开盘5分钟拦截买入信号 ──
        if morning_no_buy and sig and sig.action in ('BUY_LOW', 'ADD_POS'):
            sig = None

        # B3/R2: SELL_HIGH 成本锚定 — 亏损单不由 SELL_HIGH 通道卖出
        _cost_anchor = 0.0  # TODO(PhaseD): 寻优 cost_anchor
        if (sig and sig.action == "SELL_HIGH"
                and feats_cache.get("profit_pct", 0) < _cost_anchor):
            sig = None  # 降级：交回 PANIC/TREND_EXIT 接管

        if sig is None:
            _last_dec = context.engine.last_decision.get(code, {})
            _audit_write({
                "event": "no_signal", "code": code, "time": str(now),
                "buy_score": buy_score, "sell_score": sell_score,
                "pos_qty": pos_qty, "price": cp,
                "index_regime": context.last_index_regime,
                "buy_blocks": _last_dec.get("buy_blocks", []),
                "sell_blocks": _last_dec.get("sell_blocks", []),
                "decision_reason": _last_dec.get("reason", ""),
                "profit_pct": feats_cache.get("profit_pct", 0),
                "daily_atr": feats_cache.get("daily_atr", 0),
                "is_deep_loss": feats_cache.get("is_deep_loss", False),
            })
            continue

        # ── 信号事件写入 ──
        if sig is not None:
            try:
                write_signal(str(now), code, sig.action, sig.score,
                             reasons=sig.reasons, pos_qty=pos_qty)
            except Exception:
                pass

        # ── 参数准备 ──
        stock_params = STOCK_PARAMS.get(code, {})
        max_buys = stock_params.get("max_buy_times_per_stock", 3)

        # ── D1: 引擎冷却/计数 ──
        threshold = stock_params.get("notify_sell_threshold", 65) if sig.action in ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "TARGET_SELL") else \
                    stock_params.get("notify_buy_threshold", 43)

        if sig.score < threshold:
            continue

        # 执行交易
        if sig.action in ("BUY_LOW", "ADD_POS"):
            if _killed:
                try:
                    write_risk(str(now), "kill_switch", f"KILL_SWITCH 阻止 {code} 买入", code=code)
                except Exception:
                    pass
                continue
            bc = context.daily_buy_count.get(code, 0)
            if bc >= max_buys:
                continue

            # N3: 现金预检（移到 sizer 之前，供 target_t 计算）
            available_cash = INITIAL_CASH
            _cash_ok = False
            try:
                _acct = context.account()
                _c = getattr(_acct, 'cash', None)
                if _c is not None:
                    _c = _c() if callable(_c) else _c
                    if isinstance(_c, dict):
                        # gm3 account().cash 返回 dict，键名可能是 available/available_cash/total
                        _v = _c.get('available') or _c.get('available_cash') or _c.get('cash') or _c.get('total') or 0
                        available_cash = float(_v)
                    else:
                        available_cash = float(_c)
                    _cash_ok = True
            except Exception:
                pass
            if not _cash_ok:
                available_cash = 0  # N15: fail-closed 每 bar 生效
                if not getattr(context, '_cash_warned', False):
                    context._cash_warned = True
                    print(f'[N8] WARN: 无法读取可用现金 → fail-closed: 禁止买入')

            # N10: 算 target_t（仓位上限约束下的最大仓位，供 sizer 算 max_buyable）
            pos_limit_pct = float(PARAMS.get('max_single_position_pct', 0.80))
            # N16: 单票预算制（等权 25%/票，Phase C 全量后改趋势加权）
            # TODO(PhaseD): 寻优预算权重
            _n_stocks = max(len(STOCKS), 1)
            _stock_budget = available_cash / _n_stocks
            estimated_equity = _stock_budget + pos_qty * cp
            max_pos_shares = int(estimated_equity * pos_limit_pct / cp / 100) * 100 if cp > 0 else 0
            _base_ref = getattr(context, f'_base_ref_{code}', 0) or pos_qty
            target_t = max(max_pos_shares, _base_ref, pos_qty)
            holding_with_target = dict(holding, target_t=target_t)

            qty = context.sizer.calc_buy_qty(code, holding_with_target, sig.score, threshold)
            if qty <= 0:
                qty = 300  # sizer 返回 0 时的最小交易量

            # N3: 现金预检
            max_by_cash = int(available_cash * 0.95 / cp / 100) * 100 if cp > 0 else 0
            qty = min(qty, max_by_cash) if max_by_cash > 0 else qty
            if qty < 100:
                if bc == 0:
                    print(f'[{now:%H:%M:%S}] BUY {code} 现金不足跳过: 可用={available_cash:.0f}')
                try:
                    write_risk(str(now), "cash_insufficient",
                               f"available={available_cash:.0f} needed={qty*cp:.0f}", code=code)
                except Exception:
                    pass
                continue

            # N2: 仓位上限检查
            current_pos_value = pos_qty * cp
            new_pos_value = current_pos_value + qty * cp
            total_equity_value = available_cash + current_pos_value
            if total_equity_value > 0 and new_pos_value / total_equity_value > pos_limit_pct:
                print(f'[{now:%H:%M:%S}] BUY {code} 仓位上限拦截: {new_pos_value/total_equity_value:.0%}>{pos_limit_pct:.0%}')
                try:
                    write_risk(str(now), "position_limit",
                               f"{new_pos_value/total_equity_value:.1%}>{pos_limit_pct:.0%} qty={qty}", code=code)
                except Exception:
                    pass
                continue
            try:
                write_order(str(now), code, "BUY", qty, cp)
            except Exception:
                pass
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

        elif sig.action in ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "TARGET_SELL"):
            # T4: 仲裁器统一处理（地板 + 阈值 + sizer + 下单 + 审计）
            _sell_arbiter(context, code, sig, pos_qty, cp, now, holding,
                          threshold, stock_params, gm_sym)


def on_order_status(context, order):
    symbol = order["symbol"]
    status = order["status"]
    volume = order["volume"]
    code = _raw_code(symbol)  # 提前到price兜底之前
    # ①-1/F6: 成交价优先 filled_vwap —— 掘金市价单 order["price"] 携带涨跌停保护价
    # (2026-07-29 C1: 600481卖出真实成交4.01被记为跌停价3.52；买入路径会用此价计算cost，错误价会毒化成本)
    price = order.get("filled_vwap") or order.get("vwap") or order.get("price") or 0
    if price <= 0:
        price = context.latest_pre_close.get(code, 0)
    side = order["side"]

    # F9: 在途卖单释放（成交/拒单/撤单/过期均归还额度）
    if side == 2 and status in (3, 4, 5, 6, 8, 12):
        _ifl = getattr(context, "_inflight_sell", None)
        if _ifl and symbol in _ifl:
            _ifl[symbol] = max(0, int(_ifl[symbol]) - int(volume))

    if status == 3:  # 全部成交
        _side = "BUY" if side == 1 else "SELL"
        _pos_after = int(context.executed_orders.get(symbol, {}).get("qty", 0)) if _side == "SELL" else                      int(context.executed_orders.get(symbol, {}).get("qty", 0)) + volume if symbol in context.executed_orders else volume
        try:
            write_fill(str(datetime.now()), _raw_code(symbol), _side, volume, price,
                       pos_after=_pos_after)
        except Exception:
            pass
        # P0-2: 成交回调接线冷却（只有真的成交了才计冷却，避免下单即计）
        if code in STOCKS:
            _action = 'BUY_LOW' if side == 1 else 'SELL_HIGH'
            context.engine.record_trade_action(code, _action, volume, price)
        if side == 1:  # 买入
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0, "cost": price})
            old_qty = int(old.get("qty", 0))
            old_cost = float(old.get("cost", price))
            new_qty = old_qty + volume
            new_cost = (old_cost * old_qty + price * volume) / new_qty if new_qty > 0 else price
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                # N25-2: 当日买入不解锁(T+1), available保持旧值
                "available": int(old.get("available", 0)),
                "t_qty": new_qty,
                "cost": new_cost,
                "type": "stock",
                "pre_close": price,
            }
            # 底仓确认（含F7回补单：已settled标的回补成交同样同步台账并释放_base_ordered）
            if code in context._base_ordered:
                context._base_settled.add(code)
                # F12: 同步台账时保留做T状态键——直接整体替换会清空
                # _target_filled_l1/_trail_state/_trail_peak，导致同一持仓期内
                # TARGET 同档重复触发、TRAIL 状态机重置（WP-B 回放包 fix3 实证）
                _keep = {k: v for k, v in context.manual_position.get(symbol, {}).items()
                         if k.startswith("_target_") or k.startswith("_trail_")}
                context.manual_position[symbol] = dict(context.executed_orders[symbol], **_keep)
                # 底仓参考量=镜像目标值（供 sizer/sell_floor/tail 使用）
                setattr(context, f'_base_ref_{code}',
                        int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or volume))
                context._base_ordered.discard(code)
                print(f"[BASE] {code} 底仓成交 {volume}股@{price:.2f}")
        elif side == 2:  # 卖出
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0})
            old_qty = int(old.get("qty", 0))
            old_cost = old.get("cost", price)
            new_qty = max(0, old_qty - volume)
            # cost 保持不变（买入成本），不覆写为卖出价
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                "available": new_qty,
                "t_qty": new_qty,
                "cost": old_cost,
                "type": "stock",
                "pre_close": price,
            }
            # N26+N28: 成交时写入审计(含通道信息)
            _act, _sc = getattr(context, "_pending_sell_action", {}).pop(symbol, ("", 0))
            # WP-B回放包: 回测下用仿真时钟(context.now)，否则验收无法对齐窗口日期
            _ts_now = str(getattr(context, "now", None) or datetime.now())
            _audit_write({"event": "sell", "code": code, "qty": volume, "price": price,
                          "time": _ts_now, "pos_after_sell": new_qty,
                          "action": _act, "score": _sc})
    elif status in (4, 5, 6, 8, 12):  # 拒单/撤单/待撤/已拒绝(8)/已过期(12) —— F2修复: 2026-07-28前漏掉8导致所有拒单静默
        _rej_detail = ""
        try:
            _rej_detail = order.get("ord_rej_reason_detail", "") or ""
        except Exception:
            pass
        context.rejected_order_count = getattr(context, 'rejected_order_count', 0) + 1
        # N5: 底仓拒单恢复
        if code in getattr(context, '_base_ordered', set()):
            if not hasattr(context, '_base_retry_count'):
                context._base_retry_count = {}
            retry = context._base_retry_count.get(code, 0) + 1
            context._base_retry_count[code] = retry
            if retry <= MAX_BASE_RETRY:
                context._base_ordered.discard(code)
                print(f'[ORDER] {code} 底仓拒单 status={status} 重试 #{retry} {_rej_detail}')
            else:
                print(f'[ORDER] {code} 底仓拒单已达上限({MAX_BASE_RETRY})，停止重试')
        print(f"[ORDER] {symbol} 被拒 status={status} {_rej_detail}")
        # N25-2: 卖出拒单回滚manual_position(下单时已虚减)
        if side == 2 and symbol in context.manual_position:
            mp = context.manual_position[symbol]
            mp["qty"] = mp.get("qty", 0) + volume
            mp["available"] = mp.get("available", 0) + volume
            mp["t_qty"] = mp.get("t_qty", 0) + volume
            _audit_write({"event": "sell_rollback", "code": code, "qty": volume,
                          "time": str(getattr(context, "now", None) or datetime.now())})
            # F14: 拒单不消耗日卖出配额/总成交计数（防止误耗挤占信号通道）
            context.daily_sell_count[code] = max(0, context.daily_sell_count.get(code, 0) - 1)
            context.total_trade_count = max(0, context.total_trade_count - 1)
        try:
            _r_code = _raw_code(symbol)
            _r_side = "BUY" if side == 1 else "SELL"
            write_reject(str(datetime.now()), _r_code, _r_side, volume,
                         reason=f"status={status} {_rej_detail}",
                         raw={"status": status, "side": side, "volume": volume,
                              "rej_detail": _rej_detail})
            write_risk(str(datetime.now()), "order_rejected",
                         f"{_r_side} {volume}股被拒 status={status} {_rej_detail}", code=_r_code)
        except Exception:
            pass


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
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
        filename="main.py", mode=MODE_LIVE,
        token="480a6c84b0f43417ffcc9c15162dd7256ca9c3b0")
