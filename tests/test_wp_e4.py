# coding=utf-8
"""
tests/test_wp_e4.py — WP-E4 515180红利ETF 纳入做T体系 + M2池闸个股参数化 验证

背景（owner 2026-08-24 决策）：
  owner 在掘金仿真终端手动买入 SHSE.515180（易方达中证红利ETF）50,000 股 @1.451，
  定性防守仓，纳入策略做T体系观察做T效率。
  515180 是境内股票型 ETF（T+1、最小单位 100 股，与股票机制一致），
  但其 amp20≈0.8%、单手价值≈145 元，会被 M2 池闸硬编码门槛
  （amp20>=0.03 / amount20>=2e8 / lot_value>=2000）永久拦截——
  本 WP 将三个阈值改为 STOCK_PARAMS 个股可覆盖（缺省保持现值）。

验证范围：
  T1  STOCKS / STOCK_NAMES / MIRROR_HOLDINGS 含 515180 且值正确
  T2  _refresh_daily_ctx 对 515180 用个股 M2 阈值：
      构造 amp20=0.008 / amount20=5e7 / lot=145 的日线数据
      → 515180 _m2_pool_pass=True；同一数据对无覆盖的普通票(600481) → False
  T3  _held_codes / _slot_full 计数含 manual_position 中的 515180（占 1 槽）
  T4  STOCK_PARAMS["515180"] 关键键值就位

运行（逐文件运行，不要用 unittest discover）:
  "$DAIMON_USER_PYTHON" tests/test_wp_e4.py
"""
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
TMP = tempfile.mkdtemp(prefix="gmwpe4_test_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402
from config.params import PARAMS, STOCK_PARAMS  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_e4.jsonl")
main._audit_file = None

CODE = "515180"
GM_SYM = "SHSE.515180"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


# ══ T1: STOCKS / STOCK_NAMES / MIRROR_HOLDINGS 注册 ══
check("T1a STOCKS['515180'] == 'SHSE.515180'",
      main.STOCKS.get(CODE) == GM_SYM, f"val={main.STOCKS.get(CODE)}")
check("T1b STOCK_NAMES['515180'] == '红利ETF'",
      main.STOCK_NAMES.get(CODE) == "红利ETF", f"val={main.STOCK_NAMES.get(CODE)}")
_mh = main.MIRROR_HOLDINGS.get(CODE, {})
check("T1c MIRROR_HOLDINGS['515180'] == {qty:50000, cost:1.451}",
      _mh.get("qty") == 50000 and abs(_mh.get("cost", 0) - 1.451) < 1e-9,
      f"val={_mh}")
check("T1d REVERSE_MAP 含 SHSE.515180 → 515180",
      main.REVERSE_MAP.get(GM_SYM) == CODE, f"val={main.REVERSE_MAP.get(GM_SYM)}")


# ══ T2: _refresh_daily_ctx M2 阈值个股覆盖 ══
def make_daily_rows(close=1.45, half_range=None, volume=34482759, n=120):
    """构造日线：close 恒定、high-low=2*half_range → TR 恒定时 amp20=2*half_range/close。
    缺省 half_range=0.0058 → amp20 = 0.0116/1.45 = 0.008；
    volume*close ≈ 5e7 → amount20=5e7；单手价值 = 1.45*100 = 145。"""
    if half_range is None:
        half_range = 0.0058
    rows = []
    end = pd.Timestamp("2026-08-21 15:00:00")
    for i in range(n):
        ts = end - timedelta(days=n - 1 - i)
        rows.append({
            "eob": ts, "open": close, "high": close + half_range,
            "low": close - half_range, "close": close, "volume": volume,
        })
    return rows


def fresh_ctx():
    return SimpleNamespace(_daily_ctx_cache_map={}, latest_pre_close={})


NOW = datetime(2026, 8, 24, 9, 31, 0)

with mock.patch.object(main, "history_n", return_value=make_daily_rows()):
    ctx_etf = main._refresh_daily_ctx(fresh_ctx(), CODE, GM_SYM, NOW)
check("T2a 构造数据指标复核: amp20≈0.008 / amount20≈5e7 / lot=145",
      abs(ctx_etf["_m2_amp20"] - 0.008) < 1e-6
      and abs(ctx_etf["_m2_amount20"] - 5e7) < 1e4
      and abs(ctx_etf["_m2_lot_value"] - 145.0) < 1e-9,
      f"amp={ctx_etf['_m2_amp20']:.5f} amt={ctx_etf['_m2_amount20']:.0f} lot={ctx_etf['_m2_lot_value']}")
check("T2b 515180 个股阈值覆盖 → _m2_pool_pass=True",
      ctx_etf.get("_m2_pool_pass") is True,
      f"pass={ctx_etf.get('_m2_pool_pass')} status={ctx_etf.get('daily_status')}")

# 同一数据对无 M2 覆盖的普通票（600481 仅有 _note，无 m2_* 键）→ 走缺省门槛 → False
with mock.patch.object(main, "history_n", return_value=make_daily_rows()):
    ctx_normal = main._refresh_daily_ctx(fresh_ctx(), "600481", main.STOCKS["600481"], NOW)
check("T2c 无覆盖普通票(600481)同数据 → _m2_pool_pass=False（缺省门槛不变）",
      ctx_normal.get("_m2_pool_pass") is False
      and ctx_normal.get("daily_status") == "pool_gate_fail",
      f"pass={ctx_normal.get('_m2_pool_pass')} status={ctx_normal.get('daily_status')}")

# 515180 个股阈值的边界：amp20=0.004 < 0.005 仍应拦截（覆盖不是无条件放行）
with mock.patch.object(main, "history_n",
                       return_value=make_daily_rows(half_range=0.0029)):  # amp20=0.004
    ctx_low = main._refresh_daily_ctx(fresh_ctx(), CODE, GM_SYM, NOW)
check("T2d 515180 amp20=0.004 < 个股阈值0.005 → 仍拦截",
      ctx_low.get("_m2_pool_pass") is False,
      f"amp={ctx_low['_m2_amp20']:.5f} pass={ctx_low.get('_m2_pool_pass')}")


# ══ T3: _held_codes / _slot_full 槽位计数含 515180 ══
def pos_ctx(symbols):
    return SimpleNamespace(manual_position={
        s: {"qty": 50000 if s == GM_SYM else 500, "available": 0, "cost": 1.451}
        for s in symbols})


c3 = pos_ctx([GM_SYM, "SZSE.000988", "SHSE.600481", "SHSE.600176"])
_held3 = main._held_codes(c3)
check("T3a 515180 计入 _held_codes（占 1 槽）",
      CODE in _held3 and len(_held3) == 4, f"held={_held3}")
check("T3b 4票满仓（含515180）→ _slot_full=True",
      main._slot_full(c3) is True,
      f"count={main._held_position_count(c3)}/{PARAMS.get('max_concurrent_positions')}")
c3b = pos_ctx([GM_SYM, "SZSE.000988", "SHSE.600481"])
check("T3c 3票（含515180）→ _slot_full=False",
      main._slot_full(c3b) is False,
      f"count={main._held_position_count(c3b)}")
c3c = pos_ctx([GM_SYM])
c3c.manual_position[GM_SYM]["qty"] = 0   # 清仓后不占槽
check("T3d 515180 qty=0 时不占槽", main._held_codes(c3c) == [],
      f"held={main._held_codes(c3c)}")


# ══ T4: STOCK_PARAMS["515180"] 关键键值就位 ══
_sp = STOCK_PARAMS.get(CODE, {})
check("T4a M2 个股阈值键值就位",
      _sp.get("m2_amp20_min") == 0.005
      and _sp.get("m2_amount20_min") == 30000000
      and _sp.get("m2_lot_value_min") == 100,
      f"m2=({_sp.get('m2_amp20_min')},{_sp.get('m2_amount20_min')},{_sp.get('m2_lot_value_min')})")
check("T4b 做T低波动定制键值就位",
      _sp.get("min_profit_per_t") == 0.002
      and _sp.get("min_profit_space") == 0.005
      and _sp.get("vwap_buy_deviation") == -0.006
      and _sp.get("stock_qty_base_pct") == 0.1,
      f"t=({_sp.get('min_profit_per_t')},{_sp.get('min_profit_space')},"
      f"{_sp.get('vwap_buy_deviation')},{_sp.get('stock_qty_base_pct')})")
check("T4c 节流键值就位",
      _sp.get("max_sell_times_per_stock") == 2
      and _sp.get("max_buy_times_per_stock") == 2
      and _sp.get("cooldown_minutes") == 30,
      f"throttle=({_sp.get('max_sell_times_per_stock')},{_sp.get('max_buy_times_per_stock')},"
      f"{_sp.get('cooldown_minutes')})")
check("T4d _note 标记 WP-E4 决策",
      "WP-E4" in str(_sp.get("_note", "")), f"note={_sp.get('_note')}")


# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
