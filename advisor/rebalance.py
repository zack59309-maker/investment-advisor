# -*- coding: utf-8 -*-
"""调仓计划：当前持仓 + 目标组合 → 买/卖/持有指令。

这是"有持仓"与"每次重新建仓"的分水岭：
- 目标数量来自 build_portfolio（已按手数取整）；
- 差额 = 目标 - 当前，正数买、负数卖、零持有；
- 先卖后买，买入受"现金 + 卖出回款"约束，不够就按比例缩量。
"""
from __future__ import annotations

import math

import pandas as pd

from .domain import OrderIntent, Position, RebalancePlan, lot_size

BUY_FEE_RATE = 0.00025
SELL_FEE_RATE = 0.00025
STAMP_DUTY = 0.0005          # 仅股票卖出
MIN_FEE = 5.0
SELL_SLIP = 0.995            # 卖出限价 = 最新价 × 0.995，保证成交


def buy_fee(amount: float) -> float:
    return max(MIN_FEE, amount * BUY_FEE_RATE) if amount > 0 else 0.0


def sell_fee(amount: float, asset: str) -> float:
    if amount <= 0:
        return 0.0
    fee = max(MIN_FEE, amount * SELL_FEE_RATE)
    if asset in ("stock", "dividend", "growth"):
        fee += amount * STAMP_DUTY
    return round(fee, 2)


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def _floor_lot(qty: float, asset: str) -> float:
    lot = lot_size(asset)
    return max(0, int(qty // lot) * lot)


def plan_rebalance(target: pd.DataFrame, positions: list[Position], cash: float,
                   equity: float, weights: dict, sell_unknown: bool = True) -> RebalancePlan:
    cash = float(cash or 0.0)
    equity = float(equity or 0.0)
    plan = RebalancePlan(cash_before=round(cash, 2), equity=round(equity, 2),
                         target_cash=round(equity * float(weights.get("cash", 0.0)), 2))

    trows: list[dict] = []
    if target is not None and not target.empty:
        for _, r in target.iterrows():
            if str(r.get("asset")) == "cash":
                continue
            trows.append({
                "code": str(r.get("代码")),
                "name": str(r.get("名称")),
                "asset": str(r.get("asset")),
                "category": str(r.get("类别")),
                "qty": _f(r.get("买入数量")),
                "buy": _f(r.get("买入挂单价")),
                "last": _f(r.get("最新价"), _f(r.get("买入挂单价"))),
                "rank": int(_f(r.get("排名"), 9)),
                "stop": _f(r.get("止损价")),
                "take": _f(r.get("止盈价")),
                "reason": str(r.get("理由") or ""),
            })

    current = {p.code: p for p in positions if p.quantity > 0}
    if not trows:
        for p in current.values():
            plan.orders.append(OrderIntent(
                code=p.code, name=p.name, asset=p.asset, side="HOLD", quantity=p.quantity,
                price=p.last_price if p.priced else p.avg_cost,
                amount=p.market_value, current_qty=p.quantity, target_qty=p.quantity,
                reason="本期没有可用候选，持仓不动", priority=3,
                category=p.asset,
            ))
        plan.warnings.append("本期规则没有筛出任何标的：不生成买入，也不强制卖出。")
        plan.cash_after = round(cash, 2)
        return _finalize(plan)

    target_codes = {t["code"] for t in trows}
    missing_px = [p.code for p in current.values() if not p.priced]
    unknown_sells = 0
    kept_value = 0.0
    kept_count = 0

    # ---------- 1) 卖出：超配、清仓、跌出候选池 ----------
    for code, p in current.items():
        if code in target_codes:
            t = next(x for x in trows if x["code"] == code)
            delta = t["qty"] - p.quantity
            if delta >= -1e-6:
                continue
            sell_qty = min(p.quantity, -delta)
            if sell_qty < p.quantity - 1e-6:
                sell_qty = _floor_lot(sell_qty, p.asset)
            if sell_qty <= 0:
                continue
            px = p.last_price if p.priced else p.avg_cost
            limit = round(px * SELL_SLIP, 3)
            amount = sell_qty * limit
            plan.orders.append(OrderIntent(
                code=code, name=p.name or t["name"], asset=p.asset, side="SELL",
                quantity=sell_qty, price=limit, amount=round(amount, 2),
                fee=sell_fee(amount, p.asset), current_qty=p.quantity, target_qty=t["qty"],
                reason=f"超配：目标 {t['qty']:g}，当前 {p.quantity:g}", priority=1,
                category=t["category"],
            ))
        elif sell_unknown:
            px = p.last_price if p.priced else p.avg_cost
            limit = round(px * SELL_SLIP, 3)
            amount = p.quantity * limit
            unknown_sells += 1
            plan.orders.append(OrderIntent(
                code=code, name=p.name, asset=p.asset, side="SELL", quantity=p.quantity,
                price=limit, amount=round(amount, 2), fee=sell_fee(amount, p.asset),
                current_qty=p.quantity, target_qty=0.0,
                reason="不在本期候选池，清仓", priority=1, category=p.asset,
            ))
        else:
            kept_value += p.market_value
            kept_count += 1
            plan.orders.append(OrderIntent(
                code=code, name=p.name, asset=p.asset, side="HOLD", quantity=p.quantity,
                price=p.last_price if p.priced else p.avg_cost, amount=p.market_value,
                current_qty=p.quantity, target_qty=p.quantity,
                reason="不在本期候选池，按设置保留", priority=3, category=p.asset,
            ))

    # ---------- 2) 买入：目标 - 当前 ----------
    for t in sorted(trows, key=lambda x: (x["rank"], x["code"])):
        p = current.get(t["code"])
        cur = p.quantity if p else 0.0
        delta = t["qty"] - cur
        if delta <= 0:
            continue
        qty = _floor_lot(delta, t["asset"])
        if qty <= 0:
            continue
        amount = qty * t["buy"]
        plan.orders.append(OrderIntent(
            code=t["code"], name=t["name"], asset=t["asset"], side="BUY", quantity=qty,
            price=t["buy"], amount=round(amount, 2), fee=buy_fee(amount),
            current_qty=cur, target_qty=t["qty"],
            reason=t["reason"] or f"目标 {t['qty']:g}，当前 {cur:g}",
            priority=1 if t["rank"] <= 2 else 2, category=t["category"],
        ))

    # ---------- 3) 持有（数量已达标） ----------
    for t in trows:
        p = current.get(t["code"])
        if p and abs(t["qty"] - p.quantity) <= 1e-6:
            extra = ""
            if t["stop"]:
                extra = f"；跌破 {t['stop']:g} 止损"
            plan.orders.append(OrderIntent(
                code=t["code"], name=t["name"], asset=t["asset"], side="HOLD",
                quantity=p.quantity, price=p.last_price if p.priced else p.avg_cost,
                amount=p.market_value, current_qty=p.quantity, target_qty=t["qty"],
                reason=f"数量已达标{extra}", priority=3, category=t["category"],
            ))

    # ---------- 4) 现金约束：先卖后买，不够按比例缩 ----------
    proceeds = sum(o.amount - o.fee for o in plan.orders if o.side == "SELL")
    plan.sell_proceeds = round(proceeds, 2)
    # 保留的非候选持仓已经占用了目标仓位，必须从可买额度里扣掉，否则会整体超配
    available = max(0.0, cash + proceeds - kept_value)

    buys = [o for o in plan.orders if o.side == "BUY"]
    buy_total = sum(o.amount + o.fee for o in buys)
    if buy_total > available and buys:
        gross = sum(o.amount for o in buys)
        fees_est = sum(o.fee for o in buys)
        scale = min(1.0, max(0.0, available - fees_est) / gross) if gross > 0 else 0.0
        for o in buys:
            cap = max(0.0, o.target_qty - o.current_qty)
            q = min(_floor_lot(o.quantity * scale, o.asset), _floor_lot(cap, o.asset))
            o.quantity = q
            o.amount = round(q * o.price, 2)
            o.fee = buy_fee(o.amount)
        # 按优先级把缩量后的零钱补回，避免大额闲置；但不得超过目标缺口
        leftover = available - sum(o.amount + o.fee for o in buys)
        for o in sorted([x for x in buys if x.quantity > 0], key=lambda x: x.priority):
            lot = lot_size(o.asset)
            unit = o.price * lot
            cap = max(0.0, o.target_qty - o.current_qty)
            while (leftover >= unit + buy_fee(o.amount + unit)
                   and o.quantity + lot <= cap + 1e-6):
                o.quantity += lot
                o.amount = round(o.quantity * o.price, 2)
                o.fee = buy_fee(o.amount)
                leftover -= unit
        plan.orders = [o for o in plan.orders if not (o.side == "BUY" and o.quantity <= 0)]
        plan.warnings.append(
            f"现金不足以完成全部目标买入：按 {scale*100:.0f}% 缩量；"
            f"调仓后预计现金 {cash + proceeds - sum(o.amount + o.fee for o in buys):,.0f} 元。"
        )

    plan.buy_cost = round(sum(o.amount for o in plan.orders if o.side == "BUY"), 2)
    plan.fees = round(sum(o.fee for o in plan.orders if o.side in ("BUY", "SELL")), 2)
    plan.cash_after = round(cash + plan.sell_proceeds - plan.buy_cost
                            - sum(o.fee for o in plan.orders if o.side == "BUY"), 2)

    if kept_count:
        plan.warnings.insert(0, f"保留 {kept_count} 只非候选持仓（市值 {kept_value:,.0f} 元），"
                                "已从可买额度中扣除，避免整体超配。")
    if unknown_sells:
        plan.warnings.insert(0, f"{unknown_sells} 只持仓不在本期候选池，已生成清仓指令；"
                                "不想清仓可用 --keep-unknown（界面：取消勾选“清仓不在候选池的持仓”）。")
    if missing_px:
        plan.warnings.append(
            f"{len(missing_px)} 只持仓没有取到行情，卖出价按成本价估算：{', '.join(missing_px[:6])}"
        )
    if abs(plan.cash_after - plan.target_cash) > max(500.0, equity * 0.02):
        plan.warnings.append(
            f"调仓后现金 {plan.cash_after:,.0f} 元，与目标现金 {plan.target_cash:,.0f} 元有偏差，"
            "通常是手数取整或候选不足导致。"
        )
    return _finalize(plan)


def _finalize(plan: RebalancePlan) -> RebalancePlan:
    order = {"SELL": 0, "BUY": 1, "HOLD": 2}
    plan.orders.sort(key=lambda o: (order.get(o.side, 9), o.priority, o.code))
    return plan


def orders_frame(plan: RebalancePlan) -> pd.DataFrame:
    rows = []
    for o in plan.orders:
        rows.append({
            "动作": {"BUY": "买入", "SELL": "卖出", "HOLD": "持有"}[o.side],
            "类别": o.category or o.asset,
            "代码": o.code, "名称": o.name,
            "当前数量": round(o.current_qty, 2),
            "目标数量": round(o.target_qty, 2),
            "差额": round(o.quantity if o.side == "BUY" else (-o.quantity if o.side == "SELL" else 0), 2),
            "委托价": round(o.price, 3) if o.price else None,
            "金额": round(o.amount, 0),
            "预估费用": round(o.fee, 2),
            "理由": o.reason,
        })
    return pd.DataFrame(rows)


def action_table(plan: RebalancePlan) -> pd.DataFrame:
    rows = []
    for o in plan.orders:
        tag = f"{o.code} {o.name}".strip()
        unit = "张" if o.asset == "cb" else ("股" if o.asset in ("stock", "dividend", "growth") else "份")
        if o.side == "SELL":
            rows.append({
                "优先级": 1, "动作": "卖出", "标的": tag,
                "指令": f"限价 {o.price:g} 卖出 {o.quantity:g} {unit}，预计回款 {o.amount:,.0f} 元",
                "触发条件": f"价格 ≥ {o.price:g}；跌停/停牌则次日再挂",
            })
        elif o.side == "BUY":
            rows.append({
                "优先级": o.priority, "动作": "买入", "标的": tag,
                "指令": f"限价 {o.price:g} 买入 {o.quantity:g} {unit}，金额约 {o.amount:,.0f} 元",
                "触发条件": f"价格 ≤ {o.price:g}；开盘跳空高于 {o.price*1.02:.2f} 则当日放弃",
            })
        else:
            rows.append({
                "优先级": 3, "动作": "持有", "标的": tag,
                "指令": f"继续持有 {o.quantity:g} {unit}，不加不减",
                "触发条件": "按止损/止盈价每日检查",
            })
    rows.append({
        "优先级": 4, "动作": "现金", "标的": "现金/货基",
        "指令": f"调仓后预计现金 {plan.cash_after:,.0f} 元，放入货币ETF/短融ETF 或券商理财",
        "触发条件": "随时",
    })
    df = pd.DataFrame(rows)
    return df.sort_values(["优先级"]).reset_index(drop=True) if not df.empty else df
