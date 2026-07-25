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
_06T_BRIDGE = r"E:\06_T\t_io\gm_bridge"
_LOCAL_BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gm_bridge")

def _bridge_dir() -> str:
    """桥目录：E:\06_T\t_io\gm_bridge 优先，不存在则用本地 gm_bridge"""
    try:
        if os.path.exists(r"E:\06_T"):
            d = _06T_BRIDGE
            os.makedirs(d, exist_ok=True)
            return d
    except Exception:
        pass
    os.makedirs(_LOCAL_BRIDGE, exist_ok=True)
    return _LOCAL_BRIDGE

BRIDGE_DIR = _bridge_dir()


def _events_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"events_{date_str}.jsonl")


def _heartbeat_path() -> str:
    return os.path.join(BRIDGE_DIR, "heartbeat.json")


def _kill_switch_path() -> str:
    return os.path.join(BRIDGE_DIR, "KILL_SWITCH")


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


def write_risk(time_str: str, kind: str, detail: str = ""):
    """风控事件（仓位拦截/地板保护/PANIC/熔断/急停）"""
    _append_jsonl(_events_path(), {
        "event": "risk",
        "time": time_str,
        "kind": kind,
        "detail": detail,
    })


def write_heartbeat(time_str: str, bar: str, positions: Dict[str, Any],
                    cash: float = 0.0, index_regime: str = "", index_score: float = 0.0):
    """每分钟心跳"""
    _write_json(_heartbeat_path(), {
        "event": "heartbeat",
        "time": time_str,
        "bar": bar,
        "positions": positions,
        "cash": cash,
        "index_regime": index_regime,
        "index_score": index_score,
    })


# ── 公开 API：风控文件 ──

def check_kill_switch() -> bool:
    """检查 KILL_SWITCH 文件是否存在。存在 → 返回 True（禁止新开仓）"""
    try:
        return os.path.exists(_kill_switch_path())
    except Exception:
        return False
