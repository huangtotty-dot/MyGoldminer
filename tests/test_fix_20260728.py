# coding=utf-8
"""
tests/test_fix_20260728.py — 2026-07-28 复盘必修项 F1/F2 修复验证

验证范围（对应 docs/每日复盘/复盘_2026-07-28.md 第五节）:
  T1  F2: status=8(已拒绝) 卖出拒单 → manual_position 回滚 + reject/risk 事件落桥
  T2  F2: status=8 底仓买入拒单 → _base_ordered 移除(可重试) + reject 事件落桥
  T3  ①-1: status=3 成交 price=0 → 回退到 latest_pre_close，fill 事件价格非 0
  T4  F2: status=12(已过期) 同样进入拒单分支
  T5  F1: 启动持仓对账 → 持仓灌入台账 + _base_settled 跳过底仓重发 + reconcile_init 审计

运行（项目真实环境 Python3.11 + gm sdk）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_fix_20260728.py

注意: 本测试把事件桥目录与审计日志重定向到临时目录，不污染真实数据。
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

# ── 重定向事件桥到临时目录（writer 函数运行时读模块全局 BRIDGE_DIR）──
import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gmfix_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

# 审计日志重定向，避免污染 gmcache/backtrace.jsonl
main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_test.jsonl")

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


def fresh_ctx(**kw):
    ctx = SimpleNamespace(
        executed_orders={},
        manual_position={},
        latest_pre_close={"000988": 110.0, "600481": 4.33},
        _base_ordered=set(),
        _base_settled=set(),
        rejected_order_count=0,
        engine=SimpleNamespace(record_trade_action=lambda *a, **k: None),
        _pending_sell_action={},
        mode=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


# ── T1: status=8 卖出拒单 → 回滚 + 事件 ──
ctx = fresh_ctx()
ctx.manual_position["SZSE.000988"] = {"name": "华工科技", "qty": 0, "available": 0, "t_qty": 0}
order = {"symbol": "SZSE.000988", "status": 8, "volume": 300, "side": 2,
         "price": 0, "ord_rej_reason_detail": "可用股份不足"}
main.on_order_status(ctx, order)
mp = ctx.manual_position["SZSE.000988"]
ev = read_events()
check("T1a status=8卖出拒单→manual_position回滚", mp["qty"] == 300 and mp["available"] == 300,
      f"qty={mp['qty']} avail={mp['available']}")
check("T1b 事件桥出现order_rejected且带拒绝原因",
      any(e.get("kind") == "order_rejected" and "可用股份不足" in str(e.get("detail", "")) for e in ev),
      f"events={[(e.get('event'), e.get('kind')) for e in ev]}")
check("T1c sell_rollback审计落盘",
      any(a.get("event") == "sell_rollback" and a.get("qty") == 300 for a in read_audit()))

# ── T2: status=8 底仓买入拒单 → _base_ordered 移除可重试 + 事件 ──
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
ctx._base_ordered.add("600481")
main.MAX_BASE_RETRY = getattr(main, "MAX_BASE_RETRY", 3)
order = {"symbol": "SHSE.600481", "status": 8, "volume": 1400, "side": 1,
         "price": 0, "ord_rej_reason_detail": "可用资金不足"}
main.on_order_status(ctx, order)
ev = read_events()
check("T2a 底仓拒单→_base_ordered移除(允许重试)", "600481" not in ctx._base_ordered)
check("T2b 底仓拒单→reject事件带资金不足原因",
      any(e.get("event") == "reject" and "资金不足" in str(e.get("reason", "")) for e in ev),
      f"events={[e.get('event') for e in ev]}")

# ── T3: ①-1 status=3 成交 price=0 → 昨收回退 ──
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
order = {"symbol": "SZSE.000988", "status": 3, "volume": 300, "side": 1, "price": 0}
main.on_order_status(ctx, order)
ev = read_events()
fills = [e for e in ev if e.get("event") == "fill"]
check("T3a 市价单price=0→fill事件价格回退昨收110.0",
      len(fills) == 1 and fills[0].get("price") == 110.0,
      f"fill={fills}")
check("T3b 台账成本=回退价(非0)",
      ctx.executed_orders.get("SZSE.000988", {}).get("cost") == 110.0,
      f"cost={ctx.executed_orders.get('SZSE.000988', {}).get('cost')}")

# ── T4: status=12(已过期) 也进拒单分支 ──
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
ctx._base_ordered.add("600176")
order = {"symbol": "SHSE.600176", "status": 12, "volume": 300, "side": 1, "price": 0}
main.on_order_status(ctx, order)
check("T4 status=12→拒单计数+_base_ordered移除",
      ctx.rejected_order_count == 1 and "600176" not in ctx._base_ordered,
      f"rej_count={ctx.rejected_order_count}")

# ── T5: F1 启动持仓对账 ──
class FakeAccount:
    def positions(self, symbol="", side=None):
        if symbol == "SHSE.600481":
            return [SimpleNamespace(volume=5600, available=1400, vwap=4.3025)]
        return []

ctx = fresh_ctx()
ctx.mode = main.MODE_LIVE
ctx.account = lambda: FakeAccount()
main._reconcile_positions_at_init(ctx)
mp = ctx.manual_position.get("SHSE.600481", {})
check("T5a 持仓对账→manual_position灌入真实持仓",
      mp.get("qty") == 5600 and mp.get("available") == 1400,
      f"qty={mp.get('qty')} avail={mp.get('available')}")
check("T5b 成本取gm vwap=4.3025", abs(mp.get("cost", 0) - 4.3025) < 1e-9,
      f"cost={mp.get('cost')}")
check("T5c 已持仓标的入_base_settled(跳过重发底仓单)",
      "600481" in ctx._base_settled and getattr(ctx, "_base_ref_600481", 0) == 5600)
check("T5d reconcile_init审计落盘",
      any(a.get("event") == "reconcile_init" and a.get("code") == "600481" for a in read_audit()))
check("T5e 无持仓标的不入_base_settled", len(ctx._base_settled) == 1)

# ── T6: F6 市价单price=涨跌停保护价 → 成交价优先filled_vwap ──
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
ctx.manual_position["SHSE.600481"] = {"name": "双良节能", "qty": 1400, "available": 1400, "t_qty": 1400, "cost": 3.945}
ctx.executed_orders["SHSE.600481"] = {"name": "双良节能", "qty": 1400, "available": 1400, "t_qty": 1400, "cost": 3.945, "type": "stock", "pre_close": 3.91}
# 卖出：price=3.52(跌停保护价) filled_vwap=4.01(真实成交)
order = {"symbol": "SHSE.600481", "status": 3, "volume": 200, "side": 2,
         "price": 3.52, "filled_vwap": 4.01}
main.on_order_status(ctx, order)
ev = read_events()
fills = [e for e in ev if e.get("event") == "fill"]
check("T6a MKT卖出fill价=真实成交4.01(非跌停价3.52)",
      len(fills) == 1 and abs(fills[0].get("price", 0) - 4.01) < 1e-9,
      f"fill_price={fills[0].get('price') if fills else None}")
# 买入：price=4.32(涨停保护价) filled_vwap=3.98(真实成交) → 成本不得毒化
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
order = {"symbol": "SHSE.600481", "status": 3, "volume": 1400, "side": 1,
         "price": 4.32, "filled_vwap": 3.98}
main.on_order_status(ctx, order)
cost = ctx.executed_orders.get("SHSE.600481", {}).get("cost", 0)
check("T6b MKT买入成本=真实成交3.98(非涨停价4.32)",
      abs(cost - 3.98) < 1e-9, f"cost={cost}")
# 无filled_vwap时回退vwap→price→昨收 链仍可用
if os.path.exists(EVENTS_PATH):
    os.remove(EVENTS_PATH)
ctx = fresh_ctx()
order = {"symbol": "SZSE.000988", "status": 3, "volume": 100, "side": 1, "price": 0, "filled_vwap": 0, "vwap": 0}
main.on_order_status(ctx, order)
ev = read_events()
fills = [e for e in ev if e.get("event") == "fill"]
check("T6c 全零价回退昨收110.0(①-1回归)",
      len(fills) == 1 and fills[0].get("price") == 110.0,
      f"fill={fills}")

# ── 汇总 ──
failed = [r for r in results if not r[1]]
print("\n" + "=" * 50)
print(f"测试目录: {TMP}")
print(f"通过 {len(results) - len(failed)}/{len(results)}")
if failed:
    print("失败项:")
    for name, _, detail in failed:
        print(f"  FAIL {name} {detail}")
    sys.exit(1)
print("全部通过 ✅")
