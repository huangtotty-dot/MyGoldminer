# coding=utf-8
"""
gm_bridge/ops_guard.py — 运维自举与双向看门狗（2026-08-06 红日整改）

背景：0806 策略 09:42 静默僵死 5.3 小时，watcher 告警从未发出（进程未启动或同样僵死），
且控制台日志未落盘导致根因无法追查。本模块解决三件事：

1. 控制台日志落盘：init 时把 stdout/stderr tee 到 logs/strategy_YYYYMMDD.log，
   崩溃 traceback / 挂起前最后的 print 全部留痕（L1/L2 缺口的策略侧修复）。
2. watcher 自动拉起：点掘金终端"运行"按钮即由策略进程自动拉起 watcher，
   无需单独双击 start_monitor.bat。
3. 双向看门狗：watcher 每 2s 写 watcher_heartbeat.json；策略每根 bar 检查，
   心跳缺失/过期 >90s 自动重生 watcher（冷却 300s 防抖动）。
   反向（watcher 盯策略心跳）由 watcher._check_heartbeat 既有逻辑承担。

设计约束：
- watcher 僵死/未启动不影响交易链路（拉起失败仅 print，不抛异常）；
- 所有动作落 print → 进 strategy 日志，供复盘归因。
"""

import os
import subprocess
import sys
import time
from datetime import datetime

_WATCHER_HB_NAME = "watcher_heartbeat.json"
_STALE_SECONDS = 90          # watcher 心跳超过 90s 未刷新视为死亡
_RESPAWN_COOLDOWN = 300      # 重生冷却，防反复拉起
_state = {"last_respawn": 0.0, "bootstrapped": False}

# 桥目录统一由 writer 决定（0806 迁移：项目内 runtime/bridge）
try:
    from gm_bridge.writer import BRIDGE_DIR as _BRIDGE_DIR
except ImportError:
    from writer import BRIDGE_DIR as _BRIDGE_DIR


class _TeeStream:
    """同时写原控制台与日志文件的流。"""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._fh.write(data)
            self._fh.flush()
        except Exception:
            pass
        return len(data)

    def flush(self):
        for s in (self._stream, self._fh):
            try:
                s.flush()
            except Exception:
                pass


def watcher_heartbeat_path():
    return os.path.join(_BRIDGE_DIR, _WATCHER_HB_NAME)


def bootstrap_logging(project_dir):
    """stdout/stderr tee 到 logs/strategy_YYYYMMDD.log（幂等）。"""
    if _state["bootstrapped"]:
        return
    _state["bootstrapped"] = True
    try:
        log_dir = os.path.join(project_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "strategy_%s.log" % datetime.now().strftime("%Y%m%d"))
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        fh.write("\n===== strategy run start %s pid=%d =====\n"
                 % (datetime.now().isoformat(timespec="seconds"), os.getpid()))
        sys.stdout = _TeeStream(sys.stdout, fh)
        sys.stderr = _TeeStream(sys.stderr, fh)
        print("[ops] 控制台日志已落盘: %s" % log_path)
    except Exception as e:
        # 落盘失败不阻断策略启动
        try:
            sys.__stdout__.write("[ops] 日志落盘失败: %s\n" % e)
        except Exception:
            pass


def watcher_alive():
    """watcher 心跳文件存在且 90s 内刷新过。"""
    try:
        return (time.time() - os.path.getmtime(watcher_heartbeat_path())) <= _STALE_SECONDS
    except Exception:
        return False


def ensure_watcher(project_dir):
    """watcher 心跳缺失/过期则自动拉起（冷却 300s）。每根 bar 调用，代价 = 一次 stat。"""
    if watcher_alive():
        return
    now = time.time()
    if now - _state["last_respawn"] < _RESPAWN_COOLDOWN:
        return
    _state["last_respawn"] = now
    watcher_py = os.path.join(project_dir, "gm_bridge", "watcher.py")
    if not os.path.exists(watcher_py):
        print("[ops] watcher 拉起失败: 文件不存在 %s" % watcher_py)
        return
    stdout_path = os.path.join(project_dir, "logs", "watcher_stdout.log")
    try:
        os.makedirs(os.path.dirname(stdout_path), exist_ok=True)
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(stdout_path, "ab") as out:
            try:
                subprocess.Popen(["python", watcher_py], cwd=project_dir,
                                 stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
            except FileNotFoundError:
                # PATH 无 python 时退回当前解释器（掘金终端内嵌 Python 也可跑 watcher，
                # 其依赖仅 json/os/requests 及 E:\06_T\config）
                subprocess.Popen([sys.executable, watcher_py], cwd=project_dir,
                                 stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
        print("[ops] watcher 心跳缺失/过期(>%ds)，已自动拉起，stdout→%s"
              % (_STALE_SECONDS, stdout_path))
    except Exception as e:
        print("[ops] watcher 自动拉起失败: %s" % e)
