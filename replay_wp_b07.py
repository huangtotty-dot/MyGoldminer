# coding=utf-8
"""
replay_wp_b07.py — WP-B07 回补价格记忆 验收回放（0805 五洲新春高接场景）

场景定义（锚定 docs/回测复盘/fix5施工与验收手册.md WP-B07）：
  单票 603667 五洲新春 / 底仓 800 / 15万 / 2026-08-04~2026-08-06
  验收: 卖出后若出现回补价 > 前卖价×(1+1%) 的买入 → 不得成交（被延迟），
        事件桥应有 buyback_armed / buyback_delayed（降档带成交则为 buyback_downgrade）；
        价格 ≤ 前卖价的正常回补不受影响，回补成交后有 buyback_filled。

产物：docs/回测复盘/回放包_WP-B/wpb07/
  backtrace.jsonl + events.jsonl（事件桥重定向到场景目录）

用法:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe replay_wp_b07.py
"""
import os
import sys

PROJ = os.path.dirname(os.path.abspath(__file__))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

OUT_DIR = os.path.join(PROJ, "docs", "回测复盘", "回放包_WP-B", "wpb07")
os.makedirs(OUT_DIR, exist_ok=True)

# ── 事件桥重定向到场景目录（不污染 runtime/bridge 实时桥） ──
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
START = "2026-08-04 08:00:00"
END = "2026-08-06 16:00:00"

# 审计日志按场景落盘
main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")

# WP-B07: 预置 603667 持仓——2026-08-04 趋势闸延迟建仓 + 08-05 日线取数失败 G4 拦截，
# 回放窗口内底仓无法落地（与当日实盘/O-01 故障一致），不做T信号链路无从触发。
# 验收需要"已持有 800 股"起点，故在 init 末尾注入台账（等效开盘前已建仓）。
_orig_init = main.init

def _seeded_init(context):
    _orig_init(context)
    _sym = "SHSE.603667"
    _seed = {"name": "五洲新春", "qty": 800, "available": 800, "t_qty": 800,
             "cost": 52.0, "type": "stock", "pre_close": 52.0}
    context.executed_orders[_sym] = dict(_seed)
    context.manual_position[_sym] = dict(_seed)
    context._base_settled.add("603667")
    context._base_ref_603667 = 800
    print("[replay] WP-B07 预置持仓: 603667 800股 cost=52.0")

main.init = _seeded_init

print(f"[replay] WP-B07 场景=603667 窗口={START}~{END} 资金={CASH}")
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
