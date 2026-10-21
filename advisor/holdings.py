# -*- coding: utf-8 -*-
"""持仓导入/展示：CSV、JSON → 账本；账本 → 可读表格。"""
from __future__ import annotations

import csv
import json
import os
from typing import Iterable

import pandas as pd

from . import store
from .domain import Position, canonical_code, detect_asset, effective_asset, lot_size
from .domain.ledger import portfolio_totals

ASSET_CN = {"etf": "ETF", "cb": "可转债", "stock": "个股",
            "dividend": "红利价值股", "growth": "成长/主题股", "cash": "现金"}

_ASSET_ALIASES = {
    "etf": "etf", "ETF": "etf", "基金": "etf", "场内基金": "etf",
    "cb": "cb", "可转债": "cb", "转债": "cb", "可转换债券": "cb",
    "stock": "stock", "股票": "stock", "个股": "stock",
    "dividend": "dividend", "红利": "dividend", "红利股": "dividend", "红利价值": "dividend",
    "growth": "growth", "成长": "growth", "成长股": "growth", "主题": "growth",
}

CODE_KEYS = ("code", "代码", "证券代码", "股票代码", "基金代码", "转债代码")
NAME_KEYS = ("name", "名称", "证券名称", "股票名称", "基金名称", "转债名称")
QTY_KEYS = ("quantity", "qty", "数量", "持仓数量", "股数", "份数", "张数", "持有数量")
COST_KEYS = ("cost_price", "cost", "成本价", "成本", "持仓成本", "买入均价", "均价")
ASSET_KEYS = ("asset", "类别", "资产类别", "类型")


def _pick(row: dict, keys: Iterable[str]):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    # 大小写/空格不敏感
    norm = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = norm.get(str(k).strip().lower())
        if v not in (None, ""):
            return v
    return None


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    s = str(v).strip().replace(",", "").replace("，", "").replace("¥", "").replace("元", "")
    try:
        return float(s)
    except ValueError:
        return default


def normalize_row(row: dict) -> dict | None:
    code_raw = _pick(row, CODE_KEYS)
    if not code_raw:
        return None
    code_raw = str(code_raw).strip()
    asset_hint = _pick(row, ASSET_KEYS)
    asset = _ASSET_ALIASES.get(str(asset_hint).strip(), None) if asset_hint else None
    asset = effective_asset(code_raw, asset)
    code = canonical_code(code_raw, asset)
    qty = _to_float(_pick(row, QTY_KEYS))
    cost = _to_float(_pick(row, COST_KEYS))
    if qty <= 0:
        return None
    return {
        "code": code,
        "name": str(_pick(row, NAME_KEYS) or "").strip(),
        "asset": asset,
        "quantity": qty,
        "cost_price": cost,
    }


def _read_csv(path: str) -> list[dict]:
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return [dict(r) for r in csv.DictReader(f)]
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError(f"无法识别 CSV 编码（试过 utf-8/gbk）：{last_err}")


def parse_file(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        rows = data.get("holdings", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("JSON 需要是数组，或 {\"holdings\": [...]}")
    else:
        rows = _read_csv(path)
    out = []
    for r in rows:
        n = normalize_row(r)
        if n:
            out.append(n)
    if not out:
        raise ValueError("文件里没有解析出有效持仓（需要 code/代码 与 quantity/数量 两列）")
    return out


def load_positions(prices: dict[str, float] | None = None) -> tuple[list[Position], float]:
    ps = store.get_positions(prices=prices)
    cash = float(store.get_account()["cash"])
    return ps, cash


def weights(positions: list[Position], cash: float) -> dict[str, float]:
    """按资产类别计算实际权重（含现金），返回 0~1。"""
    total = cash + sum(p.market_value for p in positions)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for p in positions:
        key = p.asset if p.asset in ("etf", "cb", "dividend", "growth") else "stock"
        out[key] = out.get(key, 0.0) + p.market_value
    out["cash"] = cash
    return {k: v / total for k, v in out.items()}


def positions_frame(positions: list[Position], cash: float) -> pd.DataFrame:
    total = cash + sum(p.market_value for p in positions)
    rows = []
    for p in positions:
        rows.append({
            "代码": p.code, "名称": p.name, "类别": ASSET_CN.get(p.asset, p.asset),
            "数量": p.quantity, "成本价": round(p.avg_cost, 3),
            "现价": round(p.last_price, 3) if p.priced else None,
            "市值": round(p.market_value, 0), "浮盈": round(p.unrealized_pnl, 0),
            "浮盈%": round(p.pnl_pct, 1),
            "权重%": round(p.market_value / total * 100, 1) if total else 0.0,
        })
    return pd.DataFrame(rows)


def summary(positions: list[Position], cash: float) -> dict:
    s = portfolio_totals(positions, cash)
    s["invested_pct"] = round(s["market_value"] / s["equity"] * 100, 1) if s["equity"] else 0.0
    s["cash_pct"] = round(s["cash"] / s["equity"] * 100, 1) if s["equity"] else 0.0
    return s


def import_file(path: str, cash: float | None = None) -> dict:
    rows = parse_file(path)
    res = store.replace_holdings(rows, cash=cash)
    res["path"] = path
    return res


def lot_of(asset: str) -> int:
    return lot_size(asset)
