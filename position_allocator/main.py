# coding=utf-8
"""main.py — CLI + GUI入口 (python main.py 直接运行)"""

import argparse
import json
import csv
import os
import sys

# 兼容直接运行 python main.py 和 python -m position_allocator.main
if __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from position_allocator.models import UserConfig, StockHolding
    from position_allocator.calculator import PositionCalculator
    from position_allocator.exporter import ExcelExporter
    from position_allocator.allocator import get_allocator
else:
    from .models import UserConfig, StockHolding
    from .calculator import PositionCalculator
    from .exporter import ExcelExporter
    from .allocator import get_allocator


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


DEFAULT_HOLDINGS = [
    StockHolding("000988.SZ", "华工科技", 108.19, 300, "光模块", "medium"),
    StockHolding("603667.SZ", "五洲新春", 49.16, 500, "轴承", "medium"),
    StockHolding("600481.SH", "双良节能", 3.82, 1400, "光伏", "high"),
    StockHolding("600176.SH", "中国巨石", 37.34, 300, "建材", "medium"),
]


def run_calculation(capital, base_r, active_r, cash_r, strategy, holdings, output_path):
    cfg = UserConfig(total_capital=capital,
                     base_position_ratio=base_r,
                     active_position_ratio=active_r,
                     cash_reserve_ratio=cash_r,
                     allocation_strategy=strategy)
    calc = PositionCalculator(cfg)
    results = calc.calculate_all(holdings)
    exp = ExcelExporter(output_path)
    path = exp.export(results, cfg, holdings)
    return results, path


def cli_main():
    parser = argparse.ArgumentParser(description="三段式仓位配比计算引擎")
    parser.add_argument("--capital", type=float, default=150000)
    parser.add_argument("--holdings", help="CSV持仓文件")
    parser.add_argument("--base-ratio", type=float, default=0.55)
    parser.add_argument("--active-ratio", type=float, default=0.25)
    parser.add_argument("--cash-ratio", type=float, default=0.20)
    parser.add_argument("--strategy", default="equal_weight")
    parser.add_argument("--output", default="仓位建议.xlsx")
    args = parser.parse_args()

    holdings = load_holdings_csv(args.holdings) if args.holdings else DEFAULT_HOLDINGS
    if not holdings:
        print("错误: 无持仓数据"); return

    results, path = run_calculation(args.capital, args.base_ratio, args.active_ratio,
                                     args.cash_ratio, args.strategy, holdings, args.output)
    print(f"[OK] 已导出: {path}")
    for key, r in results.items():
        print(f"\n{'='*50}")
        print(f"  {r.scenario_name} | 底仓{r.base_pct:.0%} 活动{r.active_pct:.0%} 现金{r.cash_pct:.0%} | 买入强度{r.buy_intensity:.0%}")
        for d in r.stock_details:
            print(f"  {d.stock_name:6s} {d.current_price:>8.2f} 底{d.base_shares:>5d} 活{d.active_shares:>5d} 总{d.total_shares:>5d} 增{d.additional_shares:>5d} {d.buy_range_str}")


# ═══════════════════════════════════════════
# GUI 界面 (tkinter)
# ═══════════════════════════════════════════

def gui_main():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    root = tk.Tk()
    root.title("三段式仓位配比计算引擎 v1.0")
    root.geometry("900x700")

    # ── 参数区 ──
    param_frame = ttk.LabelFrame(root, text="资金与比例配置", padding=10)
    param_frame.pack(fill="x", padx=10, pady=5)

    ttk.Label(param_frame, text="资金总量(元):").grid(row=0, column=0, sticky="e", padx=5)
    capital_var = tk.StringVar(value="150000")
    ttk.Entry(param_frame, textvariable=capital_var, width=12).grid(row=0, column=1, sticky="w")

    ttk.Label(param_frame, text="底仓比例:").grid(row=0, column=2, sticky="e", padx=5)
    base_var = tk.StringVar(value="0.55")
    ttk.Entry(param_frame, textvariable=base_var, width=8).grid(row=0, column=3, sticky="w")

    ttk.Label(param_frame, text="活动仓比例:").grid(row=0, column=4, sticky="e", padx=5)
    active_var = tk.StringVar(value="0.25")
    ttk.Entry(param_frame, textvariable=active_var, width=8).grid(row=0, column=5, sticky="w")

    ttk.Label(param_frame, text="现金比例:").grid(row=0, column=6, sticky="e", padx=5)
    cash_var = tk.StringVar(value="0.20")
    ttk.Entry(param_frame, textvariable=cash_var, width=8).grid(row=0, column=7, sticky="w")

    ttk.Label(param_frame, text="策略:").grid(row=1, column=0, sticky="e", padx=5)
    strategy_var = tk.StringVar(value="equal_weight")
    ttk.Combobox(param_frame, textvariable=strategy_var,
                 values=["equal_weight", "risk_weighted", "sector_constrained"],
                 state="readonly", width=18).grid(row=1, column=1, sticky="w")

    ttk.Label(param_frame, text="输出文件:").grid(row=1, column=2, sticky="e", padx=5)
    output_var = tk.StringVar(value="仓位建议.xlsx")
    ttk.Entry(param_frame, textvariable=output_var, width=20).grid(row=1, column=3, columnspan=3, sticky="w")
    ttk.Button(param_frame, text="浏览", command=lambda: output_var.set(
        filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")]))).grid(row=1, column=6, sticky="w")

    # ── 持仓表格 ──
    table_frame = ttk.LabelFrame(root, text="持仓股票列表 (双击编辑)", padding=5)
    table_frame.pack(fill="both", expand=True, padx=10, pady=5)

    cols = ("代码", "名称", "价格", "已有持仓", "板块", "风险")
    tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=8)
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=100 if c != "名称" else 130, anchor="center")
    tree.pack(side="left", fill="both", expand=True)

    scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")

    for h in DEFAULT_HOLDINGS:
        tree.insert("", "end", values=(h.stock_code, h.stock_name, h.current_price,
                                        h.existing_shares, h.sector, h.risk_level))

    # 编辑
    def on_double_click(event):
        item = tree.selection()[0] if tree.selection() else None
        if not item: return
        vals = tree.item(item, "values")
        edit = tk.Toplevel(root); edit.title("编辑股票")
        entries = {}
        for i, (label, val) in enumerate(zip(cols, vals)):
            ttk.Label(edit, text=label).grid(row=i, column=0, padx=5, pady=2)
            e = ttk.Entry(edit, width=20); e.insert(0, str(val)); e.grid(row=i, column=1, padx=5); entries[label] = e
        def save():
            tree.item(item, values=tuple(e.get() for e in entries.values())); edit.destroy()
        ttk.Button(edit, text="保存", command=save).grid(row=len(cols), columnspan=2, pady=5)

    tree.bind("<Double-1>", on_double_click)

    def add_row():
        tree.insert("", "end", values=("CODE.SH", "新股票", "10.00", "0", "", "medium"))
    def del_row():
        sel = tree.selection()
        for s in sel: tree.delete(s)
    def load_csv():
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if path:
            for item in tree.get_children(): tree.delete(item)
            for h in load_holdings_csv(path):
                tree.insert("", "end", values=(h.stock_code, h.stock_name, h.current_price,
                                                h.existing_shares, h.sector, h.risk_level))
    btn_frame = ttk.Frame(table_frame)
    btn_frame.pack(side="bottom", fill="x", pady=5)
    ttk.Button(btn_frame, text="添加", command=add_row).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="删除", command=del_row).pack(side="left", padx=5)
    ttk.Button(btn_frame, text="导入CSV", command=load_csv).pack(side="left", padx=5)

    # ── 结果区 ──
    result_text = tk.Text(root, height=12, font=("Consolas", 10))
    result_text.pack(fill="both", expand=True, padx=10, pady=5)

    def do_calc():
        try:
            cap = float(capital_var.get()); br = float(base_var.get())
            ar = float(active_var.get()); cr = float(cash_var.get())
        except ValueError:
            messagebox.showerror("错误", "参数必须为数字"); return
        holdings = []
        for item in tree.get_children():
            v = tree.item(item, "values")
            holdings.append(StockHolding(v[0], v[1], float(v[2]), int(v[3]), v[4], v[5]))
        if not holdings:
            messagebox.showerror("错误", "至少需要一只股票"); return
        try:
            results, path = run_calculation(cap, br, ar, cr, strategy_var.get(), holdings, output_var.get())
        except Exception as e:
            messagebox.showerror("计算失败", str(e)); return
        result_text.delete("1.0", "end")
        result_text.insert("end", f"[OK] 已导出: {path}\n\n")
        for key, r in results.items():
            result_text.insert("end", f"{'='*60}\n")
            result_text.insert("end", f"  {r.scenario_name} | 底仓{r.base_pct:.0%} 活动{r.active_pct:.0%} 现金{r.cash_pct:.0%} | 强度{r.buy_intensity:.0%}\n")
            for d in r.stock_details:
                result_text.insert("end",
                    f"  {d.stock_name:6s} {d.current_price:>8.2f} | 底{d.base_shares:>5d} 活{d.active_shares:>5d} 总{d.total_shares:>5d} | 增{d.additional_shares:>5d} | {d.buy_range_str}\n")
            result_text.insert("end", "\n")

    ttk.Button(root, text="开始计算", command=do_calc).pack(pady=10)

    root.mainloop()


if __name__ == "__main__":
    if "--gui" in sys.argv:
        gui_main()
    else:
        cli_main()
