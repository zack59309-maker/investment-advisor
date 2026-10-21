# -*- coding: utf-8 -*-
"""纯领域模型：不 import 任何 IO 库，方便单测与复用。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Asset = str  # etf / cb / stock / dividend / growth / cash

# 最小交易单位（A 股/ETF 100 份，可转债 10 张）
_LOT = {"etf": 100, "cb": 10, "stock": 100, "dividend": 100, "growth": 100, "cash": 1}


def lot_size(asset: str) -> int:
    return _LOT.get(asset, 100)


def exchange_of(code: str) -> str:
    """返回 sh / sz / bj；无法判断时返回 sh。"""
    c = code.lower().strip()
    if c.startswith(("sh", "sz", "bj")):
        return c[:2]
    digits = "".join(ch for ch in c if ch.isdigit())
    if digits.startswith(("5", "6", "9")):
        return "sh"
    if digits.startswith(("0", "1", "2", "3")):
        return "sz"
    if digits.startswith(("4", "8")):
        return "bj"
    return "sh"


def _digits(code: str) -> str:
    d = "".join(ch for ch in str(code) if ch.isdigit())
    return d.zfill(6) if d else ""


def detect_asset(code: str, hint: str | None = None) -> str:
    """根据代码前缀判断资产类别；hint 只接受合法值。"""
    if hint in _LOT and hint != "cash":
        return hint
    d = _digits(code)
    if not d:
        return "stock"
    if d[:3] in ("110", "111", "113", "115", "117", "118", "123", "127", "128"):
        return "cb"
    if d[:2] in ("51", "52", "56", "58") or d[:3] in (
        "159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169",
    ):
        return "etf"
    return "stock"


def effective_asset(code: str, asset: str | None = None) -> str:
    """代码优先：传了 stock/红利，但代码明显是 ETF/可转债时，以代码为准。"""
    detected = detect_asset(code)
    if asset in (None, "", "stock", "dividend", "growth") and detected in ("etf", "cb"):
        return detected
    return asset or detected


def canonical_code(code: str, asset: str | None = None) -> str:
    """统一代码格式：ETF 带 sh/sz 前缀；可转债/个股只用 6 位数字。"""
    raw = str(code).strip().lower().replace(" ", "")
    asset = effective_asset(raw, asset)
    d = _digits(raw)
    if not d:
        return raw
    if asset == "etf":
        ex = raw[:2] if raw.startswith(("sh", "sz")) else exchange_of(d)
        if d[:2] in ("15", "16", "18"):
            ex = "sz"
        if d[:2] in ("51", "52", "56", "58"):
            ex = "sh"
        return f"{ex}{d}"
    return d


@dataclass
class Trade:
    code: str
    side: str                      # BUY / SELL / DIV
    quantity: float
    price: float
    fee: float = 0.0
    trade_date: str = ""
    name: str = ""
    asset: str = "stock"
    kind: str = "trade"            # trade / opening
    note: str = ""


@dataclass
class Position:
    code: str
    name: str
    asset: str
    quantity: float
    avg_cost: float
    last_price: float = float("nan")
    realized_pnl: float = 0.0

    @property
    def cost_value(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def market_value(self) -> float:
        px = self.last_price if not _nan(self.last_price) else self.avg_cost
        return self.quantity * px

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_value

    @property
    def pnl_pct(self) -> float:
        return (self.unrealized_pnl / self.cost_value * 100.0) if self.cost_value else 0.0

    @property
    def priced(self) -> bool:
        return not _nan(self.last_price) and self.last_price > 0


def _nan(x: float) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


@dataclass
class OrderIntent:
    code: str
    name: str
    asset: str
    side: str                      # BUY / SELL / HOLD
    quantity: float
    price: float
    amount: float
    reason: str
    priority: int = 2
    current_qty: float = 0.0
    target_qty: float = 0.0
    fee: float = 0.0
    category: str = ""


@dataclass
class RebalancePlan:
    orders: list[OrderIntent] = field(default_factory=list)
    cash_before: float = 0.0
    cash_after: float = 0.0
    equity: float = 0.0
    target_cash: float = 0.0
    sell_proceeds: float = 0.0
    buy_cost: float = 0.0
    fees: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def buys(self) -> list[OrderIntent]:
        return [o for o in self.orders if o.side == "BUY"]

    @property
    def sells(self) -> list[OrderIntent]:
        return [o for o in self.orders if o.side == "SELL"]

    @property
    def holds(self) -> list[OrderIntent]:
        return [o for o in self.orders if o.side == "HOLD"]
