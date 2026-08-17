# coding=utf-8
"""
gm_bridge/feishu.py — 项目内飞书推送基建（0817 内移，去除 E:\\06_T 依赖）

背景：watcher 原依赖 E:\\06_T\\config.py 的 send_feishu_payload；0817 该文件随
E:\\06_T 暂停被清理删除，导致全天推送 sent=false（复盘 2026-08-17 异常 #1）。

webhook 读取顺序（不入库，runtime/ 已 gitignore）：
  1. 环境变量 GM_FEISHU_WEBHOOK
  2. 项目 runtime/feishu_webhook.txt（纯 URL 一行）

安全说明：自定义机器人用关键词校验（默认"掘金模拟盘"），卡片文案需含关键词。
"""

import json
import os
import urllib.request

FEISHU_KEYWORD = "掘金模拟盘"

_WEBHOOK_CACHE = None


def get_webhook() -> str:
    """读取 webhook 地址；找不到返回空串。"""
    global _WEBHOOK_CACHE
    if _WEBHOOK_CACHE is not None:
        return _WEBHOOK_CACHE
    url = os.environ.get("GM_FEISHU_WEBHOOK", "").strip()
    if not url:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "runtime", "feishu_webhook.txt")
        try:
            with open(path, encoding="utf-8") as f:
                url = f.read().strip()
        except OSError:
            url = ""
    _WEBHOOK_CACHE = url
    return url


def send_feishu_payload(payload: dict, success_log: str = "", error_prefix: str = "feishu",
                        trigger_urgent_alarm_after_success: bool = False) -> bool:
    """发送卡片/文本到自定义机器人。返回 True=成功。

    签名与旧 E:\\06_T config.send_feishu_payload 保持一致（watcher 调用处不动）。
    trigger_urgent_alarm_after_success 参数保留兼容（当前自建机器人无升级告警通道）。
    """
    url = get_webhook()
    if not url:
        print(f"[{error_prefix}] webhook 未配置（GM_FEISHU_WEBHOOK 或 runtime/feishu_webhook.txt）")
        return False
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        ok = body.get("code") == 0 or body.get("StatusCode") == 0
        if not ok:
            print(f"[{error_prefix}] 飞书返回异常: {body}")
        return ok
    except Exception as e:
        print(f"[{error_prefix}] 发送失败: {e}")
        return False
