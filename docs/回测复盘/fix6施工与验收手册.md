# fix6 施工与验收手册（工作分解 · 测试方案 · 验收标准）

> **文档定位**：把《审核报告_v1.0.1_fix5.md》中的全部待修项拆解为可执行工作包（WP），每个工作包给出改动内容、测试方案、验收标准三要素。
> **使用方式**：Pro 按批次顺序施工，每个 WP 独立 commit；批次一完成后经 K3 复审通过才启动模拟盘；批次二在模拟盘运行期间并行施工。
> **上游依据**：审核报告 v1.0.1_fix5 第三~七节；fix5 施工与验收手册（本手册未覆盖的 WP 条款继续有效）。

---

## 〇、铁律与全局约定（施工前必读）

1. **不改买卖阈值铁律仍然有效**。唯一例外：WP-F6（TRAIL k×ATR 双向带界）属于 fix5 手册 B1 原本要求的**结构补交付**，其临时值为 ⟨占位⟩ 性质，须带 `TODO(PhaseD)` 标记并在 commit message 中列明取值。
2. **每 WP 独立 commit**，commit message 格式：`fix6-F1: N18 daily_ctx缓存键加code（WP-F1）`，禁止打包提交。
3. **commit message 与产物一致性自查**（fix4/fix5 连续两轮失真教训）：message 中的任何数字（PANIC 次数、窗口截止日、笔数）必须与产物文件可核对，不一致即视为交付缺陷。
4. **对照基线**（验收比对的基准数据，均已存档）：
   - fix5 终态代码与回放包：`E:\06_T\audit\gm_fusion\v1.0.1_fix5\`
   - fix5 backtrace：`gmcache\backtrace.jsonl`（41,808 行）
   - fix4 基线：`E:\06_T\audit\gm_fusion\` 下前轮存档
5. **N18 污染的推论**：fix5 及之前所有依赖 `_stock_trend_state` / `_m2_*` / `daily_atr` 的行为证据均不可靠。**WP-F1 修复后的全窗口回放（WP-F4）是唯一权威验证场**，此前结论不得引用。
6. 所有"应触发/不应触发"类验收以 backtrace.jsonl + 成交 CSV 为唯一证据源，print 日志不作为验收证据。

---

## 一、工作包总览（WBS）

### 批次一：模拟盘上线前必须完成（P0，预计 1 天）

| WP | 内容 | 依赖 | 工作量 | 风险 |
|---|---|---|---|---|
| F1 | N18 daily_ctx 缓存跨票污染修复 | 无 | <1 小时 | 低（但行为影响大） |
| F2 | N22 持仓对账按运行模式条件化 | 无 | 半天 | 中（涉及实盘安全网） |
| F3 | N12 run_id 移入 init() | 无 | <30 分钟 | 极低 |
| F4 | 全窗口重跑回放 + Phase A 出口复审 | F1（必须同批验证） | 1 次回放 + K3 复审 | — |

### 批次二：模拟盘运行第 1~2 周补齐 Phase B（P1/P2）

| WP | 内容 | 依赖 | 工作量 | 风险 |
|---|---|---|---|---|
| F5 | N19 开盘缓冲覆盖 TARGET_SELL | F4 | 1 行 + 回放验证 | 极低 |
| F6 | N20 仲裁器优先级倒挂修复 + N23 TRAIL k×ATR 带界结构 | F4 | 半天 | 中 |
| F7 | N21 daily_status 覆盖 bug | 无 | 1 行 | 极低 |
| F8 | 600176 尾仓处置显性决策记录 | 无 | 文档级 | 无 |
| F9 | 三窗口回放包（fix2/fix3/fix4 场景） | F6 | 3 次回放 | — |
| F10 | Phase B 出口验收（K3） | F5~F9 | K3 复审 | — |

**批次一出口 = Phase A 出口；批次二出口 = Phase B 出口。** Phase C（组合层）不在本手册范围，待 Phase B 出口后另行拆解。

---

## 二、批次一（P0）

### WP-F1：N18 daily_ctx 缓存跨票污染修复

- **来源**：审核报告 fix5 第三节（P0 新发现）
- **缺陷定位**：`main.py:213-214` 缓存键只有日期没有代码；`main.py:285-286` 每日只缓存一份 ctx。后果：每日第一只处理的票（000988）的日线上下文被其余三票全天串用，M2 门槛 / R1 趋势闸 / TREND_EXIT / PANIC 触发线 / 引擎日线特征全部失真。
- **改动内容**（main.py:212-214、285-286）：
  ```python
  # 现状（缺陷）：
  if getattr(context, "_daily_ctx_date", None) == today_str and hasattr(context, "daily_ctx_cache"):
      return context.daily_ctx_cache
  ...
  context.daily_ctx_cache = ctx
  context._daily_ctx_date = today_str

  # 改为：缓存键 = 日期 + 代码
  if not hasattr(context, "_daily_ctx_cache_map"):
      context._daily_ctx_cache_map = {}
  _cache_key = f"{today_str}|{code}"
  if _cache_key in context._daily_ctx_cache_map:
      return context._daily_ctx_cache_map[_cache_key]
  ...
  context._daily_ctx_cache_map[_cache_key] = ctx
  ```
  注意：`context.daily_ctx_cache` / `context._daily_ctx_date` 的旧引用点需全部清扫（grep 确认无残留读取方）。
- **测试方案**：
  1. 静态检查：`grep -n "daily_ctx_cache" main.py` 确认旧键无残留；
  2. 行为验证（并入 WP-F4 全窗口回放）：检查 600481 与 603667 的关键时点（见验收标准）；
  3. 隔离验证：抽查任意两个交易日的 backtrace no_signal 记录，确认同日的不同票 `daily_*` 系字段值不再相同（污染期特征：四票字段逐值相等）。
- **验收标准（可证伪）**：
  - [ ] 回放中 **600481 全程 0 底仓单、0 信号交易**，事件桥有 `pool_gate` 记录（单手 626 元 < 2000 元）；
  - [ ] 回放中 **603667 在 06-05 无买入**（fix5 实证买于 14:26 @73.84），且当日有 `stock_trend_gate` 风控事件；
  - [ ] 四票 `_stock_trend_state` 每日各自闭合（无 null），且同日不同票的状态允许不同（抽查 ≥5 个交易日存在状态分化，证明隔离生效）；
  - [ ] 000988/600176 的成交序列与 fix5 基线的差异**逐笔清单化、每笔可归因**（归因类别：本票趋势闸拦截 / 本票 ATR 变化致 PANIC 线位移 / 本票 M2 指标）；
  - [ ] 旧缓存键零残留。
- **常见失败模式**：只改写入端忘记读取端；`daily_ctx_cache` 被其他函数（如引擎）按旧键引用导致脏读。
- **回退**：单 commit revert（但 revert 即恢复污染，不允许带污染上线模拟盘）。

### WP-F2：N22 持仓对账按运行模式条件化

- **来源**：审核报告 fix5 第五节 N22
- **缺陷定位**：`main.py:167` `_skip_reconcile = True` 硬编码，任何模式下 30 分钟持仓对账均不执行。回测中合理；模拟盘=准实盘场景下 manual_position 成唯一事实源，部分成交 / 回调丢失将静默漂移。
- **改动内容**（main.py:165-168）：
  ```python
  # 改为按运行模式条件化（MODE_LIVE 已在 a33c6bf 引入）
  _skip_reconcile = (context.mode != MODE_LIVE)
  ```
  若 `context.mode` 在掘金终端不可可靠获取，则改为 params.py 增加 `RUN_MODE = "backtest" | "sim"` 开关（启动模拟盘时手工置 "sim"，写入每日复盘清单检查项）。
  **成本保持要求**：对账发现 qty 不一致、以 gm 持仓覆盖 manual_position 时（main.py:184-185），必须保留我方跟踪的 `cost` 字段（gm 的 vwap 含前复权调整，不可用作成本），并 `_audit_write({"event": "reconcile_fix", ...})` 记录新旧 qty。
- **测试方案**：
  1. 回测回归：全窗口回放（与 WP-F4 同批），成交序列与 fix5 基线差异应全部归因于 F1，**不允许出现归因于 reconcile 的差异**（回测模式下 reconcile 应一次都不触发）；
  2. 模拟盘首日验收：每日复盘时比对 manual_position 与终端持仓，drift=0 或有 `reconcile_fix` 事件可解释；
  3. 成本保持验证：构造/抽查一次 reconcile_fix 事件，确认覆盖后 cost 字段未变为 gm vwap。
- **验收标准**：
  - [ ] 回测模式 reconcile 触发次数 = 0（日志可证）；
  - [ ] LIVE/sim 模式 reconcile 按 1800s 周期触发，qty 不一致有 `reconcile_fix` 审计事件且保留成本；
  - [ ] 运行模式标识出现在 backtrace run 首行或 print 首屏（可审计）。
- **回退**：单 commit revert；若模拟盘首日发现 reconcile 引入新漂移，立即 revert 并将对账改为"每日收盘人工比对"过渡。

### WP-F3：N12 run_id 移入 init()

- **来源**：审核报告 fix5 第五节 N12（连续第四轮未闭环）
- **缺陷定位**：`_AUDIT_RUN_ID` 模块级初始化为 `""`（main.py:294），仅在 `__main__` 块赋值（main.py:975）；掘金终端回测不执行 `__main__`，故 41,808 行 backtrace 的 run_id 全空。
- **改动内容**：将赋值移入策略 `init()`：
  ```python
  def init(context):
      global _AUDIT_RUN_ID
      _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
      ...
  ```
  `__main__` 块中的重复赋值删除或保留均可（保留则本地脚本运行时覆盖，不冲突）。
- **测试方案**：WP-F4 回放后检查 backtrace。
- **验收标准**：
  - [ ] backtrace run_id 100% 非空；
  - [ ] 同一 run 内 run_id 唯一一致；
  - [ ] 连续两次回放 run_id 不同。
- **回退**：单 commit revert。

### WP-F4：全窗口重跑回放 + Phase A 出口复审（K3）

- **定位**：批次一的总验收，同时是 Phase A 的正式出口复审。
- **执行**：F1/F2/F3 合入后，全窗口（2026-04-27 ~ 2026-07-24，4 标的、15 万、含费）重跑一次回放；提交材料：回放 PDF + 交易/净值/持仓 CSV + backtrace.jsonl + 通道分布表 + **与 fix5 基线的逐笔差异归因清单**。
- **Phase A 出口 checklist（K3 逐条打钩）**：
  - [ ] A1 fail-closed：代码在位（fix5 已验 ✅，本轮复核无回归）；
  - [ ] A2 M2 门槛：600481 仅观察、000988 正常建仓、数据不足不放行（WP-F1 验收 1/2 项）；
  - [ ] A3 R1 趋势闸：603667 06-05 逆势买入 0 笔、四态每日每票闭合、`stock_trend_gate`/`base_deferred` 事件在案；
  - [ ] A4 仲裁器：终态结构在位（fix5 已验 ⚠️），本轮差异全部可归因（不再追溯 fix4 的 100% 等价——施工顺序违规已造成基线断裂，fix6 起以"可归因"为标准）；
  - [ ] A5 预算制：任意时点单票市值 ≤ 预算×80%（持仓 CSV 逐日验证，本轮必须补齐执行）；
  - [ ] N12 run_id 闭环（WP-F3 验收三项）；
  - [ ] 拒单 = 0、费用 fee 全部 >0。
- **出口判定**：全部打钩 → **Phase A 关闭**，批准启动模拟盘；任一不通过 → 打回修复，模拟盘顺延。

---

## 三、批次二（P1/P2，模拟盘运行期间并行）

### WP-F5：N19 开盘缓冲覆盖 TARGET_SELL

- **来源**：审核报告 fix5 WP-B5 节（实证：603667 05-08 09:32 TARGET_SELL 穿透缓冲）
- **改动内容**（main.py:661）：拦截条件由 `sig.action == "SELL_HIGH"` 扩展为 `sig.action in ("SELL_HIGH", "TARGET_SELL")`。PANIC/TRAIL/TREND_EXIT 保留即时性（fix5 手册 B5 条款）。
- **测试方案**：fix4 场景窗口回放，统计 09:31-09:35 非保护类卖出笔数（基线：fix4 9 笔开盘即卖、fix5 1 笔 TARGET 穿透）。
- **验收标准**：
  - [ ] 09:35 前 SELL_HIGH / TARGET_SELL **0 笔**（成交 CSV 可证）；
  - [ ] 保护类通道（PANIC/TRAIL/TREND_EXIT）开盘即时性回归不破；
  - [ ] `morning_sell_blocked` 事件照常记录。
- **回退**：单 commit revert。

### WP-F6：N20 仲裁器优先级倒挂修复 + N23 TRAIL k×ATR 带界结构

- **来源**：审核报告 fix5 WP-A4 节 N20、WP-B1 节 N23
- **改动内容**：
  1. **N20**（main.py:674、680-681）：TREND_EXIT 块增加优先级守卫——`sig` 已为 TRAIL_SELL/PANIC_SELL 时不得覆盖；TARGET_SELL 块的守卫名单补入 TRAIL_SELL（现状只排除 TREND_EXIT/PANIC，仍可覆盖 P2 TRAIL）。修复后声明优先级 `P1 PANIC > P2 TRAIL > P3 TREND_EXIT > P4 TARGET > P5 SELL_HIGH` 与代码逐条一致。
  2. **N23**（main.py:638、644-646）：TRAIL 触发线由固定 5% 改为带界结构：
     ```python
     _back = max(MIN_BACK, min(k * daily_atr, MAX_BACK))
     # 临时值 ⟨占位⟩：MIN_BACK=0.03, k=1.5, MAX_BACK=0.08  # TODO(PhaseD)
     if _drawdown > _back and not _panic_on_cooldown:
     ```
     激活线 ACT_LINE=0.08 维持临时值不变。**此为本手册唯一授权的"阈值系"改动**（属 fix5 B1 结构补交付），临时值须在 commit message 列明。
- **测试方案**：
  1. 状态机单测（本地脚本模拟价格序列，不入库或入 audit 目录）：INACTIVE→ARMED→触发→COOLED→复位全路径，含"部分卖出后 peak 保持"分支、ATR 带界上下限截断分支；
  2. **fix2 场景主验收回放**（000988 单票、底仓 800、资金 20 万、fix2 同窗口）：复现 +13.9% 峰值场景；
  3. 同 bar 双通道候选统计：回放中 TRAIL 与 TREND_EXIT 同 bar 双满足的次数，确认执行 P2 而非 P3。
- **验收标准（可证伪）**：
  - [ ] fix2 场景 TRAIL 退出均价 ≥ 成本 × **1.07**（fix2 实际结局 -33.3% 的对照）；
  - [ ] 状态机单测全路径通过（含带界截断）；
  - [ ] backtrace 的 TRAIL_SELL 记录带 profit_pct / trail_peak / 触发线三字段；
  - [ ] 同 bar 双通道候选场景仲裁行为符合优先级表，无重复卖/超卖；
  - [ ] 5 月震荡段误触发次数入报告（供 Phase D 定值参考，不定值）。
- **回退**：`ENABLE_TRAIL` 开关 + revert。

### WP-F7：N21 daily_status 覆盖 bug

- **来源**：审核报告 fix5 WP-A2 节
- **改动内容**（main.py:256）：`ctx["daily_status"] = "ok"` 改为仅在未设置时兜底（如 `ctx.setdefault("daily_status", "ok")`），使 `pool_gate_fail` 可观测。
- **验收标准**：pool_gate 被拦票的 daily_status 在 backtrace/日志中可见为 `pool_gate_fail`；正常票仍为 `ok`。
- **回退**：单 commit revert。

### WP-F8：600176 尾仓处置显性决策记录（文档 WP，无代码）

- **来源**：审核报告 fix5 WP-B2 节——实现只卖"超 base_ref 部分"（main.py:672），尾仓（=底仓本体）设计上永不触发 TREND_EXIT，与 fix5 手册测试方案"尾仓落袋"期望不一致。
- **决策选项**：
  - (a) **维持底仓 0% 处置**：与实盘持仓框架"底仓 intact 为默认态"一致；TREND_EXIT 只负责利润仓落袋，底仓的命运交给 PANIC（深亏止血）与人工；
  - (b) TREND_BREAKDOWN 时底仓减半；(c) 底仓全部落袋。
- **K3 建议**：选 (a)。理由：模拟盘的目标是辅助实盘被套票回本，底仓语义必须与实盘框架同构，否则模拟盘行为不可映射到实盘；底仓保护已有 PANIC 通道兜底。
- **交付物**：决策写入 fix5 手册 B2 节（修订"底仓处置比例 ⟨占位⟩ → 0%，显性决策"）+ 本手册留痕；同步修订 B2 验收标准为"利润仓（超 base_ref 部分）在趋势破位档位落袋，底仓不被 TREND_EXIT 触碰"。
- **验收标准**：文档修订完成；回放中任意时点 TREND_EXIT 卖出后持仓 ≥ base_ref（持仓 CSV 可证）。

### WP-F9：三窗口回放包（Phase B 出口硬材料）

| 场景 | 配置 | 关键验收指标 |
|---|---|---|
| fix2 场景 | 000988 单票、底仓 800、20 万、fix2 同窗口（+13.9% 峰值后 -33.3% 结局） | TRAIL 退出均价 ≥ 成本×1.07（WP-F6 主验收复用） |
| fix3 场景 | 000988 单票、底仓 800、20 万、fix3 同窗口（+10.6% 峰值） | TARGET_SELL ≥1 档落袋且同档 0 重复 |
| fix4 场景 | 4 标的、15 万、2026-04-27~07-24 全窗口 | 亏损 SELL_HIGH 0 笔；连续 ≥5 日割肉序列 0；09:35 前非保护卖出 0 笔 |

- **每包交付**：回放 PDF + 交易/净值/持仓 CSV + backtrace.jsonl + 通道分布表，统一存 `E:\06_T\audit\gm_fusion\v1.0.1_fix6\<场景>\`。

### WP-F10：Phase B 出口验收（K3）

- [ ] B1：WP-F6 验收全部打钩；
- [ ] B2：WP-F8 决策落地 + 利润仓落袋行为可证 + 底仓未被触碰；
- [ ] B3：亏损 SELL_HIGH 0 笔（fix4 场景复证）；
- [ ] B4：fix3 场景 ≥1 档落袋、同档 0 重复、无超卖；
- [ ] B5：WP-F5 验收全部打钩；
- [ ] 三窗口回放包含齐、通道分布表含 TRAIL/TREND_EXIT/TARGET 枚举；
- [ ] N17 绞肉机场景结构性消失（fix4 场景复证）。
- **出口判定**：全部打钩 → **Phase B 关闭，准许进入 Phase C（组合层）拆解。**

---

## 四、数值留白清单更新（Phase D 寻优输入，本轮新增）

| 参数 | 当前临时值 | 位置 | 备注 |
|---|---|---|---|
| MIN_BACK / k / MAX_BACK | 0.03 / 1.5 / 0.08 | WP-F6 | 本轮新增，带 TODO(PhaseD) |
| ACT_LINE | 0.08 | main.py:638 | 沿用 |
| M2 三门槛 | 3% / 2亿 / 2000元 | main.py:249-250 | 沿用；F1 修复后 600481 将被拦，门槛有效性首次真正生效 |
| cost_anchor | 0.0 | main.py:733 | 沿用 |
| 预算权重 | 等权 25% | main.py:812-813 | Phase C 改趋势加权 |
| TARGET L1 | 0.10 | main.py:680 | L2/L3 仍留白 |

---

## 五、时间节奏（与模拟盘并行）

| 时间 | 动作 |
|---|---|
| 07-26（周六晚）~ 07-27 凌晨 | F1+F2+F3 施工（合计 <1 天） |
| 07-27 白天 | F4 全窗口回放 + K3 复审 → **Phase A 出口** |
| 07-28（周一） | **以 fix6 版本启动模拟盘**；每日按《每日复盘清单》运转 |
| 模拟盘第 1 周 | F5/F7/F8（小修 + 决策记录），F6 施工 |
| 模拟盘第 2 周 | F9 三窗口回放包 → F10 K3 复审 → **Phase B 出口** |
| Phase B 出口后 | 拆解 Phase C（组合层）；模拟盘持续积累样本，≥4 周有效样本后评估 Phase D 启动条件 |

**注意**：批次一未完成前不要启动模拟盘——N18 意味着"用 000988 的趋势给全部持仓做熔断"，N22 意味着持仓漂移无安全网，N12 意味着复盘数据无法追溯。三者的修复成本合计不到一天，不值得抢这一天。
