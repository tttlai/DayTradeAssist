from . import config, timeutil

DISCLAIMER = (
    "_Educational/personal use only. Not SEBI-registered investment advice. "
    "Rules-based technical screen — no profit is guaranteed, including on "
    "any given day. Trade only capital you can afford to lose._"
)


def format_watchlist(plans) -> str:
    today_dt = timeutil.now_ist()
    today = today_dt.strftime("%d %b %Y")
    lines = [
        f"*Day Trading Watchlist — {today}*",
        f"Budget: ₹{config.MAX_BUDGET:,.0f} | Risk/trade: {config.RISK_PER_TRADE_PCT*100:.1f}% | Square-off: {config.SQUARE_OFF_TIME}",
        "",
    ]

    if timeutil.is_fo_expiry_day(today_dt.date()):
        lines.append(
            "⚠️ _Today looks like monthly F&O expiry — price action can get "
            "erratic/choppy, especially in the last hour. Consider extra caution "
            "on breakout setups today._"
        )
        lines.append("")

    if not plans:
        lines.append("No qualifying setups today. Sitting out is a valid outcome.")
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    for p in plans:
        arrow = "🟢 LONG" if p.direction == "LONG" else "🔴 SHORT"
        lines.append(f"*{p.symbol}* — {arrow}")
        lines.append(f"  Watch for: {'break above' if p.direction=='LONG' else 'break below'} ₹{p.entry_trigger}")
        lines.append(f"  If triggered → SL ₹{p.stop_loss} | Target ₹{p.target} | Qty {p.quantity}")
        lines.append(f"  Exit by {p.square_off_time} regardless of P&L")
        if p.circuit_history:
            lines.append("  ⚡ _hit a circuit limit in the last 10 sessions — elevated risk_")
        lines.append("")

    lines.append("Confirmation alert follows ~09:35 IST once the opening range forms.")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_confirmation(plans) -> str:
    now = timeutil.now_ist().strftime("%H:%M")
    lines = [f"*Confirmation Check — {now} IST*", ""]

    actionable = [p for p in plans if p.status == "ENTER NOW"]
    no_data = [p for p in plans if p.status == "NO DATA"]
    no_trigger = [p for p in plans if p.status not in ("ENTER NOW", "NO DATA")]

    # Surfaced first and separately from the normal not-triggered list --
    # this means the live price fetch itself failed for that stock (an
    # API/token problem worth investigating), not just a quiet market day.
    if no_data:
        names = ", ".join(p.symbol for p in no_data)
        lines.append(f"⚠️ *Could not fetch live data for: {names}* — check Angel One connectivity.")
        reasons = []
        for p in no_data:
            reason = p.error or (p.gates or {}).get("error")
            if reason:
                reason = str(reason).replace("_", " ").replace("*", "").replace("`", "")
                lines.append(f"  {p.symbol}: {reason}")
            reasons.append(str(reason or ""))
        if reasons and all("empty candle list" in r for r in reasons):
            lines.append("  _Empty candles for every stock usually means the market is closed today._")
        lines.append("")

    if actionable:
        for p in actionable:
            arrow = "🟢 LONG" if p.direction == "LONG" else "🔴 SHORT"
            lines.append(f"*{p.symbol}* — {arrow} — ✅ ENTER NOW")
            lines.append(f"  Entry ~₹{p.entry_trigger} | SL ₹{p.stop_loss} | Target ₹{p.target} | Qty {p.quantity}")
            lines.append(f"  Exit by {p.square_off_time} regardless of P&L")
            if p.circuit_history:
                lines.append("  ⚡ _hit a circuit limit in the last 10 sessions — elevated risk_")
            lines.append("")
    else:
        lines.append("No watchlist stock has triggered with volume confirmation yet.")
        lines.append("")

    if no_trigger:
        names = ", ".join(p.symbol for p in no_trigger)
        lines.append(f"_Not triggered (normal, no data issue): {names}_")
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_new_triggers(plans, check_time: str) -> str:
    """A later-in-the-day re-check found something NEW that triggered.
    Deliberately shorter than format_confirmation() -- this is an update,
    not a full status report, since stocks that already triggered earlier
    or are still pending aren't repeated here."""
    lines = [f"*New Trigger — {check_time} IST*", ""]
    for p in plans:
        arrow = "🟢 LONG" if p.direction == "LONG" else "🔴 SHORT"
        lines.append(f"*{p.symbol}* — {arrow} — ✅ ENTER NOW")
        lines.append(f"  Entry ~₹{p.entry_trigger} | SL ₹{p.stop_loss} | Target ₹{p.target} | Qty {p.quantity}")
        lines.append(f"  Exit by {p.square_off_time} regardless of P&L")
        if p.circuit_history:
            lines.append("  ⚡ _hit a circuit limit in the last 10 sessions — elevated risk_")
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_gap_armed(count: int, problem: str | None, record_line: str) -> str:
    """The morning note that the gap-fade scan is on."""
    lines = ["*Gap-Fade Scan*", ""]
    if problem or count == 0:
        lines.append(f"⚠️ Not armed today: {problem or 'no stock passed the price/liquidity filters'}")
    else:
        lines.append(
            f"Watching {count} cheap, liquid stocks. At about 09:16 I'll alert on opening gaps of "
            f"{config.PENNY_GAP_MIN * 100:g}%+ (gap UP = SHORT, gap DOWN = BUY), at most {config.PENNY_MAX_ALERTS} "
            f"alerts, with a {config.PENNY_TARGET_PCT * 100:g}% target and {config.PENNY_STOP_PCT * 100:g}% stop."
        )
    if record_line:
        lines += ["", record_line]
    return "\n".join(lines)


def format_gap_alerts(alerts, hhmm: str) -> str:
    """Opening-gap fade alerts: act now at market, set target/stop once filled."""
    lines = [f"*Gap Alert — {hhmm} IST*", ""]
    for a in alerts:
        arrow = "🔴 SHORT" if a["direction"] == "SHORT" else "🟢 BUY"
        side = "above" if a["gap_pct"] > 0 else "below"
        lines.append(f"*{a['symbol']}* — {arrow} — opened {abs(a['gap_pct']):.1f}% {side} yesterday's close")
        lines.append(f"  Prev close ₹{a['prev_close']} | Open ₹{a['open']} | Now ₹{a['ltp']}")
        tgt_sign, stop_sign = ("+", "-") if a["direction"] == "BUY" else ("-", "+")
        lines.append(
            f"  Entry ~₹{a['ltp']} | Target ₹{a['target']} ({tgt_sign}{config.PENNY_TARGET_PCT * 100:g}%) | "
            f"Stop ₹{a['stop']} ({stop_sign}{config.PENNY_STOP_PCT * 100:g}%) | "
            f"Qty {a['quantity']} (about ₹{a['quantity'] * a['ltp']:,.0f})"
        )
        if a.get("large_gap"):
            lines.append("  ⚠️ _gap over 5% — often news-driven, higher risk_")
        lines.append("")
    lines.append("Place a market order now, then set the target and stop once filled. Exit by 15:15 if neither hits.")
    lines.append("_Experimental: backtests show cheap stocks tend to fade opening gaps. Not a guarantee._")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_gap_review(results) -> str:
    """How today's gap alerts turned out (entry at the alert price)."""
    lines = ["*Gap Alerts — how they turned out*", ""]
    total = 0.0
    for r in results:
        if r.get("pnl") is None:
            lines.append(f"❔ *{r['symbol']}* ({r['direction']}) — couldn't compute an outcome (no price data)")
            continue
        total += r["pnl"]
        icon = "✅" if r["pnl"] > 0 else "❌"
        lines.append(f"{icon} *{r['symbol']}* ({r['direction']}) — {r['outcome'].replace('_', ' ')}")
        lines.append(f"  Entry ₹{r['entry']} → Exit ₹{r['exit']} × {r['quantity']} = ₹{r['pnl']:+,.0f} ({r['pct']:+.2f}%)")
    lines += ["", f"If you took every alert: ₹{total:+,.0f}", "",
              "_Hypothetical: fills at the alert price, no brokerage, taxes or slippage._"]
    return "\n".join(lines)


def format_trade_paths(paths) -> str:
    """Observation-only look back at what each trade's price did after entry
    (src/tradepath.py). Informational: nothing here changes any decision."""
    lines = ["*Trade Review — what price did after entry*", ""]
    for p in paths:
        src = {"stream_1m": "1-min candles", "rest_15m": "15-min candles"}.get(p.get("source"), "")
        lines.append(f"*{p['symbol']}* — {p['direction']}" + (f" _({src})_" if src else ""))
        lines.append(
            f"  Best {p['mfe_atr']:+.2f} ATR at {p['mfe_t']} | worst -{p['mae_atr']:.2f} ATR at {p['mae_t']} "
            f"| exit {p['exit_atr']:+.2f} ATR"
        )
        events = [
            f"{e['event'].replace('_', ' ')} {e['t']}"
            for e in p["events"]
            if not e["event"].startswith(("favorable", "adverse"))
        ]
        if events:
            lines.append("  Events: " + "; ".join(events))
        else:
            lines.append("  Events: none of the warning signs occurred")
        lines.append("")
    lines.append("_Review only — shows how a trade evolved; it is not an exit signal._")
    return "\n".join(lines)


def format_daily_summary(results, failed_symbols=None) -> str:
    """results: list of (TradePlan, tradesim.SimResult, pnl) for trades that
    were actually confirmed ('ENTER NOW') today. failed_symbols: confirmed
    trades whose outcome couldn't be computed (an Angel One API failure
    fetching that specific stock's intraday candles), surfaced separately
    from a genuinely empty day so a data problem isn't mistaken for
    "nothing happened"."""
    failed_symbols = failed_symbols or []
    today = timeutil.now_ist().strftime("%d %b %Y")
    lines = [f"*End of Day Summary — {today}*", ""]

    if not results and not failed_symbols:
        lines.append("No trades were confirmed today, so there's nothing to summarize.")
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    if failed_symbols:
        names = ", ".join(failed_symbols)
        lines.append(f"⚠️ *Could not compute outcome for: {names}* — check Angel One connectivity/Railway logs.")
        lines.append("")

    if not results:
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    total_pnl = 0.0
    for plan, sim, trade_pnl in results:
        total_pnl += trade_pnl
        emoji = "✅" if trade_pnl > 0 else ("❌" if trade_pnl < 0 else "➖")
        # tradesim.py's outcome constants include raw underscores
        # (TIME_EXIT, NO_TRIGGER) -- a single unescaped "_" inside
        # Telegram's Markdown parse mode opens an italic span, and an odd
        # count of them anywhere in the message breaks the whole send with
        # "can't parse entities" (confirmed in production). Space instead.
        outcome_display = sim.outcome.replace("_", " ")
        lines.append(f"{emoji} *{plan.symbol}* ({plan.direction}) — {outcome_display}")
        lines.append(
            f"  Entry ₹{plan.entry_trigger} → Exit ₹{sim.exit_price} × {plan.quantity} = ₹{trade_pnl:,.0f}"
        )
        lines.append("")

    verdict = "profit" if total_pnl > 0 else ("loss" if total_pnl < 0 else "breakeven")
    lines.append(f"*If you followed every signal exactly: ₹{total_pnl:,.0f} {verdict}*")
    lines.append("")
    lines.append(
        "_Hypothetical only — assumes fills at the exact alerted levels with no "
        "slippage, brokerage, or taxes. Your actual result will differ._"
    )
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
