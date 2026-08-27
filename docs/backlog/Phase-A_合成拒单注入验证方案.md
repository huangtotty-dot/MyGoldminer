# Phase A 合成拒单注入验证方案

> 状态：**已实施并运行**（2026-08-26）——**结论：不通过（卖向全绿 / 买向发现真实缺陷 D1+D2），
> Phase A 放行证据不成立，按补遗应回退放行并修链路（修复另立工作包）**
>
> **2026-08-27 更新：修复后通过（WP-A1 commit `bf42abf`）**——买向拒单对称回滚落地后
> 重新实跑 `test_phase_a_reject_injection.py` **45/45**（原 35 项 + 新增 T-A1~T-A4），
> S4a/S4e/S4f/S4h 由 FAIL 转 PASS，Phase A 恢复完全放行（O-17/O-18 关闭）。
> 关联放行条款：`docs/每日复盘清单_掘金模拟盘.md:161` —— Phase A 自 2026-08-14 完全放行，
> 补遗要求以**受控合成注入验证**替代生产拒单证据（掘金仿真回测器对 T+1 委托静默丢弃，
> 回放路径不可行）；**若合成验证失败，Phase A 放行回退并修链路**。

---

## 1. 订单链路事实路径（代码走查，全部带行号）

### 1.1 拒单如何回到策略（注入点的事实依据）

- 掘金 gm SDK（本机已核实常量）：`OrderStatus_Rejected=8`、`OrderStatus_Expired=12`、
  `OrderStatus_Canceled=5`、`OrderStatus_DoneForDay=4`、`OrderStatus_PendingCancel=6`、
  `OrderStatus_Filled=3`、`OrderStatus_PartiallyFilled=2`；执行回报侧另有
  `ExecType_Rejected=8`（`on_execution_report`）。
- 策略**只注册 `on_order_status`**（`main.py:2030`），未订阅 `on_execution_report` ——
  拒单以 `order["status"]=8`（或 4/5/6/12）形式进入该回调，**这就是唯一注入点**。
- 生产实证：2026-08-03 尾盘 600481 连续 7 笔 `status=8` 拒单均经此回调
  （`docs/修复方案/F14_F15_修复方案_20260803.md:12`）。

### 1.2 下单 → 下单时副作用（拒单前已发生的状态变更）

| 路径 | 下单点 | 下单时副作用（拒单需要回滚/对称处理的对象） |
|---|---|---|
| 做T 卖出（仲裁器） | `main.py:194` `order_volume(...)` | `_inflight_sell += qty`（:201）；**虚冷却** `sell_cooldown[code]=now+30m`（:210）；`daily_sell_count+1`（:211）；`total_trade_count+1`（:212）；**manual_position 虚减** qty/available/t_qty（:213-217）；TARGET 置 `_target_l1_state="pending"` 并落盘（:219-221）；`_pending_sell_action[sym]=(action,score)`（:227） |
| 尾盘归位 TAIL | `main.py:1691` | 同上（:1702-1724），`_pending_sell_action=("TAIL",0)`（:1722） |
| 做T 买入（on_bar 内联） | `main.py:1990` | `daily_buy_count+1`（:1994）；`total_trade_count+1`（:1996）；**manual_position 虚增** qty/t_qty（**不加 available，T+1**，:1997-2003）；`buy_count_per_stock` 同步（:2004） |
| 底仓建仓 BASE | `main.py:1484` | 仅 `_base_ordered.add(code)`（:1488）；**不碰 manual_position** |

注：三个卖出/买入路径都是"下单成功即改内存台账，成交/拒单回调再校正"的乐观更新模型。

### 1.3 回报处理（`on_order_status`, main.py:2030-2219）

- **价格兜底**：`filled_vwap → vwap → price → 昨收`（:2037-2039）。
- **在途释放**：`side==2 且 status∈{3,4,5,6,8,12}` → `_inflight_sell` 归还（:2042-2046）。
- **成交分支 `status==3`**（:2048-2168）：清 mute 键（:2050）→ 写 fill 事件（:2056-2060）
  → `record_trade_action` 正式冷却/回补记忆（:2066）→ 更新 executed_orders（买:2073-2088 /
  卖:2119-2133）→ 卖方向写 sell 审计并 pop `_pending_sell_action`（:2135）→ TARGET 置
  filled 落盘（:2142-2144）→ 回补 armed 事件（:2146-2159）→ O-10 刷新 sell_state 指纹
  （:2165-2168）。
- **拒单分支 `status∈{4,5,6,8,12}`**（:2170-2219，F2 修复 2026-07-28 补入 status=8）：
  - `rejected_order_count+1`（:2176）——双向都有；
  - N5 底仓拒单：`_base_retry_count` 递增，≤`MAX_BASE_RETRY=3`（:97）时移出
    `_base_ordered` 允许重发，超限滞留停试（:2178-2187）；
  - **N25-2 卖出拒单回滚 manual_position**（仅 `side==2`，:2189-2196）+ `sell_rollback`
    审计（:2197-2198）+ 清 mute 键（:2192）；
  - WP-B14 TARGET 拒单 → `_target_l1_state=None` 落盘，拒单不耗档（:2199-2203）；
  - F14 卖出拒单回补 `daily_sell_count`/`total_trade_count`（:2204-2208）；
  - **留痕（双向）**：`write_reject` + `write_risk("order_rejected")` 带柜台原因
    （:2209-2219）。
- **拒单分支明确不做的事**：
  - 不碰 `executed_orders`（台账只在 status==3 更新）→ 台账层无幻影成交；
  - 不清 `sell_cooldown`（:202-203 注释：拒单不补冷却属保守可接受）→ engine 信号门控
    `sell_cooldown_ok`（`signals/engine.py:506`）30 分钟内不再产出卖信号 → **不会下一 bar
    无脑重发同一卖单**；
  - **不回滚买方向的 manual_position 虚增、不回补 daily_buy_count/total_trade_count**
    （回滚与 F14 回补都在 `if side == 2` 内）——**本方案重点验证的不对称点**。

### 1.4 状态持久化与下游读取

- `sell_state.json`：`runtime/state/sell_state.json`（`main.py:798`），仅 live 落盘
  （:840-844）；字段 `_target_l1_state/_trail_state/_trail_peak/pos_key/_buyback`
  （:863-870）；清仓作废（:848-849）；次日 INIT 按 `pos_key` 校验恢复，存疑即重置
  （:874-896）。
- 持仓读取 `_get_holding`（:474-553）：优先级 manual_position → 30 分钟对账（live，
  :495-543，含终端空仓归零自愈 :528-541）→ executed_orders。**回测/回放口径跳过对账**
  （:494），manual_position 污染无自愈。
- 飞书推送：事件桥 `events_YYYYMMDD.jsonl` → `gm_bridge/watcher.py:278-285` 消费
  `reject` 事件即推「❌ 委托被拒」（无节流去重门）。离线验证以事件落盘为推送前置证据。

---

## 2. 注入用例设计

测试文件：`tests/test_phase_a_reject_injection.py`（无 pytest，脚本直跑）。
夹具模式沿用 `tests/test_fix_20260728.py` / `test_fix_20260731.py`：重定向事件桥
`writer.BRIDGE_DIR`、审计 `main._AUDIT_LOG_PATH`、`main.SELL_STATE_PATH` 到临时目录；
monkeypatch `main.order_volume` 截获委托后，**下单段走真实 `_sell_arbiter`、回报段走真实
`on_order_status`**，两端均为生产代码。

| 用例 | 注入内容 | 验证断言（Phase A 放行标准映射） |
|---|---|---|
| S1 | SELL_HIGH 仲裁器下单 200 股 → `status=8` 拒单 | ①manual_position 回滚无幻影持仓；②executed_orders 不变且无 fill 事件（无幻影成交）；③在途释放；④reject+order_rejected 留痕带柜台原因（飞书前置）；⑤sell_rollback 审计；⑥F14 配额回补；⑦拒单不清虚冷却（防下 bar 重发）；⑧拒单计数 |
| S2 | TARGET_SELL 下单 → `status=8` 拒单（MODE_LIVE 落盘） | sell_state.json：下单置 pending 落盘 → 拒单清回 None 落盘（不耗档）；pos_key 指纹复原不被虚减态污染；TRAIL 状态机（ARMED/peak）不受波及 |
| S3 | `status∈{4,5,6,12}` 逐一枚举 + `status=1` 阴性对照 | 终态集合等价入拒单分支（回滚/计数/放额/事件）；非终态 status=1 不触发任何副作用 |
| S4 | BUY 下单时副作用重放（:1994-2004 语义）→ `status=8` side=1 拒单 | 买向对称性：manual_position 应回滚（无幻影持仓）；daily_buy_count/total_trade_count 应回补；台账/fill 干净；留痕；`_get_holding` 下游传播检查 |
| S5 | 底仓买入连续 4 次 `status=8` 拒单 | N5 重试 ≤3 次后滞留停试（有界、非疯狂重试）；全程无幻影持仓；`_base_ref_` 不被误置；逐次留痕 |

覆盖边界（哪些是单测实证、哪些是走查结论）：

- **实证**：S1/S2/S3/S5 全链路（真实下单函数+真实回报回调）；S4 的回报段（真实
  `on_order_status`）。
- **夹具重放**：S4 买入下单时副作用为 on_bar 内联代码（`main.py:1985-2022`），无法脱离
  GM context 直接调用，按 :1994-2004 相同语义在夹具中重放并断言保真（S4-pre）。
- **走查结论**（非单测）：飞书推送本身（watcher.py:278-285 无门控直推，离线不可达）；
  sell_cooldown 门控生效点（signals/engine.py:506）；live 30 分钟对账自愈
  （main.py:495-541，已有 `test_fix_20260731.py` T10 覆盖归零路径）。

---

## 3. 运行结果（逐用例 预期 vs 实际）

> 运行命令：`"$DAIMON_USER_PYTHON" tests/test_phase_a_reject_injection.py`
> 实跑结果（2026-08-26，修复前）：**通过 31/35，FAIL 4 项（全部集中在 S4 买入方向）**，退出码 1。
> 实跑结果（2026-08-27，WP-A1 修复后）：**通过 45/45**（原 35 项全转绿 + 新增 T-A1~T-A4），退出码 0。

| 用例 | 断言 | 预期 | 实际 | 结论 |
|---|---|---|---|---|
| S1a | SELL 下单时副作用（虚减/在途/虚冷却/计数） | 全部生效 | 全部生效 | PASS |
| S1b | 拒单→manual_position 回滚 500/500/500 | 回滚 | 回滚（main.py:2190-2196） | PASS |
| S1c | 无幻影成交：executed_orders 不变 + 无 fill 事件 | 不变/无 | 不变/无（台账仅 status==3 更新） | PASS |
| S1d | 在途量释放归零 | 归零 | 归零（:2043-2046） | PASS |
| S1e | 留痕：reject + order_rejected 事件带柜台原因 | 双向留痕 | 落桥，watcher.py:278-285 即推飞书 | PASS |
| S1f | sell_rollback 审计 | 落盘 | 落盘 | PASS |
| S1g | F14 配额回补（daily_sell_count/total_trade_count） | 回补 0/0 | 回补 0/0（:2204-2208） | PASS |
| S1h | 拒单不清虚冷却（防下 bar 无脑重发） | 冷却保留 | 保留 30min，engine.py:506 门控 | PASS |
| S1i | rejected_order_count+1 | +1 | +1 | PASS |
| S2a | TARGET 下单即置 pending 落盘 | pending 落盘 | pending 落盘（:219-221） | PASS |
| S2b | TARGET 拒单→状态清回 None 落盘（不耗档） | None 落盘 | None 落盘（:2199-2203） | PASS |
| S2c | pos_key 指纹复原 500@100.0000 | 复原 | 复原（回滚后 persist） | PASS |
| S2d | TRAIL 状态机不受波及 | ARMED/120 不动 | 不动 | PASS |
| S2e | 状态段保留 + buyback 镜像字段 | 保留 | 保留 | PASS |
| S3-4/5/6/12 | 终态集合等价入拒单分支 | 回滚+计数+放额+事件 | 全部等价 | PASS ×4 |
| S3-neg | status=1（非终态）阴性对照 | 无任何副作用 | 无 | PASS |
| S4-pre | 夹具保真（买向 T+1：只加 qty 不加 available） | 1700/1400 | 1700/1400 | PASS |
| **S4a** | **BUY 拒单→manual_position 应回滚（无幻影持仓）** | **回滚 1400** | **不回滚，残留 1700**（拒单分支仅 side==2，:2190） | **FAIL（缺陷 D1）** |
| S4b | BUY 拒单→executed_orders 台账不变 | 不变 1400 | 不变 | PASS |
| S4c | BUY 拒单→无 fill 事件 | 无 | 无 | PASS |
| S4d | BUY 拒单留痕（reject + order_rejected） | 留痕 | 留痕（:2209-2219 双向） | PASS |
| **S4e** | **BUY 拒单→daily_buy_count 应回补（F14 买向对称）** | **回补 0** | **不回补，残留 1** | **FAIL（缺陷 D2）** |
| **S4f** | **BUY 拒单→total_trade_count 应回补** | **回补 5** | **不回补，残留 6** | **FAIL（缺陷 D2）** |
| S4g | 拒单计数+1 | +1 | +1 | PASS |
| **S4h** | **幻影持仓向下游传播检查（_get_holding）** | **读回 1400** | **读回 1700（回测口径无对账自愈）** | **FAIL（D1 传播实证）** |
| S5-1/2/3 | 底仓拒单 ≤3 次→移出 _base_ordered 允许重发 | 移出+retry 递增 | 符合（:2178-2187） | PASS ×3 |
| S5-4 | 第 4 次超上限→滞留停试 | 滞留 | 滞留（当日 :1505-1507 跳过重发） | PASS |
| S5b | 底仓拒单全程无幻影持仓 | 无 | 无（底仓下单不碰 manual_position） | PASS |
| S5c | 4 次拒单逐次留痕 + 计数=4 | 4 条 reject | 4 条 | PASS |
| S5d | 拒单不置 _base_ref_ | 不置 | 不置（仅成交时置 :2115） | PASS |

观察项（OBS，非 PASS/FAIL 断言）：

1. SELL 拒单后 `_pending_sell_action` 残留不 pop（:2135 仅成交分支 pop）；后续同票新卖单
   下单时覆盖（:227），买成交分支不读该键（:2069 仅 side==2）——良性残留，修复包顺手清理。
2. TARGET 下单时落盘 pos_key 为虚减后指纹（300@100）；进程若崩于下单→回调之间，次日
   INIT 指纹不符作废 pending——保守方向，可接受。
3. D1 幻影持仓自愈路径 = live 30 分钟对账（:495-541）；窗口期内地板检查（:128）、
   tail 归位 excess 计算（:1673-1678）、TARGET 触发均以幻影 qty 计算。available 未被
   虚增（T+1 语义保持）→ 仲裁器卖出量被 available 封顶（:180），不会直接卖出幻影股，
   但地板保护/tail 超额归位会被架空（tail 可卖出本应保留的真实底仓）。
4. 底仓拒单超上限后 code 滞留 _base_ordered → 当日 :1505-1507 不再重发，次日 D1 重置
   恢复——保守有界，非疯狂重试。

---

## 4. 总结论

**（修复前，2026-08-26）不通过（部分通过）——Phase A 放行证据不成立，按放行补遗应回退并修链路。**

- **卖出方向（含 TARGET/TRAIL 状态机、尾盘 tail、在途守卫、配额回补、留痕、防重发）：
  全部通过**。状态机在拒单冲击下自洽，生产 12 日零拒单期间该链路行为可外推为健康。
- **买入方向（做T 买入，非底仓）：发现真实缺陷 D1+D2（见下节）**，违反放行断言
  ①"拒单不使 pos 变化"。底仓买入方向（N5 重试）通过。
- 断言③留痕（reject/risk 事件→飞书推送前置）双向均满足；断言④防疯狂重试双向均满足
  （卖向靠虚冷却保留，买向靠 daily_buy_count 消耗上限有界，底仓靠 MAX_BASE_RETRY=3）。

**（修复后，2026-08-27）通过——Phase A 放行证据成立，恢复完全放行（WP-A1 `bf42abf`）。**

- WP-A1 落地买向拒单对称回滚（快照法）后，原 4 项 FAIL（S4a/S4e/S4f/S4h）全部转 PASS，
  并新增 T-A1~T-A4 覆盖底仓 BASE 排除、无快照兜底、按委托键控防串单、计数下限防御，
  合成注入测试 45/45。
- 全回归无新增红；O-17（D1）/O-18（D2）随本包关闭。

## 5. 缺陷清单与修复建议

### D1（高）BUY 拒单不回滚 manual_position → 幻影持仓

- **事实**：做T 买入下单时副作用虚增 `manual_position` qty/t_qty（`main.py:1997-2003`，
  乐观更新），但拒单分支回滚仅覆盖 `side==2`（`main.py:2190`）。BUY 被拒后
  manual_position 残留幻影 qty（S4a/S4h 实证：1400 → 拒单后仍 1700）。
- **影响面**：
  - live：≤30 分钟对账自愈（:495-541）；窗口期内地板保护架空（:128 以幻影 pos_qty
    判定）、tail 归位 excess 虚高（:1673，可卖出真实底仓股）、TARGET/TRAIL 触发位偏移；
  - 回测/回放口径：跳过对账（:494），污染永久存在；
  - 幻影持仓期间若发生真实成交，O-10 指纹刷新（:2165-2168）会把错误 pos_key 写进
    sell_state.json → 次日 INIT 作废活跃状态（保守方向，但等于状态机被间接受损）。
- **修复建议**（另立工作包）：拒单分支增加 `side==1 且 code 不在 _base_ordered` 的
  对称回滚——按下单时副作用逆运算（qty/t_qty 减回 volume；cost 按加权逆推或直接
  快照下单前条目恢复，推荐快照法避免浮点漂移），并写 `buy_rollback` 审计。

### D2（中）BUY 拒单不回补 daily_buy_count / total_trade_count（F14 买向缺失）

- **事实**：F14 回补逻辑嵌在 `if side == 2` 内（:2204-2208）。BUY 被拒后
  `daily_buy_count`/`total_trade_count`/`buy_count_per_stock` 残留（S4e/S4f 实证）。
- **影响面**：拒单误耗当日买入配额（`max_buy_times_per_stock=3`，:1808/:1843），
  极端日多票拒单可挤占后续合法买入信号；方向上是保守的（不会放大交易），但语义与
  F14 卖向修复不对称。
- **修复建议**：D1 同一工作包内对称回补三键。

### 观察项级（不阻塞）

- `_pending_sell_action` 拒单残留不 pop（良性，见 OBS-1），D1 工作包顺手清理。
- S4 单测覆盖边界：买入下单时副作用为 on_bar 内联代码，单测以同语义夹具重放
  （S4-pre 保真断言守门）；修复后建议把买入下单段抽函数以便直测（或接受现状并在
  测试头注释维持边界说明）。

### 回归要求

D1/D2 修复后重跑本测试，S4a/S4e/S4f/S4h 应转 PASS（35/35），且
`test_fix_20260728.py`（F2 拒单回归）/ `test_fix_20260731.py`（F9 在途守卫）/
`test_wp_b13b14.py`（T6 TARGET 拒单回滚）保持全绿。
