# coding=utf-8
"""
replay_wp_b13b14_n19.py — WP-B13/B14 验收回放（N19 实证场景：603667 05-08 09:32）

场景定义（锚定 docs/回测复盘/修复方案_WP-B13-B14_TARGET开盘缓冲与L1位图持久化.md 3.2）：
  单票 603667 五洲新春 / 底仓 800 / 15万 / 2026-05-07~2026-05-12
  实证样本：05-08 09:32 TARGET_SELL 100@72.75（旧代码穿透开盘缓冲）。
  预置持仓 cost=66.0（72.75 处 ≈ +10.2%，与实证 profit 一致）。

验收标准：
  N19: ① 09:35 前 TARGET_SELL 0 笔成交；② morning_sell_blocked 有 action=TARGET_SELL 样本；
       ③ 09:35 后如条件仍满足应正常落袋（analyze_replay.py 可复核）。

产物：docs/回测复盘/回放包_WP-B/b13b14_n19/
  backtrace.jsonl + events.jsonl（事件桥重定向到场景目录）

用法（须在掘金终端环境运行，token 需有效）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe replay_wp_b13b14_n19.py
"""
import os
import sys

PROJ = os.path.dirname(os.path.abspath(__file__))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

OUT_DIR = os.path.join(PROJ, "docs", "回测复盘", "回放包_WP-B", "b13b14_n19")
os.makedirs(OUT_DIR, exist_ok=True)

import gm_bridge.writer as writer
writer.BRIDGE_DIR = OUT_DIR

import main
from gm.api import run, MODE_BACKTEST, ADJUST_PREV
from utils.gm_token import load_token

main.STOCKS = {"603667": "SHSE.603667"}
main.STOCK_NAMES = {"603667": "五洲新春"}
main.MIRROR_HOLDINGS = {"603667": {"qty": 800, "cost": 0}}
main.INITIAL_CASH = 150000
CASH = 150000
START = "2026-05-07 08:00:00"
END = "2026-05-12 16:00:00"

main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")

# N19: 预置 603667 持仓 800 股 cost=66.0 —— 05-08 09:32 价 72.75 → +10.2% 触发 TARGET
# （不回放则当日无法自然建仓到该成本，与 wpb07 预置口径一致）
_orig_init = main.init


def _seeded_init(context):
    _orig_init(context)
    _sym = "SHSE.603667"
    _seed = {"name": "五洲新春", "qty": 800, "available": 800, "t_qty": 800,
             "cost": 66.0, "type": "stock", "pre_close": 66.0}
    context.executed_orders[_sym] = dict(_seed)
    context.manual_position[_sym] = dict(_seed)
    context._base_settled.add("603667")
    context._base_ref_603667 = 800
    print("[replay] WP-B13/B14-N19 预置持仓: 603667 800股 cost=66.0")


main.init = _seeded_init

print(f"[replay] WP-B13/B14-N19 场景=603667 窗口={START}~{END} 资金={CASH}")
print(f"[replay] 产物目录={OUT_DIR}")

run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
    filename="main.py",
    mode=MODE_BACKTEST,
    token=load_token(),
    backtest_start_time=START,
    backtest_end_time=END,
    backtest_initial_cash=CASH,
    backtest_commission_ratio=0.00015,
    backtest_slippage_ratio=0.0001,
    backtest_adjust=ADJUST_PREV,
    backtest_match_mode=1)
