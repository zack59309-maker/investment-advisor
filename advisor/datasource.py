# -*- coding: utf-8 -*-
"""统一数据入口：多源降级 + 缓存 + 字段标准化。所有网络访问都从这里走。"""
from __future__ import annotations

import datetime as dt

import akshare as ak
import pandas as pd

from . import config as cfg
from .core import FetchLog, cached, fetch_first, num, to_num

TTL_SPOT = 0.5      # 实时行情缓存 30 分钟
TTL_DAILY = 12.0    # 日线缓存 12 小时
TTL_SLOW = 24.0     # 财务/宏观缓存 1 天


# ------------------------------------------------------------------ A股实时
def a_spot(log: FetchLog) -> pd.DataFrame:
    """A股实时行情：新浪为主，东财为备。返回 code/name/price/change_pct/turnover。"""
    df = fetch_first([
        ("A股实时-新浪", ak.stock_zh_a_spot),
        ("A股实时-东财", ak.stock_zh_a_spot_em),
    ], ttl_hours=TTL_SPOT, log=log)
    if df is None:
        return pd.DataFrame()

    if "symbol" in df.columns:  # 新浪
        out = pd.DataFrame({
            "code": df["symbol"].astype(str),
            "name": df["name"].astype(str),
            "price": to_num(df["trade"]),
            "change_pct": to_num(df["changepercent"]),
            "volume": to_num(df.get("volume")),
            "amount": to_num(df.get("amount")),
            "pb": pd.NA,
        })
    else:  # 东财
        out = pd.DataFrame({
            "code": df["代码"].astype(str),
            "name": df["名称"].astype(str),
            "price": to_num(df["最新价"]),
            "change_pct": to_num(df.get("涨跌幅")),
            "volume": to_num(df.get("成交量")),
            "amount": to_num(df.get("成交额")),
            "pb": to_num(df["市净率"]) if "市净率" in df.columns else pd.NA,
        })
    out["code6"] = out["code"].str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)
    out["exchange"] = out["code"].str[:2]
    return out


def index_spot(log: FetchLog) -> pd.DataFrame:
    df = cached("指数实时-新浪", ak.stock_zh_index_spot_sina, TTL_SPOT, log)
    if df is None:
        return pd.DataFrame()
    return pd.DataFrame({
        "code": df["代码"].astype(str),
        "name": df["名称"].astype(str),
        "price": to_num(df["最新价"]),
        "change_pct": to_num(df["涨跌幅"]),
    })


# ------------------------------------------------------------------ 可转债
def cb_board(log: FetchLog) -> pd.DataFrame:
    """可转债全表：新浪实时价 + 东财发行明细（转股价/评级）→ 自算转股价值与溢价率。"""
    spot = cached("可转债实时-新浪", ak.bond_zh_hs_cov_spot, TTL_SPOT, log)
    basic = cached("可转债明细-东财", ak.bond_zh_cov, TTL_SLOW, log)
    if spot is None or spot.empty:
        return pd.DataFrame()

    s = pd.DataFrame({
        "cb_code": spot["code"].astype(str).str.zfill(6),
        "cb_name": spot["name"].astype(str),
        "cb_price": to_num(spot["trade"]),
        "cb_amount": to_num(spot["amount"]),
        "cb_volume": to_num(spot["volume"]),
    })

    if basic is not None and not basic.empty:
        b = pd.DataFrame({
            "cb_code": basic["债券代码"].astype(str).str.zfill(6),
            "stock_code": basic["正股代码"].astype(str).str.zfill(6),
            "stock_name": basic["正股简称"].astype(str),
            "stock_price": to_num(basic["正股价"]),
            "convert_price": to_num(basic["转股价"]),
            "issue_size": to_num(basic["发行规模"]),
            "rating": basic["信用评级"].astype(str),
        })
        df = s.merge(b, on="cb_code", how="left")
    else:
        df = s
        for c in ("stock_code", "stock_name", "stock_price", "convert_price",
                  "issue_size", "rating"):
            df[c] = pd.NA

    df = df[df["stock_price"].notna() & df["convert_price"].notna()
            & (df["convert_price"] > 0) & (df["cb_price"] > 0)].copy()
    df["convert_value"] = 100.0 / df["convert_price"] * df["stock_price"]
    df["premium_rate"] = (df["cb_price"] / df["convert_value"] - 1) * 100
    df["double_low"] = df["cb_price"] + df["premium_rate"]
    df["rating_score"] = df["rating"].map(cfg.rating_score)

    # 集思录补充 YTM / 剩余年限 / 强赎触发价（可用时）
    jsl = cached("可转债-集思录", ak.bond_cb_jsl, TTL_SPOT, log)
    if jsl is not None and not jsl.empty:
        j = pd.DataFrame({
            "cb_code": jsl["代码"].astype(str).str.zfill(6),
            "ytm": to_num(jsl.get("到期税前收益")),
            "year_left": to_num(jsl.get("剩余年限")),
            "redeem_price": to_num(jsl.get("强赎触发价")),
            "jsl_double_low": to_num(jsl.get("双低")),
        })
        df = df.merge(j, on="cb_code", how="left")
    else:
        df["ytm"] = pd.NA
        df["year_left"] = pd.NA
        df["redeem_price"] = pd.NA
        df["jsl_double_low"] = pd.NA
    return df.reset_index(drop=True)


def cb_daily(code: str, log: FetchLog) -> pd.DataFrame:
    """可转债日线（新浪），用于持仓里没进全表时的估值回退。"""
    code = str(code).zfill(6)
    symbol = ("sh" if code.startswith("11") else "sz") + code
    df = cached(f"可转债日线-新浪-{symbol}",
                lambda: ak.bond_zh_hs_cov_daily(symbol=symbol), TTL_DAILY, log)
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    for c in ("close", "open", "high", "low", "volume"):
        if c in d.columns:
            d[c] = to_num(d[c])
    return d.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def bond_yield_curve(log: FetchLog) -> pd.DataFrame:
    df = cached("中美国债收益率", lambda: ak.bond_zh_us_rate(start_date="20150101"),
                TTL_SLOW, log)
    if df is None:
        return pd.DataFrame()
    out = df.rename(columns={
        "中国国债收益率2年": "cn_2y", "中国国债收益率5年": "cn_5y",
        "中国国债收益率10年": "cn_10y", "中国国债收益率30年": "cn_30y",
        "中国国债收益率10年-2年": "cn_10y_2y", "美国国债收益率10年": "us_10y",
    })
    out["date"] = pd.to_datetime(out["日期"], errors="coerce")
    cols = ["date", "cn_2y", "cn_5y", "cn_10y", "cn_30y", "cn_10y_2y", "us_10y"]
    return out[[c for c in cols if c in out.columns]].dropna(subset=["date"]).reset_index(drop=True)


# ------------------------------------------------------------------ ETF
def etf_list(log: FetchLog) -> pd.DataFrame:
    df = cached("ETF分类-新浪", lambda: ak.fund_etf_category_sina(symbol="ETF基金"),
                TTL_SLOW, log)
    if df is None:
        return pd.DataFrame()
    out = pd.DataFrame({
        "code": df["代码"].astype(str),
        "name": df["名称"].astype(str),
        "price": to_num(df["最新价"]),
        "change_pct": to_num(df["涨跌幅"]),
    })
    out["code6"] = out["code"].str.replace(r"^(sh|sz)", "", regex=True).str.zfill(6)
    return out


def etf_daily(code: str, log: FetchLog) -> pd.DataFrame:
    """ETF 日线（新浪优先，腾讯备），返回 date/close/volume/amount。"""
    def _sina():
        return ak.fund_etf_hist_sina(symbol=code)

    def _tx():
        df = ak.stock_zh_a_hist_tx(symbol=code)
        return df.rename(columns={"date": "date"})

    df = fetch_first([(f"ETF日线-新浪-{code}", _sina), (f"ETF日线-腾讯-{code}", _tx)],
                     ttl_hours=TTL_DAILY, log=log)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("close", "open", "high", "low", "volume", "amount"):
        if c in df.columns:
            df[c] = to_num(df[c])
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def etf_spot_filtered(log: FetchLog) -> pd.DataFrame:
    """只保留配置里 ETF 池的实时行情，附带配置中的中文名与风险标签。"""
    lst = etf_list(log)
    if lst.empty:
        return pd.DataFrame()
    rows = []
    by_code = {r["code"]: r for _, r in lst.iterrows()}
    for code, meta in cfg.ETF_UNIVERSE.items():
        r = by_code.get(code)
        rows.append({
            "code": code,
            "code6": code[2:],
            "name": meta["name"],
            "tag": meta["tag"],
            "risk": meta["risk"],
            "price": num(r["price"]) if r is not None else float("nan"),
            "change_pct": num(r["change_pct"]) if r is not None else float("nan"),
            "in_sina": r is not None,
        })
    return pd.DataFrame(rows)


def stock_daily_sina(code: str, exchange: str, log: FetchLog) -> pd.DataFrame:
    """个股前复权日线（新浪优先，腾讯备）。code 为 6 位数字。"""
    symbol = f"{exchange}{code}"

    def _sina():
        return ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")

    def _tx():
        return ak.stock_zh_a_hist_tx(symbol=symbol)

    df = fetch_first([(f"个股日线-新浪-{symbol}", _sina), (f"个股日线-腾讯-{symbol}", _tx)],
                     ttl_hours=TTL_DAILY, log=log)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("close", "open", "high", "low", "volume"):
        if c in df.columns:
            df[c] = to_num(df[c])
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


# ------------------------------------------------------------------ 财务
def financial_indicator(code: str, start_year: str, log: FetchLog) -> pd.DataFrame:
    """新浪财务指标：每股收益、每股净资产等（仅作 PB 回退用）。"""
    df = cached(f"财务指标-新浪-{code}-{start_year}",
                lambda: ak.stock_financial_analysis_indicator(symbol=code,
                                                              start_year=start_year),
                TTL_SLOW, log)
    if df is None:
        return pd.DataFrame()
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=500)
    df = df[df["日期"] >= cutoff]
    return df.dropna(subset=["日期"]).sort_values("日期", ascending=False)


def dividend_history(date: str, log: FetchLog) -> pd.DataFrame:
    """东财分红送配：按报告期取全市场分红方案。"""
    df = cached(f"分红送配-东财-{date}", lambda: ak.stock_fhps_em(date=date), TTL_SLOW, log)
    return df if df is not None else pd.DataFrame()


def index_cons(symbol: str, log: FetchLog) -> pd.DataFrame:
    df = cached(f"指数成分-{symbol}", lambda: ak.index_stock_cons(symbol=symbol),
                TTL_SLOW, log)
    if df is None:
        return pd.DataFrame()
    return df


def index_history(code: str, log: FetchLog) -> pd.DataFrame:
    """指数日线，用于回测基准。code 形如 sh000300。"""
    df = cached(f"指数日线-{code}", lambda: ak.stock_zh_index_daily(symbol=code),
                TTL_DAILY, log)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = to_num(df["close"])
    return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def index_valuation(symbol: str, log: FetchLog) -> pd.DataFrame:
    """指数估值分位（乐咕），用于判断大盘温度。"""
    df = cached(f"指数估值-{symbol}", lambda: ak.stock_index_pe_lg(symbol=symbol),
                TTL_SLOW, log)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    return df.dropna(subset=["日期"]).sort_values("日期").reset_index(drop=True)


def macro_snapshot(log: FetchLog) -> dict:
    """宏观快照：10 年国债、LPR、CPI。失败字段留空。"""
    out: dict = {}
    yc = bond_yield_curve(log)
    if not yc.empty:
        last = yc.dropna(subset=["cn_10y"]).tail(1)
        if not last.empty:
            out["cn_10y"] = num(last["cn_10y"].iloc[0])
            out["cn_10y_date"] = last["date"].iloc[0].strftime("%Y-%m-%d")
    lpr = cached("LPR", ak.macro_china_lpr, TTL_SLOW, log)
    if lpr is not None and not lpr.empty:
        try:
            out["lpr_1y"] = num(lpr["LPR1Y"].iloc[-1])
            out["lpr_5y"] = num(lpr["LPR5Y"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
    cpi = cached("CPI", ak.macro_china_cpi_monthly, TTL_SLOW, log)
    if cpi is not None and not cpi.empty:
        try:
            out["cpi_yoy"] = num(cpi["今值"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
    out["as_of"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return out
