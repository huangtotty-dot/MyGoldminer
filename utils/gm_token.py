# -*- coding: utf-8 -*-
"""掘金 token 加载（合并方案 P0-2，2026-08-28）。
token 不再硬编码入库（旧 token 已入 git 历史视为泄露，用户已换发）。
读取顺序：环境变量 GM_TOKEN → superTrader t_io/state/gm_config.json
（合并后同路径；路径可被 GM_CONFIG 或 SUPERTRADER_ROOT 覆盖）。
"""
import json
import os


def load_token() -> str:
    t = os.environ.get("GM_TOKEN")
    if t:
        return t
    cfg = os.environ.get("GM_CONFIG") or os.path.join(
        os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader"),
        "t_io", "state", "gm_config.json")
    try:
        return json.load(open(cfg, encoding="utf-8")).get("token")
    except Exception as e:
        raise SystemExit(f"[FATAL] 读取掘金 token 失败({cfg}): {e}")
