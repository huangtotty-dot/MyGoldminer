# coding=utf-8
"""
tests/test_wp_b22.py — WP-B22 MA5破位容差 + 600481 M2池闸定制 验证

背景（docs/backlog/WP-B22_方案_MA5破位容差.md，owner 2026-08-26 批复）：
  0826 515180 防守仓被 −0.10%（0.0014 元）噪音级破位全离，卖在当日最低 1.435，
  当日收 1.455 站回 MA5——whipsaw 净亏 −843.30 + 机会成本 ≈−1,000。
  B22 新增 STOCK_PARAMS.ma5_break_tolerance（比例，默认 0.0）：
    cp < ma5×(1−tol) 才 MA5_EXIT；买侧禁令/解禁判定同一口径（同一容差）。
  515180 tol=0.005（0.5%），其余 16 票 tol=0（owner 纪律"跌破五日均线直接离场"不动）。
  顺带：600481 m2_lot_value_min=400（单手 435 元，留 ~8% 余量）。

验证范围（方案测试计划 7 项）：
  T1  515180 tol=0.005：−0.1% 触碰 → 不触发 MA5_EXIT（0826 whipsaw 回归）
  T2  515180 −0.6% 真破位 → MA5_EXIT 全离 50000 + 当日破位标记 _ma5_broken
  T3  默认票（tol=0）行为不变：任意 cp<ma5 即 MA5_EXIT（0820 三票回归）；
      买侧同口径：默认票 BASE 建仓 cp<ma5 → ma5_break_block 拦截
  T4  买侧禁令同容差：515180 cp=ma5×(1−0.3%) 容差内 → 不判破位、可正常评估建仓
      （信号通道 BUY_LOW 正常下单 + BASE 通道正常建仓，均无 ma5_break_block）
  T5  边界：cp 恰等于 ma5×(1−tol) → 不触发（严格小于）
  T6  600481 M2 池闸：lot=435 ≥ 400 → _m2_pool_pass=True；lot=390 < 400 → False
  T7  全回归套件 + WP-E4 16 测试复跑（单独命令执行）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b22.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pandas as pd

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gmwpb22_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from data.indicators import Signal  # noqa: E402
import signals.position_builder as pb  # noqa: E402
import main  # noqa: E402
from config.params import STOCK_PARAMS  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b22.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b22.json")

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


# ── 场景常量 ──
CODE_ETF = "515180"
GM_ETF = main.STOCKS[CODE_ETF]
MA5_ETF = 1.455                    # 0826 当日收 1.455 站回 MA5
TOL_ETF = float(STOCK_PARAMS[CODE_ETF]["ma5_break_tolerance"])
ETF_CP_NOISE = MA5_ETF * (1 - 0.001)     # −0.1% 噪音触碰（容差内）
ETF_CP_TRUE = MA5_ETF * (1 - 0.006)      # −0.6% 真破位（超容差）
ETF_CP_BAN_TOL = MA5_ETF * (1 - 0.003)   # −0.3%（买侧容差内）
ETF_CP_BOUND = MA5_ETF * (1 - TOL_ETF)   # 恰等于 ma5×(1−tol)

CODE_DEF = "603667"
GM_DEF = main.STOCKS[CODE_DEF]
MA5_DEF = 56.0
DEF_CP = MA5_DEF * (1 - 0.003)           # −0.3%（tol=0 → 破位）

DAILY_CTX = {"index_regime": "range", "intraday_alerts": [],
             "daily_atr": 0.02, "_m2_pool_pass": True}


def make_df(price, t, n=30):
    """构造 _build_bar_df 可用的合成 1 分钟 df（被打桩，仅需非空）。"""
    end = pd.Timestamp(t)
    rows = []
    for i in range(n):
        ts = end - timedelta(minutes=n - 1 - i)
        c = price if i == n - 1 else price * 0.999
        rows.append({
            "time": ts, "open": c, "high": c + 0.03, "low": c - 0.03,
            "close": c, "volume": 10000, "amount": c * 10000,
            "vwap": c, "rsi": 50.0, "bb_pct": 0.5,
            "macd_hist": 0.0, "ema_spread": 0.0, "range_pos": 0.5,
            "vol_ratio": 1.0, "mom5": 0.0, "lower_shadow": 0.0,
            "upper_shadow": 0.0, "day_amplitude": 0.02,
            "date": str(ts.date()), "prev_high": price * 1.02,
        })
    return pd.DataFrame(rows)


def make_ctx(code, now_str, qty, cost, base_ref=None, trend="TREND_RANGE"):
    """构造可通过 on_bar 前导段（D1/D4/心跳/底仓跳过）直达 MA5 判定区的假 context。"""
    gm_sym = main.STOCKS[code]
    eng = SignalEngine()
    eng._last_feats[code] = {"profit_pct": 0.0}
    eng.evaluate = lambda *a, **k: (0.0, 0.0, None)
    dt = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    ctx = SimpleNamespace(
        now=dt,
        cur_date=None,
        daily_buy_count={}, daily_sell_count={}, daily_trade_price={},
        engine=eng,
        _last_ir_date=dt.date(),
        mode=None,
        _last_bar_eob={},
        bar_cache={},
        manual_position={gm_sym: {"name": main.STOCK_NAMES.get(code, code), "qty": qty,
                                  "available": qty, "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
        executed_orders={},
        _base_ordered=set(),
        _base_settled={code},
        _inflight_sell={},
        sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200,
                              calc_buy_qty=lambda *a, **k: 100),
        latest_pre_close={code: cost},
        total_trade_count=0,
        rejected_order_count=0,
        audit_records=[],
        last_index_regime="range",
        last_index_score=0.0,
        _daily_ctx_cache_map={},
        _pending_sell_action={},
        _day_open={},
        account=lambda: SimpleNamespace(cash=lambda: 1000000.0),
    )
    ctx._trend_override = trend
    if base_ref is not None:
        setattr(ctx, f"_base_ref_{code}", base_ref)
    return ctx


def drive_bar(ctx, code, now_str, close, ma5, trend=None, buy_sig=None):
    """驱动 main.on_bar 一根 bar；返回本 bar 内 order_volume 的调用列表。"""
    ctx.now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    gm_sym = main.STOCKS[code]
    df = make_df(close, now_str)
    if trend is None:
        trend = getattr(ctx, "_trend_override", "TREND_RANGE")
    if buy_sig is not None:
        ctx.engine.evaluate = lambda *a, **k: (buy_sig.score, 0.0, buy_sig)
    calls = []
    with mock.patch.object(main, "_build_bar_df", return_value=df), \
         mock.patch.object(main, "_refresh_daily_ctx",
                           return_value=dict(DAILY_CTX, _stock_trend_state=trend, daily_ma5=ma5)), \
         mock.patch.object(main, "_get_holding",
                           side_effect=lambda c, cd, s: dict(c.manual_position[s])), \
         mock.patch.object(main, "_base_topup_qty", return_value=0), \
         mock.patch.object(main.ops_guard, "ensure_watcher", return_value=None), \
         mock.patch.object(main, "check_kill_switch", return_value=False), \
         mock.patch.object(main, "order_volume", side_effect=lambda **kw: calls.append(kw)):
        main.on_bar(ctx, [{"symbol": gm_sym, "eob": now_str + ":00",
                           "open": close, "high": close + 0.03, "low": close - 0.03,
                           "close": close, "volume": 10000, "amount": close * 10000}])
    return calls


def sell_calls(calls):
    return [c for c in calls if c.get("side") == main.OrderSide_Sell]


def buy_calls(calls):
    return [c for c in calls if c.get("side") == main.OrderSide_Buy]


def buy_low_sig(code, price, score=70.0):
    return Signal(code=code, name=main.STOCK_NAMES.get(code, code), action="BUY_LOW",
                  price=price, score=score, reasons=["b22-test"])


def read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_audit():
    if not os.path.exists(main._AUDIT_LOG_PATH):
        return []
    with open(main._AUDIT_LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def ev_signals(action=None):
    return [e for e in read_events()
            if e.get("event") == "signal" and (action is None or e.get("action") == action)]


def ev_risk(kind=None):
    return [e for e in read_events()
            if e.get("event") == "risk" and (kind is None or e.get("kind") == kind)]


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


def clear_audit():
    if main._audit_file:
        main._audit_file.close()
        main._audit_file = None
    open(main._AUDIT_LOG_PATH, "w", encoding="utf-8").close()


# ══ T0: 参数就位 ══
check("T0a 515180 ma5_break_tolerance==0.005", TOL_ETF == 0.005, f"tol={TOL_ETF}")
check("T0b 默认票(603667) ma5_break_tolerance 缺省 0.0",
      STOCK_PARAMS[CODE_DEF].get("ma5_break_tolerance", 0.0) == 0.0,
      f"tol={STOCK_PARAMS[CODE_DEF].get('ma5_break_tolerance', 0.0)}")
check("T0c 600481 m2_lot_value_min==400",
      STOCK_PARAMS["600481"].get("m2_lot_value_min") == 400,
      f"lot_min={STOCK_PARAMS['600481'].get('m2_lot_value_min')}")
check("T0d 600481 m2_amount20_min==1.5亿（0827 owner 批复）",
      STOCK_PARAMS["600481"].get("m2_amount20_min") == 150000000,
      f"amt_min={STOCK_PARAMS['600481'].get('m2_amount20_min')}")
check("T0d 515180 amp/amt 阈值不受影响",
      STOCK_PARAMS[CODE_ETF].get("m2_amp20_min") == 0.005
      and STOCK_PARAMS[CODE_ETF].get("m2_amount20_min") == 30000000,
      f"amp={STOCK_PARAMS[CODE_ETF].get('m2_amp20_min')} "
      f"amt={STOCK_PARAMS[CODE_ETF].get('m2_amount20_min')}")


# ══ T1: 515180 −0.1% 触碰（容差内）→ 不触发 MA5_EXIT（0826 whipsaw 回归） ══
clear_events(); clear_audit()
c1 = make_ctx(CODE_ETF, "2026-08-26 09:35:00", qty=50000, cost=1.451, base_ref=50000)
calls1 = drive_bar(c1, CODE_ETF, "2026-08-26 09:35:00", ETF_CP_NOISE, ma5=MA5_ETF)
check("T1a −0.1% 触碰 → 无卖出（容差 0.5% 过滤噪音）",
      len(sell_calls(calls1)) == 0, f"sells={[c.get('volume') for c in sell_calls(calls1)]}")
check("T1b 无 MA5_EXIT 信号事件",
      len(ev_signals("MA5_EXIT")) == 0, f"sig={[e.get('action') for e in ev_signals()]}")
check("T1c 未置破位标记 _ma5_broken",
      getattr(c1, "_ma5_broken", {}).get(CODE_ETF) is None,
      f"broken={getattr(c1, '_ma5_broken', {}).get(CODE_ETF)}")


# ══ T2: 515180 −0.6% 真破位 → MA5_EXIT 全离 + 当日破位标记 ══
clear_events(); clear_audit()
c2 = make_ctx(CODE_ETF, "2026-08-26 09:35:00", qty=50000, cost=1.451, base_ref=50000)
calls2 = drive_bar(c2, CODE_ETF, "2026-08-26 09:35:00", ETF_CP_TRUE, ma5=MA5_ETF)
s2 = sell_calls(calls2)
check("T2a −0.6% 真破位 → MA5_EXIT 全离 50000",
      len(s2) == 1 and s2[0]["volume"] == 50000, f"sells={[c.get('volume') for c in s2]}")
check("T2b MA5_EXIT 信号事件 1 条", len(ev_signals("MA5_EXIT")) == 1,
      f"sig={len(ev_signals('MA5_EXIT'))}")
check("T2c 当日破位标记 _ma5_broken",
      getattr(c2, "_ma5_broken", {}).get(CODE_ETF) == "2026-08-26",
      f"broken={getattr(c2, '_ma5_broken', {}).get(CODE_ETF)}")


# ══ T3: 默认票（tol=0）行为不变 ══
clear_events(); clear_audit()
c3 = make_ctx(CODE_DEF, "2026-08-20 09:35:00", qty=500, cost=50.0, base_ref=500)
calls3 = drive_bar(c3, CODE_DEF, "2026-08-20 09:35:00", DEF_CP, ma5=MA5_DEF)
s3 = sell_calls(calls3)
check("T3a 默认票 tol=0：cp<ma5（−0.3%）→ MA5_EXIT 触发全离（0820 三票回归）",
      len(s3) == 1 and s3[0]["volume"] == 500, f"sells={[c.get('volume') for c in s3]}")
check("T3b MA5_EXIT 信号事件 1 条", len(ev_signals("MA5_EXIT")) == 1,
      f"sig={len(ev_signals('MA5_EXIT'))}")

# 买侧同口径：默认票 BASE 建仓 cp<ma5 → ma5_break_block 拦截（tol=0 零容差）
clear_events(); clear_audit()
c3b = make_ctx(CODE_DEF, "2026-08-20 09:35:00", qty=0, cost=50.0, base_ref=500)
c3b._base_settled = set()   # 未建仓 → 走 BASE 路径（MIRROR 603667 qty=800）
calls3b = drive_bar(c3b, CODE_DEF, "2026-08-20 09:35:00", DEF_CP, ma5=MA5_DEF)
check("T3c 默认票 BASE 建仓 cp<ma5 → 无下单（ma5_break_block 拦截）",
      len(buy_calls(calls3b)) == 0, f"buys={len(buy_calls(calls3b))}")
check("T3d 默认票买侧 ma5_break_block 留痕",
      len(ev_risk("ma5_break_block")) >= 1, f"risk={len(ev_risk('ma5_break_block'))}")


# ══ T4: 买侧禁令同容差——515180 cp=ma5×(1−0.3%) 容差内不判破位 ══
# 信号通道：持 50000，BUY_LOW 正常下单（1818 买侧比较不拦截、1575 卖侧不替换）。
# 用 09:36 驱动——09:35 落在 N6 开盘买入隔离窗(09:00-09:35)内，BUY_LOW 会被隔离，
# 测不到 B22 买侧容差行为。
clear_events(); clear_audit()
c4 = make_ctx(CODE_ETF, "2026-08-26 09:36:00", qty=50000, cost=1.451, base_ref=50000)
c4.sizer.calc_buy_qty = lambda *a, **k: 500
sig4 = buy_low_sig(CODE_ETF, ETF_CP_BAN_TOL, score=70.0)
calls4 = drive_bar(c4, CODE_ETF, "2026-08-26 09:36:00", ETF_CP_BAN_TOL, ma5=MA5_ETF, buy_sig=sig4)
b4 = buy_calls(calls4)
check("T4a 信号通道：−0.3% 容差内 BUY_LOW 正常下单 500",
      len(b4) == 1 and b4[0]["volume"] == 500, f"buys={[(c.get('volume')) for c in b4]}")
check("T4b 信号通道：无 MA5_EXIT 替换（卖侧同容差不触发）",
      len(ev_signals("MA5_EXIT")) == 0, f"sig={[e.get('action') for e in ev_signals()]}")
check("T4c 信号通道：无 ma5_break_block 拦截",
      len(ev_risk("ma5_break_block")) == 0, f"risk={len(ev_risk('ma5_break_block'))}")

# BASE 通道：未持仓，−0.3% 容差内 → 正常建仓（1375 BASE 比较不拦截）
clear_events(); clear_audit()
c4b = make_ctx(CODE_ETF, "2026-08-26 09:35:00", qty=0, cost=1.451, base_ref=50000)
c4b._base_settled = set()   # 未建仓 → 走 BASE 路径（MIRROR 515180 qty=50000）
with mock.patch.object(pb, "eval_dual_channels",
                       return_value={"verdict": "signal", "channel": "ice",
                                     "composite_score": 85.0}):
    calls4b = drive_bar(c4b, CODE_ETF, "2026-08-26 09:35:00", ETF_CP_BAN_TOL, ma5=MA5_ETF)
b4b = buy_calls(calls4b)
check("T4d BASE 通道：−0.3% 容差内正常建仓 50000（MA5 未拦截）",
      len(b4b) == 1 and b4b[0]["volume"] == 50000, f"buys={[(c.get('volume')) for c in b4b]}")
check("T4e BASE 通道：无 ma5_break_block 拦截",
      len(ev_risk("ma5_break_block")) == 0, f"risk={len(ev_risk('ma5_break_block'))}")


# ══ T5: 边界——cp 恰等于 ma5×(1−tol) → 不触发（严格小于） ══
clear_events(); clear_audit()
c5 = make_ctx(CODE_ETF, "2026-08-26 09:35:00", qty=50000, cost=1.451, base_ref=50000)
calls5 = drive_bar(c5, CODE_ETF, "2026-08-26 09:35:00", ETF_CP_BOUND, ma5=MA5_ETF)
check("T5a cp == ma5×(1−tol) → 不触发 MA5_EXIT（严格 <）",
      len(sell_calls(calls5)) == 0 and len(ev_signals("MA5_EXIT")) == 0,
      f"sells={len(sell_calls(calls5))} sig={len(ev_signals('MA5_EXIT'))}")


# ══ T6: 600481 M2 池闸（lot 阈值 400） ══
def make_daily_rows(close, half_range, volume, n=120):
    """构造日线：close 恒定 → amp20=2*half_range/close；amount20=volume*close；lot=close*100。"""
    rows = []
    end = pd.Timestamp("2026-08-21 15:00:00")
    for i in range(n):
        ts = end - timedelta(days=n - 1 - i)
        rows.append({"eob": ts, "open": close, "high": close + half_range,
                     "low": close - half_range, "close": close, "volume": volume})
    return rows


def fresh_ctx():
    return SimpleNamespace(_daily_ctx_cache_map={}, latest_pre_close={})


NOW_PG = datetime(2026, 8, 24, 9, 31, 0)
with mock.patch.object(main, "history_n", return_value=make_daily_rows(4.35, 0.10, 60000000)):
    ctx_ok = main._refresh_daily_ctx(fresh_ctx(), "600481", main.STOCKS["600481"], NOW_PG)
check("T6a 600481 lot=435(≥400) 且 amp/amt 过缺省 → _m2_pool_pass=True",
      ctx_ok.get("_m2_pool_pass") is True,
      f"amp={ctx_ok.get('_m2_amp20',0):.4f} amt={ctx_ok.get('_m2_amount20',0):.2e} "
      f"lot={ctx_ok.get('_m2_lot_value',0):.0f} pass={ctx_ok.get('_m2_pool_pass')}")
with mock.patch.object(main, "history_n", return_value=make_daily_rows(3.90, 0.10, 60000000)):
    ctx_low = main._refresh_daily_ctx(fresh_ctx(), "600481", main.STOCKS["600481"], NOW_PG)
check("T6b 600481 lot=390(<400) → 仅 lot 卡 → _m2_pool_pass=False",
      ctx_low.get("_m2_pool_pass") is False
      and ctx_low.get("daily_status") == "pool_gate_fail",
      f"amp={ctx_low.get('_m2_amp20',0):.4f} amt={ctx_low.get('_m2_amount20',0):.2e} "
      f"lot={ctx_low.get('_m2_lot_value',0):.0f} pass={ctx_low.get('_m2_pool_pass')}")

# ── T6c/T6d: 600481 amt 覆盖 1.5 亿（0827 owner 批复；0826-0827 解禁空转 amt=1.97 亿卡全局 2 亿线） ──
with mock.patch.object(main, "history_n", return_value=make_daily_rows(4.35, 0.10, 39000000)):
    ctx_amt_ok = main._refresh_daily_ctx(fresh_ctx(), "600481", main.STOCKS["600481"], NOW_PG)
check("T6c 600481 amt=1.70亿(≥1.5亿,<旧全局2亿) → _m2_pool_pass=True",
      ctx_amt_ok.get("_m2_pool_pass") is True,
      f"amt={ctx_amt_ok.get('_m2_amount20',0):.2e} pass={ctx_amt_ok.get('_m2_pool_pass')}")
with mock.patch.object(main, "history_n", return_value=make_daily_rows(4.35, 0.10, 33000000)):
    ctx_amt_low = main._refresh_daily_ctx(fresh_ctx(), "600481", main.STOCKS["600481"], NOW_PG)
check("T6d 600481 amt=1.44亿(<1.5亿) → 仅 amt 卡 → _m2_pool_pass=False",
      ctx_amt_low.get("_m2_pool_pass") is False,
      f"amt={ctx_amt_low.get('_m2_amount20',0):.2e} pass={ctx_amt_low.get('_m2_pool_pass')}")

# 默认票不受 600481 覆盖影响（全局 amt 仍 2 亿）
with mock.patch.object(main, "history_n", return_value=make_daily_rows(10.0, 0.30, 19000000)):
    ctx_def = main._refresh_daily_ctx(fresh_ctx(), "000988", main.STOCKS["000988"], NOW_PG)
check("T6e 默认票 000988 amt=1.90亿(<全局2亿) 仍被卡 → _m2_pool_pass=False",
      ctx_def.get("_m2_pool_pass") is False,
      f"amt={ctx_def.get('_m2_amount20',0):.2e} pass={ctx_def.get('_m2_pool_pass')}")


# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
