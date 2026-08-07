# coding=utf-8
"""
tests/test_wp_e2.py — WP-E2 max_pos 接线（总权益预算制 + 个股最大仓位约束）验证

验证范围（对应 docs/回测复盘/fix5施工与验收手册.md WP-E2）:
  T1  总权益口径：现金 + 多票持仓市值；无 bar 价格退化成本价；空仓=纯现金
  T2  等权预算与 max_pos_shares 取整（equity×80%/16）
  T3  到顶拦截：pos >= max(max_pos_shares, base_ref) → max_pos_cap 事件+audit+每票每日去重
  T4  新票 300 股兜底保留 / 持仓票不再兜底（源码接线断言）
  T5  N2 分母修正：总权益含其他票持仓（公式 + 源码接线断言）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_e2.py
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
TMP = tempfile.mkdtemp(prefix="gmwpe2_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_wpe2.jsonl")

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


# ══ T1: 总权益口径 ══
ctx1 = SimpleNamespace(
    manual_position={
        "SHSE.603667": {"qty": 800, "cost": 52.0},
        "SZSE.000988": {"qty": 500, "cost": 100.0},
        "SZSE.300054": {"qty": 0, "cost": 74.0},   # 零持仓不计
    },
    bar_cache={
        "SHSE.603667": [{"close": 50.0}],
        "SZSE.000988": [{"close": 90.0}],
    },
)
eq1 = main._total_equity(ctx1, 20000.0)
check("T1a 总权益=现金+多票持仓市值(20000+800×50+500×90)",
      abs(eq1 - (20000 + 800 * 50 + 500 * 90)) < 0.01, f"equity={eq1}")

ctx1b = SimpleNamespace(
    manual_position={"SHSE.603667": {"qty": 800, "cost": 52.0}},
    bar_cache={},  # 无 bar 价格
)
eq1b = main._total_equity(ctx1b, 20000.0)
check("T1b 无 bar 价格退化成本价估值(20000+800×52)",
      abs(eq1b - (20000 + 800 * 52)) < 0.01, f"equity={eq1b}")

eq1c = main._total_equity(SimpleNamespace(manual_position={}, bar_cache={}), 150000.0)
check("T1c 空仓时总权益=纯现金", abs(eq1c - 150000.0) < 0.01, f"equity={eq1c}")

# ══ T2: 等权预算与 max_pos_shares（WP-E3 修订：分母 len(STOCKS)=16 → 槽位数=4） ══
bud2, mps2 = main._stock_budget_cap(None, "603667", 50.0, 150000.0)
n_slots = main.PARAMS.get("max_concurrent_positions", 4)
check("T2a 等权预算=150000×80%/4=30000（WP-E3 槽位制分母）",
      n_slots == 4 and abs(bud2 - 150000 * 0.8 / 4) < 0.01,
      f"slots={n_slots} budget={bud2}")
check("T2b cp=50 → max_pos_shares=floor(30000/50/100)×100=600", mps2 == 600, f"mps={mps2}")
_, mps2b = main._stock_budget_cap(None, "603667", 74.0, 150000.0)
_, mps2c = main._stock_budget_cap(None, "603667", 100.0, 150000.0)
_, mps2d = main._stock_budget_cap(None, "603667", 0.0, 150000.0)
check("T2c 取整边界: cp=74→400 / cp=100→300 / cp=0→0",
      mps2b == 400 and mps2c == 300 and mps2d == 0,
      f"74→{mps2b} 100→{mps2c} 0→{mps2d}")

# ══ T3: 到顶拦截 + 事件 + 去重 ══
now = datetime.now().replace(microsecond=0)
ctx3 = SimpleNamespace()
blocked3 = main._check_max_pos_cap(ctx3, "603667", now, 800, 800, 500, 7500.0, 150000.0)
ev3 = [e for e in read_events() if e.get("kind") == "max_pos_cap"]
au3 = [a for a in read_audit() if a.get("event") == "max_pos_cap"]
check("T3a pos800>=ceiling800 → 拦截", blocked3 is True)
check("T3b risk 事件 max_pos_cap 含 budget/equity/weight/max_pos_shares/pos_qty",
      len(ev3) == 1 and all(k in ev3[0].get("detail", "")
                            for k in ("budget=7500", "equity=150000", "weight=",
                                      "max_pos_shares=500", "pos_qty=800")),
      f"detail={ev3 and ev3[0].get('detail')}")
check("T3c audit 同步落盘", len(au3) == 1 and au3[0].get("code") == "603667",
      f"audit={au3}")

# 去重：同日第二次拦截不重复写事件，但拦截语义不变
blocked3b = main._check_max_pos_cap(ctx3, "603667", now + timedelta(minutes=5),
                                    800, 800, 500, 7500.0, 150000.0)
ev3b = [e for e in read_events() if e.get("kind") == "max_pos_cap"]
check("T3d 每票每日去重: 同日第二次拦截不写新事件但仍拦截",
      blocked3b is True and len(ev3b) == 1, f"events={len(ev3b)}")

# 次日重新放行事件
blocked3c = main._check_max_pos_cap(ctx3, "603667", now + timedelta(days=1),
                                    800, 800, 500, 7500.0, 150000.0)
ev3c = [e for e in read_events() if e.get("kind") == "max_pos_cap"]
check("T3e 次日重置去重键再写一条", blocked3c is True and len(ev3c) == 2,
      f"events={len(ev3c)}")

# 未到顶 / 底仓取高语义
pass3f = main._check_max_pos_cap(ctx3, "603667", now, 800, 800, 1000, 7500.0, 150000.0)
pass3g = main._check_max_pos_cap(ctx3, "603667", now, 500, 800, 100, 7500.0, 150000.0)
block3h = main._check_max_pos_cap(ctx3, "603667", now, 0, 0, 500, 7500.0, 150000.0)
check("T3f 未到顶(pos800<mps1000)放行", pass3f is False)
check("T3g 底仓取高: pos500>mps100 但<base_ref800 → 放行(不在底仓下方收口)",
      pass3g is False)
check("T3h 无持仓不拦截(走新票兜底通道)", block3h is False)

# force 分支（sizer 返回 0 确认）
ctx3i = SimpleNamespace()
block3i = main._check_max_pos_cap(ctx3i, "000988", now, 400, 500, 1000, 7500.0, 150000.0,
                                  force=True)
au3i = [a for a in read_audit() if a.get("event") == "max_pos_cap" and a.get("reason") == "sizer_zero_at_cap"]
check("T3i force=True(sizer_zero_at_cap) 即使未到顶也拦截并留痕",
      block3i is True and len(au3i) == 1, f"audit={au3i}")

# ══ T4: 新票 300 股兜底保留 / 持仓票不再兜底 ══
with open(os.path.join(PROJ, "main.py"), encoding="utf-8") as _f:
    _src = _f.read()
check("T4a pos_qty<=0 全新建仓保留 qty=300 兜底",
      "if pos_qty <= 0:\n                    qty = 300" in _src)
check("T4b 旧的无条件 qty=300 兜底已移除",
      "qty = 300  # sizer 返回 0 时的最小交易量" not in _src)
check("T4c 持仓票 sizer=0 走 max_pos_cap(force) 拦截后 continue",
      "force=True)" in _src)

# ══ T5: N2 分母修正 ══
# 场景：现金 5000 / 本票 1000 股@50 / 其他票持仓 80000 → 买 500 股@50
# 旧分母=5000+50000=55000 → 75000/55000=136%>80% 误拦
# 新分母=5000+50000+80000=135000 → 55.6%<80% 放行
ctx5 = SimpleNamespace(
    manual_position={
        "SHSE.603667": {"qty": 1000, "cost": 50.0},
        "SZSE.000988": {"qty": 800, "cost": 100.0},
    },
    bar_cache={
        "SHSE.603667": [{"close": 50.0}],
        "SZSE.000988": [{"close": 100.0}],
    },
)
eq5 = main._total_equity(ctx5, 5000.0)
new_pos_value = 1000 * 50 + 500 * 50
old_denom = 5000 + 1000 * 50
check("T5a 场景实证: 旧分母误拦(136%>80%) 新分母正确放行(55.6%<80%)",
      new_pos_value / old_denom > 0.80 and new_pos_value / eq5 <= 0.80,
      f"old={new_pos_value/old_denom:.1%} new={new_pos_value/eq5:.1%} equity={eq5}")
check("T5b N2 源码接线: total_equity_value 改用 _total_eq",
      "total_equity_value = _total_eq" in _src)

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
