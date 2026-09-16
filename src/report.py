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
        lines.append(f"⚠️ *Could not fetch live price for: {names}* — check Angel One connectivity.")
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


def format_daily_summary(results) -> str:
    """results: list of (TradePlan, tradesim.SimResult, pnl) for trades that
    were actually confirmed ('ENTER NOW') today."""
    today = timeutil.now_ist().strftime("%d %b %Y")
    lines = [f"*End of Day Summary — {today}*", ""]

    if not results:
        lines.append("No trades were confirmed today, so there's nothing to summarize.")
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    total_pnl = 0.0
    for plan, sim, trade_pnl in results:
        total_pnl += trade_pnl
        emoji = "✅" if trade_pnl > 0 else ("❌" if trade_pnl < 0 else "➖")
        lines.append(f"{emoji} *{plan.symbol}* ({plan.direction}) — {sim.outcome}")
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
