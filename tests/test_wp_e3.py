# coding=utf-8
"""
tests/test_wp_e3.py — WP-E3 持仓槽位制（≤4 支 + 预算按 4 支分解）验证

验证范围（对应 docs/回测复盘/fix5施工与验收手册.md WP-E3）:
  T1  槽位计数（qty=0 不占槽、held_codes 正确）
  T2  第 5 支建仓被 slot_full 拦截 + 每票每日去重 + 次日重置
  T3  已持仓票做T买入不受槽位闸限制（闸条件仅 pos_qty<=0，源码接线断言）
  T4  清仓到 0 释放槽位后新票可建
  T5  预算分母=max_concurrent_positions=4（equity×80%/4）
  T6  底仓建仓块槽满 → base_deferred(reason=slot_full) + audit slot_full(where=base)

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_e3.py
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
TMP = tempfile.mkdtemp(prefix="gmwpe3_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_wpe3.jsonl")

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


def make_ctx(held: dict):
    """held: {code: qty} → manual_position 语境"""
    return SimpleNamespace(
        manual_position={f"SHSE.{c}" if c.startswith("6") else f"SZSE.{c}": {"qty": q, "cost": 50.0}
                         for c, q in held.items()},
    )


# ══ T1: 槽位计数 ══
ctx1 = make_ctx({"603667": 800, "000988": 500, "300054": 0, "600176": 100})
codes1 = main._held_codes(ctx1)
check("T1a qty=0 不占槽: 4 票入账仅 3 槽",
      main._held_position_count(ctx1) == 3, f"count={main._held_position_count(ctx1)}")
check("T1b held_codes 正确且不含零持仓票",
      sorted(codes1) == ["000988", "600176", "603667"], f"codes={codes1}")

# ══ T2: 第 5 支建仓 slot_full 拦截 + 去重 + 次日重置 ══
ctx2 = make_ctx({"603667": 800, "000988": 500, "600176": 500, "600481": 1400})
check("T2a 持 4 支 → 槽满", main._slot_full(ctx2) is True)
now = datetime.now().replace(microsecond=0)
emitted2 = main._emit_slot_full(ctx2, "300054", now, "buy")
ev2 = [e for e in read_events() if e.get("kind") == "slot_full"]
au2 = [a for a in read_audit() if a.get("event") == "slot_full"]
check("T2b 第 5 支候选 → slot_full 事件(含 held_count/held_codes/candidate)",
      emitted2 is True and len(ev2) == 1
      and "held_count=4/4" in ev2[0].get("detail", "")
      and "candidate=300054" in ev2[0].get("detail", "")
      and "held_codes=" in ev2[0].get("detail", ""),
      f"detail={ev2 and ev2[0].get('detail')}")
check("T2c audit slot_full(where=buy) 同步落盘",
      len(au2) == 1 and au2[0].get("where") == "buy" and au2[0].get("held_count") == 4,
      f"audit={au2}")
emitted2b = main._emit_slot_full(ctx2, "300054", now + timedelta(minutes=3), "buy")
ev2b = [e for e in read_events() if e.get("kind") == "slot_full"]
check("T2d 每票每日去重: 同日第二次不写新事件", emitted2b is False and len(ev2b) == 1)
emitted2c = main._emit_slot_full(ctx2, "300054", now + timedelta(days=1), "buy")
ev2c = [e for e in read_events() if e.get("kind") == "slot_full"]
check("T2e 次日重置去重键再写一条", emitted2c is True and len(ev2c) == 2)

# ══ T3: 已持仓票做T买入不受槽位闸限制 ══
with open(os.path.join(PROJ, "main.py"), encoding="utf-8") as _f:
    _src = _f.read()
check("T3 买入块槽位闸条件仅 pos_qty<=0（做T买入 pos_qty>0 不检查）",
      "if pos_qty <= 0 and _slot_full(context):" in _src)

# ══ T4: 清仓到 0 释放槽位 ══
ctx4 = make_ctx({"603667": 800, "000988": 500, "600176": 500, "600481": 1400})
assert main._slot_full(ctx4) is True
ctx4.manual_position["SZSE.000988"]["qty"] = 0  # 清仓释放 1 槽
check("T4 清仓到 0 即释放槽位(4→3)，新候选可建",
      main._slot_full(ctx4) is False and main._held_position_count(ctx4) == 3)

# ══ T5: 预算分母=4 ══
bud5, mps5 = main._stock_budget_cap(None, "603667", 50.0, 150000.0)
check("T5 每股预算=150000×80%/4=30000，cp=50 → max_pos_shares=600",
      abs(bud5 - 30000.0) < 0.01 and mps5 == 600,
      f"budget={bud5} mps={mps5}")

# ══ T6: 底仓建仓块槽满 → base_deferred(reason=slot_full) ══
ctx6 = make_ctx({"603667": 800, "000988": 500, "600176": 500, "600481": 1400})
emitted6 = main._emit_slot_full(ctx6, "002639", now, "base")
ev6 = [e for e in read_events()
       if e.get("kind") == "base_deferred" and "reason=slot_full" in e.get("detail", "")]
au6 = [a for a in read_audit() if a.get("event") == "slot_full" and a.get("where") == "base"]
check("T6a base 路径复用 base_deferred 事件且 detail 含 reason=slot_full",
      emitted6 is True and len(ev6) == 1 and "candidate=002639" in ev6[0].get("detail", ""),
      f"detail={ev6 and ev6[0].get('detail')}")
check("T6b audit slot_full(where=base) 同步落盘", len(au6) == 1, f"audit={au6}")
check("T6c 底仓块源码接线: 槽满复用延迟机制(_emit_slot_full where=base + _topup_blocked)",
      '_emit_slot_full(context, code, now, "base")' in _src)

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
