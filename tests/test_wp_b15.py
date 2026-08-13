# coding=utf-8
"""
tests/test_wp_b15.py — WP-B15 验证：信号刷屏节流（事件层去重）

背景（详见 docs/回测复盘/修复方案_WP-B15-B16_信号刷屏节流与000988权重冲突.md）：
  0813 603667 TARGET_SELL 地板拦截重复触发 98 次、000988 BUY_LOW 到顶重复 37 次。
  B-15 在事件层去重（记录层节流，决策层不动）：
    2.1 地板事件去重——同持仓状态重复拦截只写 1 条 floor_protection + sell_skip；
    2.2 信号 mute——被下游拦截点（地板/到顶）拦截的信号，同源信号事件静默；
        mute 键含日期（日切自清），持仓变化由成交/对账/拒单回滚清键。

验证范围：
  T1  地板去重：pos=floor 的 TARGET 贴线连续 10 bar → floor_protection + sell_skip 各 1 条；
      信号事件 1 条；10 bar 均无下单
  T2  状态变化再写：同上 + 中途 pos 变化(100→140 仍在地板) → 再次拦截写第 2 条
  T3  解封即成交：地板拦 N bar 后 pos 升至地板上方（成交清键）→ 当 bar TARGET 下单成交，
      事件正常写，B-14 置位 pending→filled
  T4  BUY_LOW mute：pos=ceiling 连续 10 bar BUY_LOW → 信号事件 1 条 + max_pos_cap 1 条；
      卖出破 ceiling 后恢复（信号事件第 2 条）
  T5  跨日重置：日切后同场景 → 键自清，新日记首条照写
  T6  回归保护：非拦截信号（正常 BUY_LOW/SELL_HIGH）→ 事件逐条照写，不误伤

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b15.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb15_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from data.indicators import Signal  # noqa: E402
import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b15.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b15.json")

CODE = "603667"
GM_SYM = main.STOCKS[CODE]
PROFIT_CP = 57.28   # 成本 52.0 → +10.2%（TARGET 触发，floor 场景）
COST = 52.0

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def make_df(price, t, n=30):
    """构造 FeatureExtractor 可用的合成 1 分钟 df（_build_bar_df 被打桩，仅需非空）。"""
    end = pd.Timestamp(t)
    rows = []
    for i in range(n):
        ts = end - timedelta(minutes=n - 1 - i)
        c = price if i == n - 1 else price * 0.999
        rows.append({
            "time": ts, "open": c, "high": c + 0.03, "low": c - 0.03,
            "close": c, "volume": 10000, "amount": c * 10000,
            "vwap": price, "rsi": 50.0, "bb_pct": 0.5,
            "macd_hist": 0.0, "ema_spread": 0.0, "range_pos": 0.5,
            "vol_ratio": 1.0, "mom5": 0.0, "lower_shadow": 0.0,
            "upper_shadow": 0.0, "day_amplitude": 0.02,
            "date": str(ts.date()), "prev_high": price * 1.02,
        })
    return pd.DataFrame(rows)


DAILY_CTX = {"index_regime": "range", "intraday_alerts": [],
             "daily_atr": 0.02, "_stock_trend_state": "TREND_RANGE",
             "_m2_pool_pass": True, "daily_prev_close": COST}


def make_ctx(now_str, qty=800, cost=COST, base_ref=None, trend="TREND_RANGE", mode=None):
    """构造可通过 on_bar 前导段（D1/D4/心跳/底仓跳过）直达门控链的假 context。"""
    eng = SignalEngine()
    eng._last_feats[CODE] = {"profit_pct": 0.0}
    eng.evaluate = lambda *a, **k: (0.0, 0.0, None)
    dt = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    ctx = SimpleNamespace(
        now=dt,
        cur_date=None,
        daily_buy_count={}, daily_sell_count={}, daily_trade_price={},
        engine=eng,
        _last_ir_date=dt.date(),
        mode=mode,
        _last_bar_eob={},
        bar_cache={},
        manual_position={GM_SYM: {"name": "五洲新春", "qty": qty, "available": qty,
                                  "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
        executed_orders={},
        _base_ordered=set(),
        _base_settled={CODE},
        _inflight_sell={},
        sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200),
        latest_pre_close={CODE: cost},
        total_trade_count=0,
        rejected_order_count=0,
        audit_records=[],
        last_index_regime="range",
        last_index_score=0.0,
        _daily_ctx_cache_map={},
        _pending_sell_action={},
    )
    ctx._trend_override = trend
    if base_ref is not None:
        setattr(ctx, f"_base_ref_{CODE}", base_ref)
    return ctx


def drive_bar(ctx, now_str, close, trend=None):
    """驱动 main.on_bar 一根 bar；返回本 bar 内 order_volume 的调用列表。
    必须先对齐 ctx.now（on_bar 从 context.now 取时钟，否则跨 bar 复用旧时点）。"""
    ctx.now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    df = make_df(close, now_str)
    if trend is None:
        trend = getattr(ctx, "_trend_override", "TREND_RANGE")
    calls = []
    with mock.patch.object(main, "_build_bar_df", return_value=df), \
         mock.patch.object(main, "_refresh_daily_ctx", return_value=dict(DAILY_CTX, _stock_trend_state=trend)), \
         mock.patch.object(main, "_get_holding",
                           side_effect=lambda c, cd, s: dict(c.manual_position[s])), \
         mock.patch.object(main, "_base_topup_qty", return_value=0), \
         mock.patch.object(main.ops_guard, "ensure_watcher", return_value=None), \
         mock.patch.object(main, "check_kill_switch", return_value=False), \
         mock.patch.object(main, "order_volume", side_effect=lambda **kw: calls.append(kw)):
        main.on_bar(ctx, [{"symbol": GM_SYM, "eob": now_str + ":00",
                           "open": close, "high": close + 0.03, "low": close - 0.03,
                           "close": close, "volume": 10000, "amount": close * 10000}])
    return calls


def clear_audit():
    if main._audit_file:
        main._audit_file.close()
        main._audit_file = None
    open(main._AUDIT_LOG_PATH, "w", encoding="utf-8").close()


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


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
    out = []
    for e in read_events():
        if e.get("event") == "signal" and (action is None or e.get("action") == action):
            out.append(e)
    return out


def ev_risk(kind=None):
    return [e for e in read_events()
            if e.get("event") == "risk" and (kind is None or e.get("kind") == kind)]


def audit_sell_skip(reason="floor"):
    return [e for e in read_audit()
            if e.get("event") == "sell_skip" and e.get("reason") == reason]


def l1_state(ctx):
    return ctx.manual_position[GM_SYM].get("_target_l1_state")


def buy_low_sig(price, score=60.0):
    return Signal(code=CODE, name="五洲新春", action="BUY_LOW",
                  price=price, score=score, reasons=["b15-test"])


def sell_high_sig(price, score=75.0):
    return Signal(code=CODE, name="五洲新春", action="SELL_HIGH",
                  price=price, score=score, reasons=["b15-test"])


# ══ T1: 地板去重 —— pos=floor 的 TARGET 贴线连续 10 bar，各通道只留 1 条 ══
clear_audit(); clear_events()
c1 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=100)  # min_hold=50, pos-100<50
all_calls = []
for i in range(10):
    all_calls.append(drive_bar(c1, f"2026-08-12 09:{36+i:02d}:00", PROFIT_CP))
check("T1a 10 bar 均无下单（地板拦截）",
      all(len(c) == 0 for c in all_calls), f"orders={[len(c) for c in all_calls]}")
check("T1b 信号事件仅 1 条（TARGET 贴线 10 bar → 98→1 的验收口径）",
      len(ev_signals("TARGET_SELL")) == 1, f"信号事件={len(ev_signals('TARGET_SELL'))}")
check("T1c floor_protection 仅 1 条", len(ev_risk("floor_protection")) == 1,
      f"floor={len(ev_risk('floor_protection'))}")
check("T1d sell_skip(reason=floor) 仅 1 条", len(audit_sell_skip("floor")) == 1,
      f"skip={len(audit_sell_skip('floor'))}")
check("T1e mute 已置位（TARGET 地板同源信号静默）",
      getattr(c1, f"_sig_muted_{CODE}_TARGET_SELL", "") == "2026-08-12",
      f"mute={getattr(c1, f'_sig_muted_{CODE}_TARGET_SELL', '')}")

# ══ T2: 状态变化再写 —— 中途 pos 变化(100→140 仍在地板) → 第 2 条 ══
clear_audit(); clear_events()
c2 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=100)
drive_bar(c2, "2026-08-12 09:36:00", PROFIT_CP)
drive_bar(c2, "2026-08-12 09:37:00", PROFIT_CP)
drive_bar(c2, "2026-08-12 09:38:00", PROFIT_CP)
c2.manual_position[GM_SYM]["qty"] = 140   # 仍在地板（140-100=40<50）但状态签名变化
drive_bar(c2, "2026-08-12 09:39:00", PROFIT_CP)
check("T2a 状态变化后再拦截写第 2 条 floor_protection",
      len(ev_risk("floor_protection")) == 2, f"floor={len(ev_risk('floor_protection'))}")
check("T2b 状态变化后再拦截写第 2 条 sell_skip(floor)",
      len(audit_sell_skip("floor")) == 2, f"skip={len(audit_sell_skip('floor'))}")
check("T2c 信号事件仍 1 条（mute 未解，无新信息）",
      len(ev_signals("TARGET_SELL")) == 1, f"信号={len(ev_signals('TARGET_SELL'))}")

# ══ T3: 解封即成交 —— 成交清键后，当 bar TARGET 下单成交，B-14 pending→filled ══
clear_audit(); clear_events()
c3 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=100)
drive_bar(c3, "2026-08-12 09:36:00", PROFIT_CP)          # bar1: 地板拦 + mute
# 买入成交抬升持仓 → 清 mute 键（真实路径：成交回调 on_order_status）
main.on_order_status(c3, {"symbol": GM_SYM, "status": 3, "volume": 200,
                          "side": 1, "price": PROFIT_CP, "filled_vwap": PROFIT_CP})
c3.manual_position[GM_SYM]["qty"] = 300
c3.manual_position[GM_SYM]["available"] = 300
c3.manual_position[GM_SYM]["t_qty"] = 300
check("T3a 成交回调清空 mute 键", getattr(c3, f"_sig_muted_{CODE}_TARGET_SELL", None) != "2026-08-12",
      f"mute={getattr(c3, f'_sig_muted_{CODE}_TARGET_SELL', '')!r}")
calls3 = drive_bar(c3, "2026-08-12 09:37:00", PROFIT_CP)  # bar2: 过地板 → 下单
check("T3b 解封当 bar TARGET 正常下单", len(calls3) == 1 and calls3[0]["symbol"] == GM_SYM,
      f"orders={len(calls3)}")
check("T3c 信号事件正常写（第 2 条）", len(ev_signals("TARGET_SELL")) == 2,
      f"信号={len(ev_signals('TARGET_SELL'))}")
check("T3d B-14 置位 pending（成交前防竞态）", l1_state(c3) == "pending",
      f"state={l1_state(c3)}")
main.on_order_status(c3, {"symbol": GM_SYM, "status": 3, "volume": 200,
                          "side": 2, "price": PROFIT_CP, "filled_vwap": PROFIT_CP})
check("T3e B-14 成交回调 → filled", l1_state(c3) == "filled", f"state={l1_state(c3)}")

# ══ T4: BUY_LOW mute —— pos=ceiling 连续 10 bar，卖出破 ceiling 后恢复 ══
clear_audit(); clear_events()
c4 = make_ctx("2026-08-12 09:36:00", qty=500, base_ref=500, cost=COST)  # ceiling=max(mps,500)=500
c4.engine._last_feats[CODE] = {"profit_pct": 0.01}
c4.engine.evaluate = lambda *a, **k: (60.0, 0.0, buy_low_sig(52.5))
c4.sizer.calc_buy_qty = lambda *a, **k: 100
calls4 = []
for i in range(10):
    calls4.append(drive_bar(c4, f"2026-08-12 09:{36+i:02d}:00", 52.5))
check("T4a 10 bar 均无下单（到顶拦截）", all(len(c) == 0 for c in calls4),
      f"orders={[len(c) for c in calls4]}")
check("T4b 信号事件仅 1 条（BUY_LOW 到顶 37→1）",
      len(ev_signals("BUY_LOW")) == 1, f"信号={len(ev_signals('BUY_LOW'))}")
check("T4c max_pos_cap 仅 1 条", len(ev_risk("max_pos_cap")) == 1,
      f"cap={len(ev_risk('max_pos_cap'))}")
check("T4d BUY_LOW mute 已置位", getattr(c4, f"_sig_muted_{CODE}_BUY_LOW", "") == "2026-08-12",
      f"mute={getattr(c4, f'_sig_muted_{CODE}_BUY_LOW', '')}")
# 卖出 200 破 ceiling（500→300 < 500）→ 清 mute，信号恢复
main.on_order_status(c4, {"symbol": GM_SYM, "status": 3, "volume": 200,
                          "side": 2, "price": 52.5, "filled_vwap": 52.5})
c4.manual_position[GM_SYM]["qty"] = 300
c4.manual_position[GM_SYM]["available"] = 300
c4.manual_position[GM_SYM]["t_qty"] = 300
drive_bar(c4, "2026-08-12 09:47:00", 52.5)
check("T4e 卖出破 ceiling 后信号恢复（第 2 条）", len(ev_signals("BUY_LOW")) == 2,
      f"信号={len(ev_signals('BUY_LOW'))}")
check("T4f max_pos_cap 仍 1 条（未重复）", len(ev_risk("max_pos_cap")) == 1,
      f"cap={len(ev_risk('max_pos_cap'))}")
# 回补至 ceiling（500）再次到顶 → mute 重新置位；过渡当 bar 写 1 条（仓位重回到顶的信息），
# 后续静默（mute 由上一次拦截置位，写入检查在其之后——符合"被拦后同源信号静默"语义）
c4.manual_position[GM_SYM]["qty"] = 500
c4.manual_position[GM_SYM]["available"] = 500
c4.manual_position[GM_SYM]["t_qty"] = 500
drive_bar(c4, "2026-08-12 09:48:00", 52.5)
check("T4g 再次到顶过渡 bar 写 1 条（仓位重回到顶）且 mute 已重新置位",
      len(ev_signals("BUY_LOW")) == 3 and getattr(c4, f"_sig_muted_{CODE}_BUY_LOW", "") == "2026-08-12",
      f"信号={len(ev_signals('BUY_LOW'))} mute={getattr(c4, f'_sig_muted_{CODE}_BUY_LOW', '')}")
drive_bar(c4, "2026-08-12 09:49:00", 52.5)
check("T4h 到顶后续 bar 静默，信号不再增长",
      len(ev_signals("BUY_LOW")) == 3, f"信号={len(ev_signals('BUY_LOW'))}")

# ══ T5: 跨日重置 —— 日切后键自清，新日记首条照写 ══
clear_audit(); clear_events()
c5 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=100)
drive_bar(c5, "2026-08-12 09:36:00", PROFIT_CP)   # 0812: 1 条
drive_bar(c5, "2026-08-13 09:36:00", PROFIT_CP)   # 0813: 新日 → 再写 1 条
check("T5a 信号事件每日各 1 条（共 2）", len(ev_signals("TARGET_SELL")) == 2,
      f"信号={len(ev_signals('TARGET_SELL'))}")
check("T5b 新日记 floor_protection 照写（共 2）", len(ev_risk("floor_protection")) == 2,
      f"floor={len(ev_risk('floor_protection'))}")
check("T5c 新日记 sell_skip(floor) 照写（共 2）", len(audit_sell_skip("floor")) == 2,
      f"skip={len(audit_sell_skip('floor'))}")
check("T5d 两日信号事件日期不同",
      {e.get("time", "")[:10] for e in ev_signals("TARGET_SELL")} == {"2026-08-12", "2026-08-13"},
      f"日期={ {e.get('time', '')[:10] for e in ev_signals('TARGET_SELL')} }")

# ══ T6: 回归保护 —— 非拦截信号逐条照写，不误伤 ══
clear_audit(); clear_events()
c6 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=200)  # pos<ceiling(200)，永不拦
c6.engine._last_feats[CODE] = {"profit_pct": 0.01}
c6.engine.evaluate = lambda *a, **k: (60.0, 0.0, buy_low_sig(52.5))
c6.sizer.calc_buy_qty = lambda *a, **k: 100
for i in range(3):
    drive_bar(c6, f"2026-08-12 09:{36+i:02d}:00", 52.5)
check("T6a 正常 BUY_LOW 3 bar 逐条照写", len(ev_signals("BUY_LOW")) == 3,
      f"信号={len(ev_signals('BUY_LOW'))}")
check("T6b 未到顶 → 无 max_pos_cap", len(ev_risk("max_pos_cap")) == 0,
      f"cap={len(ev_risk('max_pos_cap'))}")

clear_audit(); clear_events()
c6b = make_ctx("2026-08-12 09:36:00", qty=800, base_ref=500, cost=COST)
c6b.engine._last_feats[CODE] = {"profit_pct": 0.03}
c6b.engine.evaluate = lambda *a, **k: (0.0, 75.0, sell_high_sig(53.5))
for i in range(3):
    drive_bar(c6b, f"2026-08-12 09:{36+i:02d}:00", 53.5)
check("T6c 正常 SELL_HIGH 3 bar 逐条照写", len(ev_signals("SELL_HIGH")) == 3,
      f"信号={len(ev_signals('SELL_HIGH'))}")
check("T6d 非地板 → 无 floor_protection", len(ev_risk("floor_protection")) == 0,
      f"floor={len(ev_risk('floor_protection'))}")
check("T6e 非地板 → 无 sell_skip(floor)", len(audit_sell_skip("floor")) == 0,
      f"skip={len(audit_sell_skip('floor'))}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
