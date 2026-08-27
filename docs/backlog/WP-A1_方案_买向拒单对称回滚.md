# WP-A1 方案：买向拒单对称回滚（Phase A 放行恢复工作包）

> 立项背景：Phase A 合成拒单注入验证（2026-08-27，`docs/backlog/Phase-A_合成拒单注入验证方案.md`）
> 结论 31/35——卖向全绿，买向 4 红。按 0814 补遗条款 Phase A 放行证据不成立，
> 本工作包修复 D1（O-17，高）+ D2（O-18，中）后恢复完全放行。
> 改动全部位于拒单回调分支与下单副作用处，**不碰信号/闸门逻辑，盘中可施工**。

## 一、问题回顾（事实路径，带行号）

做T 买入下单（main.py:1990）采用乐观更新模型——**下单成功即改内存台账，回调再校正**：

| 下单时副作用 | 位置 | 拒单后现状 |
|---|---|---|
| `manual_position` 虚增 qty/t_qty（不加 available，T+1 语义） | :1997-2003 | ❌ **不回滚** → 幻影持仓（D1，注入实证 1400→1700） |
| `daily_buy_count[code] += 1` | :1994 | ❌ 不回补（D2，误耗当日 3 次买入配额） |
| `total_trade_count += 1` / `buy_count_per_stock` 同步 | :1996/:2004 | ❌ 不回补（D2） |
| 对照：卖向拒单已有对称回滚 `sell_rollback`（:2189-2196）+ F14 回补（:2204-2208） | — | ✅ 已绿 |

幻影持仓危害窗口：live 下 ≤30 分钟对账自愈（:495-541），但窗口期内**地板保护（:128）、
尾盘归位 excess（:1673）、TARGET/TRAIL 触发位**均以幻影 qty 计算；回测口径无自愈、污染永久。

## 二、改动设计（快照法，3 处改动）

### 改动 1：下单时留快照（main.py:1990-2004 做T 买入段）

```python
# 下单副作用之前，留存 manual_position 条目快照（含"无此条目"状态）
_snap = context.manual_position.get(gm_sym)
context._pending_buy_snapshot[gm_sym] = copy.deepcopy(_snap) if _snap else None
```

- 选快照法而非逆运算：避免成本加权逆推的浮点漂移（Phase A 文档推荐）。
- `_pending_buy_snapshot` 为 dict，INIT 时初始化 `{}`，纯日内状态无需落盘
  （进程崩溃重启后由 INIT 对账重建 manual_position，快照丢失无影响）。

### 改动 2：拒单分支加买向对称回滚（main.py:2170-2219）

在现有 `if side == 2:` 回滚块之后加对称分支：

```python
elif side == 1 and code not in context._base_ordered:
    # WP-A1: 做T 买入拒单对称回滚（底仓 BASE 走 N5 重试路径，不碰 manual_position，排除）
    _snap = context._pending_buy_snapshot.pop(cl_ord_id, None)  # 按委托键控
    if _snap is _MISSING:  # 无快照兜底：按 volume 逆减，结果 ≤0 删条目，审计标 fallback=1
        ...逆减兜底...
    else:  # 有快照：None 快照 → 整条删除；否则 deepcopy 恢复
        ...快照恢复...
    daily_buy_count[code] -= 1（下限 0）；total_trade_count -= 1（下限 0）；buy_count_per_stock[code] -= 1（下限 0）
    写 buy_rollback 审计（对照 sell_rollback 格式）
```

- **排除底仓 BASE**：`code not in _base_ordered`——BASE 下单不碰 manual_position（:1484-1488），
  其拒单归 N5 重试逻辑（:2178-2187）管，不得回滚。
- **无快照兜底**（防御：进程内遗留/版本热切换场景）：按 `volume` 逆减 qty/t_qty，
  结果 ≤0 则删除条目，并在审计里标 `fallback=1`。
- `_pending_sell_action` 拒单残留顺手 pop（OBS-1，良性清理）。

### 改动 3：成交分支丢弃快照（main.py:2048-2168 成交处理）

`status==3` 且 `side==1` 时 `_pending_buy_snapshot.pop(gm_sym, None)`——
成交即真实，快照使命结束。（部分成交 status=2 同样丢弃：部分成交量已在 fill 分支按实重算，快照仅用于"纯拒单"场景。）

## 三、边界与排除清单

| 场景 | 行为 | 依据 |
|---|---|---|
| 底仓 BASE 拒单 | **不回滚**（N5 重试 ≤3 次照旧） | BASE 无虚增副作用 |
| 部分成交后剩余被拒 | 不回滚（fill 分支已按实校正） | status=2/3 走成交分支 |
| 连续多笔同票买入后被拒 | 逐笔快照逐个回滚（快照 keyed by 委托，非按票合并） | 实现时按 order/clOrdId 键控，防串单 |
| 计数回补下限 | max(0, x−1)，不得为负 | 防御 |
| 进程崩溃于下单→回调之间 | 快照丢失，次日 INIT 对账自愈（live）；回测口径在测试头注释声明边界 | 与 OBS S4 口径一致 |

## 四、测试计划

1. **原注入测试转绿**：`tests/test_phase_a_reject_injection.py` S4a/S4e/S4f/S4h → PASS（35/35）；
   测试内"下单时副作用"夹具段同步改为调用新快照逻辑（S4-pre 保真断言保留守门）。
2. **新增用例**（同文件追加或 test_wp_a1.py）：
   - T-A1 底仓 BASE 拒单不回滚 manual_position、N5 重试计数不受影响
   - T-A2 无快照兜底路径（手工构造无快照拒单）→ 逆减回滚 + `fallback=1` 审计
   - T-A3 成交后快照丢弃（buy filled → 再拒单同票新单 → 只回滚新单）
   - T-A4 计数下限防御（daily_buy_count=0 时拒单不为负）
3. **回归**：`test_fix_20260728`（F2 拒单回归）/ `test_fix_20260731`（F9 在途守卫）/
   `test_wp_b13b14`（T6 TARGET 拒单回滚）必须保持绿；全量回归其余文件全绿
   （O-16 已知两红除外）。

## 五、影响面与回滚

- 改动 3 处、净增约 40 行 + 测试；只影响拒单回调分支与买入下单段，无信号/闸门语义变化
- 回滚：`git revert` 单 commit 即可
- 风险残余：快照键控若串单（同票并发两笔买）会回滚错笔——用 clOrdId/order_id 键控规避，
  掘金回报带 order_id，注入测试 T-A3 覆盖

## 六、验收与 Phase A 恢复条件

1. `test_phase_a_reject_injection` **35/35** + 新增 4 用例全绿 + 全回归绿
2. 文档更新：Phase-A 验证方案文档结论改"修复后通过（WP-A1 commit 号）"；O-17/O-18 关闭
3. 复盘清单 5.5 Phase A 状态恢复"完全放行（合成验证 35/35 通过，WP-A1）"
4. 次日（0828）复盘核对：盘中若发生真实拒单，buy_rollback 审计应出现（无拒单则无事发生）

## 七、施工步骤

1. main.py 三处改动（约 40 行）
2. 测试改造 + 新增用例，跑至 35/35 + 4 新绿
3. 全回归 → commit → push
4. 文档/backlog/清单三处状态更新，随日报一并归档
