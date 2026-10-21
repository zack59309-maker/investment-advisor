# -*- coding: utf-8 -*-
"""持仓估值：把行情源里的最新价映射到持仓代码。

行情源各用各的代码格式（ETF 带 sh/sz、可转债/个股是 6 位），
这里统一成 canonical code，避免"持仓里有、行情里找不到"。
"""
from __future__ import annotations

import pandas as pd

from . import datasource as ds
from .core import FetchLog
from .domain import Position, canonical_code, exchange_of


def build_quote_map(spot: pd.DataFrame | None = None,
                    cb_board: pd.DataFrame | None = None,
                    etf_list: pd.DataFrame | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}

    if etf_list is not None and not etf_list.empty:
        for _, r in etf_list.iterrows():
            raw = str(r.get("code") or "")
            code6 = str(r.get("code6") or raw)[-6:]
            px = r.get("price")
            if not code6 or px is None or pd.isna(px) or float(px) <= 0:
                continue
            code = canonical_code(raw if raw.startswith(("sh", "sz")) else code6, "etf")
            out[code] = {"price": float(px), "name": str(r.get("name") or ""), "asset": "etf"}

    if cb_board is not None and not cb_board.empty:
        for _, r in cb_board.iterrows():
            code = canonical_code(str(r.get("cb_code") or ""), "cb")
            px = r.get("cb_price")
            if not code or px is None or pd.isna(px) or float(px) <= 0:
                continue
            out[code] = {"price": float(px), "name": str(r.get("cb_name") or ""), "asset": "cb"}

    if spot is not None and not spot.empty:
        for _, r in spot.iterrows():
            code = canonical_code(str(r.get("code6") or r.get("code") or ""), "stock")
            px = r.get("price")
            if not code or px is None or pd.isna(px) or float(px) <= 0:
                continue
            out.setdefault(code, {"price": float(px), "name": str(r.get("name") or ""),
                                  "asset": "stock"})
    return out


def fetch_quotes(positions: list[Position], log: FetchLog | None = None,
                 spot: pd.DataFrame | None = None, cb_board: pd.DataFrame | None = None,
                 etf_list: pd.DataFrame | None = None) -> dict[str, float]:
    """返回 {canonical_code: 最新价}；缺失的再用日线收盘价补。"""
    log = log or FetchLog()
    spot = spot if spot is not None else ds.a_spot(log)
    cb_board = cb_board if cb_board is not None else ds.cb_board(log)
    etf_list = etf_list if etf_list is not None else ds.etf_list(log)
    qmap = build_quote_map(spot, cb_board, etf_list)
    prices: dict[str, float] = {}
    for p in positions:
        q = qmap.get(p.code)
        if q and q["price"] > 0:
            prices[p.code] = q["price"]
            if not p.name and q.get("name"):
                p.name = q["name"]

    missing = [p for p in positions if p.code not in prices]
    for p in missing:
        px = 0.0
        try:
            if p.asset == "etf":
                d = ds.etf_daily(p.code, log)
            elif p.asset == "cb":
                d = ds.cb_daily(p.code[-6:], log)
            else:
                d = ds.stock_daily_sina(p.code[-6:], exchange_of(p.code), log)
            if not d.empty and "close" in d.columns:
                px = float(d["close"].dropna().iloc[-1])
        except Exception:  # noqa: BLE001
            px = 0.0
        if px > 0:
            prices[p.code] = px
    return prices
