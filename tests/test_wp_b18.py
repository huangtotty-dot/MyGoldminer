# coding=utf-8
"""
tests/test_wp_b18.py — WP-B18 回补记忆跨日持久化 + 通道互斥矩阵（合并 B-17）验证

背景（docs/backlog/B-18_方案_跨日回补持久化与互斥矩阵.md）：
  WP-B07 回补记忆纯日内（TTL240min + 跨日清空），0818 暴露隔夜/次日回踩机会成本
  （000988 最低恰触 target 未接回；600481 8 次精确平触 4.36 未回补）。
  B-18: armed 跨日持久化（sell_state.json 新增 _buyback）+ INIT 恢复三道闸
  （pos_key/有效期/互斥）+ 互斥矩阵 M1-M4 + 触发价 price<=target_price。

验证范围：
  T1  跨日持久化+恢复：persist 落盘 _buyback → 次日 restore 恢复 → 盘中回踩 target 触发 BUY_LOW
  T2  有效期作废：expire_date 已过 → restore 作废 + buyback_expired 事件
  T3  pos_key 不符：恢复时持仓变化 → 段作废，不恢复
  T4  M1 互斥：TREND_DOWN + TREND_EXIT 卖出 → 盘中触发拦截 buyback_blocked:M1 + 清记忆
  T5  M3 跳空延迟：开盘偏离 target>2% → delay；无跳空/过 09:35 → pass
  T6  M4 深亏：profit<-8% PANIC 域 → 拦截 buyback_blocked:M4 + 清记忆
  T7  恢复后平触 target → 达标触发（3.3 平触语义）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b18.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb18_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine, ScoringEngine  # noqa: E402
from data.indicators import Signal  # noqa: E402
import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b18.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b18.json")

CODE = "603667"
GM_SYM = main.STOCKS[CODE]

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


HOLDING = {"name": "五洲新春", "qty": 800, "available": 800, "t_qty": 800,
           "cost": 50.0, "type": "stock", "pre_close": 52.0}
DAILY_CTX = {"index_regime": "range", "intraday_alerts": [], "daily_atr": 0.02}


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


def make_ctx(now_str, qty=800, cost=50.0, base_ref=None, trend="TREND_RANGE"):
    """构造可通过 on_bar 前导段直达门控链的假 context（与 test_wp_b15 同款）。"""
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


def drive_bar(ctx, now_str, close, trend=None):
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


def buy_low_sig(price, score=70.0):
    return Signal(code=CODE, name="五洲新春", action="BUY_LOW",
                  price=price, score=score, reasons=["b18-test"])


# ══ T1: 跨日持久化 + 恢复 + 盘中回踩触发 ══
se.SIM_NOW = datetime(2026, 8, 17, 9, 50, 0)
rm_state(); clear_events()
c1 = live_ctx(800, 50.0, datetime(2026, 8, 17, 9, 50, 0))
c1.engine.awaiting_buyback[CODE] = c1.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c1, CODE, GM_SYM)
bb1 = main._sell_state_load().get(CODE, {}).get("_buyback") or {}
check("T1a persist 落盘 _buyback(价/量/通道/有效期)",
      bb1.get("sell_price") == 52.14 and bb1.get("sell_qty") == 300
      and bb1.get("sell_action") == "SELL_HIGH" and bb1.get("expire_date"),
      f"bb={bb1}")

se.SIM_NOW = datetime(2026, 8, 18, 9, 31, 0)
c2 = live_ctx(800, 50.0, datetime(2026, 8, 18, 9, 31, 0))
main._sell_state_restore(c2)
ab2 = c2.engine.awaiting_buyback.get(CODE, {})
check("T1b INIT 恢复记忆(persisted/价/目标/有效期)",
      ab2.get("sell_price") == 52.14 and ab2.get("persisted") is True
      and ab2.get("target_price") == 52.04,
      f"ab={ {k: ab2.get(k) for k in ('sell_price','target_price','expire_date','persisted')} }")
check("T1b2 恢复写 buyback_restored 事件",
      any(e.get("event") == "buyback_restored" and e.get("code") == CODE for e in read_events()))

se.SIM_NOW = datetime(2026, 8, 18, 10, 30, 0)
df1 = make_df(52.00, "2026-08-18 10:30:00")   # 回踩 ≤ target 52.04
with mock.patch.object(ScoringEngine, "calc_buy_score", return_value=(60.0, [])), \
     mock.patch.object(ScoringEngine, "calc_sell_score", return_value=(0.0, [])):
    _bs, _ss, sig1 = c2.engine.evaluate(CODE, "五洲新春", df1, dict(HOLDING), dict(DAILY_CTX))
check("T1c 恢复后盘中回踩 target → BUY_LOW 触发",
      sig1 is not None and sig1.action == "BUY_LOW", f"sig={sig1 and sig1.action}")

# ══ T2: 有效期作废 ══
se.SIM_NOW = datetime(2026, 8, 17, 9, 50, 0)
rm_state(); clear_events()
c_t = live_ctx(800, 50.0, datetime(2026, 8, 17, 9, 50, 0))
c_t.engine.awaiting_buyback[CODE] = c_t.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
c_t.engine.awaiting_buyback[CODE]["expire_date"] = "2026-08-10"   # 强制已过期
main._sell_state_persist(c_t, CODE, GM_SYM)
se.SIM_NOW = datetime(2026, 8, 18, 9, 31, 0)
c_t2 = live_ctx(800, 50.0, datetime(2026, 8, 18, 9, 31, 0))
main._sell_state_restore(c_t2)
check("T2a 有效期已过 → 不作废恢复（TARGET/TRAIL 状态不受影响）",
      CODE not in c_t2.engine.awaiting_buyback
      and main._sell_state_load().get(CODE, {}).get("_buyback") is None,
      f"ab={c_t2.engine.awaiting_buyback.get(CODE)}")
check("T2b 写 buyback_expired 事件",
      any(e.get("event") == "buyback_expired" and e.get("code") == CODE for e in read_events()))

# ══ T3: pos_key 不符作废 ══
se.SIM_NOW = datetime(2026, 8, 17, 9, 50, 0)
rm_state()
c_pk = live_ctx(800, 50.0, datetime(2026, 8, 17, 9, 50, 0))
c_pk.engine.awaiting_buyback[CODE] = c_pk.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_pk, CODE, GM_SYM)
se.SIM_NOW = datetime(2026, 8, 18, 9, 31, 0)
c_pk2 = live_ctx(600, 50.0, datetime(2026, 8, 18, 9, 31, 0))   # 持仓变化 → pos_key 不符
main._sell_state_restore(c_pk2)
check("T3 pos_key 不符 → 段作废，回补不恢复",
      CODE not in c_pk2.engine.awaiting_buyback and CODE not in main._sell_state_load())

# ══ T4: M1 互斥（TREND_DOWN + TREND_EXIT）盘中拦截 ══
se.SIM_NOW = datetime(2026, 8, 18, 10, 0, 0)
rm_state(); clear_events()
c4 = make_ctx("2026-08-18 10:00:00", qty=800, cost=50.0, base_ref=500)
c4.engine.evaluate = lambda *a, **k: (70.0, 0.0, buy_low_sig(48.0))
c4.engine.awaiting_buyback[CODE] = {
    "sell_price": 52.14, "sell_qty": 300, "sell_action": "TREND_EXIT",
    "target_price": 52.04, "sell_time": datetime(2026, 8, 17, 9, 50, 0),
    "expire_date": "2026-08-20", "persisted": True,
}
calls4 = drive_bar(c4, "2026-08-18 10:00:00", 48.0, trend="TREND_DOWN")
blk4 = [e for e in read_events() if e.get("event") == "buyback_blocked" and e.get("code") == CODE]
check("T4a M1 TREND_DOWN+TREND_EXIT → 拦截且无下单",
      len(calls4) == 0 and len(blk4) == 1 and blk4[0].get("rule") == "M1",
      f"orders={len(calls4)} blocked={blk4}")
check("T4b M1 拦截后记忆清除", CODE not in c4.engine.awaiting_buyback)

# ══ T5: M3 跳空延迟（函数级） ══
se.SIM_NOW = datetime(2026, 8, 18, 9, 30, 0)
c5 = live_ctx(800, 50.0, datetime(2026, 8, 18, 9, 30, 0))
ab5 = {"sell_price": 52.14, "sell_qty": 300, "sell_action": "SELL_HIGH",
       "target_price": 52.04, "sell_time": datetime(2026, 8, 17, 9, 50, 0),
       "expire_date": "2026-08-20", "persisted": True}
now930 = datetime(2026, 8, 18, 9, 30, 0)
now936 = datetime(2026, 8, 18, 9, 36, 0)
a1, r1 = main._buyback_mutex_block(c5, CODE, dict(DAILY_CTX), 53.50, 52.00, now930, ab5)  # 偏离2.8%
a2, r2 = main._buyback_mutex_block(c5, CODE, dict(DAILY_CTX), 52.20, 52.00, now930, ab5)  # 偏离0.3%
a3, r3 = main._buyback_mutex_block(c5, CODE, dict(DAILY_CTX), 53.50, 52.00, now936, ab5)  # 过09:35
check("T5a 开盘跳空>2% 且 09:35 前 → delay(M3)", a1 == "delay" and r1 == "M3", f"act={a1} rule={r1}")
check("T5b 无跳空(0.3%) → pass", a2 == "pass", f"act={a2}")
check("T5c 过 09:35 后跳空 → pass（开盘缓冲已过）", a3 == "pass", f"act={a3}")

# ══ T6: M4 恢复时深亏拦截（PANIC 域不回补——接回高抛≠摊平亏损） ══
se.SIM_NOW = datetime(2026, 8, 17, 9, 50, 0)
rm_state(); clear_events()
c6a = live_ctx(800, 60.0, datetime(2026, 8, 17, 9, 50, 0))
c6a.engine.awaiting_buyback[CODE] = c6a.engine.arm_awaiting_buyback(CODE, 62.0, 300, "SELL_HIGH")
main._sell_state_persist(c6a, CODE, GM_SYM)
se.SIM_NOW = datetime(2026, 8, 18, 9, 31, 0)
c6b = live_ctx(800, 60.0, datetime(2026, 8, 18, 9, 31, 0))
c6b.bar_cache = {GM_SYM: [{"close": 50.0}]}   # 现价 50 → profit -16.7% < -8%
main._sell_state_restore(c6b)
blk6 = [e for e in read_events() if e.get("event") == "buyback_blocked" and e.get("code") == CODE]
check("T6a M4 恢复时深亏背景 → 不恢复 + buyback_blocked:M4",
      CODE not in c6b.engine.awaiting_buyback and len(blk6) == 1 and blk6[0].get("rule") == "M4",
      f"ab={c6b.engine.awaiting_buyback.get(CODE)} blocked={blk6}")

# 对照：恢复时无深亏（bar_cache 价 58 → profit -3.3%）→ 正常恢复
se.SIM_NOW = datetime(2026, 8, 17, 9, 50, 0)
c6c_p = live_ctx(800, 60.0, datetime(2026, 8, 17, 9, 50, 0))
c6c_p.engine.awaiting_buyback[CODE] = c6c_p.engine.arm_awaiting_buyback(CODE, 62.0, 300, "SELL_HIGH")
main._sell_state_persist(c6c_p, CODE, GM_SYM)
se.SIM_NOW = datetime(2026, 8, 18, 9, 31, 0)
c6c = live_ctx(800, 60.0, datetime(2026, 8, 18, 9, 31, 0))
c6c.bar_cache = {GM_SYM: [{"close": 58.0}]}
main._sell_state_restore(c6c)
check("T6b 恢复时非深亏(-3.3%) → 正常恢复",
      CODE in c6c.engine.awaiting_buyback,
      f"ab={c6c.engine.awaiting_buyback.get(CODE, {}).get('sell_price')}")

# ══ T7: 恢复后平触 target → 达标触发（3.3 平触语义） ══
se.SIM_NOW = datetime(2026, 8, 18, 10, 30, 0)
c7 = live_ctx(800, 50.0, datetime(2026, 8, 18, 10, 30, 0))
c7.engine.awaiting_buyback[CODE] = {
    "sell_price": 52.14, "sell_qty": 300, "sell_action": "SELL_HIGH",
    "target_price": 52.04, "sell_time": datetime(2026, 8, 17, 9, 50, 0),
    "expire_date": "2026-08-20", "persisted": True,
}
df7 = make_df(52.04, "2026-08-18 10:30:00")   # 平触 target
with mock.patch.object(ScoringEngine, "calc_buy_score", return_value=(60.0, [])), \
     mock.patch.object(ScoringEngine, "calc_sell_score", return_value=(0.0, [])):
    _bs7, _ss7, sig7 = c7.engine.evaluate(CODE, "五洲新春", df7, dict(HOLDING), dict(DAILY_CTX))
check("T7 恢复后平触 target → 达标触发 BUY_LOW",
      sig7 is not None and sig7.action == "BUY_LOW", f"sig={sig7 and sig7.action}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
