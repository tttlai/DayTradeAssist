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


def format_gap_armed(count: int, problem: str | None, record_line: str,
                     for_label: str | None = None, sheet: list[dict] | None = None) -> str:
    """The note that the gap-fade scan is armed: tonight for tomorrow
    (for_label set) or, as the fallback, this morning. In the evening version
    `sheet` (penny.order_rows) becomes a ready-to-use order sheet."""
    title = f"*Gap-Fade Scan — ready for {for_label}*" if for_label else "*Gap-Fade Scan*"
    lines = [title, ""]
    if problem or count == 0:
        lines.append(f"⚠️ Not armed: {problem or 'no stock passed the price/liquidity filters'}")
    else:
        if for_label:
            intro = f"Built from today's close: watching {count} cheap, liquid stocks. Tomorrow at about 09:15"
        else:
            intro = f"Watching {count} cheap, liquid stocks. At about 09:15"
        lines.append(
            f"{intro} I'll alert on opening gaps of {config.PENNY_GAP_MIN * 100:g}%+ (gap UP = SHORT, gap DOWN = BUY), "
            f"at most {config.PENNY_MAX_ALERTS} alerts, with a {config.PENNY_TARGET_PCT * 100:g}% target "
            f"and {config.PENNY_STOP_PCT * 100:g}% stop."
        )
        if for_label:
            lines.append("Nothing to place tonight: the direction isn't known until the market opens.")
        if sheet:
            lines += ["", "*Order sheet* — whichever way a stock OPENS decides (skip it if it opens beyond "
                      f"{config.PENNY_GAP_MAX * 100:g}% either way). Most liquid first:"]
            for r in sheet:
                sell = f"SELL if open >= {r['sell_if_open_at_or_above']}" if r["sell_if_open_at_or_above"] is not None else "no sell"
                lines.append(f"{r['symbol']}: prev {r['prev_close']} | {sell} | BUY if open <= {r['buy_if_open_at_or_below']}")
            lines.append(f"(top {len(sheet)} of {count} most liquid)")
    if record_line:
        lines += ["", record_line]
    return "\n".join(lines)


def market_gap_line(gap_pct: float | None, indicative: bool = False) -> str | None:
    """One line on the market's own gap, with a warning on shock days."""
    if gap_pct is None:
        return None
    what = "indicatively set to open" if indicative else "opened"
    line = f"📊 Market {what} {gap_pct:+.1f}% (Nifty)"
    if abs(gap_pct) > config.PENNY_SHOCK_GAP * 100:
        line += (
            f" — ⚠️ big market-wide gap (overnight global/news move). Gap fades have worked far less on days "
            f"like this (about +0.2% vs +0.9% per trade in a 265-day test)."
        )
    return line


def format_gap_preopen(alerts, hhmm: str, market_gap_pct: float | None = None) -> str:
    """Heads-up from NSE's indicative pre-open prices, ahead of the real open."""
    lines = [f"*Pre-Open Heads-Up — {hhmm} IST*", ""]
    market = market_gap_line(market_gap_pct, indicative=True)
    if market:
        lines += [market, ""]
    lines.append("Indicative opening prices (NOT final) point to these gap-fade setups:")
    lines.append("")
    for a in alerts:
        arrow = "🔴 SHORT" if a["direction"] == "SHORT" else "🟢 BUY"
        lines.append(
            f"{arrow} *{a['symbol']}* — indicative open {a['gap_pct']:+.1f}% vs yesterday's close "
            f"(₹{a['prev_close']} → ₹{a['open']})"
        )
    lines += ["", "The real 09:15 open decides: you'll get the official alert right after it. "
              "Use this time to have the order screen ready.", "_Experimental: pre-open prices can move at the open._"]
    return "\n".join(lines)


def format_gap_alerts(alerts, hhmm: str) -> str:
    """Opening-gap fade alerts: act now at market, set target/stop once filled."""
    lines = [f"*Gap Alert — {hhmm} IST*", ""]
    market = market_gap_line(alerts[0].get("market_gap_pct")) if alerts else None
    if market:
        lines += [market, ""]
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
        if a.get("preflagged"):
            lines.append("  ✔ also flagged in the pre-open heads-up")
        if a.get("large_gap"):
            lines.append("  ⚠️ _gap over 5% — often news-driven, higher risk_")
        lines.append("")
    lines.append("Place a market order now, then set the target and stop once filled. Exit by 15:15 if neither hits.")
    lines.append("_Experimental: backtests show cheap stocks tend to fade opening gaps. Not a guarantee._")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_gap_skipped(gap_pct: float, hhmm: str) -> str:
    """Said once when PENNY_SKIP_ON_SHOCK holds the alerts back."""
    return (
        f"*Gap Alerts skipped — {hhmm} IST*\n\nThe market opened {gap_pct:+.1f}% (Nifty), past the "
        f"{config.PENNY_SHOCK_GAP * 100:g}% shock level, so no gap-fade alerts today "
        "(PENNY_SKIP_ON_SHOCK is on)."
    )


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


def format_gap_sheet_result(rows, for_label: str) -> str:
    """The end-of-day note: P&L for every sheet stock that met the gap rule."""
    from . import penny

    t = penny.sheet_totals(rows)
    lines = [f"*Gap-Fade Sheet — results for {for_label}*", ""]
    if not t["signals"]:
        lines.append(f"None of the {t['stocks']} stocks opened with a 2-5% gap, so no trade on the sheet.")
        return "\n".join(lines)
    lines.append(
        f"{t['signals']} of {t['stocks']} stocks met the gap rule; {t['winners']} would have won. "
        f"Taking every one at the open: ₹{t['total']:+,.0f} (average {t['avg_pct']:+.2f}% per trade)."
    )
    if t["alerted"]:
        lines.append(f"The {t['alerted']} I actually alerted on: ₹{t['alerted_total']:+,.0f}.")
    graded = sorted((r for r in rows if r.get("pnl_rs") is not None), key=lambda r: r["pnl_rs"], reverse=True)
    lines.append("")
    room = 3300 - sum(len(x) + 1 for x in lines)  # stay under Telegram's 4096-character limit
    shown = 0
    for r in graded:
        line = (
            f"{'✅' if r['pnl_rs'] > 0 else '❌'} {r['signal']} *{r['symbol']}* gap {r['gap_pct']:+.1f}% → "
            f"{r['outcome']}: ₹{r['pnl_rs']:+,.0f} ({r['pnl_pct']:+.2f}%)" + (" 🔔" if r.get("alerted") == "yes" else "")
        )
        if room < len(line) + 1:
            break
        lines.append(line)
        room -= len(line) + 1
        shown += 1
    if shown < len(graded):
        lines.append(f"…and {len(graded) - shown} more (smaller P&L).")
    lines += ["", "🔔 = the ones I alerted on. _Hypothetical: filled at the open, no brokerage, taxes or slippage._"]
    return "\n".join(lines)


def _plain(text: str) -> str:
    """Dynamic text safe for Telegram Markdown (no stray underscores/asterisks)."""
    return str(text).replace("_", " ").replace("*", "")


def format_overnight_watch(us: dict, ranked: dict, nifty: dict | None, label: str) -> str:
    """The overnight shock note: what the US did, then the stocks that have
    historically gapped down the most after such mornings."""
    moves = us["moves"]
    spx = moves.get("S&P 500", 0.0)
    lines = [f"*Overnight Shock Watch — {_plain(label)}*", ""]
    lines.append(
        f"US close {us['as_of'].strftime('%a %d %b')}: "
        + ", ".join(f"{name} {pct * 100:+.1f}%" for name, pct in moves.items())
    )
    if nifty:
        lines.append(
            f"After US drops of 1%+ the Nifty has gapped {nifty['gap'] * 100:+.2f}% on average "
            f"({nifty['n']} mornings), often recovering by the close. "
            + (f"This drop is bigger than most (S&P {spx * 100:+.1f}%), so expect more." if spx <= -0.02 else "")
        )
    sections = (
        ("liquid", "Most affected, liquid stocks"), ("penny", "Penny range (Rs 10-100)"),
        ("commodity", "Silver/gold ETFs (they follow the metal, not a gap fade)"),
    )
    for key, title in sections:
        rows = ranked.get(key) or []
        if not rows:
            continue
        lines += ["", f"*{title}*"]
        for r in rows:
            lines.append(f"{_plain(r['symbol'])}: opens {r['gap'] * 100:+.1f}% on average, then {r['oc'] * 100:+.1f}% by the close")
    lines += ["", "_Averages of what happened after US drops of 1%+ over the past year (not a forecast): "
                  "magnitudes for a drop this size are uncertain, and gaps in these stocks often reverse. "
                  "Not advice._"]
    return "\n".join(lines)


def format_news_report(events, headlines, label: str, since_label: str = "", session_label: str | None = None) -> str:
    """The news note for the session `label`: each market-moving story with
    when it was released, the stocks it could affect and how far each already
    moved in the last session."""
    from . import news
    arrows = {"up": "🟢 likely UP", "down": "🔴 likely DOWN", "mixed": "🟡 mixed"}
    by_id = {h["id"]: h for h in headlines}
    lines = [f"*News Watch — for the {_plain(label)} session*"]
    if since_label:
        lines.append(f"News released since {_plain(since_label)} IST")
    lines.append("")
    if not events:
        lines.append(f"Nothing in {len(headlines)} headlines looked likely to move particular stocks.")
        return "\n".join(lines)
    footer = ["", "_Picked by an AI from headlines only, and not tested against price history. A stock that "
                  "already moved a lot may have the news priced in. Check the story before acting. Not advice._"]
    shown = 0
    for n, e in enumerate(events, 1):
        released = []  # (time, outlet), earliest first, one per outlet
        for h in sorted((by_id[i] for i in e.get("source_ids", []) if i in by_id), key=lambda h: h["published"]):
            if h["source"] not in [outlet for _, outlet in released]:
                released.append((news.stamp(h["published"]), h["source"]))
        block = [
            f"*{n}. {_plain(e['headline'])}*",
            "🕒 Released " + ", ".join(f"{when} IST ({_plain(outlet)})" for when, outlet in released[:3])
            if released else "🕒 Release time not identified",
            f"{arrows.get(e.get('direction'), '')}, {_plain(e.get('strength', ''))} impact, {_plain(e.get('sector', ''))}",
            _plain(e.get("why", "")),
        ]
        for s in e["stocks"]:
            move = ""
            if s.get("close") is not None:
                move = f" ₹{s['close']:g}"
                if s.get("change") is not None:
                    move += f" ({s['change'] * 100:+.1f}% last session{', big move already' if abs(s['change']) >= 0.05 else ''})"
            block.append(f"• {_plain(s['symbol'])}{move}: {_plain(s.get('link', ''))}")
        block.append("")
        if len("\n".join(lines + block + footer)) > 3900:
            break
        lines += block
        shown += 1
    if shown < len(events):
        lines.append(f"…{len(events) - shown} more left out for length.")
    if session_label:
        lines.append(f"Prices and moves are from {_plain(session_label)}.")
    return "\n".join(lines + footer)


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
