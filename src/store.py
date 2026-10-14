"""
Permanent, append-only records (one JSON object per line) that must survive
redeploys: every confirmed trade and every day's watchlist/gate detail, kept
for later analysis and backtesting. Lives in config.PERSIST_DIR -- a Railway
Volume in production (see config.py), plain data/ locally.

Writes never raise: losing a log line must not break the trading phases that
call this.
"""
import json

from . import config


def _path(name: str):
    return config.PERSIST_DIR / name


def read_records(name: str) -> list[dict]:
    path = _path(name)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn/corrupt line shouldn't hide the rest
    return records


def append_record(name: str, record: dict, key: tuple[str, ...] | None = None) -> bool:
    """Append `record`. With `key` (field names), skip if a record with the
    same values already exists, so re-running a phase never duplicates it.
    Returns True if written."""
    try:
        if key:
            ident = tuple(record.get(k) for k in key)
            if any(tuple(r.get(k) for k in key) == ident for r in read_records(name)):
                return False
        path = _path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except Exception as e:
        print(f"[store] could not write {name}: {e.__class__.__name__}: {e}")
        return False


def trade_stats(trades: list[dict]) -> str:
    """Plain-text summary of trades.jsonl for the /stats command."""
    if not trades:
        return "No trades recorded yet."
    done = [t for t in trades if t.get("pnl") is not None]
    lines = []
    if done:
        wins = [t for t in done if t["pnl"] > 0]
        total = sum(t["pnl"] for t in done)
        days = len({t.get("date") for t in done})
        by_outcome = {}
        for t in done:
            by_outcome[t.get("outcome")] = by_outcome.get(t.get("outcome"), 0) + 1
        lines.append(f"{len(done)} trades over {days} days | wins {len(wins)} ({100 * len(wins) / len(done):.0f}%)")
        lines.append(f"Total P&L Rs {total:,.0f} | avg Rs {total / len(done):,.0f} per trade")
        lines.append("Outcomes: " + ", ".join(f"{str(k).replace('_', ' ')} {v}" for k, v in sorted(by_outcome.items())))
    unknown = len(trades) - len(done)
    if unknown:
        lines.append(f"{unknown} trade(s) with no computable outcome (data failure)")
    lines.append("")
    lines.append("Last 8:")
    for t in trades[-8:]:
        pnl = "n/a" if t.get("pnl") is None else f"Rs {t['pnl']:,.0f}"
        lines.append(
            f"{t.get('date')} {t.get('symbol')} {t.get('direction')} "
            f"{str(t.get('outcome')).replace('_', ' ')} {pnl}"
        )
    return "\n".join(lines)
