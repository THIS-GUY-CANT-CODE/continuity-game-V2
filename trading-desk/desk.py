"""Daily desk: read prices, apply the sleeve rule, write the morning brief.

Works in two modes:
  * online  (--fetch): pulls ^GSPC, ^VIX, VUAG.L, VWRP.L, SGLN.L from Yahoo via yfinance.
  * offline (default): uses data/spx_daily.csv (Date,Close of the S&P 500) which you
    extend with --append "YYYY-MM-DD,close", plus optional --vix/--vuag/--vwrp/--sgln
    quotes and --sma200 for the 200-day average while the CSV is shorter than 200 rows.

State lives in state/portfolio.json.  Fills are recorded with --record, e.g.
  --record "BUY VUAG 400 110.82 2026-10-27"     sleeve entry: £400 at £110.82
  --record "SELL VUAG 112.40 2026-10-30"        sleeve exit at £112.40
  --record "CORE VWRP 650 141.78 2026-10-26"    core purchase: £650 at £141.78
  --record "CORE SGLN 130 59.66 2026-10-26"
  --record "CASH 120"                            set the cash buffer
  --record "CONTRIBUTE 100 2026-11-01"           money added to the account

Brief goes to briefs/YYYY-MM-DD.md and state/latest_brief.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rules import RULES, indicators  # noqa: E402

STATE = HERE / "state" / "portfolio.json"
PRICES = HERE / "data" / "spx_daily.csv"
BRIEFS = HERE / "briefs"
LATEST = HERE / "state" / "latest_brief.json"
TRIGGERS = ["rsi2_lt_10", "rsi4_lt_25", "three_down_closes", "two_pct_off_10d_high"]


# ----------------------------------------------------------------------------- data
def load_prices() -> pd.DataFrame:
    df = pd.read_csv(PRICES, parse_dates=["Date"]).dropna()
    df = df.sort_values("Date").drop_duplicates("Date", keep="last").set_index("Date")
    return df.rename(columns={"Close": "close"})


def append_close(text: str) -> None:
    d, c = [x.strip() for x in text.split(",")]
    datetime.strptime(d, "%Y-%m-%d")
    df = pd.read_csv(PRICES)
    df = df[df["Date"] != d]
    df = pd.concat([df, pd.DataFrame({"Date": [d], "Close": [float(c)]})]).sort_values("Date")
    df.to_csv(PRICES, index=False)


def try_fetch(state: dict) -> dict | None:
    """Pull live data via yfinance. Returns quotes dict or None if the host is unreachable."""
    try:
        import yfinance as yf  # type: ignore
        data = yf.download(["^GSPC", "^VIX", "VUAG.L", "VWRP.L", "SGLN.L"], period="2y", auto_adjust=False, progress=False, group_by="ticker")
        spx = data["^GSPC"]["Close"].dropna()
        if spx.empty:
            return None
        pd.DataFrame({"Date": spx.index.strftime("%Y-%m-%d"), "Close": spx.values.round(2)}).to_csv(PRICES, index=False)
        q = {"vix": float(data["^VIX"]["Close"].dropna().iloc[-1])}
        for t, k in (("VUAG.L", "vuag"), ("VWRP.L", "vwrp"), ("SGLN.L", "sgln")):
            s = data[t]["Close"].dropna()
            if not s.empty:
                v = float(s.iloc[-1])
                q[k] = v / 100 if v > 1000 else v  # LSE quotes in pence for some lines
        q["source"] = "yahoo"
        return q
    except Exception as e:  # noqa: BLE001
        print(f"[desk] live fetch unavailable ({type(e).__name__}: {str(e)[:80]}); using CSV + overrides", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------- state
def load_state() -> dict:
    return json.loads(STATE.read_text())


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def record(s: dict, text: str) -> None:
    p = text.split()
    kind = p[0].upper()
    sl = s["sleeve"]
    if kind == "BUY":  # BUY VUAG 400 110.82 [date]
        amt, px = float(p[2]), float(p[3])
        sl.update({"in_position": True, "entry_date": p[4] if len(p) > 4 else str(date.today()), "entry_price": px,
                   "units": round(amt / px, 6), "cost_gbp": amt, "entry_spx": s.get("last_spx_close")})
        sl["history"].append({"side": "BUY", "date": sl["entry_date"], "price": px, "gbp": amt})
    elif kind == "SELL":  # SELL VUAG 112.40 [date]
        px = float(p[2])
        proceeds = round(sl["units"] * px, 2)
        pnl = round(proceeds - sl["cost_gbp"], 2)
        sl["history"].append({"side": "SELL", "date": p[3] if len(p) > 3 else str(date.today()), "price": px, "gbp": proceeds, "pnl": pnl})
        sl["realised_pnl_gbp"] = round(sl.get("realised_pnl_gbp", 0.0) + pnl, 2)
        sl["size_gbp"] = round(sl["size_gbp"] + pnl, 2)
        sl.update({"in_position": False, "entry_date": None, "entry_price": None, "units": 0, "cost_gbp": 0, "entry_spx": None})
    elif kind == "CORE":  # CORE VWRP 650 141.78 [date]
        tkr, amt, px = p[1].upper(), float(p[2]), float(p[3])
        if s.get("unallocated_gbp", 0) >= amt:
            s["unallocated_gbp"] = round(s["unallocated_gbp"] - amt, 2)
        else:
            s["cash_buffer_gbp"] = round(max(0.0, s["cash_buffer_gbp"] - amt), 2)
        for h in s["core"]:
            if h["ticker"] == tkr:
                h["units"] = round(h["units"] + amt / px, 6)
                h["cost_gbp"] = round(h["cost_gbp"] + amt, 2)
                h.setdefault("lots", []).append({"date": p[4] if len(p) > 4 else str(date.today()), "gbp": amt, "price": px})
    elif kind == "CASH":
        s["cash_buffer_gbp"] = float(p[1])
    elif kind == "CONTRIBUTE":
        s["contributions"].append({"date": p[2] if len(p) > 2 else str(date.today()), "gbp": float(p[1])})
        s["cash_buffer_gbp"] = round(s["cash_buffer_gbp"] + float(p[1]), 2)
    else:
        raise SystemExit(f"unknown record kind: {kind}")


# ----------------------------------------------------------------------------- brief
def trading_days_between(idx: pd.DatetimeIndex, start: str, end: pd.Timestamp) -> int:
    return int(((idx > pd.Timestamp(start)) & (idx <= end)).sum())


def build(s: dict, prices: pd.DataFrame, quotes: dict, today: date, sma200_override: float | None) -> tuple[str, dict]:
    d = indicators(prices)
    last = d.iloc[-1]
    asof = d.index[-1]
    close = float(last["close"])
    s["last_spx_close"] = close
    # 200-day average: real if we have it, otherwise the stored/overridden level
    if sma200_override:
        s["sma200"] = {"level": sma200_override, "asof": str(today)}
    sma200 = float(last["sma200"]) if pd.notna(last["sma200"]) else (s.get("sma200") or {}).get("level")
    if sma200 is None:
        raise SystemExit("Need at least 200 closes or --sma200 <level>")
    above_trend = close > sma200
    fires = {
        "rsi2_lt_10": bool(last["rsi2"] < 10),
        "rsi4_lt_25": bool(last["rsi4"] < 25),
        "three_down_closes": bool(last["down_streak"] >= 3),
        "two_pct_off_10d_high": bool(last["dd_from_high10"] <= -0.02),
    }
    n_fire = sum(fires.values())
    sl = s["sleeve"]
    vuag = quotes.get("vuag")

    # ---- sleeve decision
    if sl["in_position"]:
        held = trading_days_between(d.index, sl["entry_date"], asof)
        sl["days_held"] = held
        stop_hit_spx = sl.get("entry_spx") and close <= sl["entry_spx"] * (1 - sl["stop_pct"])
        stop_hit_px = vuag and sl.get("entry_price") and vuag <= sl["entry_price"] * (1 - sl["stop_pct"])
        if close > float(last["sma5"]):
            action, why = "SELL", "the S&P 500 closed above its 5-day average (the bounce we were waiting for)"
        elif held >= sl["max_hold_days"]:
            action, why = "SELL", f"time stop: held {held} trading days without a bounce"
        elif stop_hit_spx or stop_hit_px:
            action, why = "SELL", f"disaster stop: price is {100*sl['stop_pct']:.0f}% or more below entry"
        else:
            action, why = "HOLD", f"no exit yet (close {close:,.2f} is below the 5-day average {last['sma5']:,.2f}); {held} trading day(s) since entry, max {sl['max_hold_days']}"
    else:
        if above_trend and n_fire >= 2:
            action, why = "BUY", f"{n_fire} of 4 pullback triggers fired and the index is above its 200-day average"
        elif not above_trend:
            action, why = "CASH", "the index is BELOW its 200-day average: no dip-buying in a downtrend, sleeve stays in cash earning interest"
        else:
            action, why = "CASH", f"only {n_fire} of 4 pullback triggers fired (need 2); nothing to do"

    # ---- core dip-buy
    buf = s["cash_buffer_gbp"]
    dip_buy = above_trend and buf >= 60 and (n_fire >= 3 or last["dd_from_high10"] <= -0.04)

    # ---- valuation (only with quotes)
    val_lines, total = [], None
    if all(k in quotes for k in ("vwrp", "sgln")):
        core_val = sum(h["units"] * quotes[h["ticker"].lower()] for h in s["core"])
        sleeve_val = (sl["units"] * vuag) if (sl["in_position"] and vuag) else sl["size_gbp"]
        unalloc = s.get("unallocated_gbp", 0.0)
        total = core_val + buf + sleeve_val + unalloc
        for h in s["core"]:
            v = h["units"] * quotes[h["ticker"].lower()]
            val_lines.append(f"| {h['ticker']} {h['name']} | £{v:,.2f} | {('%+.2f' % (v - h['cost_gbp'])) if h['cost_gbp'] else 'not bought yet'} |")
        val_lines.append(f"| Cash buffer | £{buf:,.2f} | — |")
        if unalloc > 0:
            val_lines.append(f"| Cash waiting to be invested in the core | £{unalloc:,.2f} | — |")
        val_lines.append(f"| Sleeve ({'in VUAG' if sl['in_position'] else 'cash'}) | £{sleeve_val:,.2f} | {sl.get('realised_pnl_gbp', 0):+.2f} realised |")

    # ---- text
    instr = {
        "BUY": f"After 08:05 UK, buy **£{sl['size_gbp']:.0f} of VUAG** (Vanguard S&P 500 UCITS ETF, GBP acc). Use a limit order at the live offer or a market order after the opening auction. Then record it: `--record \"BUY VUAG {sl['size_gbp']:.0f} <fill price> {today}\"`.",
        "SELL": f"After 08:05 UK, **sell all VUAG** ({sl['units']} units). Then record it: `--record \"SELL VUAG <fill price> {today}\"`.",
        "HOLD": "Do nothing with the sleeve today. Do not add, do not trim.",
        "CASH": "Do nothing with the sleeve today. It stays as cash earning interest.",
    }[action]
    trig_names = {"rsi2_lt_10": "RSI(2) below 10", "rsi4_lt_25": "RSI(4) below 25", "three_down_closes": "3 straight down closes", "two_pct_off_10d_high": "2%+ below 10-day high"}
    trig_rows = "\n".join(f"| {trig_names[k]} | {'FIRED' if v else 'no'} |" for k, v in fires.items())
    vix = quotes.get("vix")
    lines = [
        f"# Morning brief — {today.strftime('%A %d %B %Y')}",
        f"*Signals read from the S&P 500 close of {asof.date()} ({close:,.2f}). Prices in £ unless stated.*",
        "",
        f"## Sleeve decision: **{action}**",
        f"Why: {why}.",
        "",
        instr,
        "",
        "## Readings",
        "| Check | Value |", "|---|---|",
        f"| S&P 500 close | {close:,.2f} ({100*float(last['ret1']):+.2f}% on the day) |",
        f"| vs 200-day average ({sma200:,.0f}) | {100*(close/sma200-1):+.2f}% → trend filter {'ON (above)' if above_trend else 'OFF (below)'} |",
        f"| 5-day average | {float(last['sma5']):,.2f} |",
        f"| RSI(2) / RSI(4) | {float(last['rsi2']):.1f} / {float(last['rsi4']):.1f} |",
        f"| Down-close streak | {int(last['down_streak'])} |",
        f"| From 10-day high | {100*float(last['dd_from_high10']):+.2f}% |",
        f"| VIX | {vix if vix else 'n/a'} |",
        "", "| Pullback trigger | State |", "|---|---|", trig_rows,
        f"\nTriggers fired: **{n_fire} of 4** (sleeve buys on 2+, trend permitting).",
        "",
        "## Core",
        (f"Dip-buy: **deploy £60 of the cash buffer into VWRP** today (buffer £{buf:.0f}). Then record it: `--record \"CORE VWRP 60 <fill price> {today}\"`."
         if dip_buy else f"No core action. Cash buffer £{buf:.0f} waits for a deeper dip (3+ triggers or 4% off the 10-day high)."),
    ]
    if val_lines:
        lines += ["", "## Valuation (latest quotes)", "| Holding | Value | P&L |", "|---|---|---|", *val_lines, f"| **Total** | **£{total:,.2f}** | vs £{s['start_value'] + sum(c['gbp'] for c in s['contributions']):,.0f} put in |"]
    lines += ["", "## Rules you promised to keep",
              "1. One sleeve position at a time, £{:.0f}, VUAG only. No single stocks for the first 90 days.".format(sl["size_gbp"]),
              f"2. Exit on the first close above the 5-day average, or after {sl['max_hold_days']} trading days, or {100*sl['stop_pct']:.0f}% below entry. No exceptions, no averaging down.",
              "3. If the sleeve is down 15% from its peak, stop for a month and review.",
              "4. Nothing on leverage, nothing on CFDs, nothing you did not plan the night before.",
              "5. Trade from this brief, not from your feed."]
    brief = "\n".join(lines) + "\n"
    summary = {"date": str(today), "asof": str(asof.date()), "spx_close": close, "action": action, "why": why, "instruction": instr,
               "triggers": fires, "n_fire": n_fire, "above_trend": above_trend, "sma200": sma200, "sma5": float(last["sma5"]),
               "rsi2": round(float(last["rsi2"]), 1), "rsi4": round(float(last["rsi4"]), 1), "down_streak": int(last["down_streak"]),
               "off_10d_high_pct": round(100 * float(last["dd_from_high10"]), 2), "vix": vix, "dip_buy": bool(dip_buy), "quotes": quotes,
               "sleeve": {k: sl[k] for k in ("in_position", "entry_date", "entry_price", "units", "size_gbp", "days_held")}, "total_value": total}
    return brief, summary


# ----------------------------------------------------------------------------- html
def _inline(md: str) -> str:
    import html, re
    t = html.escape(md)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def render_html(summary: dict, s: dict) -> tuple[str, str]:
    """Return (decision_card_inner, full_brief_inner) HTML for the plan page."""
    act = summary["action"]
    label = {"BUY": "Buy the dip", "SELL": "Sell the bounce", "HOLD": "Hold", "CASH": "Stay in cash"}[act]
    instr = _inline(summary["instruction"].split(" Then record it")[0])
    d = datetime.strptime(summary["date"], "%Y-%m-%d").date()
    core = ("Core: deploy £60 of the cash buffer into VWRP today." if summary["dip_buy"] else "Core: no action.")
    decision = (
        f'<div class="dec-head"><span class="eyebrow">Today\'s brief · {d.strftime("%A %d %B %Y")}</span>'
        f'<span class="pill pill-{act.lower()}">Sleeve: {label}</span></div>'
        f'<p class="why">{_inline(summary["why"]).capitalize()}. {core}</p>'
        f'<p class="instr">{instr}</p>'
        f'<p class="asof">Read from the S&amp;P 500 close of {summary["asof"]} ({summary["spx_close"]:,.2f}); '
        f'{summary["n_fire"]} of 4 pullback triggers fired; trend filter {"on" if summary["above_trend"] else "off"}.</p>'
    )
    tn = {"rsi2_lt_10": "RSI(2) below 10", "rsi4_lt_25": "RSI(4) below 25", "three_down_closes": "3 straight down closes", "two_pct_off_10d_high": "2%+ below 10-day high"}
    trig = "".join(f'<tr><td>{tn[k]}</td><td class="num">{"<span class=\"fired\">fired</span>" if v else "no"}</td></tr>' for k, v in summary["triggers"].items())
    q = summary.get("quotes", {})
    sl = summary["sleeve"]
    pos = (f'in VUAG since {sl["entry_date"]} at £{sl["entry_price"]:.2f} ({sl["units"]} units)' if sl["in_position"] else "cash, earning interest")
    val = ""
    if summary.get("total_value") is not None:
        rows = []
        for h in s["core"]:
            v = h["units"] * q.get(h["ticker"].lower(), 0)
            rows.append(f'<tr><td>{h["ticker"]} · {h["name"]}</td><td class="num">£{v:,.2f}</td><td class="num">{("%+.2f" % (v - h["cost_gbp"])) if h["cost_gbp"] else "not bought yet"}</td></tr>')
        rows.append(f'<tr><td>Cash buffer</td><td class="num">£{s["cash_buffer_gbp"]:,.2f}</td><td class="num">—</td></tr>')
        if s.get("unallocated_gbp", 0) > 0:
            rows.append(f'<tr><td>Cash waiting to be invested in the core</td><td class="num">£{s["unallocated_gbp"]:,.2f}</td><td class="num">—</td></tr>')
        sv = sl["units"] * q["vuag"] if (sl["in_position"] and q.get("vuag")) else sl["size_gbp"]
        rows.append(f'<tr><td>Sleeve ({pos})</td><td class="num">£{sv:,.2f}</td><td class="num">{s["sleeve"].get("realised_pnl_gbp", 0):+.2f} realised</td></tr>')
        put_in = s["start_value"] + sum(c["gbp"] for c in s["contributions"])
        rows.append(f'<tr class="total"><td>Total</td><td class="num">£{summary["total_value"]:,.2f}</td><td class="num">vs £{put_in:,.0f} put in</td></tr>')
        val = f'<h4>Valuation at the latest quotes ({q.get("source", "manual")})</h4><div class="scroll"><table class="tbl"><thead><tr><th>Holding</th><th class="num">Value</th><th class="num">P&amp;L</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    full = (
        f'<div class="dec-head"><span class="eyebrow">{d.strftime("%A %d %B %Y")}</span><span class="pill pill-{act.lower()}">Sleeve: {label}</span></div>'
        f'<p class="why">{_inline(summary["why"]).capitalize()}.</p><p class="instr">{instr}</p>'
        f'<h4>Readings (S&amp;P 500 close of {summary["asof"]})</h4><div class="scroll"><table class="tbl"><tbody>'
        f'<tr><td>Close</td><td class="num">{summary["spx_close"]:,.2f}</td></tr>'
        f'<tr><td>200-day average</td><td class="num">{summary["sma200"]:,.0f} ({100*(summary["spx_close"]/summary["sma200"]-1):+.2f}%) · trend filter {"on" if summary["above_trend"] else "off"}</td></tr>'
        f'<tr><td>5-day average</td><td class="num">{summary["sma5"]:,.2f}</td></tr>'
        f'<tr><td>RSI(2) / RSI(4)</td><td class="num">{summary["rsi2"]} / {summary["rsi4"]}</td></tr>'
        f'<tr><td>Down-close streak</td><td class="num">{summary["down_streak"]}</td></tr>'
        f'<tr><td>From 10-day high</td><td class="num">{summary["off_10d_high_pct"]:+.2f}%</td></tr>'
        f'<tr><td>VIX</td><td class="num">{summary["vix"] if summary["vix"] else "n/a"}</td></tr></tbody></table></div>'
        f'<h4>Pullback triggers ({summary["n_fire"]} of 4; the sleeve buys on 2 or more)</h4><div class="scroll"><table class="tbl"><tbody>{trig}</tbody></table></div>'
        f'<h4>Core</h4><p>{core} Cash buffer £{s["cash_buffer_gbp"]:.0f}{"" if summary["dip_buy"] else " waits for a deeper dip (3+ triggers or 4% off the 10-day high)"}.</p>'
        + val
    )
    return decision, full


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", default=str(date.today()))
    ap.add_argument("--fetch", action="store_true", help="try to pull live data via yfinance first")
    ap.add_argument("--append", action="append", default=[], help='add an S&P 500 close: "YYYY-MM-DD,7623.68"')
    ap.add_argument("--sma200", type=float, default=None, help="200-day average of the S&P 500 when the CSV is short")
    ap.add_argument("--vix", type=float); ap.add_argument("--vuag", type=float); ap.add_argument("--vwrp", type=float); ap.add_argument("--sgln", type=float)
    ap.add_argument("--record", action="append", default=[], help="record a fill or cash change (see module doc)")
    ap.add_argument("--no-write", action="store_true", help="print the brief without writing files")
    a = ap.parse_args()
    today = datetime.strptime(a.today, "%Y-%m-%d").date()

    s = load_state()
    for r in a.record:
        record(s, r)
    quotes = {}
    if a.fetch:
        quotes = try_fetch(s) or {}
    for x in a.append:
        append_close(x)
    for k in ("vix", "vuag", "vwrp", "sgln"):
        v = getattr(a, k)
        if v is not None:
            quotes[k] = v
    if quotes and "source" not in quotes:
        quotes["source"] = "manual"
    prices = load_prices()
    brief, summary = build(s, prices, quotes, today, a.sma200)
    print(brief)
    if not a.no_write:
        BRIEFS.mkdir(exist_ok=True)
        (BRIEFS / f"{today}.md").write_text(brief)
        LATEST.write_text(json.dumps(summary, indent=2, default=str))
        dec, full = render_html(summary, s)
        (HERE / "state" / "latest_decision.html").write_text(dec)
        (HERE / "state" / "latest_brief.html").write_text(full)
        save_state(s)


if __name__ == "__main__":
    main()
