"""回測績效報表產生器。"""
from __future__ import annotations

import pandas as pd

from src.backtest.engine import TradeRecord


def build_report(trades: list[TradeRecord], initial_capital: float = 10_000_000) -> dict:
    """計算整體績效指標。"""
    if not trades:
        return {"error": "無交易紀錄"}

    df = pd.DataFrame([t.__dict__ for t in trades])

    total_trades  = len(df)
    win_trades    = (df["net_pnl"] > 0).sum()
    loss_trades   = (df["net_pnl"] <= 0).sum()
    win_rate      = win_trades / total_trades if total_trades else 0

    avg_win       = df[df["net_pnl"] > 0]["net_pnl"].mean() if win_trades else 0
    avg_loss      = df[df["net_pnl"] <= 0]["net_pnl"].mean() if loss_trades else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss else float("inf")
    expectancy_r  = df["r_multiple"].mean()

    total_pnl     = df["net_pnl"].sum()
    total_cost    = df["cost"].sum()
    return_pct    = total_pnl / initial_capital * 100

    # 最大回撤
    cumulative = df["net_pnl"].cumsum()
    rolling_max = cumulative.cummax()
    drawdown = cumulative - rolling_max
    max_drawdown = drawdown.min()
    max_drawdown_pct = max_drawdown / initial_capital * 100

    # 出場原因分布
    exit_dist = df["exit_reason"].value_counts().to_dict()

    return {
        "總交易筆數":    total_trades,
        "獲利筆數":      int(win_trades),
        "虧損筆數":      int(loss_trades),
        "勝率":          f"{win_rate:.1%}",
        "平均獲利":      f"NT${avg_win:,.0f}",
        "平均虧損":      f"NT${avg_loss:,.0f}",
        "盈虧比":        f"{profit_factor:.2f}",
        "平均 R 倍數":   f"{expectancy_r:+.3f}R",
        "總淨損益":      f"NT${total_pnl:,.0f}",
        "總交易成本":    f"NT${total_cost:,.0f}",
        "報酬率":        f"{return_pct:+.2f}%",
        "最大回撤":      f"NT${max_drawdown:,.0f} ({max_drawdown_pct:.2f}%)",
        "出場原因": exit_dist,
    }


def print_report(report: dict, trades: list[TradeRecord]) -> None:
    """印出回測報告。"""
    print("\n" + "=" * 60)
    print("  ORB-15 + Trailing Stop 回測結果")
    print("=" * 60)
    for k, v in report.items():
        if k == "出場原因":
            print(f"  {k}：")
            for reason, cnt in v.items():
                print(f"    {reason}: {cnt} 筆")
        else:
            print(f"  {k}：{v}")
    print("=" * 60)

    if trades:
        df = pd.DataFrame([t.__dict__ for t in trades])
        print("\n  最佳 5 筆交易：")
        top5 = df.nlargest(5, "net_pnl")[["date","symbol","direction","entry_price","exit_price","net_pnl","r_multiple","exit_reason"]]
        print(top5.to_string(index=False))
        print("\n  最差 5 筆交易：")
        bot5 = df.nsmallest(5, "net_pnl")[["date","symbol","direction","entry_price","exit_price","net_pnl","r_multiple","exit_reason"]]
        print(bot5.to_string(index=False))

        # 各股勝率
        print("\n  各股票績效（交易 ≥ 3 次）：")
        sym_stats = df.groupby("symbol").agg(
            trades=("net_pnl", "count"),
            win_rate=("net_pnl", lambda x: (x > 0).mean()),
            avg_r=("r_multiple", "mean"),
            total_pnl=("net_pnl", "sum"),
        ).query("trades >= 3").sort_values("total_pnl", ascending=False)
        if not sym_stats.empty:
            sym_stats["win_rate"] = sym_stats["win_rate"].map("{:.0%}".format)
            sym_stats["avg_r"]    = sym_stats["avg_r"].map("{:+.2f}R".format)
            sym_stats["total_pnl"] = sym_stats["total_pnl"].map("NT${:,.0f}".format)
            print(sym_stats.to_string())
    print()
