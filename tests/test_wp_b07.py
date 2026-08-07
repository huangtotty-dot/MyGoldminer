# coding=utf-8
"""
tests/test_wp_b07.py — WP-B07 回补价格记忆（awaiting_buyback 接通 + 高接门控）验证

验证范围（对应 docs/回测复盘/fix5施工与验收手册.md WP-B07）:
  T1  记忆建立：全部卖出通道（SELL_HIGH/PANIC_SELL/TRAIL_SELL/TREND_EXIT/TARGET_SELL/TAIL）
  T2  TTL 过期清除（awaiting_buyback_ttl_minutes=240）
  T3  高接延迟：回补价 > 前卖价×(1+1%) → 不产生 BUY_LOW，last_decision 记 buyback_above_sell_delayed
  T4  高接降档带：溢价 (0, 1%] → 信号保留 + details 带 buyback_downgrade
  T5  低吸接回：价格 ≤ 前卖价 → 不受限 + 激励加分/降阈值
  T6  回补成交清除记忆
  T7  每日清零保留
  T8  main.py 卖出成交回调 → buyback_armed 事件（真实通道名）
  T9  main.py 买入成交回调 → buyback_filled 事件 + 记忆清除
  T10 _apply_buyback_downgrade 减半取整 / 不足 min_unit 延迟 / 无 flag 透传
  T11 0805 场景合成回放：52.14 卖出 → 54.30 高接被延迟 → 回落至 52.00 正常接回 → 闭环清除

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b07.py
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
TMP = tempfile.mkdtemp(prefix="gmwpb07_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se
from signals.engine import SignalEngine, ScoringEngine
import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_wpb07.jsonl")

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def make_df(price, t, n=30, vwap=None):
    """构造 FeatureExtractor 可用的合成 1 分钟 df（n>=14 走 ATR 分支）。"""
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


def new_engine(now_str):
    se.SIM_NOW = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    return SignalEngine()


def eval_with_scores(eng, code, df, buy_score=75.0, sell_score=0.0):
    """固定评分，隔离 WP-B07 门控逻辑做判定。"""
    with mock.patch.object(ScoringEngine, "calc_buy_score", return_value=(buy_score, [])), \
         mock.patch.object(ScoringEngine, "calc_sell_score", return_value=(sell_score, [])):
        return eng.evaluate(code, "五洲新春", df, dict(HOLDING), dict(DAILY_CTX))


# ══ T1: 记忆建立（全卖出通道） ══
eng = new_engine("2026-08-05 09:50:00")
ok_all = True
for act in ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "TARGET_SELL", "TAIL"):
    eng.awaiting_buyback.clear()
    ret = eng.record_trade_action("603667", act, 300, 52.14)
    ab = eng.awaiting_buyback.get("603667")
    if not (ab and ab["sell_price"] == 52.14 and ab["sell_qty"] == 300
            and ab["sell_action"] == act and ret.get("armed")):
        ok_all = False
check("T1 全卖出通道成交均建立回补记忆(含价/量/通道)", ok_all)

# qty=0（地板保护账面登记）不建记忆
eng.awaiting_buyback.clear()
ret0 = eng.record_trade_action("603667", "PANIC_SELL", 0, 52.14)
check("T1b qty=0 账面登记不建记忆",
      "603667" not in eng.awaiting_buyback and ret0.get("armed") is None)

# ══ T2: TTL 过期清除 ══
eng2 = new_engine("2026-08-05 09:35:00")
eng2.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 13, 36, 0)  #  elapsed 241min > TTL 240
df2 = make_df(51.0, "2026-08-05 13:36:00")
eval_with_scores(eng2, "603667", df2)
check("T2 TTL=240min 过期后记忆清除", "603667" not in eng2.awaiting_buyback,
      f"diag={eng2.diagnostics.get('603667')}")

# ══ T3: 高接延迟（0805 场景核心） ══
eng3 = new_engine("2026-08-05 09:50:00")
eng3.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 10, 30, 0)
df3 = make_df(54.30, "2026-08-05 10:30:00")   # 溢价 +4.15% > 延迟线 1%
bs, ss, sig3 = eval_with_scores(eng3, "603667", df3, buy_score=75.0)
dec3 = eng3.last_decision.get("603667", {})
check("T3a 回补价54.30>前卖价52.14×1.01 → BUY_LOW 被延迟",
      sig3 is None and dec3.get("reason") == "buyback_above_sell_delayed",
      f"sig={sig3} reason={dec3.get('reason')}")
check("T3b last_decision/diagnostics 留前卖价/当前价/溢价明细",
      dec3.get("sell_price") == 52.14 and abs(dec3.get("premium", 0) - 0.0414) < 0.001
      and "buyback_delayed" in eng3.diagnostics.get("603667", {}),
      f"premium={dec3.get('premium')}")

# ══ T4: 高接降档带（溢价 0~1%） ══
eng4 = new_engine("2026-08-05 09:50:00")
eng4.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 10, 30, 0)
df4 = make_df(52.50, "2026-08-05 10:30:00")   # 溢价 +0.69% ∈ (0%, 1%]
bs4, ss4, sig4 = eval_with_scores(eng4, "603667", df4, buy_score=75.0)
dg4 = [d for d in (sig4.details if sig4 else []) if isinstance(d, dict) and d.get("buyback_downgrade")]
check("T4 溢价0.69% → 信号保留且 details 带 buyback_downgrade",
      sig4 is not None and sig4.action == "BUY_LOW" and len(dg4) == 1
      and dg4[0].get("sell_price") == 52.14,
      f"sig={sig4 and sig4.action} dg={dg4}")

# ══ T5: 低吸接回（价格 ≤ 前卖价）不受限 + 激励 ══
eng5 = new_engine("2026-08-05 09:50:00")
eng5.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 10, 30, 0)
df5 = make_df(51.50, "2026-08-05 10:30:00")   # 折让 1.23% > 0.5% → 强激励 +15 / 阈值 -10
bs5, ss5, sig5 = eval_with_scores(eng5, "603667", df5, buy_score=55.0)
dg5 = [d for d in (sig5.details if sig5 else []) if isinstance(d, dict) and d.get("buyback_downgrade")]
inc5 = [d for d in (sig5.details if sig5 else []) if isinstance(d, dict) and "接回追踪" in str(d.get("指标", ""))]
check("T5a 折让1.23% → BUY_LOW 照常产生且无 downgrade 标记",
      sig5 is not None and sig5.action == "BUY_LOW" and not dg5,
      f"sig={sig5 and sig5.action}")
check("T5b 激励接通: 55+15=70 分（≥放宽阈值58）且明细含接回追踪加分",
      abs(bs5 - 70.0) < 0.01 and len(inc5) == 1,
      f"buy_score={bs5} inc={inc5}")

# T5c: 纯降阈值通道（折让<0.1% 无加分，仅阈值 -10）
eng5c = new_engine("2026-08-05 09:50:00")
eng5c.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 10, 30, 0)
df5c = make_df(52.10, "2026-08-05 10:30:00")  # 折让 0.077% < 0.1%
bs5c, ss5c, sig5c = eval_with_scores(eng5c, "603667", df5c, buy_score=60.0)
check("T5c 折让<0.1%: 无加分但阈值放宽10 → 60≥58 仍触发",
      sig5c is not None and abs(bs5c - 60.0) < 0.01,
      f"sig={sig5c and sig5c.action} buy_score={bs5c}")

# ══ T6: 回补成交清除记忆 ══
ret6 = eng5.record_trade_action("603667", "BUY_LOW", 300, 51.50)
check("T6 BUY_LOW 成交 → 记忆清除且返回被清记录",
      "603667" not in eng5.awaiting_buyback
      and (ret6.get("buyback_filled") or {}).get("sell_price") == 52.14)

# ══ T7: 每日清零保留 ══
eng7 = new_engine("2026-08-05 14:00:00")
eng7.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 6, 9, 35, 0)   # 次日
df7 = make_df(50.0, "2026-08-06 09:35:00")
eval_with_scores(eng7, "603667", df7)
check("T7 跨日 _check_date_reset 清零保留", "603667" not in eng7.awaiting_buyback)

# ══ T8/T9: main.py 成交回调事件 ══
eng8 = new_engine("2026-08-05 09:50:00")
ctx8 = SimpleNamespace(
    executed_orders={"SHSE.603667": {"name": "五洲新春", "qty": 800, "available": 800,
                                     "t_qty": 800, "cost": 50.0}},
    latest_pre_close={"603667": 52.0},
    _pending_sell_action={"SHSE.603667": ("TRAIL_SELL", 80.0)},
    _inflight_sell={"SHSE.603667": 300},
    engine=eng8,
    rejected_order_count=0,
    manual_position={},
    _base_ordered=set(),
    _base_settled={"603667"},
    mode=None,
)
main.on_order_status(ctx8, {"symbol": "SHSE.603667", "status": 3, "volume": 300,
                            "side": 2, "price": 52.14, "filled_vwap": 52.14})
ev8 = read_events()
armed8 = [e for e in ev8 if e.get("event") == "buyback_armed" and e.get("code") == "603667"]
check("T8a 卖出成交 → 记忆建立且通道名取 _pending_sell_action(TRAIL_SELL)",
      eng8.awaiting_buyback.get("603667", {}).get("sell_action") == "TRAIL_SELL",
      f"ab={eng8.awaiting_buyback.get('603667')}")
check("T8b 事件桥写 buyback_armed（含前卖价）",
      len(armed8) == 1 and armed8[0].get("sell_price") == 52.14,
      f"armed={armed8}")

main.on_order_status(ctx8, {"symbol": "SHSE.603667", "status": 3, "volume": 300,
                            "side": 1, "price": 51.50, "filled_vwap": 51.50})
ev9 = read_events()
filled9 = [e for e in ev9 if e.get("event") == "buyback_filled" and e.get("code") == "603667"]
check("T9 买入成交 → 记忆清除 + buyback_filled 事件",
      "603667" not in eng8.awaiting_buyback and len(filled9) == 1
      and filled9[0].get("sell_price") == 52.14,
      f"filled={filled9}")

# ══ T10: _apply_buyback_downgrade ══
ctx10 = SimpleNamespace(sizer=SimpleNamespace(_effective_params=lambda code: {"stock_min_trade_unit": 100}))
sig_dg = SimpleNamespace(details=[{"buyback_downgrade": True, "sell_price": 52.14,
                                   "price": 52.50, "premium": 0.0069}])
q1, dg1, mu1 = main._apply_buyback_downgrade(ctx10, "603667", sig_dg, 500)
q2, dg2, mu2 = main._apply_buyback_downgrade(ctx10, "603667", sig_dg, 150)
q3, dg3, mu3 = main._apply_buyback_downgrade(ctx10, "603667", SimpleNamespace(details=[]), 500)
check("T10a 降档: 500 → 减半取整 200", q1 == 200 and dg1 is not None and mu1 == 100, f"q={q1}")
check("T10b 降档: 150 → 0 <min_unit（调用方应延迟）", q2 == 0 and dg2 is not None, f"q={q2}")
check("T10c 无 flag 透传: 500 → 500", q3 == 500 and dg3 is None, f"q={q3}")

# ══ T11: 0805 场景合成回放（52.14 卖 → 54.30 高接延迟 → 52.00 接回 → 闭环） ══
eng11 = new_engine("2026-08-05 09:50:00")
eng11.record_trade_action("603667", "SELL_HIGH", 300, 52.14)
se.SIM_NOW = datetime(2026, 8, 5, 10, 30, 0)
_, _, s11a = eval_with_scores(eng11, "603667", make_df(54.30, "2026-08-05 10:30:00"))
se.SIM_NOW = datetime(2026, 8, 5, 11, 20, 0)
_, _, s11b = eval_with_scores(eng11, "603667", make_df(52.00, "2026-08-05 11:20:00"))
ret11 = eng11.record_trade_action("603667", "BUY_LOW", 300, 52.00)
check("T11 0805场景: 54.30高接被延迟 / 52.00正常接回 / 成交后闭环清除",
      s11a is None and s11b is not None and s11b.action == "BUY_LOW"
      and "603667" not in eng11.awaiting_buyback
      and (ret11.get("buyback_filled") or {}).get("sell_price") == 52.14,
      f"high={s11a} low={s11b and s11b.action}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
