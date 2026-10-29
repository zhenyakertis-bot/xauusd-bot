"""Backtest metrics, walk-forward split, equity curve PNG, trades CSV, Markdown report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.backtest.runner import BacktestResult
from app.config import StrategyConfig

FILLED = {"SL", "TP1", "TP2", "BREAKEVEN"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    """Stats over filled trades (expired/invalidated limits never risked anything)."""
    n_signals = len(trades)
    if n_signals == 0:
        return {"signals": 0, "trades": 0}
    filled = trades[trades["status"].isin(FILLED)].sort_values("closed_at")
    r = filled["result_r"].astype(float).to_numpy()
    wins, losses = r[r > 0], r[r < 0]
    cum = np.cumsum(r)
    dd = (
        cum - np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] if len(r) else np.array([0.0])
    )
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    return {
        "signals": n_signals,
        "trades": len(filled),
        "expired": int((trades["status"] == "EXPIRED").sum()),
        "invalidated": int((trades["status"] == "INVALIDATED").sum()),
        "open": int((trades["status"] == "OPEN").sum()),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": int((r == 0).sum()),
        "winrate": len(wins) / len(r) if len(r) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum())
        if len(losses)
        else float("inf")
        if len(wins)
        else 0.0,
        "expectancy_r": float(r.mean()) if len(r) else 0.0,
        "total_r": float(r.sum()),
        "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_r": float(-np.min(dd)) if len(r) else 0.0,
        "max_losing_streak": best,
        "avg_mfe_r": float(filled["mfe_r"].mean()) if len(filled) else 0.0,
        "avg_mae_r": float(filled["mae_r"].mean()) if len(filled) else 0.0,
    }


def _breakdown(trades: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    filled = trades[trades["status"].isin(FILLED)]
    k = key.loc[filled.index]
    g = filled.groupby(k)["result_r"]
    return pd.DataFrame(
        {
            "trades": g.size(),
            "winrate": g.apply(lambda x: (x > 0).mean()),
            "total_r": g.sum(),
            "expectancy_r": g.mean(),
        }
    )


def walk_forward(
    trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, ratio: float
) -> dict[str, Any]:
    split = start + (end - start) * ratio
    ins = trades[trades["created_at"] < split]
    oos = trades[trades["created_at"] >= split]
    return {"split": str(split), "in_sample": metrics(ins), "out_of_sample": metrics(oos)}


def _fmt(m: dict[str, Any]) -> list[str]:
    if not m.get("trades"):
        return [f"- Signals: {m.get('signals', 0)}, filled trades: 0"]
    return [
        f"- Signals: {m['signals']} (filled {m['trades']}, expired {m['expired']}, "
        f"invalidated {m['invalidated']}, still open {m['open']})",
        f"- Win / loss / BE: {m['wins']} / {m['losses']} / {m['breakeven']} "
        f"— winrate {m['winrate']:.1%}",
        f"- Profit factor: {m['profit_factor']:.2f}",
        f"- Expectancy: {m['expectancy_r']:+.2f} R per trade; total {m['total_r']:+.1f} R",
        f"- Avg win {m['avg_win_r']:+.2f} R, avg loss {m['avg_loss_r']:+.2f} R",
        f"- Max drawdown: {m['max_drawdown_r']:.1f} R; "
        f"longest losing streak {m['max_losing_streak']}",
        f"- Avg MFE {m['avg_mfe_r']:+.2f} R, avg MAE {m['avg_mae_r']:+.2f} R",
    ]


def _table(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return ["_no trades_"]
    lines = ["| | trades | winrate | total R | expectancy R |", "|---|---:|---:|---:|---:|"]
    for k, row in df.iterrows():
        lines.append(
            f"| {k} | {int(row['trades'])} | {row['winrate']:.0%} | "
            f"{row['total_r']:+.1f} | {row['expectancy_r']:+.2f} |"
        )
    return lines


def equity_png(
    trades: pd.DataFrame, path: Path, balance: float, risk_pct: float, split: pd.Timestamp
) -> None:
    filled = (
        trades[trades["status"].isin(FILLED)].sort_values("closed_at")
        if len(trades)
        else pd.DataFrame()
    )
    fig, ax = plt.subplots(figsize=(10, 4.5))
    if not filled.empty:
        t = pd.to_datetime(filled["closed_at"])
        r = filled["result_r"].astype(float).to_numpy()
        eq = balance * np.cumprod(1 + r * risk_pct / 100)
        ax.step(t, eq, where="post", color="#2563eb", linewidth=1.6)
        ax.axhline(balance, color="#9ca3af", linewidth=0.8)
        ax.axvline(
            mdates.date2num(split.to_pydatetime()),  # type: ignore[no-untyped-call]
            color="#dc2626",
            linestyle="--",
            linewidth=1,
            label="in-sample | out-of-sample",
        )
        ax.legend(loc="upper left", frameon=False)
    ax.set_title(f"XAUUSD SMC — equity at {risk_pct:g}% risk per trade (start {balance:,.0f})")
    ax.set_ylabel("Balance")
    ax.grid(alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def write_report(
    res: BacktestResult, cfg: StrategyConfig, out: Path, balance: float, risk_pct: float
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    trades = res.trades
    assert res.start is not None and res.end is not None
    start, end = pd.Timestamp(res.start), pd.Timestamp(res.end)
    split = start + (end - start) * cfg.backtest.in_sample_ratio
    summary: dict[str, Any] = {
        "period": [str(start), str(end)],
        "overall": metrics(trades),
        "walk_forward": walk_forward(trades, start, end, cfg.backtest.in_sample_ratio)
        if len(trades)
        else {},
        "engine_stages": dict(res.stages),
        "gate_blocked": dict(res.blocked),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    if len(trades):
        trades.to_csv(out / "trades.csv", index=False)
    equity_png(trades, out / "equity.png", balance, risk_pct, split)

    spread_desc = "from data" if cfg.backtest.use_data_spread else f"{cfg.backtest.spread}"
    lines = [
        f"# Backtest XAUUSD SMC — {start.date()} … {end.date()}",
        "",
        f"Risk {risk_pct:g}% per trade; spread {spread_desc}; SL slippage "
        f"{cfg.backtest.slippage}; TP1 closes {cfg.trade.tp1_close_fraction:.0%}, "
        "rest to TP2 with SL at BE.",
        "",
        "## Overall",
        *_fmt(summary["overall"]),
    ]
    if len(trades):
        wf = summary["walk_forward"]
        created = pd.to_datetime(trades["created_at"])
        lines += [
            "",
            f"## Walk-forward (split {pd.Timestamp(wf['split']).date()}, parameters unchanged)",
            "",
            "**In-sample**",
            *_fmt(wf["in_sample"]),
            "",
            "**Out-of-sample**",
            *_fmt(wf["out_of_sample"]),
            "",
            "## By session",
            *_table(_breakdown(trades, trades["session"])),
            "",
            "## By direction",
            *_table(_breakdown(trades, trades["direction"])),
            "",
            "## By weekday",
            *_table(_breakdown(trades, created.dt.day_of_week.map(lambda d: WEEKDAYS[d]))),
            "",
            "## By quarter",
            *_table(_breakdown(trades, created.dt.tz_localize(None).dt.to_period("Q").astype(str))),
        ]
    lines += [
        "",
        "## Why setups were skipped (engine stage reached, per M5 bar in kill zones)",
        *[f"- {k}: {v}" for k, v in sorted(res.stages.items(), key=lambda kv: -kv[1])],
        "",
        "Signals blocked by the gate: "
        + (", ".join(f"{k} {v}" for k, v in res.blocked.items()) or "none"),
        "",
        "Files: `trades.csv`, `equity.png`, `summary.json`.",
        "",
        "Paper backtest. Not financial advice.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
