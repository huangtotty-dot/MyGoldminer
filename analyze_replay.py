# coding=utf-8
"""
analyze_replay.py — WP-B 三窗口回放包验收分析（K3 审计辅助）v2

v2: 按 backtrace 事件流逐笔重放 持仓/成本台账（与策略同口径：买入加权、卖出不改成本），
    使 TRAIL 退出均价 vs 成本、亏损 SELL_HIGH、TARGET 同档重复 三项判定精确化。

用法: python analyze_replay.py <场景目录>
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date


def load(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def replay_ledger(bt):
    """逐事件重放，返回 (trades, episodes)
    trades: 每笔卖出 {code, qty, price, action, time, cost_at_sell, pos_after}
    episodes: 每段持仓期 {code, start_idx, end_idx, target_sold: bool}
    """
    pos = defaultdict(int)
    cost = defaultdict(float)
    trades = []
    episodes = []
    ep_open = {}
    for i, r in enumerate(bt):
        ev = r.get("event")
        code = r.get("code")
        if not code:
            continue
        if ev == "reconcile_init":
            pos[code] = int(r.get("qty", 0))
            cost[code] = float(r.get("cost", 0))
        elif ev in ("buy", "base_order"):
            q = int(r.get("qty", 0) or 0)
            p = float(r.get("price", 0) or 0)
            if q > 0 and p > 0:
                old_q = pos[code]
                new_q = old_q + q
                cost[code] = (cost[code] * old_q + p * q) / new_q if new_q > 0 else p
                pos[code] = new_q
                if old_q == 0:
                    ep_open[code] = {"start": i, "target_sold": False}
        elif ev == "sell":
            q = int(r.get("qty", 0) or 0)
            p = float(r.get("price", 0) or 0)
            act = r.get("action") or "(未标注)"
            trades.append({"code": code, "qty": q, "price": p, "action": act,
                           "time": str(r.get("time", "")), "cost_at_sell": cost[code],
                           "pos_before": pos[code]})
            if act == "TARGET_SELL" and code in ep_open:
                ep_open[code]["target_sold"] = True
                ep_open[code].setdefault("target_times", []).append(str(r.get("time", "")))
            pos[code] = max(0, pos[code] - q)
            if pos[code] == 0 and code in ep_open:
                ep = ep_open.pop(code)
                ep["end"] = i
                episodes.append(ep)
    for code, ep in ep_open.items():
        ep["end"] = None
        episodes.append(ep)
    return trades, episodes


def main(scen_dir):
    bt = load(os.path.join(scen_dir, "backtrace.jsonl"))
    scen = os.path.basename(scen_dir.rstrip("\\/"))
    trades, episodes = replay_ledger(bt)

    print(f"===== 场景 {scen} =====")
    print(f"backtrace {len(bt)} 条 ｜ 卖出成交 {len(trades)} 笔 ｜ 持仓期 {len(episodes)} 段")

    chan = defaultdict(lambda: [0, 0])
    for t in trades:
        chan[t["action"]][0] += 1
        chan[t["action"]][1] += t["qty"]
    print("\n[通道分布]（成交卖出）")
    for a, (c, q) in sorted(chan.items(), key=lambda x: -x[1][0]):
        print(f"  {a:<14} {c:>3} 笔 / {q} 股")

    if scen == "fix2":
        trails = [t for t in trades if t["action"] == "TRAIL_SELL"]
        print("\n[fix2 验收] TRAIL 退出 vs 成本x1.07（逐笔重放成本）：")
        tq = ta = 0
        allok = True
        for t in trails:
            ratio = t["price"] / t["cost_at_sell"] if t["cost_at_sell"] > 0 else 0
            ok = ratio >= 1.07
            allok = allok and ok
            tq += t["qty"]
            ta += t["price"] * t["qty"]
            print(f"  {t['time'][:16]} {t['qty']}股 @ {t['price']:.2f} 成本 {t['cost_at_sell']:.2f} 比 {ratio:.3f} {'✅' if ok else '❌'}")
        if trails:
            wavg = ta / tq
            wc = sum(t["cost_at_sell"] * t["qty"] for t in trails) / tq
            print(f"  加权: 退出均价 {wavg:.2f} vs 加权成本 {wc:.2f} x1.07={wc*1.07:.2f} → {'PASS' if wavg >= wc*1.07 else 'FAIL'}")
        else:
            print("  ⚠️ 无 TRAIL_SELL 触发（样本缺口）")

    if scen == "fix3":
        tgts = [t for t in trades if t["action"] == "TARGET_SELL"]
        print(f"\n[fix3 验收] TARGET_SELL 落袋 {len(tgts)} 笔 → {'PASS(>=1)' if tgts else 'FAIL'}")
        # 同档重复：同一段持仓期内 TARGET_SELL >1 次 = 违规
        repeats = [ep for ep in episodes if len(ep.get("target_times", [])) > 1]
        print(f"[fix3 验收] 同档重复持仓期数={len(repeats)} → {'PASS(0重复)' if not repeats else 'FAIL'}")
        for t in tgts:
            print(f"  {t['time'][:16]} {t['qty']}股 @ {t['price']:.2f} (成本 {t['cost_at_sell']:.2f}, 盈 {(t['price']/t['cost_at_sell']-1)*100:.1f}%)")

    if scen == "fix4":
        loss_sh = [t for t in trades if t["action"] == "SELL_HIGH" and t["cost_at_sell"] > 0
                   and t["price"] < t["cost_at_sell"]]
        print(f"\n[fix4-a] 亏损 SELL_HIGH={len(loss_sh)} → {'PASS' if not loss_sh else 'FAIL'}")
        for t in loss_sh[:10]:
            print(f"  {t['time'][:16]} {t['code']} @ {t['price']:.2f} < 成本 {t['cost_at_sell']:.2f}")

        sell_days = defaultdict(set)
        for t in trades:
            if t["cost_at_sell"] > 0 and t["price"] < t["cost_at_sell"]:
                sell_days[t["code"]].add(t["time"][:10])
        grinder = {}
        for code, days in sell_days.items():
            ds = sorted(d for d in days if d)
            run = best = 1
            for i in range(1, len(ds)):
                y0, m0, d0 = map(int, ds[i-1].split("-"))
                y1, m1, d1 = map(int, ds[i].split("-"))
                if (date(y1, m1, d1) - date(y0, m0, d0)).days <= 3:
                    run += 1
                    best = max(best, run)
                else:
                    run = 1
            if best >= 5:
                grinder[code] = best
        print(f"[fix4-b] 连续>=5日割肉序列: {grinder if grinder else '无'} → {'PASS' if not grinder else 'FAIL'}")

        early = [t for t in trades if "09:3" in t["time"][11:16] and t["time"][11:16] < "09:35"
                 and t["action"] not in ("PANIC_SELL", "TRAIL_SELL", "TREND_EXIT")]
        print(f"[fix4-c] 09:35前非保护卖出={len(early)} → {'PASS' if not early else 'FAIL'}")
        for t in early[:10]:
            print(f"  {t['time'][:16]} {t['code']} {t['action']} @ {t['price']:.2f}")
        blocked = [r for r in bt if r.get("event") == "morning_sell_blocked"]
        print(f"[附] morning_sell_blocked {len(blocked)} 条")

    infl = [r for r in bt if r.get("event") == "inflight_skip"]
    rej = [r for r in bt if r.get("event") == "sell_rollback"]
    print(f"\n[F9 观测] inflight_skip {len(infl)} ｜ sell_rollback {len(rej)}")


if __name__ == "__main__":
    main(sys.argv[1])
