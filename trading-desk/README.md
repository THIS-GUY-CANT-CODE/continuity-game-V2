# Trading desk — £1,300 on Trading 212

A rules-based daily routine for a small Trading 212 Stocks & Shares ISA.
Core holdings sit still; a £400 "sleeve" buys S&P 500 pullbacks inside an uptrend
and sells the bounce. Everything is in GBP-listed ETFs, so there is no stamp duty,
no FX fee and no commission; the only cost is the bid/offer spread.

## Files

| Path | What it is |
|---|---|
| `rules.py` | the signal rules (RSI, down-streaks, pullback from 10-day high, trend filter) |
| `backtest.py` | backtester with conservative next-close execution and per-side costs |
| `desk.py` | the daily brief generator: reads prices + state, decides BUY / SELL / HOLD / CASH |
| `state/portfolio.json` | holdings, sleeve status, trade history (the single source of truth here) |
| `state/latest_brief.json` | machine-readable version of the latest brief |
| `data/spx_daily.csv` | S&P 500 daily closes used for the signals |
| `briefs/YYYY-MM-DD.md` | one brief per weekday |

## The sleeve rule (what `desk.py` decides)

Buy £400 of **VUAG** at the LSE open when, on the previous US close, the S&P 500 is
above its 200-day average **and** at least two of these fired:
RSI(2) < 10 · RSI(4) < 25 · three straight down closes · 2%+ below the 10-day high.

Sell at the LSE open after the first close above the 5-day average, or after 10
trading days, or if the price is 6% below entry. One position at a time.

Backtest 1993–2025 on an S&P 500 total-return series (5 bps per side, next-close
fills, 6% stop): 274 trades, 68% winners, +0.53% average trade, profit factor 2.4,
max drawdown -11%, about 8 trades a year, in the market 11% of the time.
Positive in every sub-period but smallest in 2010–2019 (+0.34% a trade). On £400 that
is roughly £15–£20 a year in expectation. The core is where the money compounds.

The plan page lives at https://claude.ai/artifact/QZJaHezVpE7nupynwoQdgJ (also in `state/page.json`).

## Daily routine (5 minutes, before 08:00 UK)

```bash
# online, once the environment can reach Yahoo Finance (query1/query2.finance.yahoo.com):
python3 desk.py --fetch

# offline: append the latest S&P 500 close and pass quotes by hand
python3 desk.py --append "2026-10-26,7600.12" --vix 19.1 --vuag 110.5 --vwrp 141.6 --sgln 59.7

# the 200-day average is carried forward from state; refresh it when you have a new level
python3 desk.py --append "2026-10-26,7600.12" --sma200 7295
```

Record fills after you act in the app, so the next brief knows the true position:

```bash
python3 desk.py --record "CORE VWRP 650 141.78 2026-10-26" --record "CORE SGLN 130 59.66 2026-10-26" --no-write
python3 desk.py --record "BUY VUAG 400 110.82 2026-10-27" --no-write
python3 desk.py --record "SELL VUAG 112.40 2026-10-30" --no-write
python3 desk.py --record "CONTRIBUTE 100 2026-11-01" --no-write
```

(`--no-write` records without producing a brief; drop it to do both.)

## Re-running the backtest

```bash
curl -L -o /tmp/us_market_data.csv https://raw.githubusercontent.com/SteelCerberus/us-market-data/main/data/us_market_data.csv
python3 backtest.py --csv /tmp/us_market_data.csv --date-col Date --close-col "Adjusted Close" --start 1993-01-01 --cost-bps 5 --stop-pct 0.06
```

The dataset is a research-grade approximation of SPY total return (see its README's
warnings). Treat the numbers as the shape of the edge, not a promise.

## Not advice

This is a personal process document and tooling, not financial advice. Capital is at
risk. Past performance, including backtests, does not predict future returns.
