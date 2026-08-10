# coding=utf-8
"""
tests/test_fix_20260810.py — 2026-08-10 复盘①修包回归验证

背景（详见 docs/每日复盘/复盘_2026-08-10.md 第七节）：
  F7-2  _refresh_daily_ctx 成功路径 daily_status 恒为 "unavailable"——
        _default_daily_context 自带 "unavailable"，F7 的 setdefault 永不覆盖。
        后果：G4 对所有 M2 通过票恒报"日线数据不足→保守拦截"（五要素上线 3 日
        零运行的真根因，0806/0807 归因"取数失败"系误判）；引擎 daily_buy_t_ok 恒 False。
  O-04  watcher：① 多实例并存（旧代码实例 + 自动拉起实例）致告警双发/三发；
        ② O-02 时段窗进窗时以陈旧心跳算 gap，12:55 开门即误报"中断"；
        ③ 心跳类推送无去重安全网。

验证范围：
  T1  F7-2：取数成功 → daily_status=="ok"，G4 不再恒报"数据不足"（进入五要素评估）
  T2  F7-2：M2 未过 → daily_status 保持 "pool_gate_fail"（F7 原验收语义不回归）
  T3  O-01：取数异常 → unavailable + [daily] 每票每日仅打印一次 + 连续第 10 次
      失败写 data_fetch_fail 风险事件（且仅一次）
  T4  O-04：非交易时段冻结基线 → 进窗时陈旧心跳不触发误报
  T5  O-04：心跳推送 120s 去重 + 单例锁源码接线断言

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_fix_20260810.py
"""
import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gmfix0810_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)
import gm_bridge.watcher as watcher  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_0810.jsonl")
watcher.BRIDGE_DIR = TMP

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def make_daily_rows(n=120, close=50.0, volume=5_000_000):
    """构造 M2 达标（振幅≥3%/额≥2亿/单手≥2000）的日线行。"""
    rows = []
    base = datetime(2026, 1, 1)
    for i in range(n):
        c = close + i * 0.01
        rows.append({
            "eob": (base + timedelta(days=i)).strftime("%Y-%m-%d 15:00:00"),
            "open": c, "high": c * 1.02, "low": c * 0.98,
            "close": c, "volume": volume,
        })
    return rows


NOW = datetime(2026, 8, 10, 9, 31)

# ══ T1: 取数成功 → daily_status=="ok"，G4 进入五要素评估 ══
_orig_history_n = main.history_n
main.history_n = lambda **kw: make_daily_rows()
ctx_ns = SimpleNamespace(latest_pre_close={})
dc = main._refresh_daily_ctx(ctx_ns, "600176", "SHSE.600176", NOW)
check("T1a 成功取数 daily_status==ok", dc.get("daily_status") == "ok",
      f"实际={dc.get('daily_status')}")
check("T1b M2 指标已计算", dc.get("_m2_pool_pass") is True,
      f"_m2_pool_pass={dc.get('_m2_pool_pass')}")
_ok, why = main._base_entry_gate(50.0, dc)
check("T1c G4 不再恒报数据不足", "数据不足" not in why, f"G4判定={why[:60]}")

# ══ T2: M2 未过 → pool_gate_fail 保留（F7 语义） ══
main.history_n = lambda **kw: make_daily_rows(volume=1000)  # 额仅 ~5000万 < 2亿
ctx_ns2 = SimpleNamespace(latest_pre_close={})
dc2 = main._refresh_daily_ctx(ctx_ns2, "600176", "SHSE.600176", NOW)
check("T2 M2未过保持 pool_gate_fail", dc2.get("daily_status") == "pool_gate_fail",
      f"实际={dc2.get('daily_status')}")

# ══ T3: O-01 异常留痕 + 连续10次失败 risk 事件 ══
def _boom(**kw):
    raise RuntimeError("terminal busy")
main.history_n = _boom
ctx3 = SimpleNamespace(latest_pre_close={})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    for _ in range(2):
        dc3 = main._refresh_daily_ctx(ctx3, "600176", "SHSE.600176", NOW)
check("T3a 异常→unavailable", dc3.get("daily_status") == "unavailable")
check("T3b [daily]异常每票每日仅打印1次", buf.getvalue().count("[daily]") == 1,
      f"打印次数={buf.getvalue().count('[daily]')}")
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(8):  # 累计 10 次
        main._refresh_daily_ctx(ctx3, "600176", "SHSE.600176", NOW)
evs = [e for e in read_events() if e.get("kind") == "data_fetch_fail"]
check("T3c 第10次失败写 data_fetch_fail", len(evs) == 1, f"事件数={len(evs)}")
with contextlib.redirect_stdout(io.StringIO()):
    main._refresh_daily_ctx(ctx3, "600176", "SHSE.600176", NOW)  # 第 11 次不再发
evs = [e for e in read_events() if e.get("kind") == "data_fetch_fail"]
check("T3d 第11次不重复告警", len(evs) == 1, f"事件数={len(evs)}")
main.history_n = _orig_history_n

# ══ T4: O-04 时段窗基线冻结——进窗不以陈旧心跳误报 ══
pushes = []
watcher._push = lambda title, content, level="info": pushes.append((title, level))
hb_file = os.path.join(TMP, "heartbeat.json")
with open(hb_file, "w", encoding="utf-8") as f:
    f.write("{}")
stale = time.time() - 5400  # 心跳停在 90 分钟前（午休末尾场景）
os.utime(hb_file, (stale, stale))
watcher._heartbeat_path = lambda: hb_file

# 12:54（窗外）→ 冻结基线；12:55（窗内）→ gap 应≈0，不报"中断"
watcher._last_heartbeat_ok = True
watcher._last_hb_ts = stale
watcher._in_trading_window = lambda dt: False
watcher._check_heartbeat()
watcher._in_trading_window = lambda dt: True
watcher._check_heartbeat()
check("T4 进窗不以陈旧心跳误报", not any("中断" in t for t, _ in pushes),
      f"pushes={pushes}")

# 真死场景：窗内基线陈旧 700s → 必须报警（防误报不能以漏报为代价）
pushes.clear()
watcher._last_heartbeat_ok = True
watcher._last_hb_ts = time.time() - 700
watcher._check_heartbeat()
check("T4b 窗内真死仍报警", any("中断" in t for t, lv in pushes if lv == "red"),
      f"pushes={pushes}")

# ══ T5: 推送去重 + 单例锁 ══
pushes.clear()
watcher._last_push.clear()
watcher._push_throttled("hb|up", "✅ 心跳恢复", "x", "green")
watcher._push_throttled("hb|up", "✅ 心跳恢复", "x", "green")
watcher._push_throttled("hb|up", "✅ 心跳恢复", "x", "green")
check("T5a 同 key 推送 120s 内只发一次", len(pushes) == 1, f"次数={len(pushes)}")

# 单例：自心跳新鲜且 pid=本人 → 不动；过期 → 直接接管（不杀不拦）
whb = os.path.join(TMP, "watcher_heartbeat.json")
with open(whb, "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid()}, f)
watcher._ensure_singleton()  # 若误杀本人，测试进程已死，走不到这里
check("T5b 单例对本人实例放行", True)
src = inspect.getsource(watcher._ensure_singleton)
check("T5c 单例锁接线（os.kill + run()调用）",
      "os.kill" in src and "_ensure_singleton()" in inspect.getsource(watcher.run))

print("\n===== %d/%d PASS =====" % (sum(1 for _, ok, _ in results if ok), len(results)))
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
