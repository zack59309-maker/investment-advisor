# -*- coding: utf-8 -*-
"""报告层：生成中文 Markdown 日报（持仓 / 调仓 / 执行清单）。"""
from __future__ import annotations

import os

import pandas as pd

from . import config as cfg
from .core import REPORT_DIR, today
from .holdings import positions_frame
from .portfolio import action_list, position_sizing_table
from .rebalance import action_table, orders_frame

CATEGORY_CN = {"etf": "ETF", "cb": "可转债", "dividend": "红利价值股",
               "growth": "成长/主题股", "stock": "其他个股", "cash": "现金/货基"}
FENCE = chr(96) * 3


def _md_table(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    if df is None or df.empty:
        return "（无数据）"
    d = df[cols] if cols else df
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "---|" * len(d.columns)
    rows = []
    for _, r in d.iterrows():
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append("" if pd.isna(v) else f"{v:g}")
            else:
                cells.append("" if v is None else str(v))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def _health_table(log_df: pd.DataFrame) -> pd.DataFrame:
    if log_df is None or log_df.empty:
        return pd.DataFrame()
    d = log_df.copy()
    d["来源"] = (d["数据源"].str.replace(r"-(sh|sz|bj)?[0-9]{6}.*$", "", regex=True)
                 .str.replace(r"-[0-9]{8}$", "", regex=True))
    rank = {"失败": 0, "过期缓存": 1, "在线": 2, "缓存": 3}
    rows = []
    for src, g in d.groupby("来源", sort=False):
        worst = min(g["状态"], key=lambda s: rank.get(s, 9))
        rows.append({
            "数据源": src, "条目数": len(g),
            "最差状态": "❌ " + worst if worst in ("失败", "过期缓存") else "✅ " + worst,
            "示例": str(g["说明"].iloc[0])[:60],
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("最差状态").reset_index(drop=True)
    return out


def _actual_weights(positions, cash: float) -> dict[str, float]:
    total = cash + sum(p.market_value for p in positions)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for p in positions:
        key = _category_of(p)
        out[key] = out.get(key, 0.0) + p.market_value / total
    out["cash"] = cash / total
    return out


def _category_of(p) -> str:
    if p.asset == "etf":
        return "etf"
    if p.asset == "cb":
        return "cb"
    if p.asset in ("dividend", "growth"):
        return p.asset
    code = p.code[-6:]
    if code in {c[-6:] for c in cfg.DIVIDEND_CANDIDATES}:
        return "dividend"
    if code in {c[-6:] for c in cfg.GROWTH_CANDIDATES}:
        return "growth"
    return "stock"


def generate_report(tier: str, portfolio: dict, picks: dict[str, pd.DataFrame],
                    macro: dict, temp: dict, log_df: pd.DataFrame,
                    benchmark: dict | None = None, notes: list[str] | None = None,
                    positions: list | None = None, plan=None,
                    account: dict | None = None, mode: str = "build") -> str:
    p = portfolio["profile"]
    positions = positions or []
    account = account or {}
    equity = float(account.get("equity") or portfolio["capital"])
    cash = float(account.get("cash") or 0.0)
    is_rebalance = mode == "rebalance" and len(positions) > 0

    L: list[str] = []
    _n = [0]

    def H(title: str) -> str:
        _n[0] += 1
        return f"## {'一二三四五六七八九十'[_n[0] - 1]}、{title}"

    L.append(f"# 个人投资决策日报 · {p['name']} · {today()}")
    L.append("")
    L.append(f"- 风险定位：{p['desc']}")
    L.append(f"- 目标收益：{p['target_return']}　|　回撤容忍：{p['max_drawdown']}　|　调仓节奏：{p['rebalance']}")
    if is_rebalance:
        L.append(f"- 模式：**持仓调仓**　|　总资产：{equity:,.0f} 元"
                 f"（持仓 {account.get('market_value', 0):,.0f} + 现金 {cash:,.0f}）"
                 f"　|　仓位：{account.get('invested_pct', 0)}%")
    else:
        L.append(f"- 模式：**新建仓**　|　本金：{portfolio['capital']:,.0f} 元"
                 f"　|　已配置：{portfolio['invested_pct']}%　|　现金：{portfolio['cash_pct']}%")
    L.append(f"- 市场温度：{temp.get('temp', '未知')}"
             f"（沪深300 PE {temp.get('hs300_pe', 'NA')}，历史分位 {temp.get('hs300_pe_pct', 'NA')}%）"
             f"　|　10年国债 {macro.get('cn_10y', 'NA')}%（{macro.get('cn_10y_date', '')}）"
             + (f"　|　LPR 1Y {macro.get('lpr_1y', 'NA')}%" if macro.get("lpr_1y") else ""))
    L.append("")

    L.append(H("资产配置（目标 vs 实际）"))
    L.append("")
    if is_rebalance:
        actual = _actual_weights(positions, cash)
    else:
        actual = {}
        if not portfolio["allocation"].empty:
            cn2k = {"ETF": "etf", "可转债": "cb", "红利价值股": "dividend",
                    "成长/主题股": "growth", "现金/货基": "cash"}
            actual = {cn2k[k]: v / 100.0 for k, v in portfolio["allocation"].items() if k in cn2k}
    rows = []
    for k in ("etf", "cb", "dividend", "growth", "stock", "cash"):
        tgt = portfolio["weights"].get(k, 0.0) * 100
        act = actual.get(k, 0.0) * 100
        if k == "stock" and act <= 0 and tgt <= 0:
            continue
        rows.append({"资产类别": CATEGORY_CN[k], "目标权重": f"{tgt:.1f}%",
                     "实际权重": f"{act:.1f}%", "说明": _asset_note(k, portfolio, macro)})
    L.append(_md_table(pd.DataFrame(rows)))
    L.append("")
    if notes:
        L.append("**今日无标的/标的不足的原因（重要）**")
        L.append("")
        for n in notes:
            L.append(f"- {n}")
        L.append("")
        L.append("> 规则筛不出标的时，资金留在现金/货基而不是硬凑。这是纪律，不是失误。")
        L.append("")

    if is_rebalance:
        L.append(H("当前持仓"))
        L.append("")
        L.append(_md_table(positions_frame(positions, cash)))
        L.append("")
        s = account.get("summary") or {}
        if s:
            L.append(f"- 持仓市值 {s.get('market_value', 0):,.0f} 元　|　"
                     f"成本 {s.get('cost_value', 0):,.0f} 元　|　"
                     f"浮盈 {s.get('unrealized_pnl', 0):,.0f} 元　|　"
                     f"已实现 {s.get('realized_pnl', 0):,.0f} 元")
            L.append("")

    if is_rebalance and plan is not None:
        L.append(H("今日调仓清单（按此挂单）"))
        L.append("")
        L.append(_md_table(orders_frame(plan)))
        L.append("")
        if plan.warnings:
            L.append("**调仓提示**")
            L.append("")
            for w in plan.warnings:
                L.append(f"- {w}")
            L.append("")
    else:
        L.append(H("今日买入清单（具体标的）"))
        L.append("")
        L.append(_md_table(portfolio["holdings"], ["类别", "代码", "名称", "最新价", "买入挂单价",
                                                  "买入数量", "买入金额", "实际权重", "止损价", "止盈价", "评分"]))
        L.append("")
        L.append("### 每只标的的入选理由")
        L.append("")
        for _, h in portfolio["holdings"].iterrows():
            if h["asset"] == "cash":
                L.append(f"- **{h['名称']}**：{h['理由']}")
            else:
                L.append(f"- **{h['名称']}（{h['代码']}）**：{h['理由']}"
                         f"　→ 买入 ≤{h['买入挂单价']}，止损 {h['止损价']}，止盈 {h['止盈价']}")
        L.append("")

    L.append(H("执行清单（照着做即可）"))
    L.append("")
    actions = action_table(plan) if (is_rebalance and plan is not None) else action_list(portfolio)
    L.append(_md_table(actions, ["优先级", "动作", "标的", "指令", "触发条件"]))
    L.append("")

    if not is_rebalance:
        L.append(H("不同本金下的手数对照"))
        L.append("")
        L.append(position_sizing_table(portfolio))
        L.append("")

    L.append(H("备选池（前 10，未进组合但可轮动）"))
    L.append("")
    for asset, cn in [("cb", "可转债"), ("etf", "ETF"), ("dividend", "红利价值股"), ("growth", "成长/主题股")]:
        df = picks.get(asset)
        if df is None or df.empty:
            continue
        sel = df.head(10)
        cols = ["cb_code" if asset == "cb" else "code", "cb_name" if asset == "cb" else "name",
                "cb_price" if asset == "cb" else "last", "score"]
        cols = [c for c in cols if c in sel.columns]
        L.append(f"**{cn}**")
        L.append("")
        L.append(_md_table(sel[cols]))
        L.append("")

    if benchmark:
        L.append(H("规则回测（等权持有 N 只，按月轮动）"))
        L.append("")
        L.append(_md_table(pd.DataFrame([benchmark])))
        L.append("")

    L.append(H("数据体检"))
    L.append("")
    health = _health_table(log_df)
    L.append(_md_table(health) if not health.empty else "（无日志）")
    L.append("")
    bad = [] if health.empty else health[health["最差状态"].str.startswith("❌")]
    if not bad.empty:
        L.append("> ⚠️ 存在失败的数据源。**当天不要按本报告下单**，"
                 "先执行 pip install -U akshare 并重跑；若仍失败，检查网络/代理。")
        L.append("")

    L.append(H("风险声明"))
    L.append("")
    L.append("> 本报告由规则引擎根据公开数据自动生成，**不构成投资建议**。"
             "所有价位是基于当前价格的机械推算，未考虑突发消息、停牌、强赎、违约、流动性等风险。"
             "请自行核对数据，并用不超过总资产 30% 的资金做验证。")
    L.append("")
    L.append(H("可选：交给大模型做二次解读的 prompt"))
    L.append("")
    L.append(FENCE + "text")
    L.append("你是我的投资研究助理。下面是一份规则引擎生成的组合与调仓计划，请：")
    L.append("1) 逐只指出基本面/消息面风险点（尤其是可转债的强赎、正股 ST、评级下调）；")
    L.append("2) 指出组合的集中度与相关性风险；")
    L.append("3) 对每只标的给出'执行/观望/放弃'三选一，并说明理由；")
    L.append("4) 不要给出新的价格预测。")
    L.append("")
    if is_rebalance:
        L.append("当前持仓：")
        for pos in positions:
            L.append(f"- {pos.code} {pos.name} 数量{pos.quantity:g} 成本{pos.avg_cost:g} "
                     f"现价{(pos.last_price if pos.priced else pos.avg_cost):g}")
        L.append("")
        L.append("调仓计划：")
        for o in (plan.orders if plan else []):
            L.append(f"- {o.side} {o.code} {o.name} {o.quantity:g} @ {o.price:g}；{o.reason}")
    else:
        L.append("候选组合：")
        for _, h in portfolio["holdings"].iterrows():
            L.append(f"- {h['类别']} {h['代码']} {h['名称']} 买入价{h['买入挂单价']} "
                     f"止损{h['止损价']} 理由:{h['理由']}")
    L.append(FENCE)
    L.append("")
    return "\n".join(L)


def _asset_note(k: str, portfolio: dict, macro: dict) -> str:
    if k == "cash":
        return f"参考收益≈{portfolio['cash_yield']:.2f}%（10年国债+0.5%），随时可取"
    if k == "cb":
        return "债底+期权，双低与低溢价优先，注意强赎与评级"
    if k == "etf":
        return "宽基/红利/行业ETF，按动量+均线+波动筛选"
    if k == "dividend":
        return "高股息大票，股息率+PB+趋势打分"
    if k == "growth":
        return "高波动成长，只做趋势向上，严格止损"
    if k == "stock":
        return "持仓中不在本期候选池的个股，未纳入打分"
    return ""


def save_report(tier: str, md: str, holdings: pd.DataFrame, picks: dict[str, pd.DataFrame],
                mode: str = "build", orders: pd.DataFrame | None = None) -> dict:
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = today()
    label = "调仓" if mode == "rebalance" else "决策"
    hold_label = "目标组合" if mode == "rebalance" else "买入"
    md_path = os.path.join(REPORT_DIR, f"{stamp}_{tier}_{label}日报.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    paths = {"markdown": md_path}
    if holdings is not None and not holdings.empty:
        csv_path = os.path.join(REPORT_DIR, f"{stamp}_{tier}_{hold_label}清单.csv")
        holdings.to_csv(csv_path, index=False, encoding="utf-8-sig")
        paths["holdings_csv"] = csv_path
    if orders is not None and not orders.empty:
        p = os.path.join(REPORT_DIR, f"{stamp}_{tier}_调仓指令.csv")
        orders.to_csv(p, index=False, encoding="utf-8-sig")
        paths["orders_csv"] = p
    for asset, df in picks.items():
        if df is not None and not df.empty:
            p = os.path.join(REPORT_DIR, f"{stamp}_{tier}_候选池_{asset}.csv")
            df.to_csv(p, index=False, encoding="utf-8-sig")
            paths[f"picks_{asset}"] = p
    return paths
