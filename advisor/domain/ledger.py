# -*- coding: utf-8 -*-
"""持仓账本：交易 -> 持仓（移动加权平均成本）。纯函数，无 IO。"""
from __future__ import annotations

from collections.abc import Iterable

from .models import Position, Trade, canonical_code, effective_asset


def _new_position(t: Trade) -> Position:
    return Position(code=canonical_code(t.code, t.asset), name=t.name or "",
                    asset=effective_asset(t.code, t.asset),
                    quantity=0.0, avg_cost=0.0)


def apply_trade(state: dict[str, Position], t: Trade) -> Position:
    """把一笔交易应用到持仓表；返回变化后的持仓。"""
    code = canonical_code(t.code, t.asset)
    pos = state.get(code)
    if pos is None:
        pos = _new_position(t)
        state[code] = pos
    if t.name and not pos.name:
        pos.name = t.name
    side = (t.side or "").upper()
    qty = float(t.quantity)
    price = float(t.price)
    fee = float(t.fee or 0.0)

    if side == "BUY":
        if qty <= 0:
            raise ValueError(f"买入数量必须大于 0：{code} {qty}")
        total_cost = pos.cost_value + qty * price + fee
        new_qty = pos.quantity + qty
        pos.avg_cost = round(total_cost / new_qty, 6)
        pos.quantity = round(new_qty, 4)
    elif side == "SELL":
        if qty <= 0:
            raise ValueError(f"卖出数量必须大于 0：{code} {qty}")
        if qty > pos.quantity + 1e-6:
            raise ValueError(f"卖出数量超过持仓：{code} 持有 {pos.quantity:g}，卖出 {qty:g}")
        pos.realized_pnl += (price - pos.avg_cost) * qty - fee
        pos.quantity = round(pos.quantity - qty, 4)
        if pos.quantity <= 1e-6:
            pos.quantity = 0.0
            pos.avg_cost = 0.0
    elif side == "DIV":
        # 分红只影响现金，由上层记账；这里不改持仓
        pass
    else:
        raise ValueError(f"未知交易方向：{t.side}")
    return pos


def compute_positions(trades: Iterable[Trade], include_closed: bool = False) -> list[Position]:
    """按时间顺序重放交易，返回持仓列表。"""
    state: dict[str, Position] = {}
    for t in trades:
        apply_trade(state, t)
    out = [p for p in state.values() if p.quantity > 1e-6 or include_closed]
    out.sort(key=lambda p: (p.asset, p.code))
    return out


def portfolio_totals(positions: Iterable[Position], cash: float) -> dict:
    """组合汇总：总资产、市值、成本、浮盈、已实现盈亏。"""
    ps = list(positions)
    market_value = sum(p.market_value for p in ps)
    cost_value = sum(p.cost_value for p in ps)
    unrealized = sum(p.unrealized_pnl for p in ps)
    realized = sum(p.realized_pnl for p in ps)
    cash = float(cash or 0.0)
    equity = cash + market_value
    return {
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "cost_value": round(cost_value, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
        "equity": round(equity, 2),
        "positions": len(ps),
    }
