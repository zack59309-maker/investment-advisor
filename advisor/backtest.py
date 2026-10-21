# -*- coding: utf-8 -*-
"""回测层：验证"动量/双低轮动"这类规则在历史上的表现，避免拍脑袋参数。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _freq_rule(freq: str) -> str:
    """把 pandas 的 resample 频率字符串映射成 to_period 能用的周期别名。"""
    f = freq.upper().rstrip("SE").rstrip("-")
    if f.startswith("W"):
        return "W"
    if f.startswith("M"):
        return "M"
    if f.startswith("Q"):
        return "Q"
    if f.startswith("Y") or f.startswith("A"):
        return "Y"
    return "M"


def returns_from_prices(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change().fillna(0.0)


def build_panel(daily_map: dict[str, pd.DataFrame], field: str = "close") -> pd.DataFrame:
    """把多只标的的日线拼成一张价格面板（列=代码，索引=日期）。"""
    series = {}
    for code, df in daily_map.items():
        if df is None or df.empty or field not in df.columns:
            continue
        s = df.set_index("date")[field].astype(float)
        s = s[~s.index.duplicated(keep="last")]
        series[code] = s
    if not series:
        return pd.DataFrame()
    panel = pd.DataFrame(series).sort_index()
    return panel


def momentum_rotation(prices: pd.DataFrame, lookback: int = 60, top_n: int = 5,
                      freq: str = "ME", cost_bps: float = 5.0,
                      start: str | None = None) -> dict:
    """每个调仓日按 lookback 日涨幅选前 top_n 只等权持有，直到下个调仓日。

    只在"当时已有足够历史数据"的标的里选，避免用上市前的假数据回测。
    """
    if prices is None or prices.empty:
        return {}
    p = prices.copy()
    if start:
        p = p[p.index >= pd.Timestamp(start)]
    p = p.dropna(how="all").ffill()
    valid = p.notna()
    if len(p) < lookback + 20:
        return {}

    rets = p.pct_change().fillna(0.0)
    dates = list(p.index)
    # 真实调仓日 = 每个自然月（或周）的最后一个"实际交易日"，
    # 不能直接用 resample().last() 的标签，因为标签可能是非交易日（如周末）
    periods = pd.Series(dates, index=pd.DatetimeIndex(dates)).resample(freq).last()
    by_period: dict = {}
    freq_rule = _freq_rule(freq)
    for d in dates:
        key = d.to_period(freq_rule)
        if key not in by_period or d > by_period[key]:
            by_period[key] = d
    rebal = set(by_period.values())

    weights = pd.Series(0.0, index=p.columns)
    port_rets: list[float] = []
    turnover_total = 0.0
    cost_rate = cost_bps / 10000.0
    for i, dt_ in enumerate(dates):
        cost_today = 0.0
        if i >= lookback and dt_ in rebal:
            hist = valid.iloc[: i + 1].sum()          # 各标的的实际有效历史长度
            eligible = hist[hist >= min(lookback, i)].index
            if len(eligible) >= 1:
                mom = (p.iloc[i] / p.iloc[i - lookback] - 1)[eligible].dropna()
                if not mom.empty:
                    chosen = mom.sort_values(ascending=False).head(top_n).index
                    new_w = pd.Series(0.0, index=p.columns)
                    new_w[chosen] = 1.0 / len(chosen)
                    turn = float((new_w - weights).abs().sum())
                    turnover_total += turn
                    cost_today = cost_rate * turn
                    weights = new_w
        r = float((rets.iloc[i] * weights).sum()) - cost_today
        port_rets.append(0.0 if i < lookback else r)

    port = pd.Series(port_rets, index=pd.DatetimeIndex(dates)).iloc[lookback:]
    m = _metrics(port, turnovers=turnover_total)
    m["交易成本(累计pct)"] = round(turnover_total * cost_bps / 100.0, 2)
    return m


def fixed_basket(prices: pd.DataFrame, weight_map: dict[str, float] | None = None,
                 freq: str = "ME", cost_bps: float = 5.0) -> dict:
    """固定权重组合，按 freq 再平衡；不传权重则等权。"""
    if prices is None or prices.empty:
        return {}
    prices = prices.dropna(how="all").ffill()
    cols = [c for c in prices.columns if c in (weight_map or prices.columns)]
    if not cols:
        return {}
    if weight_map:
        w = pd.Series({c: weight_map.get(c, 0.0) for c in cols})
        w = w / w.sum()
    else:
        w = pd.Series(1.0 / len(cols), index=cols)
    rets = prices[cols].pct_change().fillna(0.0)
    rebal = set(prices[cols].resample(freq).last().index)
    port_rets = []
    cur = w.copy()
    turnover = 0.0
    for i, dt_ in enumerate(prices.index):
        if i > 0 and dt_ in rebal:
            # 漂移到最新权重再拉回目标
            grow = (1 + rets[cols].iloc[:i]).prod()
            drifted = cur * grow / float((cur * grow).sum())
            turnover += float((w - drifted).abs().sum())
            cur = w.copy()
        port_rets.append(float((rets[cols].iloc[i] * cur).sum()))
    port = pd.Series(port_rets, index=prices.index)
    m = _metrics(port, turnovers=turnover)
    m["累计成本(pct)"] = round(turnover * cost_bps / 100.0, 2)
    return m


def _metrics(port: pd.Series, turnovers: float = 0.0, rf_annual: float = 0.017) -> dict:
    port = port.dropna()
    if port.empty:
        return {}
    nav = (1 + port).cumprod()
    years = max((port.index[-1] - port.index[0]).days / 365.25, 1 / 365.25)
    total = float(nav.iloc[-1] - 1)
    cagr = float((nav.iloc[-1]) ** (1 / years) - 1) if years > 0.5 else float("nan")
    vol = float(port.std() * math.sqrt(252))
    sharpe = (cagr - rf_annual) / vol if vol and not math.isnan(cagr) else float("nan")
    dd = float((nav / nav.cummax() - 1).min())
    monthly = (1 + port).resample("ME").prod() - 1
    win = float((monthly > 0).mean() * 100) if len(monthly) else float("nan")
    return {
        "区间": f"{port.index[0].date()} ~ {port.index[-1].date()}",
        "年数": round(years, 2),
        "累计收益": f"{total*100:.1f}%",
        "年化收益": f"{cagr*100:.1f}%" if not math.isnan(cagr) else "NA",
        "年化波动": f"{vol*100:.1f}%",
        "夏普": round(sharpe, 2) if not math.isnan(sharpe) else "NA",
        "最大回撤": f"{dd*100:.1f}%",
        "月胜率": f"{win:.0f}%" if not math.isnan(win) else "NA",
        "换手(累计)": round(turnovers, 1),
    }


def compare_vs_benchmark(port_metrics: dict, bench_metrics: dict) -> pd.DataFrame:
    keys = sorted(set(port_metrics) | set(bench_metrics))
    rows = []
    for k in keys:
        rows.append({"指标": k, "策略": port_metrics.get(k, ""), "基准": bench_metrics.get(k, "")})
    return pd.DataFrame(rows)


def parameter_sweep(prices: pd.DataFrame, lookbacks: list[int], tops: list[int],
                    freq: str = "ME", start: str | None = None,
                    cost_bps: float = 5.0) -> pd.DataFrame:
    """参数扫描：看清哪些组合真的稳健，而不是只挑一个好看的数字。"""
    rows = []
    for lb in lookbacks:
        for n in tops:
            m = momentum_rotation(prices, lookback=lb, top_n=n, freq=freq,
                                  cost_bps=cost_bps, start=start)
            if not m:
                continue
            rows.append({
                "动量窗口": lb, "持仓数": n,
                "年化收益": m.get("年化收益"), "年化波动": m.get("年化波动"),
                "夏普": m.get("夏普"), "最大回撤": m.get("最大回撤"),
                "月胜率": m.get("月胜率"), "累计换手": m.get("换手(累计)"),
            })
    return pd.DataFrame(rows).sort_values("夏普", ascending=False).reset_index(drop=True)
