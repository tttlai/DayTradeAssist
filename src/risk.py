"""Position sizing. Two caps are applied and the smaller wins:
  1. Risk cap — never risk more than RISK_PER_TRADE_PCT of MAX_BUDGET on a
     single trade if its stop-loss is hit.
  2. Budget cap — never allocate more than an equal share of MAX_BUDGET
     across today's picks (so one stock can't eat the whole budget).
"""
import math

from . import config


def position_size(entry: float, stop_loss: float, num_picks: int) -> int:
    if num_picks <= 0 or entry <= 0:
        return 0

    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        return 0

    risk_amount = config.MAX_BUDGET * config.RISK_PER_TRADE_PCT
    qty_by_risk = math.floor(risk_amount / risk_per_share)

    budget_per_pick = config.MAX_BUDGET / num_picks
    qty_by_budget = math.floor(budget_per_pick / entry)

    return max(0, min(qty_by_risk, qty_by_budget))
