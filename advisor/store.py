# -*- coding: utf-8 -*-
"""SQLite 持久化：账户、交易账本、运行记录、每日快照。

设计原则（借鉴 Ghostfolio）：**账本是事实，持仓是视图**。
positions 不单独存表，永远由 transactions 重放推导；这样加减仓、分红、
历史复盘都不会出现"持仓表和交易表对不上"的问题。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from .core import ROOT
from .domain import Position, Trade, canonical_code, compute_positions, detect_asset, effective_asset

DB_PATH = os.path.join(ROOT, "data", "advisor.db")
DEFAULT_ACCOUNT = "main"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    cash       REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code       TEXT NOT NULL,
    name       TEXT,
    asset      TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('BUY','SELL','DIV')),
    quantity   REAL NOT NULL DEFAULT 0,
    price      REAL NOT NULL DEFAULT 0,
    fee        REAL NOT NULL DEFAULT 0,
    trade_date TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'trade',
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id, trade_date, id);
CREATE INDEX IF NOT EXISTS idx_tx_code ON transactions(account_id, code);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date     TEXT NOT NULL,
    tier         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    equity       REAL NOT NULL DEFAULT 0,
    cash         REAL NOT NULL DEFAULT 0,
    invested_pct REAL NOT NULL DEFAULT 0,
    report_path  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date     TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    asset        TEXT,
    quantity     REAL NOT NULL DEFAULT 0,
    price        REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    cost         REAL NOT NULL DEFAULT 0,
    pnl          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_run ON snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_snap_date ON snapshots(run_date);
"""


def init_db() -> None:
    with _conn() as con:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO accounts(name, cash, created_at) VALUES(?,?,?)",
            (DEFAULT_ACCOUNT, 0.0, _now()),
        )


def get_account(name: str = DEFAULT_ACCOUNT) -> dict:
    init_db()
    with _conn() as con:
        row = con.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        if row is None:
            con.execute("INSERT INTO accounts(name, cash, created_at) VALUES(?,?,?)",
                        (name, 0.0, _now()))
            row = con.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        return dict(row)


def set_cash(amount: float, name: str = DEFAULT_ACCOUNT) -> float:
    init_db()
    amount = float(amount)
    if amount < 0:
        raise ValueError("现金不能为负数")
    with _conn() as con:
        con.execute("UPDATE accounts SET cash=? WHERE name=?", (amount, name))
    return amount


def _account_row(con: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
    if row is None:
        con.execute("INSERT INTO accounts(name, cash, created_at) VALUES(?,?,?)",
                    (name, 0.0, _now()))
        row = con.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
    return row


def _trades(con: sqlite3.Connection, account_id: int) -> list[Trade]:
    rows = con.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY trade_date, id", (account_id,)
    ).fetchall()
    return [
        Trade(code=r["code"], name=r["name"] or "", asset=r["asset"], side=r["side"],
              quantity=r["quantity"], price=r["price"], fee=r["fee"],
              trade_date=r["trade_date"], kind=r["kind"], note=r["note"] or "")
        for r in rows
    ]


def get_positions(prices: dict[str, float] | None = None,
                  name: str = DEFAULT_ACCOUNT) -> list[Position]:
    init_db()
    with _conn() as con:
        acc = _account_row(con, name)
        ps = compute_positions(_trades(con, acc["id"]))
    if prices:
        for p in ps:
            px = prices.get(p.code)
            if px is not None and px == px and px > 0:
                p.last_price = float(px)
    return ps


def add_trade(code: str, side: str, quantity: float, price: float, fee: float = 0.0,
              trade_date: str | None = None, name: str = "", asset: str | None = None,
              kind: str = "trade", note: str = "", update_cash: bool = True,
              account: str = DEFAULT_ACCOUNT) -> dict:
    """记录一笔交易并同步现金。买入现金不足、卖出超持仓会直接报错。"""
    init_db()
    side = side.upper()
    if side not in ("BUY", "SELL", "DIV"):
        raise ValueError("side 必须是 BUY / SELL / DIV")
    quantity = float(quantity)
    price = float(price)
    fee = float(fee or 0.0)
    asset = asset or detect_asset(code)
    code = canonical_code(code, asset)
    date = trade_date or _today()

    with _conn() as con:
        acc = _account_row(con, account)
        pos = {p.code: p for p in compute_positions(_trades(con, acc["id"]))}
        cash = float(acc["cash"])

        if side == "BUY":
            cost = quantity * price + fee
            if update_cash and cost > cash + 1e-6:
                raise ValueError(f"现金不足：需要 {cost:,.0f} 元，可用 {cash:,.0f} 元")
            cash -= cost
        elif side == "SELL":
            held = pos.get(code)
            if held is None or quantity > held.quantity + 1e-6:
                raise ValueError(f"卖出数量超过持仓：{code}")
            cash += quantity * price - fee
        else:  # DIV
            cash += quantity * price - fee

        con.execute(
            """INSERT INTO transactions(account_id, code, name, asset, side, quantity,
               price, fee, trade_date, kind, note, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc["id"], code, name, asset, side, quantity, price, fee, date, kind, note, _now()),
        )
        if update_cash:
            con.execute("UPDATE accounts SET cash=? WHERE id=?", (round(cash, 2), acc["id"]))
        return {"code": code, "side": side, "quantity": quantity, "price": price,
                "fee": fee, "cash": round(cash, 2)}


def replace_holdings(rows: Iterable[dict], cash: float | None = None,
                     account: str = DEFAULT_ACCOUNT) -> dict:
    """用一份持仓快照覆盖当前账本（开户/迁移用）。

    rows: [{code, name, quantity, cost_price, asset?}]
    写入的是 opening 交易，成本价即当前持仓均价；不会产生现金流。
    """
    init_db()
    clean: list[dict] = []
    for r in rows:
        code_raw = r.get("code") or r.get("代码")
        if not code_raw:
            continue
        asset = effective_asset(code_raw, r.get("asset"))
        code = canonical_code(code_raw, asset)
        qty = float(r.get("quantity") or r.get("qty") or 0)
        cost = float(r.get("cost_price") or r.get("cost") or r.get("price") or 0)
        if qty <= 0 or cost < 0:
            continue
        clean.append({"code": code, "name": (r.get("name") or r.get("名称") or ""),
                      "asset": asset, "quantity": qty, "cost_price": cost})

    with _conn() as con:
        acc = _account_row(con, account)
        con.execute("DELETE FROM transactions WHERE account_id=?", (acc["id"],))
        for r in clean:
            con.execute(
                """INSERT INTO transactions(account_id, code, name, asset, side, quantity,
                   price, fee, trade_date, kind, note, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (acc["id"], r["code"], r["name"], r["asset"], "BUY", r["quantity"],
                 r["cost_price"], 0.0, _today(), "opening", "持仓导入", _now()),
            )
        if cash is not None:
            cash = float(cash)
            if cash < 0:
                raise ValueError("现金不能为负数")
            con.execute("UPDATE accounts SET cash=? WHERE id=?", (round(cash, 2), acc["id"]))
        acc = _account_row(con, account)
        return {"rows": len(clean), "cash": round(float(acc["cash"]), 2)}


def clear_holdings(keep_cash: bool = True, account: str = DEFAULT_ACCOUNT) -> None:
    init_db()
    with _conn() as con:
        acc = _account_row(con, account)
        con.execute("DELETE FROM transactions WHERE account_id=?", (acc["id"],))
        if not keep_cash:
            con.execute("UPDATE accounts SET cash=0 WHERE id=?", (acc["id"],))


def list_trades(account: str = DEFAULT_ACCOUNT, limit: int = 500) -> list[dict]:
    init_db()
    with _conn() as con:
        acc = _account_row(con, account)
        rows = con.execute(
            "SELECT * FROM transactions WHERE account_id=? ORDER BY trade_date DESC, id DESC LIMIT ?",
            (acc["id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


def record_run(tier: str, mode: str, equity: float, cash: float, invested_pct: float,
               report_path: str, positions: Iterable[Position],
               account: str = DEFAULT_ACCOUNT) -> int:
    init_db()
    with _conn() as con:
        acc = _account_row(con, account)
        cur = con.execute(
            """INSERT INTO runs(run_date, tier, mode, equity, cash, invested_pct, report_path, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (_today(), tier, mode, round(equity, 2), round(cash, 2),
             round(invested_pct, 2), report_path, _now()),
        )
        run_id = int(cur.lastrowid)
        for p in positions:
            con.execute(
                """INSERT INTO snapshots(run_id, run_date, code, name, asset, quantity, price,
                   market_value, cost, pnl) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, _today(), p.code, p.name, p.asset, p.quantity,
                 p.last_price if p.priced else p.avg_cost, p.market_value,
                 p.cost_value, p.unrealized_pnl),
            )
    return run_id


def recent_runs(limit: int = 20) -> list[dict]:
    init_db()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
