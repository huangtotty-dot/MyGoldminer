# coding=utf-8
"""
signals/position_builder.py — WP-B20 双通道建仓闸（同步自 E:\\superTrader\\position_builder.py）

owner 2026-08-19 决策：完全替换 goldminer 的 G4 支撑建仓闸，同步 superTrader 的
W33 A1 双通道建仓判定：
  通道一 冰点反转（左侧）：转向确认(必要) + BOLL冰点 + 缩量；RSI 展示层
  通道二 突破跟随（右侧）：突破箱体 + 放量确认 + 趋势多头
仅 verdict=signal（冰点 80 / 突破 70 分档）放行建仓；approaching 仅留痕观察。

口径：日线指标冻结于昨收（daily_ctx 由 main._refresh_daily_ctx 产出，end_time=昨日），
盘中变量仅现价 price（箱体突破）与 5 分钟冰点（A2 盘中择时加分项）。
本模块为纯函数，无 IO；goldminer 侧仅 main.on_bar 的 BASE 建仓块消费。
"""
import math

import numpy as np
import pandas as pd

from data.indicators import resample_to_5min, add_indicators


# ═══════════════════════════════════════════
# 通道一 冰点反转（左侧）— 日线条件
# ═══════════════════════════════════════════

def check_turn_confirm(dc):
    """转向确认（必要项）：近5日MACD金叉 或 收盘站上MA5（二选一即过）。"""
    golden = dc.get("daily_macd_golden")
    price_ref = dc.get("daily_price_ref")
    ma5 = dc.get("daily_ma5")
    if golden is None or price_ref is None or ma5 is None:
        return False, "缺日线MACD/MA5数据", True
    macd_ok = bool(golden)
    ma5_ok = float(price_ref) > float(ma5)
    passed = macd_ok or ma5_ok
    return passed, f"转向确认={'通过' if passed else '未过'}（金叉={macd_ok} 站上MA5={ma5_ok}）"


def check_boll_lower(dc, max_pct=0.15):
    """日线 BOLL 冰点（情绪冰点）: bb_pct ≤ max_pct（接近/跌破下轨）。"""
    bb = dc.get("daily_boll_pct")
    if bb is None or (isinstance(bb, float) and math.isnan(bb)):
        return False, "缺日线BOLL数据", True
    passed = float(bb) <= max_pct
    return passed, f"日线bb_pct={float(bb):.3f}（需≤{max_pct}）"


def check_volume_shrink(dc, ratio_max=0.8):
    """日线缩量止跌: 当日量 < 5日均量 × ratio_max。"""
    vt, vm = dc.get("daily_vol_today"), dc.get("daily_vol_ma5")
    if vt is None or vm is None or vm <= 0:
        return False, "缺日线量能数据", True
    ratio = float(vt) / float(vm)
    passed = ratio < ratio_max
    return passed, f"日线量比={ratio:.2f}（需<{ratio_max}）", False


def check_rsi_oversold(dc):
    """日线 RSI 超卖（展示层，不计分）: RSI < 35。"""
    rsi = dc.get("daily_rsi14")
    if rsi is None or (isinstance(rsi, float) and math.isnan(rsi)):
        return False, "缺日线RSI数据", True
    passed = float(rsi) < 35
    return passed, f"日线rsi={float(rsi):.1f}（展示层，<35）"


# ═══════════════════════════════════════════
# 通道二 突破跟随（右侧）— 日线条件
# ═══════════════════════════════════════════

def check_volume_confirm(dc, ratio_min=1.5):
    """日线放量确认: 当日量 > 5日均量 × ratio_min。"""
    vt, vm = dc.get("daily_vol_today"), dc.get("daily_vol_ma5")
    if vt is None or vm is None or vm <= 0:
        return False, "缺日线量能数据", True
    ratio = float(vt) / float(vm)
    passed = ratio > ratio_min
    return passed, f"日线量比={ratio:.2f}（需>{ratio_min}）", False


def check_trend_bull(dc):
    """日线趋势多头: 当前 DIF > DEA。"""
    dif, dea = dc.get("daily_macd_dif"), dc.get("daily_macd_dea")
    if dif is None or dea is None:
        return False, "缺日线DIF/DEA数据", True
    passed = float(dif) > float(dea)
    return passed, f"DIF={float(dif):.4f} / DEA={float(dea):.4f}（需DIF>DEA）"


# ═══════════════════════════════════════════
# 箱体突破（右侧）
# ═══════════════════════════════════════════

def _detect_boxes_simple(ohlc, n_keep=3):
    """近60日滑窗箱体检测（superTrader _detect_boxes_simple 精简：窗24天/分位88/12/重叠合并）。"""
    if not ohlc or len(ohlc.get("close", [])) < 24:
        return []
    closes = np.asarray(ohlc["close"], dtype=float)
    highs = np.asarray(ohlc["high"], dtype=float)
    lows = np.asarray(ohlc["low"], dtype=float)
    n = len(closes)
    last_close = float(closes[-1])
    WIN = 24
    box_flags = np.zeros(n, dtype=bool)
    for start in range(0, n - WIN + 1, 3):
        seg = closes[start:start + WIN]
        slope = np.polyfit(np.arange(WIN), seg, 1)[0]
        rel_slope = abs(slope) / (seg.mean() or 1e-9)
        up = float(np.percentile(highs[start:start + WIN], 88))
        dn = float(np.percentile(lows[start:start + WIN], 12))
        up_touch = int(np.sum(highs[start:start + WIN] >= up * 0.992))
        dn_touch = int(np.sum(lows[start:start + WIN] <= dn * 1.008))
        w = (up - dn) / (seg.mean() or 1e-9) * 100
        if rel_slope < 0.005 and 3.0 <= w <= 22.0 and up_touch >= 2 and dn_touch >= 2:
            box_flags[start:start + WIN] = True
    boxes = {}
    i = 0
    while i < n:
        if not box_flags[i]:
            i += 1
            continue
        j = i
        while j < n and box_flags[j]:
            j += 1
        if j - i >= 20:
            up = float(np.percentile(highs[i:j], 88))
            dn = float(np.percentile(lows[i:j], 12))
            up_touch = int(np.sum(highs[i:j] >= up * 0.992))
            dn_touch = int(np.sum(lows[i:j] <= dn * 1.008))
            w = (up - dn) / (closes[i:j].mean() or 1e-9) * 100
            if 3.0 <= w <= 22.0 and up_touch >= 2 and dn_touch >= 2:
                boxes[(round(up, 3), round(dn, 3))] = {"low": round(dn, 3), "high": round(up, 3)}
        i = j
    merged = []
    for b in list(boxes.values()):
        hit = next((m for m in merged if
                    min(b["high"], m["high"]) - max(b["low"], m["low"]) > min(b["high"]-b["low"], m["high"]-m["low"]) * 0.5), None)
        if hit:
            hit["low"] = min(hit["low"], b["low"])
            hit["high"] = max(hit["high"], b["high"])
        else:
            merged.append(dict(b))
    for b in merged:
        if b["low"] <= last_close <= b["high"]:
            b["rel"] = 0
        elif last_close > b["high"]:
            b["rel"] = -1
        else:
            b["rel"] = -2
    merged.sort(key=lambda b: (0 if b["rel"] == 0 else 1, -b["high"]))
    return merged[:n_keep]


def check_box_breakout(ohlc, price, min_pct=0.3, max_pct=8.0, retest_max_pct=2.0):
    """突破当前箱体上沿（只认 rel=0 当前箱体；rel=-1 突破后回踩）。
    返回 (broken, box_high, pct_above, detail)。"""
    cur = float(price) if price else 0.0
    boxes = _detect_boxes_simple(ohlc)
    rel0 = next((b for b in boxes if b.get("rel") == 0), None)
    if rel0 and rel0.get("high") and cur > rel0["high"]:
        pct = (cur - rel0["high"]) / rel0["high"] * 100
        if min_pct <= pct <= max_pct:
            return True, rel0["high"], round(pct, 2), f"突破箱体上沿{rel0['high']}，超出{pct:.2f}%"
        if pct > max_pct:
            return False, rel0["high"], round(pct, 2), f"强势突破(>{pct:.2f}%)"
        return False, rel0["high"], round(pct, 2), f"未达突破阈值({pct:.2f}%)"
    prev = [b for b in boxes if b.get("rel") == -1]
    if prev:
        top = max(prev, key=lambda b: b["high"])
        if top.get("high") and cur > top["high"]:
            pct = (cur - top["high"]) / top["high"] * 100
            if min_pct <= pct <= retest_max_pct:
                return True, top["high"], round(pct, 2), f"突破后回踩({pct:.2f}%)"
    return False, None, None, "未突破箱体"


# ═══════════════════════════════════════════
# 5 分钟冰点（A2 盘中择时加分项）
# ═══════════════════════════════════════════

def build_m5_df(rows):
    """从 1 分钟 bar rows（bar_cache 的 time/open/high/low/close/volume/amount）构造 5 分钟指标 df。"""
    if not rows or len(rows) < 25:
        return None
    try:
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        if df.empty:
            return None
        df5 = resample_to_5min(df)
        if df5.empty:
            return None
        return add_indicators(df5)
    except Exception:
        return None


def _m5_iceberg_check(df5):
    """5 分钟冰点：MACD金叉 + BOLL下轨 + RSI超卖 + 缩量 四条件（每条件20分，≥70 通过）。"""
    if df5 is None or df5.empty or len(df5) < 20:
        return False, "5分钟数据不足"
    c = {}
    if "macd" in df5.columns:
        dif, dea = df5["macd"], df5["macd_signal"]
        cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
        c["golden"] = bool(cross.tail(5).any())
    else:
        c["golden"] = False
    c["boll"] = bool(float(df5["bb_pct"].iloc[-1]) <= 0.15) if "bb_pct" in df5.columns else False
    _r = df5["rsi"].iloc[-1] if "rsi" in df5.columns else None
    c["rsi"] = bool(pd.notna(_r) and float(_r) < 30)
    vol = df5["volume"] if "volume" in df5.columns else None
    if vol is not None and len(df5) >= 25:
        recent = float(vol.tail(5).mean())
        prior = float(vol.iloc[-25:-5].mean())
        c["shrink"] = bool(prior > 0 and recent / prior < 0.8)
    else:
        c["shrink"] = False
    score = sum(20 for v in c.values() if v)
    passed = score >= 70
    return passed, f"5分钟冰点{'通过' if passed else f'={score}/80未过'}"


# ═══════════════════════════════════════════
# 双通道判定（单一真源）
# ═══════════════════════════════════════════

def eval_dual_channels(daily_ctx, price, m5_df=None, scan_type="intraday"):
    """W33 A1 双通道建仓判定。返回 {channel, verdict, composite_score, iceberg, breakout, conditions}。
    verdict=signal（冰点 80 / 突破 70 分档）→ 可建仓；approaching 仅留痕观察。"""
    daily_ctx = daily_ctx or {}
    # 通道一 冰点反转
    turn = check_turn_confirm(daily_ctx)
    boll = check_boll_lower(daily_ctx)
    shrink = check_volume_shrink(daily_ctx)
    rsi = check_rsi_oversold(daily_ctx)
    turn_p = bool(turn[0]); boll_p = bool(boll[0]); shrink_p = bool(shrink[0])
    ice_hits = int(boll_p) + int(shrink_p)
    m5_ice, m5_detail = (_m5_iceberg_check(m5_df) if scan_type == "intraday" else (False, "待日内确认"))
    c1_verdict = "weak"; c1_status = None
    if turn_p and ice_hits == 2:
        if scan_type == "intraday" and m5_ice:
            c1_verdict = "signal"; c1_status = "immediate"
        elif scan_type == "intraday":
            c1_verdict = "approaching"; c1_status = "intraday_pending"
        else:
            c1_verdict = "approaching"; c1_status = "next_day_pending"
    elif turn_p and ice_hits == 1:
        c1_verdict = "approaching"
        c1_status = "intraday_pending" if scan_type == "intraday" else "next_day_pending"
    c1_score = int(turn_p) * 40 + int(boll_p) * 20 + int(shrink_p) * 20

    # 通道二 突破跟随
    ohlc = daily_ctx.get("_daily_ohlc")
    bx = check_box_breakout(ohlc, price)
    volc = check_volume_confirm(daily_ctx)
    trend = check_trend_bull(daily_ctx)
    box_p = bool(bx[0]); volc_p = bool(volc[0]); trend_p = bool(trend[0])
    c2_score = int(box_p) * 40 + int(volc_p) * 30 + int(trend_p) * 30
    c2_verdict = "signal" if c2_score >= 70 else ("approaching" if c2_score >= 40 else "weak")

    # 顶层通道与 verdict
    _iv = {"signal": 3, "approaching": 2, "weak": 1}.get(c1_verdict, 0)
    _bv = {"signal": 3, "approaching": 2, "weak": 1}.get(c2_verdict, 0)
    if _iv >= _bv and _iv > 1:
        channel = "iceberg" if _iv > _bv else "both"
        verdict = "signal" if c1_verdict == "signal" else "approaching"
    elif _bv > 1:
        channel = "breakout"
        verdict = "signal" if c2_verdict == "signal" else "approaching"
    else:
        channel = None
        verdict = "weak"

    conditions = {
        "c1_turn_confirm": turn, "c1_boll_lower": boll, "c1_volume_shrink": shrink,
        "c1_rsi_oversold": rsi, "c1_m5_iceberg": (m5_ice, m5_detail),
        "c2_box_breakout": (box_p, f"箱体{ '突破' if box_p else '未突破'}"),
        "c2_volume_confirm": volc, "c2_trend_bull": trend,
    }
    return {
        "channel": channel,
        "verdict": verdict,
        "composite_score": max(c1_score, c2_score),
        "iceberg": {"verdict": c1_verdict, "score": c1_score, "status": c1_status},
        "breakout": {"verdict": c2_verdict, "score": c2_score},
        "conditions": conditions,
    }
