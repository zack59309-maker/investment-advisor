# -*- coding: utf-8 -*-
"""选券/选股引擎：把原始行情与财务数据折算成评分、评级与具体买卖价位。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import config as cfg
from .core import FetchLog, num
from . import datasource as ds


# ------------------------------------------------------------------ 技术指标
def tech_snapshot(daily: pd.DataFrame) -> dict:
    """从日线里算动量、均线、波动率、支撑压力、ATR。"""
    if daily is None or daily.empty or len(daily) < 30:
        return {}
    d = daily.tail(500).copy()
    close = d["close"].astype(float)
    last = float(close.iloc[-1])

    def ret(n: int) -> float:
        if len(close) <= n:
            return float("nan")
        return float(close.iloc[-1] / close.iloc[-1 - n] - 1) * 100

    daily_ret = close.pct_change().dropna()
    vol = float(daily_ret.tail(60).std() * math.sqrt(252) * 100) if len(daily_ret) > 20 else float("nan")
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float("nan")
    ma120 = float(close.tail(120).mean()) if len(close) >= 120 else float("nan")

    # ATR14
    atr = float("nan")
    if {"high", "low"}.issubset(d.columns) and len(d) > 20:
        h, l, c = d["high"].astype(float), d["low"].astype(float), close
        tr = pd.concat([(h - l).abs(), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean())

    low60 = float(close.tail(60).min())
    high60 = float(close.tail(60).max())
    return {
        "last": last,
        "ret20": ret(20), "ret60": ret(60), "ret120": ret(120),
        "vol_annual": vol,
        "ma20": ma20, "ma60": ma60, "ma120": ma120,
        "above_ma20": bool(last > ma20),
        "above_ma60": bool(pd.notna(ma60) and last > ma60),
        "atr": atr,
        "atr_pct": (atr / last * 100) if pd.notna(atr) and last else float("nan"),
        "low60": low60, "high60": high60,
        "drawdown_from_high": (last / high60 - 1) * 100 if high60 else float("nan"),
    }


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def scale(x: float, lo: float, hi: float) -> float:
    """把 x 从 [lo,hi] 线性映射到 0~1；nan 返回 0。"""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if hi == lo:
        return 0.0
    return clamp((x - lo) / (hi - lo), 0.0, 1.0)


# ------------------------------------------------------------------ 可转债
def screen_cb(board: pd.DataFrame, profile: dict) -> pd.DataFrame:
    if board is None or board.empty:
        return pd.DataFrame()
    f = profile["cb_filters"]
    d = board.copy()

    d = d[d["cb_price"] <= f["price_max"]]
    d = d[d["premium_rate"] <= f["premium_max"]]
    if f.get("exclude_st"):
        d = d[~d["stock_name"].fillna("").str.contains("ST", case=False, na=False)]
    if f.get("min_turnover"):
        d = d[d["cb_amount"].fillna(0) >= f["min_turnover"]]
    if f.get("rating_min"):
        d = d[d["rating"].map(lambda r: cfg.rating_at_least(r, f["rating_min"]))]
    if d.empty:
        return d

    # 评分：双低越低越好(45) + 低价(20) + 低溢价(20) + 评级(15)
    d["s_dblow"] = d["double_low"].map(lambda x: 1 - scale(x, 100, 160))
    d["s_price"] = d["cb_price"].map(lambda x: 1 - scale(x, 95, f["price_max"]))
    d["s_prem"] = d["premium_rate"].map(lambda x: 1 - scale(x, 0, f["premium_max"]))
    d["s_rating"] = d["rating_score"].map(lambda x: scale(x, 0, len(cfg.RATING_ORDER)))
    d["score"] = (d["s_dblow"] * 45 + d["s_price"] * 20 + d["s_prem"] * 20
                  + d["s_rating"] * 15).round(1)

    # 价位：买入挂单价略低于现价，止损用 ATR 或固定比例，止盈参考强赎触发价/固定比例
    d["buy_limit"] = (d["cb_price"] * 0.995).round(3)
    sl = profile["stop_loss"]["cb"]
    d["stop_loss"] = (d["buy_limit"] * (1 - sl)).round(3)
    tp = profile["take_profit"]["cb"]
    tp_fixed = d["buy_limit"] * (1 + tp)
    d["take_profit"] = np.where(
        d["redeem_price"].notna() & (d["redeem_price"] > d["buy_limit"] * 1.02),
        np.minimum(tp_fixed, d["redeem_price"].fillna(tp_fixed)),
        tp_fixed,
    ).round(3)
    d["reason"] = [
        f"双低{v:.0f}；价格{p:.1f}元；溢价{pr:.1f}%；评级{r}"
        for v, p, pr, r in zip(d["double_low"], d["cb_price"], d["premium_rate"], d["rating"])
    ]
    return d.sort_values("score", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------ ETF
def screen_etf(etf_spot: pd.DataFrame, tier: str, profile: dict, log: FetchLog) -> pd.DataFrame:
    if etf_spot is None or etf_spot.empty:
        return pd.DataFrame()
    allow_risk = cfg.ETF_RISK_ALLOW.get(tier, {"low", "mid", "high"})
    d = etf_spot[etf_spot["in_sina"] & etf_spot["risk"].isin(allow_risk)].copy()
    rows = []
    for _, r in d.iterrows():
        daily = ds.etf_daily(r["code"], log)
        t = tech_snapshot(daily)
        if not t:
            continue
        rows.append({**r.to_dict(), **t})
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    vol = out["volume"].fillna(0) if "volume" in out.columns else pd.Series([0.0] * len(out), index=out.index)
    out["amount_est"] = out["last"] * vol

    mw = profile["etf_filters"]["mom_window"]
    mom_col = f"ret{mw}"
    if mom_col not in out.columns:            # 窗口无对应列则取最接近的
        avail = [c for c in ("ret20", "ret60", "ret120") if c in out.columns]
        mom_col = min(avail, key=lambda c: abs(int(c[3:]) - mw)) if avail else "ret60"

    # 打分：动量/趋势优先，低波动只是加分项，避免货币/短融类"低波动陷阱"排到最前
    out["s_mom"] = out[mom_col].map(lambda x: scale(x, -8, 20))
    out["s_mom20"] = out["ret20"].map(lambda x: scale(x, -4, 8))
    out["s_ma"] = out.apply(lambda r: (0.5 if r["above_ma20"] else 0) + (0.5 if r["above_ma60"] else 0), axis=1)
    out["s_vol"] = out["vol_annual"].map(lambda x: 1 - scale(x, 20, 50))
    out["s_dd"] = out["drawdown_from_high"].map(lambda x: 1 - scale(-x, 0, 15))
    out["score"] = (out["s_mom"] * 45 + out["s_mom20"] * 15 + out["s_ma"] * 20
                    + out["s_vol"] * 10 + out["s_dd"] * 10).round(1)

    if profile["etf_filters"].get("require_positive_mom"):
        out = out[out[mom_col] > 0]

    out["buy_limit"] = (out["last"] * 0.99).round(3)
    sl = profile["stop_loss"]["etf"]
    out["stop_loss"] = (out["buy_limit"] * (1 - sl)).round(3)
    out["take_profit"] = (out["buy_limit"] * (1 + profile["take_profit"]["etf"])).round(3)
    out["reason"] = [
        f"{tag}；{int(mom_col[3:])}日动量{m:.1f}%；年化波动{v:.0f}%；距60日高点{d:.1f}%"
        for tag, m, v, d in zip(out["tag"], out[mom_col], out["vol_annual"], out["drawdown_from_high"])
    ]
    return out.sort_values("score", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------ 个股
def dividend_yield_map(log: FetchLog) -> dict[str, float]:
    """从东财分红送配表取各股股息率(%)，近三年年报数据去重合并。"""
    year = pd.Timestamp.now().year
    frames = []
    for y in (year - 1, year - 2, year - 3):
        df = ds.dividend_history(f"{y}1231", log)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return {}
    d = pd.concat(frames, ignore_index=True)
    if "现金分红-股息率" not in d.columns:
        return {}
    d = d.dropna(subset=["现金分红-股息率"])
    d["code"] = d["代码"].astype(str).str.zfill(6)
    d = d.drop_duplicates(subset=["code"], keep="first")
    return {r["code"]: float(r["现金分红-股息率"]) * 100 for _, r in d.iterrows()}


def _fundamentals(code: str, spot_row: pd.Series, log: FetchLog,
                  div_map: dict[str, float] | None = None) -> dict:
    """取股息率（东财分红表）与 PB（行情附带或财务指标回退）。"""
    info: dict = {}
    price = num(spot_row.get("price"))
    if div_map and code in div_map:
        info["div_yield"] = div_map[code]
    pb = num(spot_row.get("pb"))
    if pd.notna(pb) and pb > 0:
        info["pb"] = pb
    if "pb" not in info:
        ind = ds.financial_indicator(code, start_year=str(pd.Timestamp.now().year - 1), log=log)
        if not ind.empty and "每股净资产_调整后(元)" in ind.columns:
            bps = num(ind["每股净资产_调整后(元)"].iloc[0])
            if price and bps:
                info["pb"] = price / bps
    return info


def screen_stocks(candidates: list[str], spot: pd.DataFrame, profile: dict, log: FetchLog,
                  mode: str = "dividend", div_map: dict[str, float] | None = None,
                  return_all: bool = False):
    """mode=dividend 走红利价值逻辑；mode=growth 走动量成长逻辑。

    return_all=True 时返回 (入选, 全部打分) 二元组，便于生成"差一点"的观察名单。
    """
    if spot is None or spot.empty or not candidates:
        return pd.DataFrame()
    by_code = spot.drop_duplicates(subset=["code6"]).set_index("code6")
    rows = []
    for code in candidates:
        if code not in by_code.index:
            continue
        srow = by_code.loc[code]
        if isinstance(srow, pd.DataFrame):
            srow = srow.iloc[0]
        exchange = str(srow.get("exchange", "sh"))
        daily = ds.stock_daily_sina(code, exchange, log)
        t = tech_snapshot(daily)
        if not t:
            continue
        fund = _fundamentals(code, srow, log, div_map) if mode == "dividend" else {}
        rows.append({
            "code": code, "name": srow.get("name"), "exchange": exchange,
            "change_pct": num(srow.get("change_pct")),
            **t, **fund,
        })
    if not rows:
        return (pd.DataFrame(), pd.DataFrame()) if return_all else pd.DataFrame()
    out = pd.DataFrame(rows)

    out = _score_frame(out, mode)
    all_scored = out.copy()

    # 入选条件（硬性）
    if mode == "dividend":
        out = out[out["drawdown_from_high"].fillna(0) > -45]      # 剔除破位暴跌的
        out = out[out["trend_ok"]]                                 # 必须站上20日线
    else:
        out = out[out["above_ma20"] | (out["ret60"] > 5)]

    if out.empty:
        return (out, all_scored) if return_all else out

    key = "dividend" if mode == "dividend" else "growth"
    out["buy_limit"] = (out["last"] * 0.99).round(2)
    sl = profile["stop_loss"][key]
    atr_stop = out["atr_pct"].fillna(0) * 2.5 / 100
    use_sl = np.clip(np.maximum(sl, atr_stop), sl, sl * 1.6)
    out["stop_loss"] = (out["buy_limit"] * (1 - use_sl)).round(2)
    out["take_profit"] = (out["buy_limit"] * (1 + profile["take_profit"][key])).round(2)
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    return (out, all_scored) if return_all else out


def _score_frame(out: pd.DataFrame, mode: str) -> pd.DataFrame:
    """给候选池打上评分、理由与趋势判定（不剔除任何行）。"""
    out = out.copy()
    if mode == "dividend":
        for col in ("div_yield", "pb"):
            if col not in out.columns:
                out[col] = float("nan")
        out["trend_ok"] = out["above_ma20"]
        out["s_yield"] = out["div_yield"].map(lambda x: scale(x, 1.0, 7.0))
        out["s_pb"] = out["pb"].map(lambda x: 1 - scale(x, 0.4, 3.0))
        out["s_ma"] = out.apply(lambda r: (0.5 if r["above_ma20"] else 0) + (0.5 if r["above_ma60"] else 0), axis=1)
        out["s_dd"] = out["drawdown_from_high"].map(lambda x: 1 - scale(-x, 0, 30))
        out["score"] = (out["s_yield"] * 40 + out["s_pb"] * 20 + out["s_ma"] * 20
                        + out["s_dd"] * 20).round(1)
        out["reason"] = [
            f"股息率{y:.2f}%；PB{pb:.2f}；距60日高点{d:.1f}%；" + ("站上20/60日线" if a and b else "均线未完全走好")
            for y, pb, d, a, b in zip(out["div_yield"], out["pb"], out["drawdown_from_high"],
                                      out["above_ma20"], out["above_ma60"])
        ]
    else:
        out["trend_ok"] = out["above_ma20"]
        out["s_mom"] = out["ret60"].map(lambda x: scale(x, -5, 30))
        out["s_mom20"] = out["ret20"].map(lambda x: scale(x, -5, 15))
        out["s_ma"] = out.apply(lambda r: (0.5 if r["above_ma20"] else 0) + (0.5 if r["above_ma60"] else 0), axis=1)
        out["s_vol"] = out["vol_annual"].map(lambda x: scale(x, 25, 70))
        out["score"] = (out["s_mom"] * 35 + out["s_mom20"] * 20 + out["s_ma"] * 25
                        + out["s_vol"] * 20).round(1)
        out["reason"] = [
            f"60日动量{m:.1f}%；20日动量{m2:.1f}%；年化波动{v:.0f}%；" + ("站上20/60日线" if a and b else "20日线上")
            for m, m2, v, a, b in zip(out["ret60"], out["ret20"], out["vol_annual"],
                                      out["above_ma20"], out["above_ma60"])
        ]
    return out


def near_miss_notes(screened: pd.DataFrame, mode: str) -> list[str]:
    """当某类资产筛不出标的时，给出可解释的说明与"差一点"的观察名单。"""
    if screened is None or screened.empty:
        return [f"{'红利' if mode == 'dividend' else '成长'}档：候选池无满足条件的标的（均线/动量全部不达标）"]
    if mode != "growth":
        return []
    n = len(screened)
    above20 = int(screened["above_ma20"].sum())
    if n == 0:
        return []
    top = screened.sort_values("ret60", ascending=False).head(3)
    notes = [f"成长档池内 {n} 只候选，其中仅 {above20} 只站上20日线；"
             f"动量最强的三只（仅观察，不买入）："
             + "、".join(f'{r["name"]}({r["code"]}) 60日{r["ret60"]:.1f}%' for _, r in top.iterrows())]
    return notes


def pick_etf_core(etf_df: pd.DataFrame, tier: str, max_positions: int) -> pd.DataFrame:
    """组合层挑选：保守档先配债券/红利类作为底仓，再用最有动量的宽基/行业补齐。

    纯动量排序会把债券ETF排除在短线交易逻辑之外，这里补上"底仓优先"的规则。
    """
    if etf_df is None or etf_df.empty or max_positions <= 0:
        return pd.DataFrame()
    if tier != "conservative":
        return etf_df.sort_values("score", ascending=False).head(max_positions)

    bond = etf_df[etf_df["tag"] == "债券"].sort_values("score", ascending=False).head(1)
    picked = bond
    remain = max_positions - len(picked)
    if remain > 0:
        pool = etf_df[~etf_df["code"].isin(picked["code"])]
        equity_pref = pool[pool["tag"].isin(["宽基", "红利"])].sort_values("score", ascending=False)
        other = pool[~pool["code"].isin(equity_pref["code"])].sort_values("score", ascending=False)
        extra = pd.concat([equity_pref, other]).head(remain)
        picked = pd.concat([picked, extra])
    return picked.reset_index(drop=True)


def market_temperature(log: FetchLog) -> dict:
    """用沪深300估值分位做市场温度，用于整体仓位建议。"""
    val = ds.index_valuation("沪深300", log)
    out: dict = {}
    if not val.empty and "滚动市盈率" in val.columns:
        pe = pd.to_numeric(val["滚动市盈率"], errors="coerce").dropna()
        if len(pe) > 100:
            last = float(pe.iloc[-1])
            pct = float((pe < last).mean() * 100)
            out["hs300_pe"] = round(last, 2)
            out["hs300_pe_pct"] = round(pct, 1)
            out["temp"] = ("偏冷" if pct < 30 else "中性" if pct < 70 else "偏热")
    idx = ds.index_history("sh000300", log)
    if not idx.empty:
        t = tech_snapshot(idx)
        out["hs300_ret60"] = round(t.get("ret60", float("nan")), 2)
        out["hs300_above_ma60"] = t.get("above_ma60")
    return out
