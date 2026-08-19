# coding=utf-8
"""
tests/test_wp_b19.py — WP-B19 MA5破位全离通道 + O-12 pos_key容差 + O-13 推送节流 验证

背景（docs/回测复盘/施工方案_WP-B19_MA5破位全离通道.md）：
  0819 owner 决策"跌破五日均线应该直接离场"：第四类保护通道 MA5_EXIT（全离+当日禁买），
  补齐 TRAIL/PANIC/TREND_EXIT 未覆盖的 MA5 破位场景；同包 O-12 pos_key 成本容差、
  O-13 watcher 同票同 kind 风控推送节流。

验证范围（施工方案三节 8 场景）：
  T1  开盘破 MA5 持仓 500 全可用 → MA5_EXIT 500 全离，信号在 TRAIL 之前生成
  T2  破位日 BUY_LOW / buyback / BASE 三种买入意图 → 全部 ma5_break 拦截留痕
  T3  破位日部分仓位 T+1 锁定 → 卖可用部分；次日仍破位 → 续卖剩余
  T4  次日收复 MA5 → 买入禁令自动解除；无新 MA5_EXIT
  T5  同日 MA5 破位 + TRAIL 触发 → 仅 MA5_EXIT 成交（优先级 e），无 TRAIL_SELL
  T6  MA5_EXIT 成交后 → 无 buyback_armed 事件（f）
  T7  O-12: pos_key 成本差 +0.002 / +0.04（容差内）→ 恢复+指纹更新；+8% → 作废留痕
  T8  O-13: 同票同 kind 30min 内 3 次 entry_gate → 仅 1 条推送（事件流照写）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b19.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb19_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from data.indicators import Signal  # noqa: E402
import main  # noqa: E402
import gm_bridge.watcher as watcher  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b19.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b19.json")
watcher.BRIDGE_DIR = TMP

CODE = "603667"
GM_SYM = main.STOCKS[CODE]
MA5 = 56.0

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def make_df(price, t, n=30, vwap=None):
    vwap = vwap if vwap is not None else price
    end = pd.Timestamp(t)
    rows = []
    for i in range(n):
        ts = end - timedelta(minutes=n - 1 - i)
        c = price if i == n - 1 else price * 0.999
        rows.append({
            "time": ts, "open": c, "high": c + 0.03, "low": c - 0.03,
            "close": c, "volume": 10000, "amount": c * 10000,
            "vwap": vwap, "rsi": 50.0, "bb_pct": 0.5,
            "macd_hist": 0.0, "ema_spread": 0.0, "range_pos": 0.5,
            "vol_ratio": 1.0, "mom5": 0.0, "lower_shadow": 0.0,
            "upper_shadow": 0.0, "day_amplitude": 0.02,
            "date": str(ts.date()), "prev_high": price * 1.02,
        })
    return pd.DataFrame(rows)


DAILY_CTX = {"index_regime": "range", "intraday_alerts": [], "daily_atr": 0.02,
             "daily_ma5": MA5, "_m2_pool_pass": True}


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


def rm_state():
    if os.path.exists(main.SELL_STATE_PATH):
        os.remove(main.SELL_STATE_PATH)


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


def make_ctx(now_str, qty=800, cost=50.0, base_ref=None, trend="TREND_RANGE"):
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
        mode=None,
        _last_bar_eob={},
        bar_cache={},
        manual_position={GM_SYM: {"name": "五洲新春", "qty": qty, "available": qty,
                                  "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
        executed_orders={},
        _base_ordered=set(),
        _base_settled={CODE},
        _inflight_sell={},
        sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200,
                              calc_buy_qty=lambda *a, **k: 100),
        latest_pre_close={CODE: cost},
        total_trade_count=0,
        rejected_order_count=0,
        audit_records=[],
        last_index_regime="range",
        last_index_score=0.0,
        _daily_ctx_cache_map={},
        _pending_sell_action={},
        _day_open={},
    )
    ctx._trend_override = trend
    if base_ref is not None:
        setattr(ctx, f"_base_ref_{CODE}", base_ref)
    return ctx


def drive_bar(ctx, now_str, close, trend=None, ma5=MA5):
    ctx.now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    df = make_df(close, now_str)
    if trend is None:
        trend = getattr(ctx, "_trend_override", "TREND_RANGE")
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
        main.on_bar(ctx, [{"symbol": GM_SYM, "eob": now_str + ":00",
                           "open": close, "high": close + 0.03, "low": close - 0.03,
                           "close": close, "volume": 10000, "amount": close * 10000}])
    return calls


def sell_calls(calls):
    return [c for c in calls if c.get("side") == main.OrderSide_Sell]


def buy_low_sig(price, score=70.0):
    return Signal(code=CODE, name="五洲新春", action="BUY_LOW",
                  price=price, score=score, reasons=["b19-test"])


def live_ctx(qty, cost, now):
    eng = SignalEngine()
    return SimpleNamespace(
        mode=main.MODE_LIVE,
        now=now,
        engine=eng,
        manual_position={GM_SYM: {"name": "五洲新春", "qty": qty, "available": qty,
                                  "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
    )


# ══ T1: 开盘破 MA5 → MA5_EXIT 全离 500（TRAIL 之前生成） ══
clear_events()
c1 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
calls1 = drive_bar(c1, "2026-08-19 09:36:00", 54.0, ma5=MA5)   # cp 54 < ma5 56
s1 = sell_calls(calls1)
sig1 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "MA5_EXIT"]
check("T1a MA5_EXIT 全离 500 股（非 sizer 定量）",
      len(s1) == 1 and s1[0]["volume"] == 500, f"sells={[(c.get('volume')) for c in s1]}")
check("T1b 信号事件 action=MA5_EXIT 且无 TRAIL_SELL（优先级 e）",
      len(sig1) == 1 and not [e for e in read_events()
                              if e.get("event") == "signal" and e.get("action") == "TRAIL_SELL"],
      f"sig={sig1 and sig1[0].get('action')}")
check("T1c 破位日标记 _ma5_broken", getattr(c1, "_ma5_broken", {}).get(CODE) == "2026-08-19",
      f"broken={getattr(c1, '_ma5_broken', {}).get(CODE)}")

# ══ T2: 破位日禁一切买入（BUY_LOW 被 MA5_EXIT 覆盖 / BASE 拦截留痕） ══
clear_events()
c2 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c2.engine.evaluate = lambda *a, **k: (70.0, 0.0, buy_low_sig(54.0))
calls2 = drive_bar(c2, "2026-08-19 09:36:00", 54.0, ma5=MA5)
buys2 = [c for c in calls2 if c.get("side") == main.OrderSide_Buy]
sig2 = [e for e in read_events() if e.get("event") == "signal"]
check("T2a 破位日 BUY_LOW 无买入成交（MA5_EXIT 优先级 e 覆盖禁买）",
      len(buys2) == 0, f"buys={len(buys2)}")
check("T2b 破位日信号为 MA5_EXIT（非 BUY_LOW）",
      any(e.get("action") == "MA5_EXIT" for e in sig2)
      and not any(e.get("action") == "BUY_LOW" for e in sig2),
      f"actions={[e.get('action') for e in sig2]}")

clear_events()
c2b = make_ctx("2026-08-19 09:36:00", qty=0, cost=50.0, base_ref=500)
c2b._base_settled = set()   # 未建仓 → 走 BASE 路径
calls2b = drive_bar(c2b, "2026-08-19 09:36:00", 54.0, ma5=MA5)
check("T2c 破位日 BASE 建仓被拦", len(calls2b) == 0, f"orders={len(calls2b)}")
risk2b = [e for e in read_events() if e.get("kind") == "ma5_break_block"]
check("T2d BASE 侧 ma5_break_block 留痕", len(risk2b) >= 1, f"risk={len(risk2b)}")

# ══ T3: T+1 锁定——卖可用部分，次日仍破位续卖剩余 ══
clear_events()
c3 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c3.manual_position[GM_SYM]["available"] = 200   # 当日买 300 锁定
calls3 = drive_bar(c3, "2026-08-19 09:36:00", 54.0, ma5=MA5)
s3 = sell_calls(calls3)
check("T3a 破位日卖可用 200（T+1 锁定部分不卖）",
      len(s3) == 1 and s3[0]["volume"] == 200, f"sells={[c.get('volume') for c in s3]}")
# 次日仍破位：先模拟 T3a 的 200 卖出成交（释放 F9 在途额度），pos=300 全可用 → 续卖 300
main.on_order_status(c3, {"symbol": GM_SYM, "status": 3, "volume": 200,
                          "side": 2, "price": 54.0, "filled_vwap": 54.0})
c3.manual_position[GM_SYM]["qty"] = 300
c3.manual_position[GM_SYM]["available"] = 300
c3.manual_position[GM_SYM]["t_qty"] = 300
calls3b = drive_bar(c3, "2026-08-20 09:36:00", 54.0, ma5=MA5)
s3b = sell_calls(calls3b)
check("T3b 次日仍破位续卖剩余 300",
      len(s3b) == 1 and s3b[0]["volume"] == 300, f"sells={[c.get('volume') for c in s3b]}")

# ══ T4: 次日收复 MA5 → 禁解除，无新 MA5_EXIT ══
clear_events()
c4 = make_ctx("2026-08-20 09:36:00", qty=300, cost=52.0, base_ref=500)
calls4 = drive_bar(c4, "2026-08-20 09:36:00", 56.5, ma5=MA5)   # cp 56.5 > ma5 56 收复
check("T4a 收复 MA5 → 无 MA5_EXIT/卖出", len(sell_calls(calls4)) == 0, f"sells={len(calls4)}")
c4.engine.evaluate = lambda *a, **k: (70.0, 0.0, buy_low_sig(56.5))
calls4b = drive_bar(c4, "2026-08-20 09:37:00", 56.5, ma5=MA5)
sig4 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "BUY_LOW"]
risk4 = [e for e in read_events() if e.get("kind") == "ma5_break_block"]
check("T4b 收复后 BUY_LOW 信号照写（禁解除）", len(sig4) == 1, f"sig={len(sig4)}")
check("T4c 收复后无新增 ma5_break_block 拦截", len(risk4) == 0, f"risk={len(risk4)}")

# ══ T5: 同日 MA5 破位 + TRAIL 回撤 → 仅 MA5_EXIT（优先级 e） ══
clear_events()
c5 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c5.manual_position[GM_SYM]["_trail_state"] = "ARMED"
c5.manual_position[GM_SYM]["_trail_peak"] = 58.0
calls5 = drive_bar(c5, "2026-08-19 09:36:00", 54.0, ma5=MA5)   # 回撤 (58-54)/58≈6.9%
s5 = sell_calls(calls5)
sig5 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "TRAIL_SELL"]
check("T5a 仅 MA5_EXIT 成交一次（全离 500）", len(s5) == 1 and s5[0]["volume"] == 500,
      f"sells={[c.get('volume') for c in s5]}")
check("T5b 无 TRAIL_SELL 信号（MA5 优先级最高）", len(sig5) == 0, f"trail_sig={len(sig5)}")

# ══ T6: MA5_EXIT 成交不生成 buyback（f） ══
clear_events()
c6 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
drive_bar(c6, "2026-08-19 09:36:00", 54.0, ma5=MA5)   # MA5_EXIT 500 卖 → pending_sell_action
main.on_order_status(c6, {"symbol": GM_SYM, "status": 3, "volume": 500,
                          "side": 2, "price": 54.0, "filled_vwap": 54.0})
arm6 = [e for e in read_events() if e.get("event") == "buyback_armed"]
check("T6a MA5_EXIT 成交后无 buyback_armed 事件", len(arm6) == 0, f"armed={len(arm6)}")
check("T6b 不生成回补记忆（awaiting_buyback 无）", CODE not in c6.engine.awaiting_buyback)

# ══ T7: O-12 pos_key 成本容差匹配 ══
se.SIM_NOW = datetime(2026, 8, 19, 9, 50, 0)
rm_state(); clear_events()
c_p = live_ctx(800, 50.0, datetime(2026, 8, 19, 9, 50, 0))
c_p.engine.awaiting_buyback[CODE] = c_p.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p, CODE, GM_SYM)
se.SIM_NOW = datetime(2026, 8, 19, 9, 31, 0)
c_r = live_ctx(800, 50.002, datetime(2026, 8, 19, 9, 31, 0))   # +0.002 容差内
main._sell_state_restore(c_r)
st7 = main._sell_state_load().get(CODE, {})
check("T7a +0.002 容差内 → 恢复 + 指纹静默更新",
      CODE in c_r.engine.awaiting_buyback and st7.get("pos_key") == "800@50.0020",
      f"ab={CODE in c_r.engine.awaiting_buyback} pos_key={st7.get('pos_key')}")
check("T7b pos_key_tolerance 留痕",
      any(a.get("event") == "pos_key_tolerance" for a in read_audit()))

# +0.04 容差内
rm_state()
c_p2 = live_ctx(800, 50.0, datetime(2026, 8, 19, 9, 50, 0))
c_p2.engine.awaiting_buyback[CODE] = c_p2.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p2, CODE, GM_SYM)
c_r2 = live_ctx(800, 50.04, datetime(2026, 8, 19, 9, 31, 0))   # +0.04 容差内(max 0.05)
main._sell_state_restore(c_r2)
check("T7c +0.04 容差内 → 恢复", CODE in c_r2.engine.awaiting_buyback)

# +8% 超容差 → 作废
rm_state()
c_p3 = live_ctx(800, 50.0, datetime(2026, 8, 19, 9, 50, 0))
c_p3.engine.awaiting_buyback[CODE] = c_p3.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p3, CODE, GM_SYM)
c_r3 = live_ctx(800, 54.0, datetime(2026, 8, 19, 9, 31, 0))   # +8% 超容差
main._sell_state_restore(c_r3)
check("T7d +8% 超容差 → 作废不恢复",
      CODE not in c_r3.engine.awaiting_buyback and CODE not in main._sell_state_load())

# ══ T8: O-13 watcher 同票同 kind 推送节流 ══
pushes = []
watcher._push = lambda title, content, level="info": pushes.append((title, level))
watcher._last_push.clear()
rec = {"event": "risk", "code": CODE, "kind": "entry_gate",
       "detail": f"cp=54 < ma5={MA5}", "time": "2026-08-19 09:36:00"}
for _ in range(3):
    watcher.handle_event(rec)
check("T8 同票同 kind 30min 内 3 次 → 仅 1 条推送", len(pushes) == 1, f"pushes={len(pushes)}")
# 30 分钟后可再推（键存时间戳）
watcher._last_push.clear()   # 模拟窗口过去
for _ in range(2):
    watcher.handle_event(rec)
check("T8b 节流窗口过后可再推", len(pushes) == 2, f"pushes={len(pushes)}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
