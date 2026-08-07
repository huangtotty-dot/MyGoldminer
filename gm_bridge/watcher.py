# coding=utf-8
"""
gm_bridge/watcher.py — 事件桥监听进程（去重/限流/心跳监控/旁路风控/飞书推送）

独立进程，通过 tail 事件文件实现实时通知。
不与 gm3 策略进程耦合——watcher 挂掉不影响交易。

使用方式:
  python gm_bridge/watcher.py [--date YYYYMMDD]

依赖:
  - E:\06_T\config.py 的 send_feishu_payload（若不可用则降级为 print）
"""

import json
import os
import sys
import time
import signal
from datetime import datetime, timedelta
from typing import Dict, Set, Optional

# ── 桥路径（0806 迁移：与手工做T系统分离，统一由 writer 决定） ──
try:
    from writer import BRIDGE_DIR
except ImportError:  # python -m gm_bridge.watcher 方式启动
    from gm_bridge.writer import BRIDGE_DIR

# ── 股票名称映射 ──
STOCK_NAMES = {
    "000988": "华工科技",
    "600481": "双良节能",
    "600176": "中国巨石",
    "603667": "五洲新春",
    "588170": "科创芯片ETF",
    "300153": "科泰电源",
    "300364": "中文在线",
}


def _stock_label(code: str) -> str:
    name = STOCK_NAMES.get(code, "")
    return f"{name}({code})" if name else code


def _events_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"events_{date_str}.jsonl")


def _heartbeat_path() -> str:
    return os.path.join(BRIDGE_DIR, "heartbeat.json")


# ── 飞书推送（尝试复用 E:\06_T 基建） ──
FEISHU_AVAILABLE = False
_send_feishu = None
_FEISHU_KEYWORD = "掘金模拟盘"

try:
    _06T = r"E:\06_T"
    if _06T not in sys.path:
        sys.path.insert(0, _06T)
    import config as _cfg
    _send_feishu = _cfg.send_feishu_payload
    _FEISHU_KEYWORD = getattr(_cfg, 'FEISHU_KEYWORD', '掘金模拟盘')
    FEISHU_AVAILABLE = True
except Exception as e:
    print(f"[watcher] 飞书不可用 ({e}), 降级为 stdout")


def _log_push(title: str, content: str, level: str, sent: bool):
    """推送留痕（0806 红日整改）：每次推送尝试落 pushes_YYYYMMDD.jsonl，
    C1-5 对账不再依赖做T系统共用的 sender 日志。"""
    try:
        rec = {"time": datetime.now().isoformat(timespec="seconds"),
               "title": title, "level": level, "sent": sent,
               "content": content[:300]}
        path = os.path.join(BRIDGE_DIR, f"pushes_{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _push(title: str, content: str, level: str = "info"):
    """推送通知（飞书优先，不可用时 print）；每次尝试落 pushes 留痕"""
    if FEISHU_AVAILABLE:
        try:
            template = {"green": "green", "orange": "orange", "red": "red"}.get(level, "blue")
            card = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": template,
                    },
                    "elements": [{"tag": "markdown", "content": content}],
                },
            }
            sent = bool(_send_feishu(payload=card, success_log="", error_prefix="watcher",
                                     trigger_urgent_alarm_after_success=(level == "red")))
            _log_push(title, content, level, sent)
            return
        except Exception:
            pass
    _log_push(title, content, level, False)
    print(f"[watcher] {level.upper()} {title}: {content[:200]}")


# ── 去重与限流 ──
_seen_signals: Set[str] = set()
_last_push: Dict[str, float] = {}  # 同类事件 60s 不重复推
_last_heartbeat_ok = True


def _dedup_key(rec: dict) -> str:
    """去重键：event + code + action + bar"""
    return f"{rec.get('event')}|{rec.get('code','')}|{rec.get('action',rec.get('side',''))}|{rec.get('time','')}"


def _should_push(event_type: str, dedup_key: str, code: str = "") -> bool:
    if dedup_key in _seen_signals:
        return False
    _seen_signals.add(dedup_key)
    now = time.time()
    # 限流键 = event_type + code（每票独立窗口，A票不挤B票）
    _throttle_key = f"{event_type}|{code}" if code else event_type
    last = _last_push.get(_throttle_key, 0)
    if now - last < 60:
        return False
    _last_push[_throttle_key] = now
    return True


# ── 旁路风控 ──
_positions: Dict[str, Dict] = {}
_cash_estimate: float = 200000


def _track_position(rec: dict):
    global _positions, _cash_estimate
    code = rec.get("code", "")
    if rec["event"] == "fill":
        side = rec.get("side", "")
        qty = rec.get("qty", 0)
        price = rec.get("price", 0)
        if code not in _positions:
            _positions[code] = {"qty": 0, "cost": 0}
        if side == "BUY":
            old_q = _positions[code]["qty"]
            old_c = _positions[code]["cost"]
            new_q = old_q + qty
            _positions[code]["qty"] = new_q
            _positions[code]["cost"] = (old_c * old_q + price * qty) / new_q if new_q > 0 else price
            _cash_estimate -= qty * price
        else:  # SELL
            _positions[code]["qty"] = max(0, _positions[code]["qty"] - qty)
            _cash_estimate += qty * price
        # 仓位超限告警
        total_mv = sum(p["qty"] * p["cost"] for p in _positions.values())
        total_eq = _cash_estimate + total_mv
        if total_eq > 0 and total_mv / total_eq > 0.80:
            _push("⚠️ 仓位超限", f"{code} 市值占比 {total_mv/total_eq:.0%} > 80%", "orange")


# ── 心跳监控 ──
_last_hb_ts: float = time.time()


def _in_trading_window(dt: datetime) -> bool:
    """O-02(2026-08-07 W32表决): 仅在交易时段内允许心跳告警——
    午休(11:30-13:00)与收盘后策略本就不写心跳，0807 每日两条假警报(11:39/15:08)。
    窗口留缓冲：09:33 起（等首根 bar 心跳落地）、11:35/15:05 止。"""
    t = dt.time()
    from datetime import time as _t
    return (_t(9, 33) <= t <= _t(11, 35)) or (_t(12, 55) <= t <= _t(15, 5))


def _check_heartbeat():
    global _last_hb_ts, _last_heartbeat_ok
    now = time.time()
    try:
        if os.path.exists(_heartbeat_path()):
            mtime = os.path.getmtime(_heartbeat_path())
            _last_hb_ts = mtime
    except Exception:
        pass
    gap = now - _last_hb_ts
    # O-02: 非交易时段不报警，并清除报警状态避免开盘即误报
    if not _in_trading_window(datetime.now()):
        _last_heartbeat_ok = True
        return
    if gap > 600 and _last_heartbeat_ok:
        _last_heartbeat_ok = False
        _push("🚨 心跳中断", f"最后心跳 {gap/60:.0f} 分钟前，策略可能已停止", "red")
    elif gap <= 600 and not _last_heartbeat_ok:
        _last_heartbeat_ok = True
        _push("✅ 心跳恢复", f"心跳已恢复 ({gap:.0f}s)", "green")


# ── 事件处理 ──
def handle_event(rec: dict):
    event = rec.get("event", "")
    dk = _dedup_key(rec)

    code = rec.get("code", "")
    label = _stock_label(code)

    if event == "signal":
        if _should_push("signal", dk, code):
            action_cn = {"BUY_LOW": "低吸", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出", "ADD_POS": "加仓"}.get(rec.get("action"), rec.get("action"))
            emoji = "🟢" if "BUY" in str(rec.get("action", "")) else "🔴"
            _push(
                f"{emoji} {action_cn}信号 — {label}",
                f"动作: {rec['action']} | 评分: {rec.get('score',0):.0f}分 | "
                f"持仓: {rec.get('pos_qty',0)}股",
                "green" if "BUY" in str(rec.get("action", "")) else "orange"
            )

    elif event == "fill":
        _track_position(rec)
        if _should_push("fill", dk, code):
            side = rec.get("side", "")
            emoji = "🔵" if side == "BUY" else "🔴"
            side_cn = "买入" if side == "BUY" else "卖出"
            _push(
                f"{emoji} 成交 — {label}",
                f"{side_cn} {rec.get('qty',0)}股 @ {rec.get('price',0):.2f} | "
                f"成交后持仓: {rec.get('pos_after', '?')}股",
                "green"
            )

    elif event == "reject":
        side_cn = "买入" if rec.get("side") == "BUY" else "卖出"
        _push(
            f"❌ 委托被拒 — {label}",
            f"{side_cn} {rec.get('qty',0)}股 | "
            f"原因: {rec.get('reason','未知')}",
            "red"
        )

    elif event == "risk":
        kind = rec.get("kind", "")
        if kind == "kill_switch":
            _push(f"🛑 KILL_SWITCH — {label}", rec.get("detail", ""), "red")
        elif kind in ("position_limit", "floor_protection"):
            pass  # 不推飞书（噪音），仅记录
        elif kind == "cash_insufficient":
            if _should_push("risk", dk, code):
                _push(f"💰 现金不足 — {label}", rec.get("detail", ""), "orange")
        else:
            if _should_push("risk", dk, code):
                _push(f"⚠️ 风控 — {label}", f"{kind}: {rec.get('detail','')}", "orange")

    elif event == "order":
        pass  # 订单事件不单独推送，等 fill 再推

    elif event == "heartbeat":
        _last_hb_ts = time.time()


# ── 主循环 ──
def run(date_str: str = None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    path = _events_path(date_str)
    print(f"[watcher] 监听 {path}")
    print(f"[watcher] 飞书: {'可用' if FEISHU_AVAILABLE else '不可用(stdout)'}")

    last_size = 0
    while True:
        try:
            if os.path.exists(path):
                size = os.path.getsize(path)
                if size > last_size:
                    with open(path, "r", encoding="utf-8") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                handle_event(rec)
                            except json.JSONDecodeError:
                                pass
                    last_size = size
        except Exception as e:
            print(f"[watcher] 读取异常: {e}")

        _write_self_heartbeat()
        _check_heartbeat()
        time.sleep(2)


def _write_self_heartbeat():
    """watcher 自心跳（0806 红日整改）：供策略侧双向看门狗检测 watcher 死活。"""
    try:
        rec = {"event": "watcher_heartbeat",
               "time": datetime.now().isoformat(timespec="seconds"),
               "pid": os.getpid()}
        with open(os.path.join(BRIDGE_DIR, "watcher_heartbeat.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass


if __name__ == "__main__":
    date_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--date" else None
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    run(date_arg)
