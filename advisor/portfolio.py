# -*- coding: utf-8 -*-
"""组合构造：把评分结果变成"具体买什么、买多少、什么价格、什么时候动"。"""
from __future__ import annotations

import math

import pandas as pd

from . import config as cfg
from .core import num

LOT = {"cb": 10, "etf": 100, "dividend": 100, "growth": 100}
ASSET_CN = {"cb": "可转债", "etf": "ETF", "dividend": "红利价值股", "growth": "成长/主题股",
            "cash": "现金/货基"}


def _lot_floor(amount: float, price: float, asset: str) -> int:
    """按最小交易单位向下取整。"""
    if not price or price <= 0 or math.isnan(price):
        return 0
    lot = LOT.get(asset, 100)
    lots = int(amount // (price * lot))
    return max(lots, 0) * lot


def build_portfolio(tier: str, capital: float, picks: dict[str, pd.DataFrame],
                    macro: dict, temp: dict) -> dict:
    """picks: {'cb': df, 'etf': df, 'dividend': df, 'growth': df}，均已按 score 降序。"""
    profile = cfg.RISK_PROFILES[tier]
    weights = dict(profile["weights"])
    max_pos = profile["max_positions"]
    max_single = profile["max_single"]

    # 温度调节：偏热减仓权益、偏冷加仓（只动 20% 的相对幅度，避免过度择时）
    t = temp.get("temp")
    if t == "偏热":
        shift = 0.05
        weights["cash"] = min(0.5, weights["cash"] + shift)
        for k in ("etf", "growth", "dividend"):
            weights[k] = max(0.0, weights[k] - shift * (weights[k] / max(1e-9, weights["etf"] + weights["growth"] + weights["dividend"])))
    elif t == "偏冷":
        shift = 0.05
        move = min(shift, weights["cash"])
        weights["cash"] -= move
        grow = weights["etf"] + weights["growth"] + weights["dividend"]
        if grow > 0:
            for k in ("etf", "growth", "dividend"):
                weights[k] += move * (weights[k] / grow)

    holdings: list[dict] = []
    plan: list[dict] = []          # 待分配的持仓（含影子份额，用于二次分配）
    cash_floor = 0.03              # 至少留 3% 现金应急
    _ASSETS = ("etf", "cb", "dividend", "growth")
    bucket_targets = {a: capital * weights.get(a, 0.0) for a in _ASSETS}
    for asset in _ASSETS:
        df = picks.get(asset)
        w = weights.get(asset, 0.0)
        n = max_pos.get(asset, 0)
        if df is None or df.empty or w <= 0 or n <= 0:
            continue
        sel = df.head(min(n, len(df)))
        # 该资产类别的总预算上限：目标权重上浮 15%，但不得吃掉现金底线
        bucket_cap = min(max(w * 1.15, w), 1.0 - cash_floor) * capital
        bucket_cost = 0.0
        for rank, (_, r) in enumerate(sel.iterrows(), start=1):
            this_w = min(w / len(sel), max_single.get(asset, w))
            price = num(r.get("cb_price") if asset == "cb" else r.get("last"))
            buy = num(r.get("buy_limit"))
            if not buy or math.isnan(buy) or buy <= 0:
                continue
            shares = _lot_floor(capital * this_w, buy, asset)
            cost = shares * buy
            if shares <= 0 or bucket_cost + cost > bucket_cap:
                continue
            bucket_cost += cost
            plan.append({
                "asset": asset, "rank": rank, "row": r, "buy": buy, "price": price,
                "shares": shares, "cost": cost, "budget": bucket_cap,
            })

    if not plan:
        return {"tier": tier, "profile": profile, "capital": capital,
                "holdings": pd.DataFrame(), "allocation": pd.Series(dtype=float),
                "weights": weights, "cash_yield": (macro.get("cn_10y") or 1.7) + 0.5,
                "invested_pct": 0.0, "cash_pct": 100.0}

    # 二次分配：按"距上限空间"把取整剩下的零钱补手，避免大额闲置
    cash_target = capital * weights.get("cash", cash_floor)
    cash_floor_amt = capital * cash_floor

    def _leftover(floor_amt: float) -> float:
        return capital - sum(p["cost"] for p in plan) - floor_amt

    def _hard_cap(p: dict) -> float:
        """单券目标金额上限 = min(类别目标权重, 单券权重上限) × 本金。"""
        return min(capital * max_single.get(p["asset"], 1.0),
                   capital * weights.get(p["asset"], 1.0))

    def _bucket_cap(asset: str) -> float:
        """类别金额上限 = 目标权重 ×1.15（超配不超过 15%）。"""
        return capital * weights.get(asset, 0.0) * 1.15

    def _room(p: dict) -> float:
        """单券还能加多少：受单券上限与该类别预算上限双重约束。"""
        bucket_used = sum(x["cost"] for x in plan if x["asset"] == p["asset"])
        bucket_room = _bucket_cap(p["asset"]) - bucket_used
        return max(0.0, min(_bucket_cap(p["asset"]) - p["cost"], bucket_room))

    def _fill(floor_amt: float) -> None:
        """用剩余现金把还有空间的标的补满；消费上限由 floor_amt 决定。"""
        for _ in range(3000):
            leftover = _leftover(floor_amt)
            if leftover <= 0:
                break
            cands = [p for p in plan
                     if leftover >= p["buy"] * LOT[p["asset"]]
                     and _room(p) >= p["buy"] * LOT[p["asset"]]]
            if not cands:
                break
            cands.sort(key=lambda p: p["cost"] / max(1e-9, _hard_cap(p)))
            moved = False
            for p in cands:
                lot_cost = p["buy"] * LOT[p["asset"]]
                add = min(int(leftover // lot_cost), int(_room(p) // lot_cost))
                if add > 0:
                    p["shares"] += add * LOT[p["asset"]]
                    p["cost"] += add * lot_cost
                    leftover -= add * lot_cost
                    moved = True
            if not moved:
                affordable = [p for p in cands if leftover >= p["buy"] * LOT[p["asset"]]]
                if not affordable:
                    break
                p = min(affordable, key=lambda x: x["cost"] / max(1e-9, _hard_cap(x)))
                p["shares"] += LOT[p["asset"]]
                p["cost"] += p["buy"] * LOT[p["asset"]]

    def _latent_gap() -> float:
        """各类"想买但没买成"的缺口总和（用于决定是否动用现金底线）。"""
        short = 0.0
        for a in _ASSETS:
            if a not in bucket_targets:
                continue
            want = bucket_targets[a]
            got = sum(p["cost"] for p in plan if p["asset"] == a)
            if want > got:
                lots = [p["buy"] * LOT[p["asset"]] for p in plan if p["asset"] != a] or [1000.0]
                if want - got > 2 * min(lots):
                    short += want - got
        return short

    _fill(cash_target)
    # 只有当"别的类别确实没配够"时才允许突破现金目标去补，否则保持现金目标
    if _latent_gap() > 0:
        _fill(max(cash_floor_amt, cash_target - _latent_gap()))

    # 全局封顶：任何类别不得超过其目标权重的 115%，超出部分卖出（转为现金）
    for asset in {p["asset"] for p in plan}:
        group = [p for p in plan if p["asset"] == asset]
        cap = _bucket_cap(asset)
        gsum = sum(p["cost"] for p in group)
        if gsum > cap and gsum > 0:
            scale = cap / gsum
            for p in group:
                lot = LOT[p["asset"]]
                p["shares"] = (int(p["shares"] * scale) // lot) * lot
                p["cost"] = p["shares"] * p["buy"]
    plan = [p for p in plan if p["shares"] > 0]
    # 封顶后如果仍有类别没配够，再用现金底线以上的钱补一次（最多补到缺口规模）
    if _latent_gap() > 0:
        _fill(max(cash_floor_amt, cash_target - _latent_gap()))
    if not plan:
        return {"tier": tier, "profile": profile, "capital": capital,
                "holdings": pd.DataFrame(), "allocation": pd.Series(dtype=float),
                "weights": weights, "cash_yield": (macro.get("cn_10y") or 1.7) + 0.5,
                "invested_pct": 0.0, "cash_pct": 100.0}

    for p in plan:
        r, asset, buy = p["row"], p["asset"], p["buy"]
        holdings.append({
            "类别": ASSET_CN[asset],
            "asset": asset,
            "排名": p["rank"],
            "代码": str(r.get("cb_code") if asset == "cb" else r.get("code")),
            "名称": str(r.get("cb_name") if asset == "cb" else r.get("name")),
            "最新价": round(p["price"], 3),
            "买入挂单价": round(buy, 3),
            "买入数量": p["shares"],
            "买入金额": round(p["cost"], 0),
            "目标权重": f"{weights.get(asset, 0)*100:.1f}%",
            "实际权重": f"{min(p['cost'] / capital, max_single.get(asset, 1.0))*100:.1f}%",
            "止损价": round(num(r.get("stop_loss")), 3),
            "止盈价": round(num(r.get("take_profit")), 3),
            "评分": num(r.get("score")),
            "理由": str(r.get("reason", ""))[:120],
        })

    used = sum(h["买入金额"] for h in holdings)
    cash_amount = max(0.0, capital - used)
    cash_yield = (macro.get("cn_10y") or 1.7) + 0.5
    if cash_amount > 0:
        holdings.append({
            "类别": ASSET_CN["cash"], "asset": "cash", "排名": 0,
            "代码": "511990/511360", "名称": "货币ETF或短融ETF（华宝添益/短融ETF）",
            "最新价": float("nan"), "买入挂单价": float("nan"),
            "买入数量": 0, "买入金额": round(cash_amount, 0),
            "目标权重": f"{weights.get('cash',0)*100:.1f}%",
            "实际权重": f"{cash_amount/capital*100:.1f}%",
            "止损价": float("nan"), "止盈价": float("nan"), "评分": float("nan"),
            "理由": f"现金/货基打底，参考收益约{cash_yield:.2f}%（10年国债+0.5%）",
        })

    df_h = pd.DataFrame(holdings)
    alloc = (df_h.groupby("类别")["买入金额"].sum() / capital * 100).round(1) if not df_h.empty else pd.Series(dtype=float)
    return {
        "tier": tier,
        "profile": profile,
        "capital": capital,
        "holdings": df_h,
        "allocation": alloc,
        "weights": weights,
        "cash_yield": cash_yield,
        "invested_pct": round(used / capital * 100, 1) if capital else 0.0,
        "cash_pct": round(cash_amount / capital * 100, 1) if capital else 0.0,
    }


def action_list(portfolio: dict) -> pd.DataFrame:
    """每日/每周执行清单。"""
    rows = []
    for _, h in portfolio["holdings"].iterrows():
        if h["asset"] == "cash":
            rows.append({
                "优先级": 3, "动作": "现金安排",
                "标的": f'{h["代码"]} {h["名称"]}',
                "指令": f'将 {h["买入金额"]:.0f} 元买入货币ETF/短融ETF，或留在券商理财',
                "触发条件": "随时",
            })
            continue
        rows.append({
            "优先级": 1 if h["排名"] <= 2 else 2,
            "动作": "建仓" if h["排名"] <= 2 else "观察后建仓",
            "标的": f'{h["代码"]} {h["名称"]}',
            "指令": f'限价 {h["买入挂单价"]} 买入 {h["买入数量"]} 份/张，金额约 {h["买入金额"]:.0f} 元',
            "触发条件": f'价格 ≤ {h["买入挂单价"]}；若开盘跳空高于 {h["最新价"]*1.02:.2f} 则当日放弃',
        })
        rows.append({
            "优先级": 4, "动作": "风控", "标的": f'{h["代码"]} {h["名称"]}',
            "指令": f'跌破 {h["止损价"]} 无条件卖出；涨到 {h["止盈价"]} 减半仓',
            "触发条件": "每个交易日收盘前检查",
        })
    df = pd.DataFrame(rows)
    return df.sort_values(["优先级"]).reset_index(drop=True) if not df.empty else df


def position_sizing_table(portfolio: dict) -> str:
    """按本金给出不同规模下的手数对照，方便小资金执行。"""
    lines = ["| 标的 | 单价 | 1000元 | 5000元 | 1万元 | 5万元 | 10万元 |",
             "|---|---|---|---|---|---|---|"]
    for _, h in portfolio["holdings"].iterrows():
        if h["asset"] == "cash" or not h["买入挂单价"] or math.isnan(h["买入挂单价"]):
            continue
        price = h["买入挂单价"]
        asset = h["asset"]
        cells = []
        for cap in (1000, 5000, 10000, 50000, 100000):
            w = float(h["实际权重"].rstrip("%")) / 100
            n = _lot_floor(cap * w, price, asset)
            cells.append(f"{n}股" if n else "不足1手")
        lines.append(f'| {h["名称"]}({h["代码"]}) | {price} | ' + " | ".join(cells) + " |")
    return "\n".join(lines)
