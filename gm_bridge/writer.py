# coding=utf-8
"""
gm_bridge/writer.py — 事件桥写入（仅写文件，不碰网络/飞书）

事件 schema 见 docs/模拟盘实施方案_20260727.md 附录 A：
  signal: 信号生成
  order:  委托发出
  fill:   全部成交
  reject: 拒单/撤单
  risk:   风控事件 (仓位/地板/PANIC/熔断/急停)
  heartbeat: 每分钟心跳（仓位+现金）
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional

# ── 桥目录配置 ──
# 0806 owner 决策：模拟盘运行时数据与 E:\06_T 手工做T系统彻底分离——
# 桥目录固定在项目内 runtime/bridge（可用环境变量 GM_BRIDGE_DIR 覆盖）。
# 历史数据（2026-07-27 ~ 08-06）留存在 E:\06_T\t_io\gm_bridge，复盘证据已逐日归档 docs/。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _bridge_dir() -> str:
    env = (os.environ.get("GM_BRIDGE_DIR") or "").strip()
    d = env if env else os.path.join(_PROJECT_ROOT, "runtime", "bridge")
    os.makedirs(d, exist_ok=True)
    return d

BRIDGE_DIR = _bridge_dir()


def _events_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"events_{date_str}.jsonl")


def _heartbeat_path() -> str:
    return os.path.join(BRIDGE_DIR, "heartbeat.json")


def _kill_switch_path() -> str:
    return os.path.join(BRIDGE_DIR, "KILL_SWITCH")


def _snapshot_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"signals_{date_str}.jsonl")


# ── 写入工具 ──

def _append_jsonl(path: str, rec: dict):
    """安全追加一行 JSON"""
    try:
        rec["_ts"] = time.time()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _write_json(path: str, data):
    """整文件覆写（heartbeat 用）"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        pass


# ── 公开 API：事件写入 ──

def write_signal(time_str: str, code: str, action: str, score: float,
                 reasons: list = None, pos_qty: int = 0):
    """信号生成事件"""
    _append_jsonl(_events_path(), {
        "event": "signal",
        "time": time_str,
        "code": code,
        "action": action,
        "score": score,
        "reasons": reasons or [],
        "pos_qty": pos_qty,
    })


def write_order(time_str: str, code: str, side: str, qty: int, price: float,
                order_id: str = "", order_type: str = "MKT"):
    """委托发出事件"""
    _append_jsonl(_events_path(), {
        "event": "order",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "order_id": str(order_id),
        "order_type": order_type,
    })


def write_fill(time_str: str, code: str, side: str, qty: int, price: float,
               order_id: str = "", pos_after: int = 0):
    """全部成交事件"""
    _append_jsonl(_events_path(), {
        "event": "fill",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "order_id": str(order_id),
        "pos_after": pos_after,
    })


def write_reject(time_str: str, code: str, side: str, qty: int,
                 reason: str = "", raw: dict = None):
    """拒单/撤单事件"""
    _append_jsonl(_events_path(), {
        "event": "reject",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "reason": reason,
        "raw": str(raw) if raw else "",
    })


def write_risk(time_str: str, kind: str, detail: str = "", code: str = ""):
    """风控事件（仓位拦截/地板保护/PANIC/熔断/急停）"""
    _append_jsonl(_events_path(), {
        "event": "risk",
        "time": time_str,
        "code": code,
        "kind": kind,
        "detail": detail,
    })


def write_heartbeat(time_str: str, bar: str, positions: Dict[str, Any],
                    cash: float = 0.0, index_regime: str = "", index_score: float = 0.0):
    """每分钟心跳（同时写实时覆盖文件 + 追加历史jsonl）"""
    rec = {
        "event": "heartbeat",
        "time": time_str,
        "bar": bar,
        "positions": positions,
        "cash": cash,
        "index_regime": index_regime,
        "index_score": index_score,
    }
    _write_json(_heartbeat_path(), rec)
    # L3: 追加历史时序快照(不覆盖)
    _append_jsonl(os.path.join(BRIDGE_DIR, f"heartbeat_{time_str[:10]}.jsonl"), rec)


def write_snapshot(time_str: str, code: str, price: float, bar: str = "",
                   buy_score=None, sell_score=None, gate: str = "",
                   gate_detail: str = "", action: str = "", pos_qty: int = 0):
    """全票每 bar 决策快照（0806 红日整改）：
    16 票 × 每 bar 一条 → signals_YYYYMMDD.jsonl。
    回放"策略活着会不会有卖点 / 改阈值会怎样"类问题的数据底座。
    纯监控产物，不参与决策；仅 MODE_LIVE 调用（回测省 I/O）。"""
    rec = {
        "event": "snapshot",
        "time": time_str,
        "bar": bar,
        "code": code,
        "price": price,
        "pos_qty": pos_qty,
        "gate": gate,
    }
    if buy_score is not None:
        rec["buy_score"] = round(float(buy_score), 1)
    if sell_score is not None:
        rec["sell_score"] = round(float(sell_score), 1)
    if gate_detail:
        rec["gate_detail"] = gate_detail[:120]
    if action:
        rec["action"] = action
    _append_jsonl(_snapshot_path(), rec)


# ── 公开 API：风控文件 ──

def check_kill_switch() -> bool:
    """检查 KILL_SWITCH 文件是否存在。存在 → 返回 True（禁止新开仓）"""
    try:
        return os.path.exists(_kill_switch_path())
    except Exception:
        return False
