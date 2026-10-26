"""Backtest the desk rules on a daily price CSV with realistic retail costs.

Execution model (deliberately conservative): a signal is read on the close of
day t and acted on at the NEXT execution point: the open of day t+1 when an
open column exists, otherwise the close of day t+1.  One position at a time,
all-in for the tactical sleeve.  A per-side cost (spread + slippage) is
charged on every entry and exit.

Usage:
  python backtest.py --csv prices.csv --date-col Date --close-col Close [--open-col Open]
                     [--start 2000-01-01] [--cost-bps 5] [--rules rsi2_10,down3]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rules import RULES, indicators, signals  # noqa: E402


def load(csv: str, date_col: str, close_col: str, open_col: str | None, start: str | None, end: str | None) -> pd.DataFrame:
    raw = pd.read_csv(csv)
    df = pd.DataFrame({"close": pd.to_numeric(raw[close_col], errors="coerce")})
    df.index = pd.to_datetime(raw[date_col], errors="coerce")
    if open_col:
        df["open"] = pd.to_numeric(raw[open_col], errors="coerce")
    df = df[~df.index.isna()].dropna(subset=["close"]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index <= end]
    return df


def run_rule(df: pd.DataFrame, rule: str, cost_side: float, stop_pct: float | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """Simulate one rule. Returns (trades, daily equity curve of the sleeve)."""
    d = signals(df, rule)
    max_hold = RULES[rule]["max_hold"]
    exec_px = d["open"] if "open" in d.columns else d["close"]
    n = len(d)
    closes = d["close"].to_numpy()
    execs = exec_px.to_numpy()
    entries = d["entry"].to_numpy()
    exits = d["exit"].to_numpy()
    dates = d.index

    equity = np.ones(n)
    in_pos = False
    entry_px = entry_i = 0
    pending_entry = pending_exit = False
    held = 0
    trades = []
    shares = 0.0
    cash = 1.0

    for i in range(n):
        # 1) act on orders queued from yesterday's close at today's execution price
        px = execs[i]
        if pending_entry and not in_pos and not math.isnan(px):
            shares = cash * (1 - cost_side) / px
            cash = 0.0
            in_pos = True
            entry_px, entry_i, held = px, i, 0
            pending_entry = False
        elif pending_exit and in_pos and not math.isnan(px):
            cash = shares * px * (1 - cost_side)
            ret = cash / (equity[i - 1] if i > 0 else 1.0) - 1.0  # unused, kept simple
            trades.append({
                "entry_date": dates[entry_i], "exit_date": dates[i],
                "entry": entry_px, "exit": px, "days": i - entry_i,
                "ret": (px * (1 - cost_side)) / (entry_px / (1 - cost_side)) - 1.0,
            })
            shares, in_pos, pending_exit = 0.0, False, False
        # 2) mark to market on the close
        equity[i] = cash + shares * closes[i]
        # 3) read today's close for tomorrow's order
        if in_pos:
            held = i - entry_i
            stopped = stop_pct is not None and closes[i] <= entry_px * (1 - stop_pct)
            if exits[i] or held >= max_hold or stopped:
                pending_exit = True
        else:
            if entries[i]:
                pending_entry = True
    eq = pd.Series(equity, index=dates)
    return pd.DataFrame(trades), eq


def stats(trades: pd.DataFrame, eq: pd.Series, bh: pd.Series) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    out = {"years": round(years, 1), "trades": len(trades)}
    if len(trades):
        r = trades["ret"]
        wins, losses = r[r > 0], r[r <= 0]
        out.update({
            "trades_per_year": round(len(trades) / years, 1),
            "win_rate": round(100 * (r > 0).mean(), 1),
            "avg_trade_pct": round(100 * r.mean(), 2),
            "median_trade_pct": round(100 * r.median(), 2),
            "avg_win_pct": round(100 * wins.mean(), 2) if len(wins) else float("nan"),
            "avg_loss_pct": round(100 * losses.mean(), 2) if len(losses) else float("nan"),
            "worst_trade_pct": round(100 * r.min(), 2),
            "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else float("inf"),
            "avg_days_held": round(trades["days"].mean(), 1),
            "exposure_pct": round(100 * trades["days"].sum() / len(eq), 1),
        })
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    dd = eq / eq.cummax() - 1
    daily = eq.pct_change().dropna()
    out.update({
        "cagr_pct": round(100 * cagr, 2),
        "max_dd_pct": round(100 * dd.min(), 2),
        "sharpe": round((daily.mean() / daily.std()) * math.sqrt(252), 2) if daily.std() > 0 else float("nan"),
        "bh_cagr_pct": round(100 * ((bh.iloc[-1] / bh.iloc[0]) ** (1 / years) - 1), 2),
        "bh_max_dd_pct": round(100 * (bh / bh.cummax() - 1).min(), 2),
    })
    return out


def by_period(trades: pd.DataFrame, eq: pd.Series, bh: pd.Series, breaks: list[str]) -> pd.DataFrame:
    rows = []
    edges = [eq.index[0]] + [pd.Timestamp(b) for b in breaks] + [eq.index[-1] + pd.Timedelta(days=1)]
    for a, b in zip(edges[:-1], edges[1:]):
        e = eq[(eq.index >= a) & (eq.index < b)]
        h = bh[(bh.index >= a) & (bh.index < b)]
        if len(e) < 50:
            continue
        t = trades[(trades["exit_date"] >= a) & (trades["exit_date"] < b)] if len(trades) else trades
        s = stats(t, e, h)
        rows.append({"period": f"{a.date()}→{(b - pd.Timedelta(days=1)).date()}", **{k: s.get(k) for k in ("trades", "win_rate", "avg_trade_pct", "cagr_pct", "max_dd_pct", "bh_cagr_pct", "bh_max_dd_pct")}})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--date-col", default="Date")
    ap.add_argument("--close-col", default="Close")
    ap.add_argument("--open-col", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--cost-bps", type=float, default=5.0, help="per-side cost in basis points (spread+slippage)")
    ap.add_argument("--rules", default=",".join(RULES))
    ap.add_argument("--breaks", default="2000-01-01,2010-01-01,2020-01-01")
    ap.add_argument("--sleeve", type=float, default=400.0, help="£ in the tactical sleeve, for £ translation")
    ap.add_argument("--out", default=None, help="write a JSON summary here")
    ap.add_argument("--stop-pct", type=float, default=None, help="disaster stop, e.g. 0.06 = exit if close is 6%% below entry")
    a = ap.parse_args()

    df = load(a.csv, a.date_col, a.close_col, a.open_col, a.start, a.end)
    bh = df["close"] / df["close"].iloc[0]
    cost = a.cost_bps / 10_000
    print(f"Data: {df.index[0].date()} → {df.index[-1].date()}  ({len(df)} rows)  cost/side={a.cost_bps}bps  exec={'next open' if 'open' in df else 'next close'}  stop={a.stop_pct}")
    summary = {}
    pd.set_option("display.width", 200)
    for rule in a.rules.split(","):
        trades, eq = run_rule(df, rule, cost, a.stop_pct)
        s = stats(trades, eq, bh)
        summary[rule] = {"label": RULES[rule]["label"], "stats": s}
        print("\n==", rule, "-", RULES[rule]["label"])
        print({k: v for k, v in s.items()})
        if len(trades):
            yearly = trades.assign(year=trades["exit_date"].dt.year).groupby("year")["ret"].agg(["count", lambda r: round(100 * r.sum(), 1), lambda r: round(100 * (r > 0).mean(), 0)])
            yearly.columns = ["trades", "sum_ret_pct", "win_rate"]
            summary[rule]["yearly"] = yearly.reset_index().to_dict(orient="records")
            print("per-period:")
            print(by_period(trades, eq, bh, a.breaks.split(",")).to_string(index=False))
            # £ translation for the sleeve
            r = trades["ret"]
            print(f"£{a.sleeve:.0f} sleeve: avg £/trade={a.sleeve*r.mean():+.2f}, worst trade £{a.sleeve*r.min():+.2f}, "
                  f"typical year £{a.sleeve*r.mean()*s['trades_per_year']:+.0f}, max sleeve drawdown £{a.sleeve*s['max_dd_pct']/100:+.0f}")
    if a.out:
        import json
        Path(a.out).write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
