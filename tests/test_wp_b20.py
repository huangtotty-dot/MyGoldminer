# coding=utf-8
"""
tests/test_wp_b20.py — WP-B20 双通道建仓闸（同步自 E:\\superTrader W33 A1）验证

owner 2026-08-19 决策：完全替换 goldminer 的 G4 支撑建仓闸为 superTrader 双通道
（冰点反转 + 突破跟随）；仅 verdict=signal（冰点80/突破70 分档）放行建仓，
approaching 仅留痕观察。

验证范围：
  T1  冰点通道 signal：转向确认 + BOLL冰点 + 缩量 + 5分钟冰点全过 → verdict=signal(channel=iceberg)
  T2  冰点 approaching：无 5 分钟冰点（数据不足）→ verdict=approaching（不建仓）
  T3  突破通道 signal：箱体突破 + 放量 + 趋势多头 → verdict=signal(channel=breakout)
  T4  weak：全条件不满足 → verdict=weak
  T5  转向未过：冰点通道弱化（即便 BOLL+缩量 过，无转向确认）
  T6  BASE 接入：突破 signal → BASE 建仓下单
  T7  BASE 接入：冰点 approaching → 拦截 + build_gate 事件留痕
  T8  BASE 接入：weak → 拦截

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b20.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb20_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
import main  # noqa: E402
from signals import position_builder as pb  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b20.jsonl")
main._audit_file = None
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b20.json")

CODE = "603667"
GM_SYM = main.STOCKS[CODE]

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def make_ohlc():
    """构造横盘箱体：60 日 close 在 50±0.4，high=51.5/low=48.5 → 箱体上沿 51.5。"""
    base = 50.0
    close = [base + (i % 5 - 2) * 0.2 for i in range(60)]
    high = [base + 1.5 for _ in range(60)]
    low = [base - 1.5 for _ in range(60)]
    return {"close": close, "high": high, "low": low}


def make_daily_ctx(**over):
    dc = {
        "daily_macd_golden": False,
        "daily_price_ref": 50.0,
        "daily_ma5": 49.0,          # 站上MA5 True（转向确认过）
        "daily_boll_pct": 0.10,     # BOLL冰点
        "daily_vol_today": 100.0,
        "daily_vol_ma5": 200.0,     # 缩量 0.5
        "daily_rsi14": 30.0,
        "daily_macd_dif": -0.5,
        "daily_macd_dea": 0.0,      # 非多头
        "_daily_ohlc": make_ohlc(),
        "daily_status": "ok",
        "_m2_pool_pass": True,
    }
    dc.update(over)
    return dc


def make_m5_df(ice=True):
    """构造 5 分钟 df（macd/macd_signal/bb_pct/rsi/volume）满足 m5 冰点 4 条件。"""
    n = 30
    macd = [0.0] * n
    sig = [0.0] * n
    bb = [0.8] * n
    rsi = [50.0] * n
    vol = [10000.0] * n
    if ice:
        for i in range(n):
            macd[i] = -0.1 if i < 25 else 0.2   # i=25 金叉
            sig[i] = 0.0
        bb[-1] = 0.10
        rsi[-1] = 25.0
        for i in range(25, n):
            vol[i] = 3000.0   # 近5根 3000 / 前20根 10000 = 0.3 < 0.8
    return pd.DataFrame({"macd": macd, "macd_signal": sig,
                         "bb_pct": bb, "rsi": rsi, "volume": vol})


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


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


# ══ T1: 冰点通道 signal（转向+BOLL冰点+缩量+5分钟冰点 全过） ══
dc1 = make_daily_ctx()
m5 = make_m5_df(ice=True)
r1 = pb.eval_dual_channels(dc1, 50.0, m5_df=m5, scan_type="intraday")
check("T1a 冰点 signal（iceberg）", r1["verdict"] == "signal" and r1["channel"] == "iceberg",
      f"verdict={r1['verdict']} channel={r1['channel']}")
check("T1b 冰点 score=80（转向40+BOLL20+缩量20）",
      r1["iceberg"]["score"] == 80 and r1["iceberg"]["status"] == "immediate",
      f"score={r1['iceberg']['score']} status={r1['iceberg']['status']}")

# ══ T2: 冰点 approaching（无 5 分钟冰点 → 数据不足降级） ══
r2 = pb.eval_dual_channels(dc1, 50.0, m5_df=None, scan_type="intraday")
check("T2 无 m5 冰点 → 冰点 approaching（不建仓）",
      r2["verdict"] == "approaching" and r2["iceberg"]["verdict"] == "approaching",
      f"verdict={r2['verdict']} ice={r2['iceberg']['verdict']}")

# ══ T3: 突破通道 signal（箱体突破+放量+多头） ══
dc3 = make_daily_ctx(
    daily_ma5=51.0,            # price_ref 50 < ma5 51 → 转向确认False（冰点弱化）
    daily_boll_pct=0.8,        # 非冰点
    daily_vol_today=300.0, daily_vol_ma5=100.0,   # 放量 3.0
    daily_rsi14=60.0,
    daily_macd_dif=1.0, daily_macd_dea=0.5,       # 多头
)
r3 = pb.eval_dual_channels(dc3, 54.0, m5_df=None, scan_type="intraday")
check("T3a 突破 signal（breakout）", r3["verdict"] == "signal" and r3["channel"] == "breakout",
      f"verdict={r3['verdict']} channel={r3['channel']}")
check("T3b 突破 score=100（箱体40+放量30+多头30）", r3["breakout"]["score"] == 100,
      f"score={r3['breakout']['score']}")

# ══ T4: weak（转向满足但 BOLL/缩量/突破 全不满足 → 双通道均弱） ══
dc4 = make_daily_ctx(
    daily_ma5=49.0,            # 转向True（站上MA5），但 BOLL/缩量/突破 不过 → 整体 weak
    daily_boll_pct=0.8,
    daily_vol_today=100.0, daily_vol_ma5=100.0,   # 量比1.0（非缩量非放量）
    daily_macd_dif=-0.5, daily_macd_dea=0.0,
)
r4 = pb.eval_dual_channels(dc4, 50.0, m5_df=None, scan_type="intraday")
check("T4 双通道均弱 → weak", r4["verdict"] == "weak", f"verdict={r4['verdict']}")

# ══ T5: 转向未过 → 冰点弱化（即便 BOLL+缩量 过） ══
dc5 = make_daily_ctx(daily_ma5=51.0)   # price_ref 50 < ma5 51 → 转向False
r5 = pb.eval_dual_channels(dc5, 50.0, m5_df=make_m5_df(ice=True), scan_type="intraday")
check("T5 转向未过 → 冰点 weak（必要项缺失）", r5["iceberg"]["verdict"] == "weak",
      f"ice={r5['iceberg']['verdict']}")


# ══ BASE 接入测试（drive_bar） ══
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


def make_ctx(now_str, qty=0, cost=50.0, base_ref=None, trend="TREND_RANGE"):
    eng = SignalEngine()
    eng._last_feats[CODE] = {"profit_pct": 0.0}
    eng.evaluate = lambda *a, **k: (0.0, 0.0, None)
    dt = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    ctx = SimpleNamespace(
        now=dt, cur_date=None,
        daily_buy_count={}, daily_sell_count={}, daily_trade_price={},
        engine=eng, _last_ir_date=dt.date(), mode=None, _last_bar_eob={},
        bar_cache={},
        manual_position={}, executed_orders={},
        _base_ordered=set(), _base_settled=set(),   # 未建仓 → 走 BASE 路径
        _inflight_sell={},
        sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200,
                              calc_buy_qty=lambda *a, **k: 100),
        latest_pre_close={CODE: cost},
        total_trade_count=0, rejected_order_count=0, audit_records=[],
        last_index_regime="range", last_index_score=0.0,
        _daily_ctx_cache_map={}, _pending_sell_action={}, _day_open={},
    )
    ctx._trend_override = trend
    if base_ref is not None:
        setattr(ctx, f"_base_ref_{CODE}", base_ref)
    return ctx


def drive_bar(ctx, now_str, close, dc):
    ctx.now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    df = make_df(close, now_str)
    calls = []
    with mock.patch.object(main, "_build_bar_df", return_value=df), \
         mock.patch.object(main, "_refresh_daily_ctx", return_value=dc), \
         mock.patch.object(main, "_base_topup_qty", return_value=0), \
         mock.patch.object(main.ops_guard, "ensure_watcher", return_value=None), \
         mock.patch.object(main, "check_kill_switch", return_value=False), \
         mock.patch.object(main, "order_volume", side_effect=lambda **kw: calls.append(kw)):
        main.on_bar(ctx, [{"symbol": GM_SYM, "eob": now_str + ":00",
                           "open": close, "high": close + 0.03, "low": close - 0.03,
                           "close": close, "volume": 10000, "amount": close * 10000}])
    return calls


# ══ T6: BASE 突破 signal → 建仓下单 ══
clear_events()
c6 = make_ctx("2026-08-19 09:36:00")
calls6 = drive_bar(c6, "2026-08-19 09:36:00", 54.0, dc3)   # dc3 突破 signal
buys6 = [c for c in calls6 if c.get("side") == main.OrderSide_Buy]
check("T6 突破 signal → BASE 建仓下单", len(buys6) == 1, f"buys={[c.get('volume') for c in buys6]}")
check("T6b base_order 审计落盘", any(a.get("event") == "base_order" for a in read_audit()))

# ══ T7: BASE 冰点 approaching → 拦截 + build_gate 事件 ══
clear_events()
c7 = make_ctx("2026-08-19 09:36:00")
calls7 = drive_bar(c7, "2026-08-19 09:36:00", 50.0, dc1)   # dc1 冰点但无 m5 → approaching
buys7 = [c for c in calls7 if c.get("side") == main.OrderSide_Buy]
risk7 = [e for e in read_events() if e.get("kind") == "build_gate"]
check("T7a 冰点 approaching → BASE 拦截无下单", len(buys7) == 0, f"buys={len(buys7)}")
check("T7b build_gate 事件留痕（approaching）",
      len(risk7) >= 1 and "approaching" in risk7[0].get("detail", ""), f"risk={risk7}")

# ══ T8: BASE weak → 拦截 ══
clear_events()
c8 = make_ctx("2026-08-19 09:36:00")
calls8 = drive_bar(c8, "2026-08-19 09:36:00", 50.0, dc4)   # dc4 weak
buys8 = [c for c in calls8 if c.get("side") == main.OrderSide_Buy]
risk8 = [e for e in read_events() if e.get("kind") == "build_gate"]
check("T8 weak → BASE 拦截无下单", len(buys8) == 0 and len(risk8) >= 1, f"buys={len(buys8)}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
