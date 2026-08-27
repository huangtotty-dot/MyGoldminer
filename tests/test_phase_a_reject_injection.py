# coding=utf-8
"""
tests/test_phase_a_reject_injection.py — Phase A 合成拒单注入验证

背景：Phase A（拒单链路/执行可靠性）自 2026-08-14 起完全放行，放行补遗要求以
**受控合成注入验证** 替代生产拒单证据（掘金仿真回测器对 T+1 委托静默丢弃，
回放路径不可行）。本测试离线向策略回报入口 main.on_order_status 注入合成
拒单/撤单事件，断言状态机自洽性：

  S1  SELL 拒单全链路（仲裁器真实下单路径 main._sell_arbiter → status=8 注入）
  S2  TARGET_SELL 拒单 → sell_state.json 状态机不被污染（live 落盘）
  S3  终态集合等价性 status∈{4,5,6,12} 入拒单分支 + status=1 阴性对照
  S4  BUY 拒单（做T 买入方向，main.py:1991-2012 下单时副作用 + status=8 注入）
  S5  底仓买入拒单 N5 重试上限（≤MAX_BASE_RETRY）
  T-A1  底仓 BASE 拒单不回滚 manual_position、N5 重试计数不受影响（WP-A1 排除项）
  T-A2  无快照兜底路径 → 逆减回滚 + fallback=1 审计（防御：进程内遗留/版本热切换）
  T-A3  成交后快照丢弃：buy filled → 同票新单拒单 → 只回滚新单（按委托键控）
  T-A4  计数下限防御：daily_buy_count=0 时拒单不为负

注入点说明：掘金 gm SDK 拒单以 on_order_status(order.status=8) 形式回到策略
（OrderStatus_Rejected=8 / Expired=12 / Canceled=5 / DoneForDay=4 /
PendingCancel=6，SDK 常量已在本机核实）。生产实证：2026-08-03 尾盘 600481
连续 7 笔 status=8 拒单均经此回调（docs/修复方案/F14_F15_修复方案_20260803.md）。
策略不订阅 on_execution_report，on_order_status 是唯一回报入口。

覆盖边界：
  - S1/S2 的下单段走真实函数 main._sell_arbiter（monkeypatch main.order_volume
    截获委托），拒单段走真实 main.on_order_status —— 两端均为生产代码。
  - S4 的买入下单时副作用为 on_bar 内联代码（main.py:1994-2004），无法脱离
    GM context 直接调用；夹具按相同语义重放该副作用（注释标注行号），验证
    对象为 on_order_status 拒单分支对买方向的处理对称性。
  - 飞书推送不可离线验证，以事件桥 reject/risk 事件落盘为推送前置证据
    （watcher.py:278-285 消费 reject 事件即推飞书，无节流去重门）。

运行：
  "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" tests/test_phase_a_reject_injection.py
"""
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

# ── 重定向事件桥/审计/sell_state 到临时目录，不污染真实数据 ──
import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="phase_a_reject_")
writer.BRIDGE_DIR = TMP

import main  # noqa: E402  (run() 在 __main__ 守卫下，import 安全)

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_phase_a.jsonl")
main.SELL_STATE_PATH = os.path.join(TMP, "sell_state_test.json")

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")
SELL_STATE = main.SELL_STATE_PATH

results = []
observations = []


def read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


def read_audit():
    if not os.path.exists(main._AUDIT_LOG_PATH):
        return []
    with open(main._AUDIT_LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_sell_state():
    if not os.path.exists(SELL_STATE):
        return {}
    with open(SELL_STATE, encoding="utf-8") as f:
        return json.load(f)


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def obs(text):
    observations.append(text)
    print(f"OBS   {text}")


NOW = datetime.now().replace(microsecond=0)
_orig_order_volume = main.order_volume


def mk_engine():
    return SimpleNamespace(
        sell_cooldown={},
        sell_count_per_stock={},
        buy_count_per_stock={},
        awaiting_buyback={},
        record_trade_action=lambda *a, **k: None,
        _get_params=lambda code: {"cooldown_minutes": 30},
    )


# ══════════════════════════════════════════════════════════════════
# S1  SELL 拒单全链路：仲裁器真实下单 → status=8 拒单注入
# ══════════════════════════════════════════════════════════════════
print("\n── S1  SELL 拒单全链路（_sell_arbiter → status=8）──")
clear_events()
placed = []
main.order_volume = lambda **kw: placed.append(kw)

ctx1 = SimpleNamespace(
    daily_sell_count={}, total_trade_count=0, rejected_order_count=0,
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 500, "available": 500,
                                     "t_qty": 500, "cost": 100.0}},
    executed_orders={"SZSE.000988": {"name": "华工科技", "qty": 500, "available": 500,
                                     "t_qty": 500, "cost": 100.0, "type": "stock",
                                     "pre_close": 100.0}},
    engine=mk_engine(),
    sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200),
    last_index_regime="range",
    _pending_sell_action={}, _inflight_sell={},
    latest_pre_close={"000988": 100.0},
    mode=None,
)
sig1 = SimpleNamespace(action="SELL_HIGH", score=80.0, reasons=["高抛"])
holding1 = {"qty": 500, "available": 500, "cost": 100.0}

r1 = main._sell_arbiter(ctx1, "000988", sig1, 500, 110.0, NOW, holding1, 65, {}, "SZSE.000988")
mp1 = ctx1.manual_position["SZSE.000988"]
check("S1a 下单成功且下单时副作用生效(虚减300/在途200/虚冷却/计数+1)",
      r1 is True and len(placed) == 1
      and mp1["qty"] == 300 and mp1["available"] == 300
      and ctx1._inflight_sell.get("SZSE.000988") == 200
      and "000988" in ctx1.engine.sell_cooldown
      and ctx1.daily_sell_count.get("000988") == 1
      and ctx1.total_trade_count == 1
      and ctx1._pending_sell_action.get("SZSE.000988", ("", 0))[0] == "SELL_HIGH",
      f"placed={len(placed)} mp_qty={mp1['qty']} inflight={ctx1._inflight_sell}")

ledger_before = dict(ctx1.executed_orders["SZSE.000988"])
clear_events()  # 只观察拒单产生的事件
main.on_order_status(ctx1, {"symbol": "SZSE.000988", "status": 8, "volume": 200,
                            "side": 2, "price": 0,
                            "ord_rej_reason_detail": "可用股份不足"})
mp1 = ctx1.manual_position["SZSE.000988"]
ev1 = read_events()

check("S1b 拒单→manual_position回滚500/500/500(无幻影持仓)",
      mp1["qty"] == 500 and mp1["available"] == 500 and mp1["t_qty"] == 500,
      f"qty={mp1['qty']} avail={mp1['available']} t_qty={mp1['t_qty']}")
check("S1c 拒单→executed_orders台账不变且无fill事件(无幻影成交)",
      ctx1.executed_orders["SZSE.000988"] == ledger_before
      and not any(e.get("event") == "fill" for e in ev1),
      f"ledger={ctx1.executed_orders['SZSE.000988'].get('qty')} fills={[e for e in ev1 if e.get('event')=='fill']}")
check("S1d 拒单→在途量释放归零", ctx1._inflight_sell.get("SZSE.000988") == 0,
      f"inflight={ctx1._inflight_sell}")
check("S1e 拒单留痕→reject事件+order_rejected风控事件且带柜台原因",
      any(e.get("event") == "reject" and "可用股份不足" in str(e.get("reason", "")) for e in ev1)
      and any(e.get("event") == "risk" and e.get("kind") == "order_rejected"
              and "可用股份不足" in str(e.get("detail", "")) for e in ev1),
      f"events={[(e.get('event'), e.get('kind')) for e in ev1]}")
check("S1f 拒单→sell_rollback审计落盘",
      any(a.get("event") == "sell_rollback" and a.get("qty") == 200 for a in read_audit()))
check("S1g 拒单不耗配额→daily_sell_count回补0/total_trade_count回补0(F14)",
      ctx1.daily_sell_count.get("000988") == 0 and ctx1.total_trade_count == 0,
      f"dsc={ctx1.daily_sell_count} ttc={ctx1.total_trade_count}")
check("S1h 拒单不清虚冷却→防下一bar无脑重发(engine.py:506门控)",
      "000988" in ctx1.engine.sell_cooldown and ctx1.engine.sell_cooldown["000988"] > NOW,
      f"cooldown={ctx1.engine.sell_cooldown}")
check("S1i 拒单计数+1", ctx1.rejected_order_count == 1,
      f"rej={ctx1.rejected_order_count}")
obs("S1: SELL拒单后 _pending_sell_action['SZSE.000988'] 残留 "
    f"{ctx1._pending_sell_action.get('SZSE.000988')} —— 拒单分支不 pop；"
    "后续同票新卖单下单时覆盖(main.py:227)，买成交分支不读该键(side==2 才读，:2069)，"
    "影响面=无（良性残留，建议修复包顺手清理）")

# ══════════════════════════════════════════════════════════════════
# S2  TARGET_SELL 拒单 → sell_state.json 状态机（live 落盘）
# ══════════════════════════════════════════════════════════════════
print("\n── S2  TARGET_SELL 拒单 → sell_state 状态机（MODE_LIVE 落盘）──")
clear_events()
if os.path.exists(SELL_STATE):
    os.remove(SELL_STATE)
placed2 = []
main.order_volume = lambda **kw: placed2.append(kw)

ctx2 = SimpleNamespace(
    daily_sell_count={}, total_trade_count=0, rejected_order_count=0,
    manual_position={"SZSE.000988": {"name": "华工科技", "qty": 500, "available": 500,
                                     "t_qty": 500, "cost": 100.0,
                                     "_target_l1_state": None,
                                     "_trail_state": "ARMED", "_trail_peak": 120.0}},
    executed_orders={"SZSE.000988": {"name": "华工科技", "qty": 500, "available": 500,
                                     "t_qty": 500, "cost": 100.0, "type": "stock",
                                     "pre_close": 100.0}},
    engine=mk_engine(),
    sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200),
    last_index_regime="range",
    _pending_sell_action={}, _inflight_sell={},
    latest_pre_close={"000988": 100.0},
    mode=main.MODE_LIVE,
    now=NOW,
)
sig2 = SimpleNamespace(action="TARGET_SELL", score=80.0, reasons=["目标止盈"])
r2 = main._sell_arbiter(ctx2, "000988", sig2, 500, 110.0, NOW,
                        {"qty": 500, "available": 500, "cost": 100.0}, 65, {}, "SZSE.000988")
st2 = read_sell_state().get("000988", {})
check("S2a TARGET下单即置pending并落盘(防竞态重复触发, WP-B14)",
      r2 is True and ctx2.manual_position["SZSE.000988"]["_target_l1_state"] == "pending"
      and st2.get("_target_l1_state") == "pending",
      f"mem={ctx2.manual_position['SZSE.000988']['_target_l1_state']} file={st2.get('_target_l1_state')}")
obs(f"S2: TARGET下单时落盘 pos_key={st2.get('pos_key')}（虚减后指纹300@100；"
    "进程若崩于下单→回调之间，次日INIT指纹不符作废pending——保守方向，可接受）")

main.on_order_status(ctx2, {"symbol": "SZSE.000988", "status": 8, "volume": 200,
                            "side": 2, "price": 0,
                            "ord_rej_reason_detail": "价格笼子"})
mp2 = ctx2.manual_position["SZSE.000988"]
st2b = read_sell_state().get("000988", {})
check("S2b TARGET拒单→状态清回None落盘(拒单不耗档, WP-B14)",
      mp2["_target_l1_state"] is None and st2b.get("_target_l1_state") is None
      and "000988" in read_sell_state(),
      f"mem={mp2['_target_l1_state']} file={st2b.get('_target_l1_state')}")
check("S2c 拒单回滚后pos_key指纹复原500@100.0000(不被虚减态污染)",
      st2b.get("pos_key") == main._pos_key(500, 100.0),
      f"pos_key={st2b.get('pos_key')}")
check("S2d TRAIL状态机不受TARGET拒单波及(ARMED/120.0不动)",
      mp2["_trail_state"] == "ARMED" and mp2["_trail_peak"] == 120.0
      and st2b.get("_trail_state") == "ARMED",
      f"trail={mp2['_trail_state']}/{mp2['_trail_peak']}")
check("S2e 拒单后该票状态段保留(qty>0不作废)且buyback镜像字段存在",
      "_buyback" in st2b and st2b.get("pos_key") is not None,
      f"keys={sorted(st2b.keys())}")

# ══════════════════════════════════════════════════════════════════
# S3  终态集合等价性 status∈{4,5,6,12} + status=1 阴性对照
# ══════════════════════════════════════════════════════════════════
print("\n── S3  终态集合 {4,5,6,12} 等价入拒单分支 / status=1 阴性对照 ──")
for st in (4, 5, 6, 12):
    clear_events()
    ctx3 = SimpleNamespace(
        daily_sell_count={"000988": 1}, total_trade_count=1, rejected_order_count=0,
        manual_position={"SZSE.000988": {"qty": 0, "available": 0, "t_qty": 0, "cost": 100.0}},
        executed_orders={}, engine=mk_engine(),
        _pending_sell_action={}, _inflight_sell={"SZSE.000988": 300},
        latest_pre_close={"000988": 100.0}, mode=None,
    )
    main.on_order_status(ctx3, {"symbol": "SZSE.000988", "status": st, "volume": 300,
                                "side": 2, "price": 0, "ord_rej_reason_detail": f"st={st}"})
    mp3 = ctx3.manual_position["SZSE.000988"]
    ev3 = read_events()
    check(f"S3-{st} status={st}→回滚300+计数+在途释放+reject事件",
          mp3["qty"] == 300 and mp3["available"] == 300
          and ctx3.rejected_order_count == 1
          and ctx3._inflight_sell.get("SZSE.000988") == 0
          and ctx3.daily_sell_count.get("000988") == 0 and ctx3.total_trade_count == 0
          and any(e.get("event") == "reject" for e in ev3),
          f"qty={mp3['qty']} rej={ctx3.rejected_order_count}")

# 阴性对照：status=1（已报，非终态）不得入拒单分支
clear_events()
ctx3n = SimpleNamespace(
    daily_sell_count={}, total_trade_count=0, rejected_order_count=0,
    manual_position={"SZSE.000988": {"qty": 0, "available": 0, "t_qty": 0, "cost": 100.0}},
    executed_orders={}, engine=mk_engine(),
    _pending_sell_action={}, _inflight_sell={"SZSE.000988": 300},
    latest_pre_close={"000988": 100.0}, mode=None,
)
main.on_order_status(ctx3n, {"symbol": "SZSE.000988", "status": 1, "volume": 300,
                             "side": 2, "price": 0})
mp3n = ctx3n.manual_position["SZSE.000988"]
check("S3-neg status=1(已报非终态)→不回滚/不计数/不放额/无事件",
      mp3n["qty"] == 0 and ctx3n.rejected_order_count == 0
      and ctx3n._inflight_sell.get("SZSE.000988") == 300
      and len(read_events()) == 0,
      f"qty={mp3n['qty']} rej={ctx3n.rejected_order_count} inflight={ctx3n._inflight_sell}")

# ══════════════════════════════════════════════════════════════════
# S4  BUY 拒单（做T 买入方向）
# ══════════════════════════════════════════════════════════════════
print("\n── S4  BUY 拒单（做T买入，下单时副作用重放 + status=8）──")
# 买入下单时副作用为 on_bar 内联代码（main.py:1994-2004），无法脱离 GM context
# 直接调用。以下夹具按相同语义重放：daily_buy_count+1 / total_trade_count+1 /
# manual_position 只加 qty,t_qty 不加 available（T+1, :1997-2003）/
# buy_count_per_stock 同步（:2004）。
clear_events()
ctx4 = SimpleNamespace(
    daily_buy_count={}, total_trade_count=5, rejected_order_count=0,
    daily_sell_count={},
    manual_position={"SHSE.600481": {"name": "双良节能", "qty": 1400, "available": 1400,
                                     "t_qty": 1400, "cost": 4.0}},
    executed_orders={"SHSE.600481": {"name": "双良节能", "qty": 1400, "available": 1400,
                                     "t_qty": 1400, "cost": 4.0, "type": "stock",
                                     "pre_close": 3.91}},
    engine=mk_engine(),
    _base_ordered=set(), _base_settled={"600481"},
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
# ── 重放 main.py:1991-2012 下单时副作用（qty=300 @ 4.10）──
# WP-A1: order_volume 之后先留 manual_position 快照（main.py:1996-2001），再改台账
_bq, _bp = 300, 4.10
ctx4._pending_buy_snapshot["SHSE.600481"] = (
    copy.deepcopy(ctx4.manual_position["SHSE.600481"]))      # :2000-2001
ctx4.daily_buy_count["600481"] = 0 + 1                      # :2002
ctx4.total_trade_count += 1                                 # :2004
_old = ctx4.manual_position["SHSE.600481"]                  # :2006-2011
_nq = int(_old["qty"]) + _bq
_nc = (_old["cost"] * _old["qty"] + _bp * _bq) / _nq
ctx4.manual_position["SHSE.600481"] = dict(_old, qty=_nq, t_qty=_nq, cost=_nc)
ctx4.engine.buy_count_per_stock["600481"] = 1               # :2012
mp4 = ctx4.manual_position["SHSE.600481"]
check("S4-pre 夹具保真：下单后qty=1700且available不加(T+1语义)",
      mp4["qty"] == 1700 and mp4["available"] == 1400,
      f"qty={mp4['qty']} avail={mp4['available']}")

ledger4_before = dict(ctx4.executed_orders["SHSE.600481"])
main.on_order_status(ctx4, {"symbol": "SHSE.600481", "status": 8, "volume": 300,
                            "side": 1, "price": 0,
                            "ord_rej_reason_detail": "可用资金不足"})
mp4 = ctx4.manual_position["SHSE.600481"]
ev4 = read_events()

# 断言方向 = Phase A 放行断言①（拒单不使 pos 变化）与卖方向 N25-2/F14 对称语义
# WP-A1(2026-08-27): 快照法对称回滚落地后转绿（main.py 拒单分支 elif side==1 回滚）
check("S4a BUY拒单→manual_position应回滚1400(无幻影持仓)",
      mp4["qty"] == 1400 and mp4["t_qty"] == 1400,
      f"qty={mp4['qty']} t_qty={mp4['t_qty']}（快照恢复，非逆运算）")
check("S4b BUY拒单→executed_orders台账不变(无幻影成交入台账)",
      ctx4.executed_orders["SHSE.600481"] == ledger4_before,
      f"ledger={ctx4.executed_orders['SHSE.600481'].get('qty')}")
check("S4c BUY拒单→无fill事件",
      not any(e.get("event") == "fill" for e in ev4),
      f"events={[e.get('event') for e in ev4]}")
check("S4d BUY拒单留痕→reject+order_rejected事件带柜台原因(飞书推送前置)",
      any(e.get("event") == "reject" and "资金不足" in str(e.get("reason", "")) for e in ev4)
      and any(e.get("event") == "risk" and e.get("kind") == "order_rejected" for e in ev4),
      f"events={[(e.get('event'), e.get('kind')) for e in ev4]}")
check("S4e BUY拒单→daily_buy_count应回补0(F14买向对称)",
      ctx4.daily_buy_count.get("600481") == 0,
      f"dbc={ctx4.daily_buy_count}")
check("S4f BUY拒单→total_trade_count应回补5(F14买向对称)",
      ctx4.total_trade_count == 5,
      f"ttc={ctx4.total_trade_count}")
check("S4g BUY拒单计数+1", ctx4.rejected_order_count == 1)
# 污染向下游传播实证：_get_holding 优先读 manual_position（main.py:477/546-548）
_h4 = main._get_holding(ctx4, "600481", "SHSE.600481")
check("S4h 幻影持仓向下游传播：_get_holding应读回1400(回测口径无对账自愈)",
      int(_h4.get("qty", 0)) == 1400,
      f"_get_holding.qty={_h4.get('qty')}")
obs("S4: 幻影持仓自愈路径=live 30分钟对账（main.py:495-541，终端无持仓→归零）；"
    "窗口期内地板检查(:128)/TARGET触发/tail归位(:1673 excess=pos_qty-base_ref)均以幻影 qty 计算；"
    "available 未被虚增（T+1语义保持）→ 仲裁器卖出量被 available 封顶(:180)，"
    "不会直接卖出幻影股，但地板保护/tail超额计算会被架空")

# ══════════════════════════════════════════════════════════════════
# S5  底仓买入拒单 N5 重试上限（≤MAX_BASE_RETRY=3）
# ══════════════════════════════════════════════════════════════════
print("\n── S5  底仓买入拒单重试上限（N5, MAX_BASE_RETRY=3）──")
clear_events()
ctx5 = SimpleNamespace(
    daily_buy_count={}, total_trade_count=0, rejected_order_count=0,
    daily_sell_count={},
    manual_position={}, executed_orders={},
    engine=mk_engine(),
    _base_ordered={"600481"}, _base_settled=set(),
    _pending_sell_action={}, _inflight_sell={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
max_retry = main.MAX_BASE_RETRY
for i in range(1, max_retry + 2):  # 1..4
    main.on_order_status(ctx5, {"symbol": "SHSE.600481", "status": 8, "volume": 1400,
                                "side": 1, "price": 0,
                                "ord_rej_reason_detail": f"资金不足#{i}"})
    if i <= max_retry:
        check(f"S5-{i} 第{i}次底仓拒单→移出_base_ordered允许重发,retry={i}",
              "600481" not in ctx5._base_ordered
              and ctx5._base_retry_count.get("600481") == i,
              f"retry={ctx5._base_retry_count}")
        ctx5._base_ordered.add("600481")  # 模拟 main.py:1488 重发底仓单
    else:
        check(f"S5-{i} 第{i}次拒单超上限→滞留_base_ordered当日停试(:1505跳过)",
              "600481" in ctx5._base_ordered
              and ctx5._base_retry_count.get("600481") == max_retry + 1,
              f"retry={ctx5._base_retry_count}")
check("S5b 底仓拒单全程→manual_position无600481条目(无幻影持仓)",
      "SHSE.600481" not in ctx5.manual_position)
check("S5c 底仓拒单留痕→4条reject事件+拒单计数=4",
      len([e for e in read_events() if e.get("event") == "reject"]) == 4
      and ctx5.rejected_order_count == 4,
      f"rej={ctx5.rejected_order_count}")
check("S5d 底仓拒单不置_base_ref_(仅成交时置, :2115)",
      getattr(ctx5, "_base_ref_600481", None) is None)
obs("S5: 超上限后 code 滞留 _base_ordered → 当日因 main.py:1505-1507 '已下单未成交跳过' "
    "不再重发，次日 D1 重置恢复——保守有界，非疯狂重试")

# ══════════════════════════════════════════════════════════════════
# T-A1  底仓 BASE 拒单：不回滚 manual_position、N5 重试计数不受影响（WP-A1 排除项）
# ══════════════════════════════════════════════════════════════════
print("\n── T-A1  底仓 BASE 拒单不回滚（WP-A1 排除项）──")
clear_events()
ctxA1 = SimpleNamespace(
    daily_buy_count={"600481": 0}, total_trade_count=0, rejected_order_count=0,
    daily_sell_count={},
    manual_position={"SHSE.600481": {"name": "双良节能", "qty": 1400, "available": 1400,
                                     "t_qty": 1400, "cost": 4.0}},
    executed_orders={}, engine=mk_engine(),
    _base_ordered={"600481"}, _base_settled=set(),
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
_buy_rollbacks_before = len([a for a in read_audit() if a.get("event") == "buy_rollback"])
main.on_order_status(ctxA1, {"symbol": "SHSE.600481", "status": 8, "volume": 1400,
                             "side": 1, "price": 0,
                             "ord_rej_reason_detail": "资金不足(BASE)"})
check("T-A1a BASE拒单→manual_position原样1400(走N5不碰台账)",
      ctxA1.manual_position["SHSE.600481"]["qty"] == 1400
      and ctxA1._base_retry_count.get("600481") == 1
      and "600481" not in ctxA1._base_ordered,
      f"qty={ctxA1.manual_position['SHSE.600481']['qty']} retry={ctxA1._base_retry_count}")
check("T-A1b BASE拒单→不新增buy_rollback审计且计数不回补",
      len([a for a in read_audit() if a.get("event") == "buy_rollback"]) == _buy_rollbacks_before
      and ctxA1.daily_buy_count.get("600481") == 0,
      f"buy_rollback_delta={len([a for a in read_audit() if a.get('event')=='buy_rollback']) - _buy_rollbacks_before}")

# ══════════════════════════════════════════════════════════════════
# T-A2  无快照兜底：逆减回滚 + fallback=1 审计（防御：进程内遗留/版本热切换）
# ══════════════════════════════════════════════════════════════════
print("\n── T-A2  无快照兜底（逆减 + fallback=1）──")
clear_events()
ctxA2 = SimpleNamespace(
    daily_buy_count={"600481": 1}, total_trade_count=6, rejected_order_count=0,
    daily_sell_count={},
    manual_position={"SHSE.600481": {"name": "双良节能", "qty": 1700, "available": 1400,
                                     "t_qty": 1700, "cost": 4.0}},
    executed_orders={}, engine=mk_engine(),
    _base_ordered=set(), _base_settled={"600481"},
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},   # 无快照 → 触发兜底
    latest_pre_close={"600481": 3.91},
    mode=None,
)
main.on_order_status(ctxA2, {"symbol": "SHSE.600481", "status": 8, "volume": 300,
                             "side": 1, "price": 0,
                             "ord_rej_reason_detail": "资金不足(无快照)"})
mpA2 = ctxA2.manual_position["SHSE.600481"]
check("T-A2a 无快照→逆减回滚1700→1400(available不虚增T+1保持)",
      mpA2["qty"] == 1400 and mpA2["t_qty"] == 1400 and mpA2["available"] == 1400,
      f"qty={mpA2['qty']} avail={mpA2['available']}")
check("T-A2b 无快照→审计buy_rollback且fallback=1、qty=300",
      any(a.get("event") == "buy_rollback" and a.get("fallback") == 1
          and a.get("qty") == 300 for a in read_audit()),
      f"audit={[a for a in read_audit() if a.get('event')=='buy_rollback']}")
check("T-A2c 无快照兜底→计数同样回补",
      ctxA2.daily_buy_count.get("600481") == 0 and ctxA2.total_trade_count == 5,
      f"dbc={ctxA2.daily_buy_count} ttc={ctxA2.total_trade_count}")

clear_events()
ctxA2b = SimpleNamespace(
    daily_buy_count={"600481": 1}, total_trade_count=6, rejected_order_count=0,
    daily_sell_count={},
    manual_position={"SHSE.600481": {"name": "双良节能", "qty": 300, "available": 0,
                                     "t_qty": 300, "cost": 4.0}},
    executed_orders={}, engine=mk_engine(),
    _base_ordered=set(), _base_settled={"600481"},
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
main.on_order_status(ctxA2b, {"symbol": "SHSE.600481", "status": 8, "volume": 300,
                              "side": 1, "price": 0,
                              "ord_rej_reason_detail": "资金不足(删条目)"})
check("T-A2d 逆减≤0→整条删除无残留",
      "SHSE.600481" not in ctxA2b.manual_position,
      f"mp={ctxA2b.manual_position}")

# ══════════════════════════════════════════════════════════════════
# T-A3  成交后快照丢弃：buy filled → 同票新单拒单 → 只回滚新单（按委托键控）
# ══════════════════════════════════════════════════════════════════
print("\n── T-A3  成交后快照丢弃（只回滚新单）──")
clear_events()
ctxA3 = SimpleNamespace(
    daily_buy_count={"600481": 1}, total_trade_count=6, rejected_order_count=0,
    daily_sell_count={},
    manual_position={"SHSE.600481": {"name": "双良节能", "qty": 1700, "available": 1400,
                                     "t_qty": 1700, "cost": 4.0}},
    executed_orders={"SHSE.600481": {"name": "双良节能", "qty": 1700, "available": 1400,
                                     "t_qty": 1700, "cost": 4.0, "type": "stock",
                                     "pre_close": 3.91}},
    engine=mk_engine(),
    _base_ordered=set(), _base_settled={"600481"},
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
# ① 买入300@4.10 快照(ord1) + 副作用
_snapA3 = ctxA3.manual_position["SHSE.600481"]
ctxA3._pending_buy_snapshot["ord1"] = copy.deepcopy(_snapA3)
ctxA3.manual_position["SHSE.600481"] = dict(_snapA3, qty=2000, t_qty=2000,
                                            cost=(4.0 * 1700 + 4.10 * 300) / 2000)
# ② ord1 全部成交 → 快照丢弃
main.on_order_status(ctxA3, {"symbol": "SHSE.600481", "status": 3, "volume": 300,
                             "side": 1, "price": 4.10, "cl_ord_id": "ord1"})
check("T-A3a 成交后ord1快照被丢弃",
      "ord1" not in ctxA3._pending_buy_snapshot,
      f"pbs={ctxA3._pending_buy_snapshot}")
# ③ 新单200(ord2) 快照 + 副作用
_snapA3b = ctxA3.manual_position["SHSE.600481"]
ctxA3._pending_buy_snapshot["ord2"] = copy.deepcopy(_snapA3b)
ctxA3.daily_buy_count["600481"] += 1
ctxA3.total_trade_count += 1
ctxA3.manual_position["SHSE.600481"] = dict(_snapA3b, qty=_snapA3b["qty"] + 200,
                                            t_qty=_snapA3b["qty"] + 200)
# ④ ord2 拒单 → 只回滚新单（回到成交后 2000 状态，不是 1400）
main.on_order_status(ctxA3, {"symbol": "SHSE.600481", "status": 8, "volume": 200,
                             "side": 1, "price": 0, "cl_ord_id": "ord2",
                             "ord_rej_reason_detail": "资金不足(ord2)"})
mpA3 = ctxA3.manual_position["SHSE.600481"]
check("T-A3b ord2拒单只回滚新单→qty回2000(1400+300成交, 非全滚回1400)",
      mpA3["qty"] == 2000 and mpA3["t_qty"] == 2000,
      f"qty={mpA3['qty']} t_qty={mpA3['t_qty']}")
check("T-A3c ord2拒单计数回补(仅本次下单)",
      ctxA3.daily_buy_count.get("600481") == 1 and ctxA3.total_trade_count == 6,
      f"dbc={ctxA3.daily_buy_count} ttc={ctxA3.total_trade_count}")

# ══════════════════════════════════════════════════════════════════
# T-A4  计数下限防御：daily_buy_count=0 时拒单不为负
# ══════════════════════════════════════════════════════════════════
print("\n── T-A4  计数下限防御（不为负）──")
clear_events()
ctxA4 = SimpleNamespace(
    daily_buy_count={}, total_trade_count=0, rejected_order_count=0,
    daily_sell_count={},
    manual_position={},
    executed_orders={}, engine=mk_engine(),
    _base_ordered=set(), _base_settled={"600481"},
    _pending_sell_action={}, _inflight_sell={},
    _pending_buy_snapshot={},
    latest_pre_close={"600481": 3.91},
    mode=None,
)
main.on_order_status(ctxA4, {"symbol": "SHSE.600481", "status": 8, "volume": 100,
                             "side": 1, "price": 0,
                             "ord_rej_reason_detail": "资金不足(下限)"})
check("T-A4a 计数下限→daily_buy_count/ttc/buy_count_per_stock均不为负",
      ctxA4.daily_buy_count.get("600481", 0) == 0 and ctxA4.total_trade_count == 0
      and ctxA4.engine.buy_count_per_stock.get("600481", 0) == 0,
      f"dbc={ctxA4.daily_buy_count} ttc={ctxA4.total_trade_count} bps={ctxA4.engine.buy_count_per_stock}")

main.order_volume = _orig_order_volume

# ══════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════
failed = [r for r in results if not r[1]]
print("\n" + "=" * 60)
print(f"测试目录: {TMP}")
print(f"通过 {len(results) - len(failed)}/{len(results)}")
if observations:
    print("观察项:")
    for o in observations:
        print(f"  OBS {o}")
if failed:
    print("失败项（= Phase A 合成注入发现的缺陷/不对称）:")
    for name, _, detail in failed:
        print(f"  FAIL {name} {detail}")
    sys.exit(1)
print("全部通过（45/45，含 WP-A1 新增 T-A1~T-A4）")
