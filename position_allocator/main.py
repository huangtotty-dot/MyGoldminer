# coding=utf-8
"""main.py — CLI入口"""

import argparse
import json
import csv
import os
import sys
from .models import UserConfig, StockHolding
from .calculator import PositionCalculator
from .exporter import ExcelExporter


def load_holdings_csv(path: str) -> list:
    holdings = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            holdings.append(StockHolding(
                stock_code=row.get("stock_code", ""),
                stock_name=row.get("stock_name", ""),
                current_price=float(row.get("current_price", 0)),
                existing_shares=int(row.get("existing_shares", 0)),
                sector=row.get("sector", ""),
                risk_level=row.get("risk_level", "medium"),
            ))
    return holdings


def load_config_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_holdings_from_config(cfg: dict) -> list:
    return [StockHolding(**h) for h in cfg.get("holdings", [])]


def main():
    parser = argparse.ArgumentParser(description="三段式仓位配比计算引擎")
    parser.add_argument("--config", help="JSON配置文件路径")
    parser.add_argument("--capital", type=float, default=150000, help="资金总量")
    parser.add_argument("--holdings", help="CSV持仓文件路径")
    parser.add_argument("--base-ratio", type=float, default=0.55)
    parser.add_argument("--active-ratio", type=float, default=0.25)
    parser.add_argument("--cash-ratio", type=float, default=0.20)
    parser.add_argument("--strategy", default="equal_weight", choices=["equal_weight", "risk_weighted", "sector_constrained"])
    parser.add_argument("--output", default="仓位建议.xlsx", help="输出文件路径")
    parser.add_argument("--interactive", action="store_true", help="交互式输入")
    args = parser.parse_args()

    # 加载配置
    if args.config:
        cfg = load_config_json(args.config)
        user_cfg = UserConfig(**cfg.get("user_config", {"total_capital": args.capital}))
        holdings = build_holdings_from_config(cfg)
    elif args.holdings:
        user_cfg = UserConfig(total_capital=args.capital,
                              base_position_ratio=args.base_ratio,
                              active_position_ratio=args.active_ratio,
                              cash_reserve_ratio=args.cash_ratio,
                              allocation_strategy=args.strategy)
        holdings = load_holdings_csv(args.holdings)
    else:
        # 使用预设示例
        print("使用预设示例数据（华工科技+五洲新春+双良节能+中国巨石）")
        user_cfg = UserConfig(total_capital=150000, allocation_strategy=args.strategy)
        holdings = [
            StockHolding("000988.SZ", "华工科技", 108.19, 300, "光模块", "medium"),
            StockHolding("603667.SZ", "五洲新春", 49.16, 500, "轴承", "medium"),
            StockHolding("600481.SH", "双良节能", 3.82, 1400, "光伏", "high"),
            StockHolding("600176.SH", "中国巨石", 37.34, 300, "建材", "medium"),
        ]

    if not holdings:
        print("错误: 无持仓数据"); sys.exit(1)

    calc = PositionCalculator(user_cfg)
    results = calc.calculate_all(holdings)

    exporter = ExcelExporter(args.output)
    path = exporter.export(results, user_cfg, holdings)
    print(f"[OK] 已导出: {path}")

    for key, r in results.items():
        print(f"\n{'='*50}")
        print(f"  {r.scenario_name} | 底仓{r.base_pct:.0%} 活动{r.active_pct:.0%} 现金{r.cash_pct:.0%} | 买入强度{r.buy_intensity:.0%}")
        for d in r.stock_details:
            print(f"  {d.stock_name:6s} {d.current_price:>8.2f} 底{d.base_shares:>5d} 活{d.active_shares:>5d} 总{d.total_shares:>5d} 增{d.additional_shares:>5d} {d.buy_range_str}")


if __name__ == "__main__":
    main()
