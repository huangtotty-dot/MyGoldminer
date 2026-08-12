# coding=utf-8
"""
tests/test_fix_20260731.py — 2026-07-31 复盘必修项 F9/F10/F11 修复验证

验证范围（对应 docs/每日复盘/复盘_2026-07-31.md 第四节）:
  T8  F9: 在途卖单守卫 —— 首单成功后在途=100；第二单被拦(inflight_skip)；
      成交回调后在途归零；虚冷却在下单时已登记
  T9  F9: 同 eob 重复 bar 去重
  T10 F11: 终端空仓 + 台账余量 → 向下对账归零 + reconcile_fix 审计
  T11 F10: 审计文件清空门控 —— MODE_LIVE 保留 / 回测模式清空

运行（项目真实环境 Python3.11 + gm sdk）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_fix_20260731.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gmfix0731_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_test0731.jsonl")

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


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


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


# ── T8: F9 在途卖单守卫 + 虚冷却 ──
placed = []
_orig_order_volume = main.order_volume
main.order_volume = lambda **kw: placed.append(kw)

now = datetime.now().replace(microsecond=0)
engine = SimpleNamespace(
    sell_cooldown={},
    sell_count_per_stock={},
    record_trade_action=lambda *a, **k: None,
    _get_params=lambda code: {"cooldown_minutes": 30},
)
ctx = SimpleNamespace(
    daily_sell_count={},
    total_trade_count=0,
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 300, "available": 300, "t_qty": 300, "cost": 108.98}},
    executed_orders={"SZSE.000988": {"name": "华工科技", "qty": 300, "available": 300, "t_qty": 300, "cost": 108.98}},
    engine=engine,
    sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 100),
    last_index_regime="range",
    _pending_sell_action={},
    _inflight_sell={},
    latest_pre_close={"000988": 91.10},
    mode=None,
)
sig = SimpleNamespace(action="PANIC_SELL", score=75.0, reasons=["深度亏损恐慌卖出"])
holding = {"qty": 300, "available": 300, "cost": 108.98}

r1 = main._sell_arbiter(ctx, "000988", sig, 300, 95.66, now, holding, 65, {}, "SZSE.000988")
check("T8a 首笔PANIC卖单成功执行", r1 is True and len(placed) == 1,
      f"placed={len(placed)}")
check("T8b 下单后在途量=100", ctx._inflight_sell.get("SZSE.000988") == 100,
      f"inflight={ctx._inflight_sell}")
check("T8c 下单即计虚冷却(不等成交)", "000988" in ctx.engine.sell_cooldown,
      f"cooldown={ctx.engine.sell_cooldown}")

# 成交回报未回，第二根bar再次评估PANIC → 必须被在途守卫拦截
r2 = main._sell_arbiter(ctx, "000988", sig, 200, 95.66, now + timedelta(minutes=1), holding, 65, {}, "SZSE.000988")
check("T8d 在途期间第二笔卖单被拦截", r2 is False and len(placed) == 1,
      f"placed={len(placed)}")
check("T8e inflight_skip审计落盘",
      any(a.get("event") == "inflight_skip" and a.get("code") == "000988" for a in read_audit()))

# 成交回调到达 → 在途释放
main.on_order_status(ctx, {"symbol": "SZSE.000988", "status": 3, "volume": 100,
                           "side": 2, "price": 95.66, "filled_vwap": 95.66})
check("T8f 成交回调后在途量归零", ctx._inflight_sell.get("SZSE.000988") == 0,
      f"inflight={ctx._inflight_sell}")

# 拒单回调同样释放在途
ctx._inflight_sell["SZSE.000988"] = 100
main.on_order_status(ctx, {"symbol": "SZSE.000988", "status": 8, "volume": 100,
                           "side": 2, "price": 0, "ord_rej_reason_detail": "仓位不足"})
check("T8g 拒单回调后在途量归零", ctx._inflight_sell.get("SZSE.000988") == 0,
      f"inflight={ctx._inflight_sell}")

main.order_volume = _orig_order_volume

# ── T9: F9 重复 bar 去重 ──
ctx9 = SimpleNamespace(_last_bar_eob={})
d1 = main._dedup_bar(ctx9, "SZSE.000988", "2026-07-31 14:48:00+08:00")
d2 = main._dedup_bar(ctx9, "SZSE.000988", "2026-07-31 14:48:00+08:00")
d3 = main._dedup_bar(ctx9, "SZSE.000988", "2026-07-31 14:49:00+08:00")
d4 = main._dedup_bar(ctx9, "SHSE.600481", "2026-07-31 14:48:00+08:00")
check("T9 同票同eob判重/新eob与异票放行",
      d1 is False and d2 is True and d3 is False and d4 is False,
      f"seq={[d1, d2, d3, d4]}")

# ── T10: F11 终端空仓 → 台账向下对账归零 ──
class EmptyAccount:
    def positions(self, symbol="", side=None):
        return []

ctx10 = SimpleNamespace(
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 300, "available": 300, "t_qty": 300, "cost": 108.98}},
    executed_orders={},
    latest_pre_close={"000988": 95.41},
    mode=main.MODE_LIVE,
    account=lambda: EmptyAccount(),
)
h = main._get_holding(ctx10, "000988", "SZSE.000988")
mp10 = ctx10.manual_position["SZSE.000988"]
check("T10a 终端空仓→manual_position归零",
      mp10["qty"] == 0 and mp10["available"] == 0 and mp10["t_qty"] == 0,
      f"mp={mp10}")
check("T10b reconcile_fix审计(old=300→new=0)",
      any(a.get("event") == "reconcile_fix" and a.get("old_qty") == 300 and a.get("new_qty") == 0
          for a in read_audit()))
check("T10c _get_holding返回零持仓", int(h.get("qty", 0) or 0) == 0,
      f"qty={h.get('qty')}")

# ── T11: F10 审计清空门控 ──
with open(main._AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
    f.write(json.dumps({"event": "probe"}) + "\n")
kept = main._maybe_clear_audit_log(SimpleNamespace(mode=main.MODE_LIVE))
check("T11a MODE_LIVE(模拟盘)→审计文件保留",
      kept is False and os.path.exists(main._AUDIT_LOG_PATH))
cleared = main._maybe_clear_audit_log(SimpleNamespace(mode=None))
check("T11b 回测模式→审计文件清空",
      cleared is True and not os.path.exists(main._AUDIT_LOG_PATH))

# ── T12: F12 底仓/回补成交同步台账时保留做T状态键 ──
ctx12 = SimpleNamespace(
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 600, "available": 600, "t_qty": 600,
                                     "cost": 150.0, "_target_l1_state": "filled",
                                     "_trail_state": "ARMED", "_trail_peak": 175.0}},
    executed_orders={"SZSE.000988": {"name": "华工科技", "qty": 600, "available": 600, "t_qty": 600, "cost": 150.0}},
    latest_pre_close={"000988": 150.0},
    _base_ordered={"000988"},
    _base_settled={"000988"},
    engine=SimpleNamespace(record_trade_action=lambda *a, **k: None),
    rejected_order_count=0,
    mode=None,
)
main.on_order_status(ctx12, {"symbol": "SZSE.000988", "status": 3, "volume": 200,
                             "side": 1, "price": 160.0, "filled_vwap": 160.0})
mp12 = ctx12.manual_position["SZSE.000988"]
check("T12a 回补成交后台账量更新(600+200=800)", mp12.get("qty") == 800,
      f"qty={mp12.get('qty')}")
check("T12b _target_l1_state(filled) 不被清空", mp12.get("_target_l1_state") == "filled")
check("T12c _trail_state/_trail_peak 不被清空",
      mp12.get("_trail_state") == "ARMED" and mp12.get("_trail_peak") == 175.0)

# ── T13: F13 TREND_EXIT 卖出量封顶到超 base_ref 部分 ──
placed13 = []
main.order_volume = lambda **kw: placed13.append(kw)
ctx13 = SimpleNamespace(
    daily_sell_count={},
    total_trade_count=0,
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 600, "available": 600, "t_qty": 600, "cost": 120.0}},
    executed_orders={},
    engine=SimpleNamespace(sell_cooldown={}, sell_count_per_stock={},
                           record_trade_action=lambda *a, **k: None,
                           _get_params=lambda code: {"cooldown_minutes": 30}),
    sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200),
    last_index_regime="range",
    _pending_sell_action={},
    _inflight_sell={},
    latest_pre_close={"000988": 125.0},
    mode=None,
)
setattr(ctx13, "_base_ref_000988", 500)
sig13 = SimpleNamespace(action="TREND_EXIT", score=78.0, reasons=["趋势破坏止盈"])
r13 = main._sell_arbiter(ctx13, "000988", sig13, 600, 130.0, now,
                         {"qty": 600, "available": 600, "cost": 120.0}, 65, {}, "SZSE.000988")
check("T13a pos600/base_ref500/excess100 → 实际卖出封顶100(非sizer的200)",
      r13 is True and placed13 and placed13[0].get("volume") == 100,
      f"placed={placed13}")
# excess<100 时整体不触发
placed13.clear()
ctx13._inflight_sell = {}
ctx13.manual_position["SZSE.000988"]["qty"] = 550
r13b = main._sell_arbiter(ctx13, "000988", sig13, 550, 130.0, now,
                          {"qty": 550, "available": 550, "cost": 120.0}, 65, {}, "SZSE.000988")
check("T13b excess=50<100 → TREND_EXIT不下单", r13b is False and not placed13)
main.order_volume = _orig_order_volume

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
