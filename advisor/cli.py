# -*- coding: utf-8 -*-
"""命令行入口：advise / holdings / trade / backtest / data-check / ui。"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

from . import backtest as bt
from . import config as cfg
from . import datasource as ds
from . import holdings as hd
from . import screener
from . import store
from .core import REPORT_DIR, FetchLog, dump_json, stamp
from .portfolio import build_portfolio
from .quotes import fetch_quotes
from .rebalance import orders_frame, plan_rebalance
from .report import generate_report, save_report
from .screener import market_temperature, screen_cb, screen_etf, screen_stocks


DIVIDEND_POOL_BY_TIER = {"conservative": 16, "balanced": 20, "aggressive": 10}
GROWTH_POOL_BY_TIER = {"conservative": 0, "balanced": 8, "aggressive": 18}


def _fetch_stock_universe(tier: str, log: FetchLog) -> pd.DataFrame:
    return ds.a_spot(log)


def run_advise(tier: str, capital: float = 100000, refresh: bool = False,
               with_backtest: bool = False, quiet: bool = False, progress=None,
               mode: str = "auto", sell_unknown: bool = True) -> dict:
    if tier not in cfg.RISK_PROFILES:
        raise SystemExit(f"未知风险档位: {tier}，可选 {list(cfg.RISK_PROFILES)}")
    if mode not in ("auto", "build", "rebalance"):
        raise SystemExit(f"未知模式: {mode}，可选 auto/build/rebalance")

    profile = cfg.RISK_PROFILES[tier]
    log = FetchLog()
    t0 = time.time()

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)
        if not quiet:
            print(f"[{time.time()-t0:5.1f}s] {msg}", flush=True)

    positions, cash = hd.load_positions()
    if mode == "auto":
        mode = "rebalance" if positions else "build"
    if mode == "rebalance" and not positions:
        raise SystemExit("当前没有持仓。先用 holdings import 导入，或加 --mode build 新建仓。")
    if mode == "build":
        positions, cash = [], 0.0
        capital = float(capital or 100000)

    say("拉取可转债全表 ...")
    cb_board = ds.cb_board(log)
    say(f"可转债 {0 if cb_board is None else len(cb_board)} 条")

    say("拉取 ETF 池行情 ...")
    etf_spot = ds.etf_spot_filtered(log)
    etf_list = ds.etf_list(log)

    say("拉取 A 股实时行情 ...")
    spot = ds.a_spot(log)
    say(f"A股 {0 if spot is None else len(spot)} 条")

    say("拉取宏观（国债收益率/LPR/CPI）...")
    macro = ds.macro_snapshot(log)
    say("计算市场温度 ...")
    temp = market_temperature(log)

    account: dict = {}
    if mode == "rebalance":
        say(f"读取持仓 {len(positions)} 只，估值中 ...")
        prices = fetch_quotes(positions, log, spot=spot, cb_board=cb_board, etf_list=etf_list)
        for p in positions:
            p.last_price = prices.get(p.code, p.avg_cost)
        account = hd.summary(positions, cash)
        capital = float(account["equity"])
        say(f"总资产 {capital:,.0f} 元（持仓 {account['market_value']:,.0f} + 现金 {cash:,.0f}）")

    say("筛选可转债 ...")
    cb_pick = screen_cb(cb_board, profile)
    say(f"可转债入选候选 {0 if cb_pick is None else len(cb_pick)} 只")

    say("筛选 ETF ...")
    etf_all = screen_etf(etf_spot, tier, profile, log)
    etf_pick = screener.pick_etf_core(etf_all, tier, profile["max_positions"]["etf"])
    say(f"ETF 候选 {0 if etf_all is None else len(etf_all)} 只，入池 {0 if etf_pick is None else len(etf_pick)} 只")

    div_pick = pd.DataFrame()
    notes: list[str] = []
    if profile["weights"].get("dividend", 0) > 0:
        n = DIVIDEND_POOL_BY_TIER.get(tier, 12)
        cand = cfg.DIVIDEND_CANDIDATES[:n]
        say(f"筛选红利股（{len(cand)} 只候选）...")
        div_map = screener.dividend_yield_map(log)
        say(f"股息率数据 {len(div_map)} 只")
        div_pick, div_all = screen_stocks(cand, spot, profile, log, mode="dividend",
                                          div_map=div_map, return_all=True)
        say(f"红利股候选 {len(div_pick)} 只")
        if div_pick.empty:
            notes += screener.near_miss_notes(div_all, "dividend")

    gro_pick = pd.DataFrame()
    if profile["weights"].get("growth", 0) > 0:
        n = GROWTH_POOL_BY_TIER.get(tier, 0)
        cand = cfg.GROWTH_CANDIDATES[:n]
        if cand:
            say(f"筛选成长股（{len(cand)} 只候选）...")
            gro_pick, gro_all = screen_stocks(cand, spot, profile, log, mode="growth",
                                              return_all=True)
            say(f"成长股候选 {len(gro_pick)} 只")
            if len(gro_pick) < profile["max_positions"]["growth"]:
                notes += screener.near_miss_notes(gro_all, "growth")

    picks = {"cb": cb_pick, "etf": etf_pick, "dividend": div_pick, "growth": gro_pick}
    portfolio = build_portfolio(tier, capital, picks, macro, temp)

    plan = None
    if mode == "rebalance":
        say("计算调仓差额 ...")
        plan = plan_rebalance(portfolio["holdings"], positions, cash, capital,
                              portfolio["weights"], sell_unknown=sell_unknown)
        account["summary"] = hd.summary(positions, cash)

    bench = None
    if with_backtest:
        say("运行规则回测 ...")
        bench = run_quick_backtest(tier, log, quiet=quiet)

    md = generate_report(tier, portfolio, picks, macro, temp, log.to_frame(), bench, notes,
                         positions=positions, plan=plan, account=account, mode=mode)
    orders = orders_frame(plan) if plan is not None else None
    paths = save_report(tier, md, portfolio["holdings"], picks, mode=mode, orders=orders)

    run_id = None
    try:
        run_id = store.record_run(
            tier=tier, mode=mode, equity=capital, cash=(cash if mode == "rebalance" else 0.0),
            invested_pct=(account.get("invested_pct", portfolio.get("invested_pct", 0))
                          if mode == "rebalance" else portfolio.get("invested_pct", 0)),
            report_path=paths.get("markdown", ""), positions=positions,
        )
    except Exception as e:  # noqa: BLE001
        if not quiet:
            print(f"  （运行记录未写入：{e}）")

    if not quiet:
        print(md)
        print("\n已生成:")
        for k, v in paths.items():
            print(f"  {k}: {v}")
    return {"portfolio": portfolio, "paths": paths, "macro": macro, "temp": temp,
            "log": log.to_frame(), "picks": picks, "benchmark": bench, "notes": notes,
            "positions": positions, "plan": plan, "account": account, "mode": mode,
            "run_id": run_id}


def _build_bench_panel(tier: str, log: FetchLog, quiet: bool = True) -> pd.DataFrame:
    allow = cfg.ETF_RISK_ALLOW.get(tier, {"low", "mid", "high"})
    pool = [c for c, m in cfg.ETF_UNIVERSE.items() if m["risk"] in allow]
    daily_map = {}
    for c in pool:
        df = ds.etf_daily(c, log)
        if not df.empty:
            daily_map[c] = df
    panel = bt.build_panel(daily_map)
    if not quiet:
        print(f"  ETF 池 {len(daily_map)} 只，价格面板 {panel.shape}")
    return panel


def run_quick_backtest(tier: str, log: FetchLog, quiet: bool = False) -> dict:
    panel = _build_bench_panel(tier, log, quiet=quiet)
    if panel.empty:
        return {}
    prof = cfg.RISK_PROFILES[tier]
    lookback = max(20, min(120, prof["etf_filters"]["mom_window"]))
    n = max(1, prof["max_positions"]["etf"])
    if not quiet:
        print(f"  参数：{lookback} 日动量，持 {n} 只，按月调仓，单边成本 5bp")
    res = bt.momentum_rotation(panel, lookback=lookback, top_n=n, freq="ME",
                               cost_bps=5.0, start="2016-01-01")
    res = {f"策略_{k}": v for k, v in res.items()}
    idx = ds.index_history("sh000300", log)
    if not idx.empty:
        bpanel = bt.build_panel({"hs300": idx})
        bm = bt.fixed_basket(bpanel[bpanel.index >= pd.Timestamp("2016-01-01")], freq="ME")
        res.update({f"基准沪深300_{k}": v for k, v in bm.items()})
    return res


def run_backtest(args) -> None:
    tier = args.tier
    log = FetchLog()
    panel = _build_bench_panel(tier, log, quiet=False)
    if panel.empty:
        print("回测数据不足")
        return

    print("\n=== 一、当前档位参数下的表现 ===")
    prof = cfg.RISK_PROFILES[tier]
    lookback = max(20, min(120, prof["etf_filters"]["mom_window"]))
    n = max(1, prof["max_positions"]["etf"])
    main = bt.momentum_rotation(panel, lookback=lookback, top_n=n, freq="ME",
                                cost_bps=5.0, start=args.start)
    bench = {}
    idx = ds.index_history("sh000300", log)
    if not idx.empty:
        bpanel = bt.build_panel({"hs300": idx})
        bench = bt.fixed_basket(bpanel[bpanel.index >= pd.Timestamp(args.start)], freq="ME")
    cmp_df = bt.compare_vs_benchmark(main, bench)
    print(cmp_df.to_string(index=False))

    print("\n=== 二、参数稳健性扫描（夏普排序）===")
    sweep = bt.parameter_sweep(panel, lookbacks=[20, 60, 120, 180],
                               tops=[1, 2, 3, 5], freq="ME", start=args.start)
    print(sweep.to_string(index=False))

    out = os.path.join(REPORT_DIR, f"{stamp()}_回测_{tier}.csv")
    pd.concat([cmp_df.assign(表="参数对比"), sweep.assign(表="参数扫描")],
              ignore_index=True).to_csv(out, index=False, encoding="utf-8-sig")
    print("\n已保存:", out)
    print("提示：夏普在 0.3 以下、或最大回撤接近基准的规则，说明没有真正跑赢，"
          "不要因为某一组参数好看就上实盘。")


def _print_holdings(with_prices: bool = False) -> None:
    log = FetchLog()
    ps, cash = hd.load_positions()
    if not ps:
        print(f"当前没有持仓（现金 {cash:,.2f} 元）。")
        print("导入：python -m advisor.cli holdings import --file 持仓.csv --cash 20000")
        return
    if with_prices:
        prices = fetch_quotes(ps, log)
        for p in ps:
            p.last_price = prices.get(p.code, p.avg_cost)
    frame = hd.positions_frame(ps, cash)
    print(frame.to_string(index=False))
    s = hd.summary(ps, cash)
    print(f"\n总资产 {s['equity']:,.2f} 元 | 市值 {s['market_value']:,.2f} | "
          f"现金 {s['cash']:,.2f} | 浮盈 {s['unrealized_pnl']:,.2f} | "
          f"已实现 {s['realized_pnl']:,.2f}")


def _import_holdings(path: str, cash: float | None) -> None:
    res = hd.import_file(path, cash=cash)
    print(f"已导入 {res['rows']} 只持仓，现金 {res['cash']:,.2f} 元。")
    _print_holdings()


def _add_holding(code: str, qty: float, cost: float, name: str = "",
                 asset: str | None = None, cash: float | None = None) -> None:
    from .domain import canonical_code, detect_asset
    asset = asset or detect_asset(code)
    code = canonical_code(code, asset)
    rows = [{"code": p.code, "name": p.name, "asset": p.asset,
             "quantity": p.quantity, "cost_price": p.avg_cost}
            for p in store.get_positions()]
    replaced = False
    for r in rows:
        if r["code"] == code:
            r.update(quantity=qty, cost_price=cost, name=name or r["name"], asset=asset)
            replaced = True
    if not replaced:
        rows.append({"code": code, "name": name, "asset": asset,
                     "quantity": qty, "cost_price": cost})
    res = store.replace_holdings(rows, cash=cash)
    print(f"{'已更新' if replaced else '已添加'} {code}：{qty:g} @ {cost:g}，"
          f"现金 {res['cash']:,.2f} 元。")


def run_data_check() -> None:
    log = FetchLog()
    checks = [
        ("A股实时-新浪", lambda: ds.a_spot(log)),
        ("可转债全表", lambda: ds.cb_board(log)),
        ("ETF池", lambda: ds.etf_spot_filtered(log)),
        ("国债收益率", lambda: ds.bond_yield_curve(log)),
    ]
    for name, fn in checks:
        t0 = time.time()
        try:
            df = fn()
            n = 0 if df is None else len(df)
            print(f"OK   {name}: {n} 行, {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("\n数据源明细:")
    print(log.to_frame().to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advisor", description="个人投资决策工作流")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("advise", help="生成决策日报与买入/调仓清单")
    p1.add_argument("--tier", default="balanced",
                    choices=list(cfg.RISK_PROFILES), help="风险档位")
    p1.add_argument("--capital", type=float, default=100000, help="本金（仅新建仓模式）")
    p1.add_argument("--mode", default="auto", choices=["auto", "build", "rebalance"],
                    help="auto=有持仓则调仓；build=忽略持仓重新建仓；rebalance=强制调仓")
    p1.add_argument("--keep-unknown", action="store_true",
                    help="持仓中不在本期候选池的标的保留，不自动清仓")
    p1.add_argument("--with-backtest", action="store_true", help="附带规则回测")
    p1.add_argument("--quiet", action="store_true", help="只写文件，不打印报告")

    p2 = sub.add_parser("backtest", help="回测 ETF 动量轮动规则并做参数稳健性扫描")
    p2.add_argument("--tier", default="balanced", choices=list(cfg.RISK_PROFILES))
    p2.add_argument("--start", default="2016-01-01", help="回测起始日")

    p3 = sub.add_parser("sweep", help="只跑参数扫描（不打印主回测）")
    p3.add_argument("--tier", default="balanced", choices=list(cfg.RISK_PROFILES))
    p3.add_argument("--start", default="2016-01-01")

    sub.add_parser("data-check", help="检查数据源可用性")

    p4 = sub.add_parser("holdings", help="持仓管理：导入/查看/添加/清空")
    hsub = p4.add_subparsers(dest="holdings_cmd", required=True)
    hi = hsub.add_parser("import", help="从 CSV/JSON 导入持仓快照")
    hi.add_argument("--file", required=True, help="CSV/JSON 路径")
    hi.add_argument("--cash", type=float, default=None, help="同时设置可用现金（元）")

    hl = hsub.add_parser("list", help="查看当前持仓")
    hl.add_argument("--prices", action="store_true", help="同时抓最新价（需联网）")

    ha = hsub.add_parser("add", help="添加/更新一只持仓（不产生现金流）")
    ha.add_argument("--code", required=True)
    ha.add_argument("--qty", type=float, required=True)
    ha.add_argument("--cost", type=float, required=True)
    ha.add_argument("--name", default="")
    ha.add_argument("--asset", default=None, choices=["etf", "cb", "stock", "dividend", "growth"])
    ha.add_argument("--cash", type=float, default=None, help="同时设置可用现金（元）")

    hr = hsub.add_parser("reset", help="清空所有持仓")
    hr.add_argument("--yes", action="store_true", help="确认清空")

    pc = sub.add_parser("cash", help="设置可用现金")
    pc.add_argument("amount", type=float)

    pt = sub.add_parser("trade", help="记录一笔买入/卖出，自动更新持仓和现金")
    tsub = pt.add_subparsers(dest="trade_cmd", required=True)
    tb = tsub.add_parser("buy")
    tb.add_argument("--code", required=True)
    tb.add_argument("--qty", type=float, required=True)
    tb.add_argument("--price", type=float, required=True)
    tb.add_argument("--fee", type=float, default=0)
    tb.add_argument("--date", default=None)
    ts = tsub.add_parser("sell")
    ts.add_argument("--code", required=True)
    ts.add_argument("--qty", type=float, required=True)
    ts.add_argument("--price", type=float, required=True)
    ts.add_argument("--fee", type=float, default=0)
    ts.add_argument("--date", default=None)

    p5 = sub.add_parser("ui", help="启动可视化界面（浏览器操作）")
    p5.add_argument("--port", type=int, default=8765)
    p5.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    args = parser.parse_args(argv)
    if args.cmd == "advise":
        run_advise(args.tier, args.capital, with_backtest=args.with_backtest,
                   quiet=args.quiet, mode=args.mode,
                   sell_unknown=not args.keep_unknown)
    elif args.cmd == "backtest":
        run_backtest(args)
    elif args.cmd == "sweep":
        log = FetchLog()
        panel = _build_bench_panel(args.tier, log, quiet=False)
        sweep = bt.parameter_sweep(panel, lookbacks=[20, 60, 120, 180], tops=[1, 2, 3, 5],
                                   freq="ME", start=args.start)
        print(sweep.to_string(index=False))
    elif args.cmd == "holdings":
        if args.holdings_cmd == "import":
            _import_holdings(args.file, args.cash)
        elif args.holdings_cmd == "list":
            _print_holdings(with_prices=args.prices)
        elif args.holdings_cmd == "add":
            _add_holding(args.code, args.qty, args.cost, args.name, args.asset, args.cash)
        elif args.holdings_cmd == "reset":
            if not args.yes:
                print("加 --yes 才会清空。")
                return 1
            store.clear_holdings(keep_cash=False)
            print("已清空持仓与现金。")
    elif args.cmd == "cash":
        print(f"现金已设为 {store.set_cash(args.amount):,.2f} 元。")
    elif args.cmd == "trade":
        side = "BUY" if args.trade_cmd == "buy" else "SELL"
        r = store.add_trade(args.code, side, args.qty, args.price, fee=args.fee,
                            trade_date=args.date)
        print(f"已记录 {side} {r['code']} {args.qty:g} @ {args.price:g}，"
              f"账上现金 {r['cash']:,.2f} 元。")
    elif args.cmd == "ui":
        from .server import serve
        serve(port=args.port, open_browser=not args.no_browser)
    elif args.cmd == "data-check":
        run_data_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
