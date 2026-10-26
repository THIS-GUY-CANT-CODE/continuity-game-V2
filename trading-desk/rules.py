"""Signal rules shared by the backtester and the daily desk.

Every rule works on a pandas DataFrame with a DatetimeIndex and a `close`
column (an `open` column is optional).  A rule returns two boolean Series,
`entry` and `exit`, computed from data up to and including each row, so a
signal on day t can only be acted on at the next execution point.
"""
from __future__ import annotations

import pandas as pd


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_down = down.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_up / avg_down.replace(0.0, float("nan"))
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_down.notna())


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add the indicator columns every rule reads."""
    d = df.copy()
    c = d["close"]
    d["sma5"] = c.rolling(5).mean()
    d["sma10"] = c.rolling(10).mean()
    d["sma200"] = c.rolling(200).mean()
    d["rsi2"] = rsi(c, 2)
    d["rsi4"] = rsi(c, 4)
    d["down_streak"] = (
        (c < c.shift(1)).astype(int)
        .groupby((c >= c.shift(1)).cumsum())
        .cumsum()
    )
    d["ret1"] = c.pct_change()
    d["vol20"] = d["ret1"].rolling(20).std() * (252 ** 0.5)
    d["dd_from_high10"] = c / c.rolling(10).max() - 1.0
    return d


RULES: dict[str, dict] = {
    "rsi2_10": {
        "label": "RSI(2) < 10 above 200-day average; exit on close above 5-day average",
        "entry": lambda d: (d["rsi2"] < 10) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["close"] > d["sma5"],
        "max_hold": 10,
    },
    "rsi2_5": {
        "label": "RSI(2) < 5 above 200-day average; exit on close above 5-day average",
        "entry": lambda d: (d["rsi2"] < 5) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["close"] > d["sma5"],
        "max_hold": 10,
    },
    "rsi4_25": {
        "label": "RSI(4) < 25 above 200-day average; exit on RSI(4) > 55",
        "entry": lambda d: (d["rsi4"] < 25) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["rsi4"] > 55,
        "max_hold": 10,
    },
    "down3": {
        "label": "3 consecutive lower closes above 200-day average; exit on close above 5-day average",
        "entry": lambda d: (d["down_streak"] >= 3) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["close"] > d["sma5"],
        "max_hold": 7,
    },
    "dip2": {
        "label": "Close at least 2% below 10-day high, above 200-day average; exit on close above 10-day average",
        "entry": lambda d: (d["dd_from_high10"] <= -0.02) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["close"] > d["sma10"],
        "max_hold": 10,
    },
    "composite2": {
        "label": "At least 2 of {RSI(2)<10, RSI(4)<25, 3 down closes, 2% off 10-day high} above 200-day average; exit on close above 5-day average",
        "entry": lambda d: (((d["rsi2"] < 10).astype(int) + (d["rsi4"] < 25).astype(int) + (d["down_streak"] >= 3).astype(int) + (d["dd_from_high10"] <= -0.02).astype(int)) >= 2) & (d["close"] > d["sma200"]),
        "exit": lambda d: d["close"] > d["sma5"],
        "max_hold": 10,
    },
    "rsi2_10_notrend": {
        "label": "RSI(2) < 10 with NO trend filter; exit on close above 5-day average",
        "entry": lambda d: d["rsi2"] < 10,
        "exit": lambda d: d["close"] > d["sma5"],
        "max_hold": 10,
    },
}


def signals(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Return the indicator frame with `entry` and `exit` boolean columns."""
    spec = RULES[rule]
    d = indicators(df)
    d["entry"] = spec["entry"](d).fillna(False)
    d["exit"] = spec["exit"](d).fillna(False)
    return d
