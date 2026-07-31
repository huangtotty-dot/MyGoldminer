# coding=utf-8
"""
replay_wp9.py — WP-B 三窗口回放包执行器（Phase B 出口硬材料）

场景定义（锚定 docs/回测复盘/fix6施工与验收手册.md WP-F9）：
  fix2: 000988 单票 / 底仓 800 / 20万 / 2026-04-26~2026-07-24
        验收: TRAIL 退出均价 >= 成本x1.07
  fix3: 000988 单票 / 底仓 800 / 20万 / 2026-04-24~2026-07-24
        验收: TARGET_SELL >=1 档落袋且同档 0 重复
  fix4: 4标的 / 15万 / 2026-04-27~2026-07-24
        验收: 亏损SELL_HIGH 0笔 / 连续>=5日割肉序列 0 / 09:35前非保护卖出 0

产物（每场景）：docs/回测复盘/回放包_WP-B/<场景>/
  backtrace.jsonl + events.jsonl + run.log（KPI 控制台输出）

用法:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe replay_wp9.py fix2
"""
import os
import sys

PROJ = os.path.dirname(os.path.abspath(__file__))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

SCEN = sys.argv[1] if len(sys.argv) > 1 else "fix3"
assert SCEN in ("fix2", "fix3", "fix4"), f"未知场景 {SCEN}"

OUT_DIR = os.path.join(PROJ, "docs", "回测复盘", "回放包_WP-B", SCEN)
os.makedirs(OUT_DIR, exist_ok=True)

# ── 事件桥重定向到场景目录（不污染 E:\06_T 实时桥） ──
import gm_bridge.writer as writer
writer.BRIDGE_DIR = OUT_DIR

import main
from gm.api import run, MODE_BACKTEST, ADJUST_PREV

if SCEN in ("fix2", "fix3"):
    main.STOCKS = {"000988": "SZSE.000988"}
    main.STOCK_NAMES = {"000988": "华工科技"}
    main.MIRROR_HOLDINGS = {"000988": {"qty": 800, "cost": 0}}
    main.INITIAL_CASH = 200000
    CASH = 200000
    START = "2026-04-26 08:00:00" if SCEN == "fix2" else "2026-04-24 08:00:00"
else:
    CASH = 150000
    main.INITIAL_CASH = 150000
    START = "2026-04-27 08:00:00"
END = "2026-07-24 16:00:00"

# 审计日志按场景落盘
main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")

print(f"[replay] 场景={SCEN} 窗口={START}~{END} 资金={CASH} 标的={list(main.STOCKS)}")
print(f"[replay] 产物目录={OUT_DIR}")

run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
    filename="main.py",
    mode=MODE_BACKTEST,
    token="480a6c84b0f43417ffcc9c15162dd7256ca9c3b0",
    backtest_start_time=START,
    backtest_end_time=END,
    backtest_initial_cash=CASH,
    backtest_commission_ratio=0.00015,
    backtest_slippage_ratio=0.0001,
    backtest_adjust=ADJUST_PREV,
    backtest_match_mode=1)
