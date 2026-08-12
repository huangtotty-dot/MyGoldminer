# coding=utf-8
"""
tests/test_wp_b13b14.py — WP-B13/B14 验证

WP-B13: 开盘卖出缓冲闸从引擎信号路径移到门控链末端（覆盖 TARGET_SELL）。
WP-B14: TARGET L1 位图三段式状态机（None→pending→filled）+ sell_state.json 跨日持久化 + TRAIL 同修。

验证范围（对应 docs/回测复盘/修复方案_WP-B13-B14_TARGET开盘缓冲与L1位图持久化.md）:
  T1  缓冲移位：09:32 TARGET 生成 → morning_sell_blocked 事件 action=TARGET_SELL，无下单
  T2  延后放行：09:36 同场景 → TARGET 正常下单并置 pending
  T3  即时性回归：09:32 PANIC/TRAIL/TREND_EXIT 均不被拦（B5 条款不破）
  T4  置位时机：信号生成→地板拦截（不下单）→ 状态仍 None，次 bar 过地板可再触发
  T5  竞态防护：下单(pending)→成交回调前再评估 → pending 期间不重复生成
  T6  拒单回滚：pending → 拒单 → 状态清回 None，次 bar 可再触发
  T7  持久化：pos_key 匹配→恢复；qty=0→作废；pos_key 不符→作废；pending 遗留→作废
  T8  TRAIL 持久化：ARMED+peak 写盘→重启恢复→状态机续接（无需重新武装）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b13b14.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb13b14_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se
from signals.engine import SignalEngine
import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b13b14.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_test.json")

CODE = "603667"
GM_SYM = main.STOCKS[CODE]
PROFIT_CP = 57.28   # 成本 52.0 → +10.2%（0812 实证样本价）
COST = 52.0

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


def read_audit():
    if not os.path.exists(main._AUDIT_LOG_PATH):
        return []
    with open(main._AUDIT_LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def l1_state(ctx):
    return ctx.manual_position[GM_SYM].get("_target_l1_state")


# ══ T1: 缓冲移位 —— 09:32 TARGET 被拦，action=TARGET_SELL，无下单 ══
clear_audit()
c1 = make_ctx("2026-08-12 09:32:00")
calls1 = drive_bar(c1, "2026-08-12 09:32:00", PROFIT_CP)
blocked1 = [e for e in read_audit()
            if e.get("event") == "morning_sell_blocked" and e.get("code") == CODE]
check("T1a 09:32 TARGET 信号被开盘缓冲拦截（morning_sell_blocked）",
      len(blocked1) == 1, f"blocked={blocked1}")
check("T1b 拦截事件 action=真实通道名 TARGET_SELL（修复硬编码 SELL_HIGH）",
      len(blocked1) == 1 and blocked1[0].get("action") == "TARGET_SELL",
      f"action={blocked1 and blocked1[0].get('action')}")
check("T1c 被拦后无任何下单", len(calls1) == 0, f"orders={calls1}")
check("T1d 被拦不置位（状态仍 None，未耗档）", l1_state(c1) is None)

# ══ T2: 延后放行 —— 09:36 同场景 TARGET 正常下单 ══
c2 = make_ctx("2026-08-12 09:36:00")
calls2 = drive_bar(c2, "2026-08-12 09:36:00", PROFIT_CP)
check("T2a 09:36 TARGET 正常下单", len(calls2) == 1 and calls2[0]["symbol"] == GM_SYM,
      f"orders={calls2}")
check("T2b 下单即置 pending（成交前防竞态重复触发）", l1_state(c2) == "pending",
      f"state={l1_state(c2)}")

# ══ T3a: 即时性回归 —— 09:32 PANIC 不被拦 ══
clear_audit()
c3a = make_ctx("2026-08-12 09:32:00", qty=800, cost=60.0)  # 深度亏损
calls3a = drive_bar(c3a, "2026-08-12 09:32:00", 50.0)
blocked3a = [e for e in read_audit()
             if e.get("event") == "morning_sell_blocked" and e.get("code") == CODE]
check("T3a 09:32 PANIC(深亏) 即时执行不被缓冲拦",
      len(calls3a) == 1 and not blocked3a, f"orders={len(calls3a)} blocked={len(blocked3a)}")

# ══ T3b: 即时性回归 —— 09:32 TRAIL_SELL 不被拦（先 09:31 武装） ══
clear_audit()
c3b = make_ctx("2026-08-12 09:31:00")
drive_bar(c3b, "2026-08-12 09:31:00", PROFIT_CP)   # 武装 ARMED peak=57.28
clear_audit()                                        # 09:31 的 TARGET 拦截属预期，只验 09:32 段
calls3b = drive_bar(c3b, "2026-08-12 09:32:00", 55.0)   # 回撤 >3% → TRAIL_SELL
blocked3b = [e for e in read_audit()
             if e.get("event") == "morning_sell_blocked" and e.get("code") == CODE]
check("T3b 09:32 TRAIL_SELL 即时执行不被缓冲拦",
      len(calls3b) == 1 and not blocked3b,
      f"orders={len(calls3b)} blocked={len(blocked3b)}")

# ══ T3c: 即时性回归 —— 09:32 TREND_EXIT 不被拦 ══
clear_audit()
c3c = make_ctx("2026-08-12 09:32:00", qty=800, base_ref=500, trend="TREND_DOWN")
calls3c = drive_bar(c3c, "2026-08-12 09:32:00", PROFIT_CP, trend="TREND_DOWN")
blocked3c = [e for e in read_audit()
             if e.get("event") == "morning_sell_blocked" and e.get("code") == CODE]
check("T3c 09:32 TREND_EXIT(趋势破坏止盈) 即时执行不被缓冲拦",
      len(calls3c) == 1 and not blocked3c,
      f"orders={len(calls3c)} blocked={len(blocked3c)}")

# ══ T4: 置位时机 —— 地板拦截不下单，状态仍 None，过地板后可再触发 ══
c4 = make_ctx("2026-08-12 09:36:00", qty=100, base_ref=100)  # 100 股：pos-100 < min_hold(50)
calls4a = drive_bar(c4, "2026-08-12 09:36:00", PROFIT_CP)
check("T4a 地板拦截（pos=100 不满足 min_hold）→ 无下单", len(calls4a) == 0, f"orders={calls4a}")
check("T4b 被地板拦后状态仍 None（未耗档）", l1_state(c4) is None)
c4.manual_position[GM_SYM]["qty"] = 300   # 午后加仓过地板
c4.manual_position[GM_SYM]["available"] = 300
c4.manual_position[GM_SYM]["t_qty"] = 300
calls4b = drive_bar(c4, "2026-08-12 10:35:00", PROFIT_CP)
check("T4c 过地板后次 bar TARGET 可再触发并下单",
      len(calls4b) == 1 and l1_state(c4) == "pending",
      f"orders={calls4b} state={l1_state(c4)}")

# ══ T5: 竞态防护 —— pending 期间不重复生成 ══
c5 = make_ctx("2026-08-12 09:36:00", qty=800)
calls5a = drive_bar(c5, "2026-08-12 09:36:00", PROFIT_CP)
c5.engine.sell_cooldown.clear()   # 清除冷却，隔离验证 pending 是唯一阻断原因
calls5b = drive_bar(c5, "2026-08-12 09:37:00", PROFIT_CP)
check("T5 pending 期间再评估不重复生成/下单（成交回调前竞态防护）",
      l1_state(c5) == "pending" and len(calls5a) == 1 and len(calls5b) == 0,
      f"state={l1_state(c5)} orders={len(calls5a)},{len(calls5b)}")

# ══ T6: 拒单回滚 —— pending → 拒单 → 清回 None，次 bar 可再触发 ══
c6 = make_ctx("2026-08-12 09:36:00", qty=800)
calls6a = drive_bar(c6, "2026-08-12 09:36:00", PROFIT_CP)
main.on_order_status(c6, {"symbol": GM_SYM, "status": 8, "volume": 200,
                          "side": 2, "price": PROFIT_CP, "filled_vwap": PROFIT_CP})
check("T6a 拒单后状态清回 None（拒单不耗档）", l1_state(c6) is None,
      f"state={l1_state(c6)}")
calls6b = drive_bar(c6, "2026-08-12 10:35:00", PROFIT_CP)
check("T6b 拒单后当日可再触发 TARGET",
      len(calls6b) == 1 and l1_state(c6) == "pending",
      f"orders={calls6b} state={l1_state(c6)}")

# ══ T7: 持久化恢复规则 ══
def live_ctx(qty, cost, **extra):
    mp = {"name": "五洲新春", "qty": qty, "available": qty, "t_qty": qty,
          "cost": cost, "type": "stock", "pre_close": cost}
    mp.update(extra)
    return SimpleNamespace(mode=main.MODE_LIVE, manual_position={GM_SYM: mp},
                           now=datetime(2026, 8, 12, 10, 0, 0))


def rm_state_file():
    if os.path.exists(main.SELL_STATE_PATH):
        os.remove(main.SELL_STATE_PATH)


rm_state_file()
# T7a: pos_key 匹配 → 恢复
c_p = live_ctx(800, COST, _target_l1_state="filled", _trail_state="ARMED", _trail_peak=57.89)
main._sell_state_persist(c_p, CODE, GM_SYM)
c_r = live_ctx(800, COST)
main._sell_state_restore(c_r)
mpr = c_r.manual_position[GM_SYM]
check("T7a pos_key 匹配 → 恢复 filled/ARMED/peak",
      mpr.get("_target_l1_state") == "filled" and mpr.get("_trail_state") == "ARMED"
      and abs(mpr.get("_trail_peak", 0) - 57.89) < 1e-9,
      f"mp={ {k: mpr.get(k) for k in ('_target_l1_state', '_trail_state', '_trail_peak')} }")

# T7b: qty=0（已清仓）→ 作废
rm_state_file()
c_p = live_ctx(800, COST, _target_l1_state="filled")
main._sell_state_persist(c_p, CODE, GM_SYM)
c_r = live_ctx(0, COST)
main._sell_state_restore(c_r)
check("T7b qty=0 → 状态段作废", CODE not in main._sell_state_load()
      and c_r.manual_position[GM_SYM].get("_target_l1_state") is None)

# T7c: pos_key 不符（人工加减仓）→ 作废
rm_state_file()
c_p = live_ctx(800, COST, _target_l1_state="filled")
main._sell_state_persist(c_p, CODE, GM_SYM)
c_r = live_ctx(600, COST)
main._sell_state_restore(c_r)
check("T7c pos_key 不符 → 状态段作废（宁多触发一档）",
      CODE not in main._sell_state_load()
      and c_r.manual_position[GM_SYM].get("_target_l1_state") is None)

# T7d: pending 遗留（进程中断，成交结果未知）→ 作废
rm_state_file()
c_p = live_ctx(800, COST, _target_l1_state="pending")
main._sell_state_persist(c_p, CODE, GM_SYM)
c_r = live_ctx(800, COST)
main._sell_state_restore(c_r)
check("T7d pending 遗留 → 作废（状态存疑即重置，防永久封档）",
      c_r.manual_position[GM_SYM].get("_target_l1_state") is None)

# ══ T8: TRAIL 持久化 —— 重启恢复后状态机续接，无需重新武装 ══
rm_state_file()
c_p = live_ctx(800, COST, _trail_state="ARMED", _trail_peak=57.89, _target_l1_state=None)
main._sell_state_persist(c_p, CODE, GM_SYM)
c8 = make_ctx("2026-08-12 10:35:00", qty=800, cost=COST)
c8.mode = main.MODE_LIVE
main._sell_state_restore(c8)
c8.mode = None   # 后续 on_bar 驱动按回测口径（心跳跳过），恢复已入内存
mp8 = c8.manual_position[GM_SYM]
check("T8a 重启恢复 ARMED + peak=57.89（peak 不归零）",
      mp8.get("_trail_state") == "ARMED" and abs(mp8.get("_trail_peak", 0) - 57.89) < 1e-9,
      f"trail={mp8.get('_trail_state')} peak={mp8.get('_trail_peak')}")
calls8 = drive_bar(c8, "2026-08-12 10:35:00", 55.0)   # 回撤 (57.89-55)/57.89≈4.99% > 3%
check("T8b 续接: 无需重新武装，直接触发 TRAIL_SELL",
      len(calls8) == 1 and c8._pending_sell_action[GM_SYM][0] == "TRAIL_SELL",
      f"orders={len(calls8)} action={c8._pending_sell_action.get(GM_SYM, ('',))[0]}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
