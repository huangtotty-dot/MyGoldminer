# 修复方案 WP-B13/B14：TARGET_SELL 开盘缓冲覆盖 + L1 位图跨日持久化

> **来源**：2026-08-12 日复盘（`复盘_2026-08-12.md` 第八节 #1/#2），backlog 条目 B-13 / B-14。
> **性质**：②结构问题工作包，走 fix5 流程（分支施工 → 回放验证 → 审计 → owner 验收后合并；红线 6/7）。
> **实证触发**：0812 09:32 603667 TARGET_SELL 信号（57.28，profit 10.2%）——① 未被开盘缓冲拦截；② 昨日 L1 已成交 200@57.25 仍重复触发。地板保护（pos 400 = min_hold）兜底拦截，零损失。
> **不在本包范围**：O-07 `_total_equity` 口径偏差（先查因后立项）；C1-7 费用字段（周六专项）；TARGET L2/L3 分档（Phase D）；TARGET 盈亏归栏口径（owner 决策项，与本包无关）。

---

## 一、问题定义与根因

### B-13：TARGET_SELL 绕过开盘缓冲（R3/B5' 覆盖不全）

**设计意图**（fix5 手册 WP-B5 条款 + fix6 WP-F5）：09:35 前非保护类卖出（SELL_HIGH、TARGET_SELL）延后；PANIC/TRAIL/TREND_EXIT 保留即时性。

**现状代码路径**（main.py 门控链，按执行顺序）：

| 行号 | 环节 | 说明 |
|---|---|---|
| 1188 | `engine.evaluate` | 产出 SELL_HIGH / BUY_LOW |
| 1206-1221 | uni_down / 个股趋势 / 尾盘熔断 | 只拦买入 |
| 1223-1256 | TRAIL_SELL 生成 | 即时类 ✓ |
| **1260-1263** | **开盘缓冲检查** | 只拦 `SELL_HIGH / TARGET_SELL`——但此刻 TARGET **尚未生成** |
| 1265-1277 | TREND_EXIT 生成 | 即时类 ✓ |
| **1281-1291** | **TARGET_SELL 生成** | 在缓冲检查**下游** → 穿透 |
| 1296-1302 | PANIC_SELL 生成 | 即时类 ✓ |
| 1304-1310 | TAIL 尾盘归位 | 14:50 后才会触发，与缓冲窗口天然互斥 |

**根因**：F5 修复（`1260` 行条件扩展为 `in ("SELL_HIGH", "TARGET_SELL")`）只覆盖引擎信号路径；TARGET_SELL 是 main.py 门控链产物（全景文档关键认知：引擎只产 SELL_HIGH/BUY_LOW），生成点在缓冲检查之后，缓冲永远看不到它。fix6/fix8 回放窗口内无早盘 TARGET 样本，漏网未被验收捕获；0812 实战首次暴露。

### B-14：`_target_filled_l1` 位图的两个生命周期缺陷

1. **不跨日持久化**：位图存于 `context.manual_position` 纯内存态，策略每日重启（INIT 对账重建持仓）后复位 False → "同一持仓期同档一次性"只在单进程内成立，跨日每交易日可重复触发。TRAIL 的 `_trail_state/_trail_peak` 同病（0812 实证：603667 浮盈全天 8.1%~11.3%，TRAIL 应已 ARMED，重启即丢失）。
2. **置位时机错误**：位图在**信号生成时**置位（main.py:1290-1291），而非成交时。0812 实证：09:32 信号生成 → 置位 True → 被地板拦截未成交 → 当日 L1 名额已被消耗，即使午后加仓过地板也不再触发。**被拦截/未成交的信号不应消耗档位。**

**同源问题**：B-07 遗留"awaiting_buyback 重启记忆丢失"——三者同为卖出体系内存态持久化缺口，本包统一治理（见范围划分）。

---

## 二、修复方案

### WP-B13：缓冲检查移至门控链末端

**改动**：删除 1260-1263 的上游检查，在 TAIL 块之后、"信号事件写入"（1411）之前插入统一缓冲闸：

```python
# R3/B5': 开盘卖出缓冲 —— 09:35 前非保护类卖出延后
# 覆盖全部生成路径（引擎 SELL_HIGH + 门控链 TARGET_SELL）；
# PANIC/TRAIL/TREND_EXIT/TAIL 保留即时性（fix5 B5 条款）
if sig and sig.action in ("SELL_HIGH", "TARGET_SELL") and now.hour == 9 and now.minute <= 35:
    _audit_write({"event": "morning_sell_blocked", "code": code, "time": str(now),
                  "action": sig.action, "reason": "开盘缓冲延后"})   # 修掉原硬编码 "SELL_HIGH"
    sig = None
```

**要点**：
- 拦截语义 = **延后而非取消**：09:36 起条件消失，下一 bar 重新评估生成，不丢信号；
- 与 B-14 联动后，被拦信号不置位 `_target_filled_l1`（置位移到成交回调），09:36 可正常再触发；
- 拦截事件仍写 backtrace（`morning_sell_blocked`），action 字段改为真实通道名，C1-5/C2 对账口径不变；
- 买入侧闸（1206-1221）、TRAIL/TREND_EXIT/PANIC/TAIL 顺序与逻辑**一律不动**。

### WP-B14：卖出体系状态持久化 + 位图置位时机修正

**1. 位图三段式状态机**（替代布尔位）：

```
None →（下单时）"pending" →（成交回调）"filled"
  ↑________（拒单/撤单回滚）________┘
```

| 接入点 | 位置 | 动作 |
|---|---|---|
| 信号生成（1281-1291） | 判定条件改读状态：`state is None` 才生成；**删除生成时置位** | 被拦/未成交不耗档位 |
| 卖出下单分支（~1585 区域） | `action=="TARGET_SELL"` 时置 `"pending"` 并落盘 | 防下单→成交回调间的竞态重复触发 |
| 成交回调（1689-1694，`side==2` 且 `_act=="TARGET_SELL"`） | 置 `"filled"` 并落盘 | 真实落袋才封档 |
| 拒单回滚（1730-1741） | 清回 `None` 并落盘 | 拒单不耗档 |
| 全仓清空复位（1292-1294） | 清仓时置 `None` 并落盘 | 新持仓期重新计数 |

**2. 持久化文件**：`runtime\state\sell_state.json`（新建目录；与事件桥 `runtime\bridge\` 分离——一个是状态、一个是流水）

```json
{
  "603667": {
    "_target_l1_state": "filled",
    "_trail_state": "ARMED",
    "_trail_peak": 57.89,
    "pos_key": "400@51.9962",
    "updated": "2026-08-12 13:58:59"
  }
}
```

**3. 恢复与失效规则**（INIT 对账后执行，~456 行区域）：
- 券商持仓 qty > 0 且 `pos_key` 与当前 `qty@cost` 一致 → 恢复该票状态；
- qty ≤ 0（已清仓）或 `pos_key` 不符（持仓结构已变，如人工加减仓）→ **作废该票状态**（保守原则：状态存疑即重置，宁多触发一档也不错杀新持仓期）；
- 每次状态变更即时写盘（json.dump，单文件 <1KB，性能无感）；
- live 模式生效，回测模式跳过（与 `_get_holding` 的 F2 口径一致）。

**4. TRAIL 同包顺带修**：`_trail_state/_trail_peak` 走同一文件与恢复规则——0812 起 TRAIL 进入武装窗口，跨日丢失已是现实缺口而非理论问题。

**5. awaiting_buyback 记忆恢复（可选 Scope-B，owner 定）**：B-07 遗留的同源问题。若同包，`sell_state.json` 增加 `awaiting_buyback` 段；若另立 WP，本包文件结构预留扩展位。**建议同包**——恢复逻辑复用，一次回放验证全覆盖。

---

## 三、测试与验收

### 3.1 单元测试（新建 `tests\test_wp_b13b14.py`）

| 用例 | 内容 | 通过标准 |
|---|---|---|
| T1 缓冲移位 | 09:32 构造 profit=10.2% 持仓 → TARGET 生成 | `morning_sell_blocked` 事件 action=TARGET_SELL，无下单 |
| T2 延后放行 | 同上场景走到 09:36 | TARGET 正常生成并下单 |
| T3 即时性回归 | 09:32 分别构造 PANIC/TRAIL/TREND_EXIT | 三者均不被拦（B5 条款不破） |
| T4 置位时机 | 信号生成→地板拦截（不下单）→ 次 bar 持仓过地板 | 拦截后状态仍 None，次 bar 可再触发 |
| T5 竞态防护 | 下单(pending)→成交回调前再评估 | pending 期间不重复生成 |
| T6 拒单回滚 | pending → 拒单 | 状态清回 None，当日可再触发 |
| T7 持久化 | 置位后重启进程（模拟 INIT 对账） | pos_key 匹配→恢复；qty=0→作废；pos_key 不符→作废 |
| T8 TRAIL 持久化 | ARMED+peak 写盘→重启 | 状态机续接，peak 不归零 |

运行方式（沿用项目惯例）：`"C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" tests\test_wp_b13b14.py`

### 3.2 回放验证（WP-B 回放包）

| 场景 | 验收标准 |
|---|---|
| **N19 实证场景复现**（603667 05-08 09:32 窗口） | 09:35 前 TARGET_SELL **0 笔成交**，`morning_sell_blocked` 有 TARGET 样本；09:35 后如条件仍满足应正常落袋 |
| **fix3 场景**（000988 单票、底仓 800、+10.6% 峰值窗口） | TARGET_SELL ≥1 档落袋且**同档 0 重复**（沿用 fix8 手册口径） |
| 保护类回归 | 603667 06-26 09:31 PANIC、05-13 09:31 TRAIL 仍在案（fix8 验收②口径） |

### 3.3 回归基线

现有 6 测试文件全绿（test_fix_20260810 20/20、test_wp_e2 20/20、test_wp_e3 13/13、test_wp_b07 18/18、test_fix_20260728 22/22、test_fix_20260731 18/18）。

### 3.4 上线后实战验证（日复盘挂钩）

- C1/C2 每日 grep：`morning_sell_blocked`（含 TARGET 样本即 B-13 生效）、TARGET 同档跨日重复（0 次即 B-14 生效）；
- 首个 TARGET 成交样本后核对：位图落盘内容与成交回调一致；
- 603667 TRAIL 首个样本时核对状态机跨日续接。

---

## 四、风险与回滚

| 风险 | 评估 | 缓解 |
|---|---|---|
| 缓冲移位影响其他通道 | 低——拦截名单不变，仅拦截时点从"引擎信号后"移到"全部门控后"；TRAIL/TREND_EXIT/PANIC/TAIL 均不在名单 | T3 即时性回归用例兜底 |
| 状态文件损坏/读写异常 | 低——文件 <1KB；读取异常时 fail-open 为空状态（等同现状），不致劣化 | try/except 包裹 + print 告警 |
| 位图置位时机改变 TARGET 触发节奏 | 中——"生成即耗档"改"成交才耗档"后，被拦场景下同日可再触发（这正是修复意图，但改变了行为） | 日复盘 C2 区重点标注首周差异 |
| pos_key 作废规则偏保守导致重复触发 | 低——人工改仓后 L1 重新可用，语义上"新持仓期新档位"自洽 | 实战观察一周 |

**回滚**：单 commit revert 即恢复原状（两 WP 同包但分两个 commit，可独立回滚）。

## 五、施工清单

1. [ ] 分支 `wp-b13b14` 开工；
2. [ ] WP-B13：缓冲移位 + action 字段修正（commit 1）；
3. [ ] WP-B14：状态机三段式 + 持久化 + TRAIL 同修（commit 2；buyback 恢复是否同包待 owner 批）；
4. [ ] `tests\test_wp_b13b14.py` T1-T8；
5. [ ] 回放包两场景 + 保护类回归；
6. [ ] 6 文件回归全绿；
7. [ ] 审计报告（改→回放→审计流程）→ owner 验收 → 合并；
8. [ ] 合并后首个交易日：日复盘 C2-8/C2-9 区加挂验证项。

**预估工作量**：代码 ~60 行净改动 + 测试 ~150 行；施工+回放+审计约 1 个工作日。
