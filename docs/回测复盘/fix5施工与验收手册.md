# fix5 施工与验收手册（工作分解 · 测试方案 · 验收标准）

> **来源文档**：《审核报告_v1.0.1_fix4.md》（第四~七章）
> **文档定位**：把审核报告中的全部待改/待实现项拆解为可执行工作包（WP），每个工作包给出改动内容、测试方案、验收标准三要素。
> **使用方式**：Pro 按 Phase 顺序施工，每个 WP 独立 commit；Phase 完成后按第九章清单提交材料，K3（审计员）验收通过才进入下一 Phase。
> **制定日期**：2026-07-25 ｜ **适用版本**：fix4（HEAD `82b18a6`）之后

---

## 〇、铁律与全局约定（施工前必读）

1. **不改买卖阈值数值**：本手册所有 `⟨占位⟩` 参数（见第八章清单）在 Phase A~C 中**只接线、不定值**；施工期临时值仅用于跑通流程，统一标记 `// TODO(PhaseD): 寻优`，数值定型是 Phase D 的事。
2. **每 WP 独立 commit**，commit message 格式：`fix5-A3: R1个股趋势熔断（WP-A3）`，禁止打包提交。
3. **统一回放环境**（所有回归测试共用，与 N11 闭环口径一致）：
   - 模式：`mode=MODE_BACKTEST`，`backtest_commission_ratio=0.00015`，`backtest_slippage_ratio=0.0001`，`backtest_adjust=ADJUST_PREV`，`backtest_match_mode=1`
   - 窗口：2026-04-24 ~ 2026-07-24（与 fix2/fix3/fix4 同窗口）
   - 资金：多标的测试 150,000；单票对照测试 200,000
4. **对照基线**（验收比对的基准数据，均已存档）：

   | 基线 | 路径 | 关键事实 |
   |---|---|---|
   | fix2 | `E:\06_T\audit\gm_fusion\v1.0.1_fix2\report\data_zip\` | +18.29% / MDD -21.2% / 7月0交易 / 底仓800股期末浮亏-43,163 |
   | fix3 | `E:\06_T\audit\gm_fusion\v1.0.1_fix3\report\data_zip\` | +16.34% / MDD -8.65% / 63笔 / 07-15清仓 |
   | fix4 | `E:\06_T\audit\gm_fusion\v1.0.1_fix4\report\data_zip\` | -2.79% / 74笔 / 600481亏损高抛91%、603667 86% |

5. **产物归档**：每轮回测的 PDF + data.zip 放入 `E:\goldenMinerReport\<版本名>\`，并复制到 `E:\06_T\audit\gm_fusion\<版本名>\report\`；回测前确认 `gmcache/backtrace.jsonl` 会落地本地（P-3 遗留项，首次回放时验证）。

---

## 一、工作包总览（WBS）

| WP | 内容 | Phase | 依赖 | 工作量 | 风险 |
|---|---|---|---|---|---|
| A1 | N15 fail-closed 门控修复 | A | 无 | 10分钟 | 极低 |
| A2 | M2 标的池门槛代码化 | A | 无 | 半天 | 低 |
| A3 | R1 个股级趋势熔断 | A | 无 | 1天 | 中 |
| A4 | T4 卖出仲裁器化（行为等价搬迁） | A | 无 | 1天 | **高（重构）** |
| A5 | N16 单票预算 estimated_equity 替换 | A | 可与 A3 并行 | 半天 | 低 |
| B1 | T1 移动止盈 TRAIL_SELL | B | A4 | 1~2天 | 中 |
| B2 | T3 趋势破坏止盈 TREND_EXIT | B | A3+A4 | 1天 | 中 |
| B3 | R2 SELL_HIGH 成本锚定 | B | **B2（原子批次，禁单独上线）** | 半天 | 中 |
| B4 | T2 分批目标止盈 TARGET_SELL | B | A4 | 1天 | 低 |
| B5 | R3 卖出开盘缓冲 | B | 无 | 半天 | 低 |
| C1 | M1 预算制全量（底仓反推） | C | A5 | 1天 | 中 |
| C2 | M3 组合状态机 | C | A3 | 1天 | 低 |
| C3 | M4/M5/M6 现金效率/组合KPI/相关性 | C | C1+C2 | 迭代 | 低 |
| Z1 | 杂项包（手册/watcher/token/注释） | 任意 | 无 | 半天 | 极低 |

---

## 二、Phase A：结构骨架（纯接线，无数值）

### WP-A1：N15 fail-closed 门控修复

- **来源**：报告 4.4-N15
- **改动内容**（main.py:620-623）：
  ```python
  # 现状（缺陷）：available_cash=0 在 _cash_warned 门控内，仅首根 bar 生效
  if not _cash_ok and not getattr(context, '_cash_warned', False):
      context._cash_warned = True
      print(...)
      available_cash = 0
  # 改为：置零每根 bar 生效，告警只打一次
  if not _cash_ok:
      available_cash = 0
      if not getattr(context, '_cash_warned', False):
          context._cash_warned = True
          print('[N8] WARN: 无法读取可用现金 → fail-closed: 禁止买入')
  ```
- **测试方案**：
  1. 静态检查：`grep -n "available_cash = 0" main.py` 确认在门控外；
  2. 故障注入测试（本地临时桩，不入库）：把 `getattr(_acct, 'cash', None)` 强制改为 `None` 跑 5 分钟回放，确认日志出现一次 WARN、且之后每根 bar 均无买入（对照缺陷版：第二根 bar 起恢复买入）；
  3. 正常回归：同窗口回放，确认现金读取正常时行为与 fix4 完全一致。
- **验收标准**：
  - [ ] 故障注入下连续 ≥10 根 bar 0 买入且 WARN 仅 1 次；
  - [ ] 正常回放成交序列与 fix4 基线一致率 100%；
  - [ ] 故障桩代码不进入 commit。
- **回退**：单文件单行级改动，直接 revert commit。

### WP-A2：M2 标的池门槛代码化

- **来源**：报告 4.6-M2 / 4.5-R5 / 蓝图 7.8
- **改动内容**：在底仓建仓块（main.py:421-428）前插入门槛检查，数据复用 `_refresh_daily_ctx` 已拉取的日线（history_n），新增计算：20日日均振幅 `mean(TR,20)/close`、20日日均成交额、单手金额 `100×price`；不达标票执行 `context._base_settled.add(code)` + `write_risk(..., "pool_gate", ...)`。三个门槛值为 `⟨占位⟩`（建议临时值：振幅 3%、成交额 2亿、单手 2000 元，`// TODO(PhaseD)`）。
- **测试方案**：
  1. 单测级：用 000988（应过）与 600481（6元低价、单手 632 元，应被拦）两个标的分别跑到建仓时点，检查事件桥是否出现 `pool_gate` 记录；
  2. 回放：多标的同窗口，确认被拦票全程 0 底仓单、0 信号交易；
  3. 边界：日线数据不足 20 日时（history_n 返回短数据）的行为——应"数据不足=不信任=仅观察"，不得放行。
- **验收标准**：
  - [ ] 600481 类低价小票自动转仅观察（事件桥有 `pool_gate` 记录）；
  - [ ] 000988 正常建仓（回归不破）；
  - [ ] 数据不足场景不放行；
  - [ ] 门槛参数集中在 params.py，带 `TODO(PhaseD)` 标记。
- **回退**：单 commit revert。

### WP-A3：R1 个股级趋势熔断（STOCK_TREND_GATE）

- **来源**：报告 4.5-R1 / 蓝图 7.2
- **改动内容**：
  1. `_refresh_daily_ctx`（main.py:135-191）内新增 `_stock_trend_state` 计算（四态：TREND_UP / TREND_RANGE / TREND_DOWN / TREND_BREAKDOWN），判定**只组合已有字段**（prev_close、ma5、ma5_slope、daily_breakdown_risk），不新增指标；
  2. uni_down 熔断块（main.py:485-486）之后插入 G3 闸门：TREND_BREAKDOWN 禁一切买入，TREND_DOWN 禁 ADD_POS，均 `write_risk(str(now), "stock_trend_gate", ...)`；
  3. **底仓建仓过闸（方案 A，报告 7.2 建议）**：TREND_BREAKDOWN 状态下底仓延迟建仓，事件桥记录 `base_deferred`；
  4. 状态每日一盘（daily_ctx 刷新时重算），日内不翻转。
- **测试方案**：
  1. 字段验证：回放时抽查 backtrace/no_signal 记录，确认 `_stock_trend_state` 每日每票有值且四态枚举闭合；
  2. 行为验证（核心）：多标的同窗口回放，检查 603667 在 06-05（73.85）与 06-11（65.39）两个历史逆势买点处**不再产生买入**；600481 底仓建仓应延迟或转仅观察；
  3. 回归验证：000988、600176 的买入时点与 fix4 基线逐笔对照，差异笔数应可逐笔解释（因趋势闸拦截的须列出清单）；
  4. 事件桥：`stock_trend_gate` 风控事件有记录，watcher 能正确显示。
- **验收标准**：
  - [ ] 回放中 600481/603667 逆势买入 **0 笔**（对照 fix4 实际 2 笔）；
  - [ ] 四态枚举每日每票闭合（无 null/未知态）；
  - [ ] 000988/600176 盈利交易回归差异清单化、每笔可归因；
  - [ ] 事件桥有 `stock_trend_gate` / `base_deferred` 记录。
- **常见失败模式**：状态计算放到了 on_bar 每分钟重算（应每日一盘）；ma5_slope 符号用反（用 fix4 下行日数据点验证：600481 04-28 应为 TREND_DOWN/BREAKDOWN）。
- **回退**：闸门整体 `if ENABLE_STOCK_TREND_GATE` 开关包裹，紧急时 params 一个布尔值关闭。

### WP-A4：T4 卖出仲裁器化（行为等价搬迁）

- **来源**：蓝图 7.7（重构纪律：**先搬家后加新通道**）
- **改动内容**：将现有三处卖出逻辑——PANIC 块（main.py:493-504）、尾盘归位块（506-512）、地板保护块（565-584）——抽取为统一 `_sell_arbiter(context, code, sig, feats_cache, pos_qty, base_ref, now)`，输出单一卖出动作族（action/qty/优先级）。**本 WP 只搬迁、零行为变更、不接任何新通道。**
- **测试方案**：
  1. 行为等价回归（唯一但严格的测试）：多标的同窗口回放，导出成交 CSV；
  2. 与 fix4 基线逐字段比对（脚本化）：标的、方向、数量、价格、时间（bar 级）；
  3. 拒单/风控事件数量比对。
- **验收标准**：
  - [ ] 成交序列与 fix4 基线**一致率 100%**（74 笔逐笔匹配，仅时间戳秒级误差可豁免）；
  - [ ] 净值期末差异 < 0.01%；
  - [ ] 比对脚本入库存档（`E:\06_T\audit\gm_fusion\` 下），供后续 Phase 复用。
- **常见失败模式**：搬迁时改了判定顺序（PANIC 与尾盘的先后）、漏搬地板豁免（PANIC floor=0）、冷却调用点丢失。**任何一笔不一致都不允许"解释过去"，必须查因。**
- **回退**：重构风险全 Phase 最高，必须独立 commit；不一致即 revert 重搬。

### WP-A5：N16 单票预算 estimated_equity 替换

- **来源**：报告 4.4-N16 / 蓝图 7.9（第一阶段：仅替换上限语义，底仓反推留给 C1）
- **改动内容**：context 增加 `budget_map`（权益 × 等权权重 `⟨占位⟩`，初始 4 票各 25%）；main.py:627 `estimated_equity = available_cash + pos_qty*cp` → `budget[code] + pos_qty*cp`。现金预检（max_by_cash）保留用全账户现金（那是真实偿付能力），仅仓位上限改预算制。
- **测试方案**：
  1. 语义验证：回放中打印/事件记录各票 target_t，确认每票上限按 25% 预算计算（fix4 中 000988 曾按 15 万全账户计算）；
  2. 回归：fix4 窗口回放，成交差异清单化（预期：部分大买单被预算闸缩量，差异可逐笔归因）。
- **验收标准**：
  - [ ] 任意时点单票市值 ≤ 预算×80%（从持仓 CSV 逐日验证）；
  - [ ] 聚合仓位不再出现"每票各自 80%"的理论空间；
  - [ ] 回归差异全部可归因。
- **回退**：单 commit revert。

**Phase A 出口条件**：A1~A5 全部 WP 验收打钩 + Phase A 整体回放报告 + K3 审计通过。

---

## 三、Phase B：止盈体系（原子批次：B1→B2→B3 必须同批，B4/B5 可独立）

### WP-B1：T1 移动止盈（TRAIL_SELL）

- **来源**：报告 4.7 / 蓝图 7.4
- **改动内容**：
  1. `manual_position` 新增三字段：`trail_state`（INACTIVE/ARMED/COOLED）、`trail_peak`、每 bar 更新 `trail_peak = max(trail_peak, price)`；全仓清空复位，部分卖出保持；
  2. 激活：`profit_pct > ACT_LINE⟨占位⟩`；触发：`price < trail_peak × (1 - max(MIN_BACK⟨占位⟩, k⟨占位⟩×daily_atr, 上限 MAX_BACK⟨占位⟩))`——**k×ATR 双向带界（N9 教训）**；
  3. 接入仲裁器优先级 2（PANIC 之后、TREND_EXIT 之前）；触发后复用 `sell_cooldown`；
  4. backtrace 与事件桥补 `TRAIL_SELL` 枚举。
- **测试方案**：
  1. 状态机单测（本地脚本，模拟价格序列）：验证 INACTIVE→ARMED→触发→COOLED→复位全路径，含"部分卖出后 peak 保持"分支；
  2. **主验收回放（fix2 场景）**：单票 000988、底仓 800、资金 20 万、同窗口——复现 fix2 的 +13.9% 峰值场景；
  3. 误触发检查：5 月震荡段的 TRAIL 触发次数统计（过多说明临时回撤参数过紧，记录现象不定值）；
  4. 持久化检查：状态字段随 `_get_holding` 对账不丢失（模拟 30 分钟对账周期）。
- **验收标准（可证伪）**：
  - [ ] fix2 场景退出均价 ≥ 成本 × **1.07**（锁定峰值利润一半以上；fix2 实际结局 -33.3%）；
  - [ ] 状态机单测全路径通过；
  - [ ] backtrace 中 TRAIL_SELL 记录带 profit_pct/trail_peak/触发线三字段（可解释性）；
  - [ ] 5 月震荡段误触发次数入报告，供 Phase D 定值参考。
- **回退**：仲裁器内 `ENABLE_TRAIL` 开关。

### WP-B2：T3 趋势破坏止盈（TREND_EXIT）

- **来源**：报告 4.7 / 蓝图 7.6
- **改动内容**：消费 A3 产出的 `_stock_trend_state`；触发条件 `profit_pct > 0 且状态翻转至 TREND_DOWN/BREAKDOWN`；动作：了结利润仓（超 base_ref 部分），底仓处置比例 `⟨占位⟩`；接入仲裁器优先级 3。
- **测试方案**：
  1. 多标的同窗口回放，检查 600176：趋势转弱档位应出现 TREND_EXIT 卖出（对照 fix4 实际：尾仓 200 股拿到浮盈 -1.3%）；
  2. 与 TRAIL 的冗余关系验证：统计同 bar 双触发次数，确认仲裁器取高优先级而非重复卖（持仓 CSV 不出现负库存/超卖）；
  3. 回归：000988 5-6 月卖强行为差异清单化。
- **验收标准**：
  - [ ] 600176 尾仓在趋势破位档位落袋（成交 CSV 可查）；
  - [ ] 全程 0 超卖（任意时点卖出量 ≤ 可用持仓）；
  - [ ] 双通道同 bar 命中时仲裁行为符合优先级表。
- **回退**：`ENABLE_TREND_EXIT` 开关；**注意 B3 依赖 B2，B2 回退则 B3 必须同步回退。**

### WP-B3：R2 SELL_HIGH 成本锚定（与 B2 原子批次，禁止单独上线）

- **来源**：报告 4.5-R2 / 蓝图 7.3
- **改动内容**：SELL_HIGH 评分不动，执行前加成本闸：`profit_pct < COST_ANCHOR⟨占位⟩` 时信号降级（交回仲裁链由 TREND_EXIT/PANIC 接管）。
- **测试方案**：
  1. **主验收回放（fix4 场景）**：多标的同窗口，统计低于成本的 SELL_HIGH 笔数（基线：600481 91%、603667 86%）；
  2. 通道接管验证：被降级的卖出需求应转由 TREND_EXIT/PANIC 完成——检查两只阴跌票是否仍有退出路径（不得出现"无通道可卖"的持仓僵尸）；
  3. 回归：000988/600176 盈利高抛笔数不得下降。
- **验收标准（可证伪）**：
  - [ ] 低于成本的 SELL_HIGH **0 笔**；
  - [ ] 600481/603667 的退出全部由 TREND_EXIT/PANIC 通道完成（backtrace 通道分布可证）；
  - [ ] 000988/600176 盈利高抛笔数回归不降；
  - [ ] **与 B2 同批 commit 或同批验收，单独出现即拒收。**
- **回退**：与 B2 捆绑回退。

### WP-B4：T2 分批目标止盈（TARGET_SELL）

- **来源**：报告 4.7 / 蓝图 7.5
- **改动内容**：分档刻度 L1/L2/L3`⟨占位⟩`，每档一次性，`target_filled` 位图存 manual_position；批次比例 `⟨占位⟩`；接入仲裁器优先级 4，与 SELL_HIGH 同 bar 命中时 qty 取大不求和。
- **测试方案**：
  1. fix3 场景回放（单票 000988、底仓 800、20 万）：+10.6% 峰值场景至少一档落袋；
  2. 位图持久化：回档触发后不重复触发（同一档 0 重复）；
  3. 回归护栏：000988 5-6 月卖强收益退化 ≤ ⟨占位⟩%（临时建议 20%）。
- **验收标准**：
  - [ ] fix3 场景 ≥1 档落袋且同档 0 重复；
  - [ ] 卖强收益退化在护栏内；
  - [ ] 与 SELL_HIGH 同 bar 合并时无超卖。
- **回退**：`ENABLE_TARGET` 开关。

### WP-B5：R3 卖出开盘缓冲

- **来源**：报告 4.5-R3
- **改动内容**：非 PANIC/TRAIL 卖出在 09:35 前延后（与 N6 买入隔离对称，main.py:462 现有逻辑扩展）；PANIC/TRAIL/TREND_EXIT 保留即时性。
- **测试方案**：fix4 窗口回放，统计 09:31-09:35 的非保护类卖出笔数（基线 9 笔开盘即卖）；确认保护类通道在 09:31 仍可即时触发（7 月 PANIC 场景回归）。
- **验收标准**：
  - [ ] 09:35 前非保护类卖出 **0 笔**；
  - [ ] 保护类通道开盘即时性回归不破。
- **回退**：单 commit revert。

**Phase B 出口条件**：B1~B5 全部验收打钩 + 三窗口回放包（fix2 场景/fix3 场景/fix4 场景）+ backtrace 通道分布表 + K3 审计通过。**特别关注：N17 绞肉机场景在 fix4 窗口应结构性消失（无连续 ≥5 日分批割肉序列）。**

---

## 四、Phase C：组合层

### WP-C1：M1 预算制全量（底仓反推）

- **来源**：蓝图 7.9
- **改动内容**：底仓 `base_qty = budget[code]/price/缩放比⟨占位⟩`，MIRROR 各票相对比例不变（信号同构保留）；TREND_BREAKDOWN 票预算冻结。
- **测试**：多标的回放，首日建仓额按预算反推（对照 fix4 的照抄实盘股数）；冻结票 0 买入。
- **验收**：单票建仓市值 ≤ 预算；MIRROR 相对比例偏差 < 5%；冻结行为有事件记录。

### WP-C2：M3 组合状态机（四象限）

- **来源**：蓝图 7.10（含"指数差+个股差=清仓避战"新象限——**此象限与底仓 intact 哲学有冲突，实盘跟随规则需用户另行人工确认，模拟盘先行验证**）
- **测试**：回放按日输出象限定档日志（事件桥），抽查 10 个交易日的定档与当日行为一致性。
- **验收**：四象限定档每日有记录；各象限行为符合矩阵表。

### WP-C3：M4/M5/M6（现金效率 / 组合 KPI 体系 / 相关性软约束）

- **来源**：报告 4.6
- **改动**：组合 KPI 日报表（复盘任务扩展）：现金占比、单票贡献分散度、盈亏比、费用占毛利比、同主题合并敞口；行业标签复用韭研概念标签产出。
- **验收**：日复盘报告含 M5 五指标；同主题敞口超限有告警记录。

**Phase C 出口条件**：C1~C3 验收 + 组合 KPI 首次全量打分表 + K3 审计通过 → **方可进入 Phase D（Optuna 数值寻优）。**

---

## 五、Z1 杂项包（任意时间做，不阻塞 Phase）

| # | 事项 | 位置 | 验收 |
|---|---|---|---|
| Z1-1 | 手册 commit 号更新为 HEAD、"fix3已验证"改"fix4已验证" | docs/模拟盘操作手册.md | 文档与 HEAD 一致 |
| Z1-2 | watcher 限流键加 code、仓位告警限流、现金估计参数化、心跳 cash 真实化 | gm_bridge/watcher.py:107-116/144/121、main.py:382 | 双票 60s 内各自信号均能推送 |
| Z1-3 | token 抽环境变量 | main.py（__main__ 块） | 源码 grep 无明文 token |
| Z1-4 | params.py 文件头旧路径注释（signal/ → signals/） | config/params.py:3 | 注释与目录一致 |
| Z1-5 | commit message 如实写回测窗口（82b18a6 教训："30天"实为同窗口） | 流程 | 后续 commit 抽查 |

---

## 六、数值留白清单（⟨占位⟩ 汇总，Phase D 寻优输入）

| 参数 | 所属 WP | 含义 | 施工期临时值建议 | Phase D 搜索域建议 |
|---|---|---|---|---|
| COST_ANCHOR | B3 | SELL_HIGH 成本锚 | 0（成本即锚） | [-5%, +2%] |
| ACT_LINE | B1 | TRAIL 激活浮盈线 | +6% | [+3%, +12%] |
| MIN_BACK / k / MAX_BACK | B1 | TRAIL 回撤幅度（带界 k×ATR） | 4% / 2.0 / 10% | [2%,6%] / [1,4] / [8%,15%] |
| L1/L2/L3 + 批次比例 | B4 | TARGET 分档与批量 | +8%/+15%/+25%，各 1/3 | 网格 |
| AMP_MIN / AMT_MIN / LOT_MIN | A2 | 池门槛 | 3% / 2亿 / 2000元 | 窄域微调 |
| 预算权重 | A5/C1 | 单票预算 | 等权 25% | 等权 vs 趋势加权 |
| 降频系数 | C2 | uni_down 象限仓位系数 | 0.5 | [0.25, 0.75] |
| TREND_DOWN 放行策略 | A3 | BUY_LOW 在 DOWN 态是否放行 | 禁 ADD_POS、BUY_LOW 放行 | 二值策略项 |
| 底仓 TRAIL 处置比例 | B1/B2 | 保护触发时底仓卖出比例 | 50% | [25%, 100%] |

---

## 七、K3 审计提交清单（每 Phase 完成后）

1. 本 Phase 全部 commit 哈希列表（与 git log 对齐，**不得漏报**——a178dc3 教训）；
2. `git diff <phase起点>..HEAD` 完整 patch；
3. 每 WP 对应回测产物：PDF + data.zip + **gmcache/backtrace.jsonl**（P-3 遗留，必须落地）；
4. WP 验收自测表（本手册各 WP 的勾选框逐项填写，附证据路径）；
5. 与基线的差异清单（逐笔归因）；
6. 新增 `⟨占位⟩` 参数的临时值与标记位置清单。

**K3 判定输出**：每 WP 给 ✅/⚠️/❌；任一 ❌ → 该 Phase 不通过，修复后重提；全部 ✅ → 明确"可进入下一 Phase"。

---

## 八、风险与纪律重申

1. **A4 是全计划最大风险点**（重构）：行为等价一致率必须 100%，任何"解释过去"的差异都视为不通过；
2. **B1+B2+B3 原子批次**：缺 B2 的 B3 会制造"无通道可卖"的持仓僵尸，单独出现即拒收；
3. **Phase D 之前禁止 Optuna**：结构未定型的寻优只会把缺陷拟合进参数（fix1→fix2 的教训）；
4. **模拟盘并行原则**：fix5 施工期间，周一上线的 fix4 版本模拟盘继续运行收集数据（仅观察纪律不变），施工在分支进行，合并前不切换线上版本；
5. **本手册与审核报告的关系**：报告（为什么）→ 蓝图（第七章：改什么）→ 本手册（怎么做、怎么测、什么算过）。三者冲突时以最新 commit 的事实为准，并回写修订。


---

## 九、W32 追加立项包

### WP-B07：回补价格记忆（awaiting_buyback 接通 + 高接门控）

- **来源**：2026-W32 周复盘 B-07 立项（W32 表决批准方案：回补价 > 前卖价×(1+容忍) 时延迟/降档）。0805 实证：603667 五洲新春 52.14 卖出 → 54.30 回补，高接 +4.15%，隐性成本 -432 超过当日做T差价 +233。
- **缺陷定位**：
  1. `signals/engine.py` `SignalEngine.__init__`（约 435 行）声明 `self.awaiting_buyback` 但全项目从未写入——死状态；`_check_date_reset`（约 453-466 行）每日清空它；
  2. `config/params.py` 84-92 行一整组 `awaiting_buyback_*` 参数为死参数（移植自 `E:\06_T\signal_engine.py`，原实现见其 284-322/566-583 行，本 WP 仅作语义参考）；
  3. 卖出后是否接回、以什么价接回无任何纪律约束。
- **改动内容**：
  1. **记忆生命周期**：卖出成交（main.py `on_order_status` status=3 side=2 分支，实际调用 `engine.record_trade_action(code, 'SELL_HIGH', volume, price)`，真实通道名由 `_pending_sell_action` 提供：SELL_HIGH/PANIC_SELL/TRAIL_SELL/TREND_EXIT/TARGET_SELL/TAIL 全覆盖）写入 `awaiting_buyback[code] = {sell_price, sell_time, sell_qty, sell_action, target_price}`；回补成交（BUY_LOW 类买入成交）后清除；TTL 用既有参数 `awaiting_buyback_ttl_minutes`（240 分钟），过期清除；**每日清零保留不变**（跨日接回由趋势闸等机制管，本 WP 只管日内）；
  2. **高接门控（核心新增）**：`evaluate()` 买入判定形成 BUY_LOW 信号后、返回前——
     - 新增全局参数（PARAMS，带 `# TODO(PhaseD)`）：`buyback_above_sell_delay_pct = 0.01`（硬延迟线）、`buyback_above_sell_downgrade_pct = 0.0`（软降档线，0=只要回补价高于前卖价即降档）；
     - 当前价 > sell_price×(1+delay_pct) → **延迟**：不产生 BUY_LOW，`last_decision[code]` 记 `{"action":"HOLD","reason":"buyback_above_sell_delayed"}`，`diagnostics` 留明细（前卖价/当前价/溢价幅度）；
     - sell_price×(1+downgrade_pct) < 当前价 ≤ sell_price×(1+delay_pct) → **降档**：信号保留，`sig.details` 加 `{"buyback_downgrade": True, ...}`；main.py 调用 sizer 处（`_apply_buyback_downgrade`，on_bar 买入段）数量减半后向下取整到 min_unit 整数倍，不足 min_unit 则延迟（等同不产生买入）；
     - 当前价 ≤ 前卖价 → 不受限制，且接通原系统激励（价格不高于前卖价版本）：`awaiting_buyback_score_boost`（折让>0.5%）/ `_weak`（折让>0.1%）加分 + `awaiting_buyback_threshold_relax` 降阈值；高接门控优先于激励；
  3. **可观测性**：事件桥新增 `write_buyback`（gm_bridge/writer.py），事件名 snake_case：`buyback_armed`（含前卖价/数量/通道）、`buyback_delayed`（含溢价%，门控延迟与降档不足 min_unit 延迟共用，reason 区分）、`buyback_downgrade`（降档成交，含数量/溢价%）、`buyback_filled`（回补完成清除，含前卖价/回补价）。
- **数值纪律**：除上述两个新增参数（带 TODO(PhaseD)）外，不修改任何既有阈值/参数数值；激励档折让刻度 0.5%/0.1% 沿用原系统 `E:\06_T\signal_engine.py` 既有语义（非本项目既有参数）。
- **测试方案**：
  1. 新建 `tests/test_wp_b07.py`（逐文件运行，不用 unittest discover）：记忆建立（全卖出通道）/ TTL 过期 / 高接延迟 / 降档带 / 低价接回激励与不受限 / 回补成交清除 / 每日清零 / main.py 成交回调事件（armed/filled）/ `_apply_buyback_downgrade` 减半取整与不足延迟；
  2. 回放验证：`replay_wp_b07.py` 单票 603667 / 底仓 800 / 15 万 / 2026-08-04~2026-08-06，对照 0805"52.14 卖出 → 54.30 回补"场景；若回放环境不可用则以合成 bar 单测场景替代并在报告中说明；
  3. 回归：`tests/test_fix_20260731.py` 18/18、`tests/test_fix_20260728.py` 22/22。
- **验收标准（可证伪）**：
  - [ ] 0805 场景回放（或等效合成场景）中 52.14 卖出后 54.30（溢价 >1%）的回补买单不再出现（被延迟），事件桥有 `buyback_delayed` 记录（含溢价%）；
  - [ ] 溢价 (0, 1%] 区间的回补单数量降为 sizer 结果的一半（向下取整到 100 的整数倍），事件桥有 `buyback_downgrade` 记录；
  - [ ] 价格 ≤ 前卖价的正常回补不受影响（照常产生 BUY_LOW 且享受激励加分/降阈值）；
  - [ ] 回补成交后记忆清除，事件桥有 `buyback_filled`；TTL 过期与每日清零行为可证；
  - [ ] 回归测试全绿（18/18 + 22/22 + 新单测全过）。
- **回退**：单 commit revert；运行时可将 PARAMS `buyback_above_sell_delay_pct` 调至 ≥1.0（延迟线名存实亡）并将 `buyback_above_sell_downgrade_pct` 调至 ≥1.0 即整体失活。


### WP-E2：max_pos 接线（总权益预算制 + 个股最大仓位约束）

- **来源**：owner 决策（2026-08-05 原话）："加仓的数量要根据总仓位分解到个股的最大仓位做约束"、"我这么多票不可能同时买入"。持仓框架：底仓+活动仓+现金三段式，现金保留 20%；当前股票池 16 只（main.py STOCKS）。
- **缺陷定位**：
  1. **预算口径错**（main.py 约 1330-1332 行）：`_stock_budget = available_cash / _n_stocks`——用可用现金等权分而非总权益，现金水位低时每股预算虚低/水位高时虚高，且完全无视其他 15 只票的持仓市值；
  2. **N2 仓位上限分母错**（约 1376 行）：`total_equity_value = available_cash + current_pos_value`——只算本票市值，"账户总权益"名不副实；
  3. **兜底洞 ×2**（约 1339-1340 行 + sizer 内部）：sizer 在 `max_buyable<=0`（已到顶）时返回 0，被 `qty=300` 强制兜底顶破上限；且 sizer 内部 `target_t<=hold_qty` 时私自放大为 `1.5×hold_qty`——到顶票仍可再买 50%；
  4. `position_allocator/` 为线下规划工具（底仓55%/活动仓25%/现金20%），未接入交易链路，本 WP 不接它，仅将"现金保留 20%"语义进参数。
- **改动内容**：
  1. **总权益口径**：新增 `_total_equity(context, available_cash)`——总权益 = 可用现金 + Σ(全部持仓市值)；持仓复用 `manual_position`（`_get_holding` 的第一优先数据源），定价取 `bar_cache` 最新收盘，某票 qty>0 但无价格数据时退化为成本价估值（mark-to-cost，注释说明）；
  2. **个股最大仓位**：新增参数 `cash_reserve_pct = 0.20`（PARAMS，带 `# TODO(PhaseD)`）；`stock_budget = total_equity × (1 - cash_reserve_pct) / len(STOCKS)`（等权，注释 TODO(PhaseD) 趋势加权）；`max_pos_shares = floor(stock_budget / cp / 100) × 100`；`target_t = max(max_pos_shares, base_ref, pos_qty)` 不变（永不在持仓下方收口，不逼卖出）；既有 `max_single_position_pct=0.80` 保留为外层安全帽，N2 检查逻辑不变、仅分母改为 `_total_equity`；
  3. **堵兜底洞**：新增 `_check_max_pos_cap` 闸——`pos_qty >= max(max_pos_shares, base_ref)` 且 `pos_qty>0` → 拦截并写 risk 事件 `max_pos_cap`（detail 含 budget/equity/weight/max_pos_shares/pos_qty），每票每日去重（O-03 风格）；sizer 返回 0 时区分：`pos_qty<=0`（全新建仓信号）保留 300 股兜底，`pos_qty>0`（已到顶）走同一 `max_pos_cap` 拦截；
  4. **可观测**：`max_pos_cap` 进事件桥（write_risk kind）+ backtrace/audit；init 启动时 print 一行当日预算表（equity/reserve/每股预算/各票 max_pos_shares 摘要）。
- **测试方案**：
  1. 新建 `tests/test_wp_e2.py`（仿 test_wp_b07.py 风格）：①总权益口径含多票持仓与成本价退化 ②等权预算与 max_pos_shares 取整 ③到顶拦截+事件+每票每日去重 ④新票 300 股兜底保留/持仓票不再兜底 ⑤N2 分母改用总权益（公式+源码接线断言）；
  2. 回归：`test_wp_b07.py` 18/18、`test_fix_20260731.py` 18/18、`test_fix_20260728.py` 22/22（已自查：三份回归均不经 on_bar 买入执行块，无 qty=300 兜底依赖）。
- **验收标准（可证伪）**：
  - [ ] 16 票等权预算 = 总权益×(1−20%)/16，init 预算表打印与手算一致；
  - [ ] 持仓已达 max_pos_shares（或 ≥ base_ref 取高者）的票买入信号被 `max_pos_cap` 拦截，事件桥与 audit 各一条（每票每日仅一条）；
  - [ ] 到顶票不再出现 sizer 1.5× 放大买入或 qty=300 强制兜底买入；
  - [ ] 全新建仓票（pos_qty=0）sizer 返回 0 时 300 股兜底保留；
  - [ ] N2 拦截分母 = 现金+全部持仓市值（含其他票），单票 80% 安全帽语义不变；
  - [ ] 回归测试全绿（18/18 + 18/18 + 22/22 + 新单测全过）。
- **回退**：单 commit revert；运行时将 PARAMS `cash_reserve_pct` 调回 0 且恢复 `_stock_budget = available_cash / _n_stocks` 一行即回旧口径（`_check_max_pos_cap` 随 revert 移除）。
