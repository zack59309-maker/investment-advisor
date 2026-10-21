# -*- coding: utf-8 -*-
"""可视化界面后端：只用 Python 标准库的 HTTP 服务，不引入 Flask/FastAPI。

启动:
    python -m advisor.server --port 8765
    # 或双击 start_ui.bat

设计要点：
- 只监听 127.0.0.1，不对外暴露；
- 分析任务跑在后台线程，前端轮询进度，避免长请求超时；
- DeepSeek 调用用 OpenAI 兼容协议，API Key 存在 data/settings.json（也可用环境变量）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd

from . import config as cfg
from . import holdings as hd
from . import store
from .core import ROOT, FetchLog, dump_json
from .cli import run_advise, run_quick_backtest
from .portfolio import action_list
from .quotes import fetch_quotes
from .rebalance import action_table, orders_frame

WEB_DIR = os.path.join(ROOT, "web")
SETTINGS_PATH = os.path.join(ROOT, "data", "settings.json")
HOST = "127.0.0.1"

TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()

DEFAULT_SETTINGS = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "last_tier": "balanced",
    "last_capital": 100000,
}


# ------------------------------------------------------------------ 设置
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:  # noqa: BLE001
            pass
    if not s.get("deepseek_api_key"):
        s["deepseek_api_key"] = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    s.update({k: v for k, v in patch.items() if v is not None})
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    dump_json(SETTINGS_PATH, s)
    return s


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）" if len(key) > 12 else "已设置"


# ------------------------------------------------------------------ 任务
def _set_task(tid: str, **kw) -> None:
    with TASK_LOCK:
        TASKS.setdefault(tid, {})
        TASKS[tid].update(kw)


def new_task(kind: str) -> str:
    tid = uuid.uuid4().hex[:12]
    _set_task(tid, id=tid, kind=kind, status="running", progress=[], started=time.time())
    return tid


def task_done(tid: str, result=None, error: str | None = None) -> None:
    _set_task(tid, status="failed" if error else "done", result=result, error=error,
              finished=time.time())


def run_advise_task(tid: str, tier: str, capital: float, with_backtest: bool,
                    mode: str = "auto", sell_unknown: bool = True) -> None:
    def say(msg: str) -> None:
        with TASK_LOCK:
            TASKS[tid]["progress"].append(f"{time.time() - TASKS[tid]['started']:5.1f}s  {msg}")
    try:
        say(f"开始：档位={tier} 本金={capital:,.0f} 模式={mode}")
        out = run_advise(tier, capital, with_backtest=with_backtest, quiet=True,
                         progress=say, mode=mode, sell_unknown=sell_unknown)
        result = result_to_json(out, tier, capital)
        say("完成")
        task_done(tid, result=result)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        task_done(tid, error=f"{type(e).__name__}: {e}")


def run_backtest_task(tid: str, tier: str, start: str) -> None:
    def say(msg: str) -> None:
        with TASK_LOCK:
            TASKS[tid]["progress"].append(msg)
    try:
        log = FetchLog()
        from .cli import _build_bench_panel
        from . import backtest as bt
        say("加载 ETF 日线 ...")
        panel = _build_bench_panel(tier, log, quiet=True)
        if panel.empty:
            task_done(tid, error="回测数据不足")
            return
        prof = cfg.RISK_PROFILES[tier]
        lb = max(20, min(120, prof["etf_filters"]["mom_window"]))
        n = max(1, prof["max_positions"]["etf"])
        say(f"主回测：{lb} 日动量 / 持 {n} 只 / 按月调仓")
        main = bt.momentum_rotation(panel, lookback=lb, top_n=n, freq="ME", start=start)
        bench = {}
        from . import datasource as ds
        idx = ds.index_history("sh000300", log)
        if not idx.empty:
            bp = bt.build_panel({"hs300": idx})
            bench = bt.fixed_basket(bp[bp.index >= pd.Timestamp(start)], freq="ME")
        say("参数扫描：20/60/120/180 日 × 1/2/3/5 只")
        sweep = bt.parameter_sweep(panel, lookbacks=[20, 60, 120, 180], tops=[1, 2, 3, 5],
                                   freq="ME", start=start)
        task_done(tid, result={
            "对比": bt.compare_vs_benchmark(main, bench).to_dict("records"),
            "扫描": sweep.to_dict("records"),
        })
        say("完成")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        task_done(tid, error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ 序列化
def _df_records(df: pd.DataFrame | None, limit: int | None = None) -> list[dict]:
    if df is None or (hasattr(df, "empty") and df.empty):
        return []
    d = df.head(limit) if limit else df
    out = []
    for rec in d.to_dict("records"):
        row = {}
        for k, v in rec.items():
            if isinstance(v, float) and pd.isna(v):
                row[str(k)] = None
            elif hasattr(v, "item"):
                row[str(k)] = v.item()
            else:
                row[str(k)] = v
        out.append(row)
    return out


def _holdings_payload(live: bool = False) -> dict:
    ps, cash = hd.load_positions()
    if live and ps:
        log = FetchLog()
        prices = fetch_quotes(ps, log)
        for p in ps:
            p.last_price = prices.get(p.code, p.avg_cost)
    s = hd.summary(ps, cash)
    rows = []
    for p in ps:
        rows.append({
            "code": p.code, "name": p.name, "asset": p.asset,
            "quantity": p.quantity, "cost_price": round(p.avg_cost, 4),
            "last_price": round(p.last_price, 4) if p.priced else None,
            "market_value": round(p.market_value, 2),
            "unrealized_pnl": round(p.unrealized_pnl, 2),
            "pnl_pct": round(p.pnl_pct, 2),
        })
    return {"cash": s["cash"], "equity": s["equity"], "market_value": s["market_value"],
            "cost_value": s["cost_value"], "unrealized_pnl": s["unrealized_pnl"],
            "realized_pnl": s["realized_pnl"], "invested_pct": s["invested_pct"],
            "cash_pct": s["cash_pct"], "positions": rows}


def result_to_json(out: dict, tier: str, capital: float) -> dict:
    portfolio = out["portfolio"]
    picks = out.get("picks", {})
    bench = out.get("benchmark") or {}
    plan = out.get("plan")
    positions = out.get("positions") or []
    account = out.get("account") or {}
    md_path = out["paths"].get("markdown", "")
    md_text = ""
    if md_path and os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
    holdings = portfolio["holdings"]
    alloc = []
    if holdings is not None and not holdings.empty:
        g = holdings.groupby("类别", as_index=False)["买入金额"].sum()
        alloc = _df_records(g)
    return {
        "tier": tier,
        "tier_name": cfg.RISK_PROFILES[tier]["name"],
        "capital": capital,
        "mode": out.get("mode", "build"),
        "equity": account.get("equity", capital),
        "cash": account.get("cash"),
        "account": account,
        "positions": _df_records(hd.positions_frame(positions, account.get("cash", 0.0)))
        if positions else [],
        "rebalance": _df_records(orders_frame(plan)) if plan is not None else [],
        "warnings": (plan.warnings if plan is not None else []),
        "macro": {k: (None if isinstance(v, float) and pd.isna(v) else v)
                  for k, v in (out.get("macro") or {}).items()},
        "temp": out.get("temp") or {},
        "allocation": alloc,
        "weights": portfolio.get("weights", {}),
        "invested_pct": portfolio.get("invested_pct"),
        "cash_pct": portfolio.get("cash_pct"),
        "cash_yield": portfolio.get("cash_yield"),
        "holdings": _df_records(holdings),
        "actions": (_df_records(action_table(plan)) if plan is not None
                    else _df_records(action_list(portfolio))),
        "picks": {k: _df_records(v, limit=10) for k, v in picks.items()
                  if v is not None and not v.empty},
        "notes": out.get("notes") or [],
        "benchmark": bench,
        "report_path": md_path,
        "report_md": md_text,
        "csv_paths": {k: v for k, v in out["paths"].items() if k != "markdown"},
    }


# ------------------------------------------------------------------ DeepSeek
def build_ai_prompt(result: dict) -> str:
    lines = [
        "你是一名严格的投研风控助理。下面是我用规则引擎筛出的候选持仓，请按以下要求逐项输出：",
        "1) 逐只标的指出风险点（可转债重点看：强赎风险、正股 ST/退市风险、评级下调、剩余规模过小；"
        "ETF 重点看：规模与流动性、跟踪偏离、行业集中度；个股重点看：估值、分红可持续性、行业景气）；",
        "2) 指出组合整体的集中度与相关性风险（例如银行 ETF + 银行个股是否重复暴露）；",
        "3) 对每只标的给出【买入 / 观望 / 放弃】三选一，并说明理由，不要给新的价格预测；",
        "4) 最后用 3 条以内说明：【本组合最可能亏钱的方式是什么】。",
        "输出用中文，结构化、简洁，不要复述我的输入。",
        "",
        f"## 组合（档位：{result.get('tier_name')}，本金 {result.get('capital'):,.0f} 元，"
        f"已配置 {result.get('invested_pct')}%，现金 {result.get('cash_pct')}%）",
    ]
    if result.get("mode") == "rebalance":
        lines.append("")
        lines.append("## 当前持仓")
        for p in result.get("positions", []):
            lines.append(f"- {p.get('代码')} {p.get('名称')}：数量 {p.get('数量')}，"
                         f"成本 {p.get('成本价')}，现价 {p.get('现价') or '—'}，"
                         f"浮盈 {p.get('浮盈')}（{p.get('浮盈%')}%）")
        lines.append("")
        lines.append("## 调仓计划")
        for o in result.get("rebalance", []):
            lines.append(f"- {o.get('动作')} {o.get('代码')} {o.get('名称')} "
                         f"{o.get('差额')} @ {o.get('委托价')}；{o.get('理由')}")
        lines.append("")
        lines.append("## 目标组合")
    lines.append("")
    for h in result.get("holdings", []):
        if h.get("asset") == "cash":
            lines.append(f"- 现金/货基 {h.get('买入金额'):,.0f} 元（{h.get('理由')}）")
        else:
            lines.append(f"- {h.get('类别')} {h.get('代码')} {h.get('名称')}："
                         f"买入价 {h.get('买入挂单价')}，数量 {h.get('买入数量')}，"
                         f"止损 {h.get('止损价')}，止盈 {h.get('止盈价')}；入选理由：{h.get('理由')}")
    notes = result.get("notes") or []
    if notes:
        lines.append("## 备注（当日无标的/标的不足的原因）")
        lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def call_deepseek(prompt: str, settings: dict, timeout: int = 180) -> dict:
    key = (settings.get("deepseek_api_key") or "").strip()
    if not key:
        return {"ok": False, "error": "未配置 DeepSeek API Key，请在右上角【设置】里填写。"}
    base = (settings.get("deepseek_base_url") or DEFAULT_SETTINGS["deepseek_base_url"]).rstrip("/")
    url = f"{base}/chat/completions"
    model = settings.get("deepseek_model") or "deepseek-chat"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的中文投研风控助理，只做风险揭示与核验，不做收益承诺。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:400]
        return {"ok": False, "error": f"HTTP {e.code}：{detail}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}（检查网络或 base_url）"}
    try:
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"返回格式异常：{str(data)[:300]}"}
    return {"ok": True, "text": text, "model": model, "elapsed": round(time.time() - t0, 1),
            "usage": usage}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "advisor-ui/1.0"

    def log_message(self, fmt, *args):  # 静音，避免刷屏
        pass

    # ---- 工具
    def _json(self, obj, code: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _file(self, path: str, ctype: str | None = None) -> None:
        if not os.path.exists(path) or not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB_DIR, "index.html"), "text/html")
        if path.startswith("/static/"):
            return self._file(os.path.join(WEB_DIR, path[len("/static/"):]))
        if path == "/api/status":
            s = load_settings()
            return self._json({
                "tiers": {k: {"name": v["name"], "desc": v["desc"],
                              "target_return": v["target_return"],
                              "max_drawdown": v["max_drawdown"],
                              "rebalance": v["rebalance"]}
                          for k, v in cfg.RISK_PROFILES.items()},
                "ai_ready": bool(s.get("deepseek_api_key")),
                "key_masked": mask_key(s.get("deepseek_api_key", "")),
                "model": s.get("deepseek_model"),
                "base_url": s.get("deepseek_base_url"),
                "defaults": {"tier": s.get("last_tier", "balanced"),
                             "capital": s.get("last_capital", 100000)},
                "reports": sorted([f for f in os.listdir(os.path.join(ROOT, "reports"))
                                   if f.endswith(".md")], reverse=True)[:30]
                if os.path.isdir(os.path.join(ROOT, "reports")) else [],
            })
        if path == "/api/holdings":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            live = (q.get("live") or ["0"])[0] in ("1", "true", "yes")
            return self._json(_holdings_payload(live=live))
        if path.startswith("/api/task/"):
            tid = path.rsplit("/", 1)[-1]
            with TASK_LOCK:
                t = TASKS.get(tid)
            if not t:
                return self._json({"error": "unknown task"}, 404)
            return self._json(t)
        if path == "/api/report":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            safe = os.path.basename(name)
            full = os.path.join(ROOT, "reports", safe)
            if not safe.endswith(".md") or not os.path.exists(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "r", encoding="utf-8") as f:
                return self._json({"name": safe, "md": f.read()})
        if path == "/api/download":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            safe = os.path.basename((q.get("name") or [""])[0])
            full = os.path.join(ROOT, "reports", safe)
            return self._file(full)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_body()

        if path == "/api/settings":
            patch = {k: v for k, v in body.items() if k in DEFAULT_SETTINGS}
            s = save_settings(patch)
            return self._json({"ok": True, "ai_ready": bool(s.get("deepseek_api_key")),
                               "key_masked": mask_key(s.get("deepseek_api_key", ""))})

        if path == "/api/advise":
            tier = body.get("tier") or "balanced"
            if tier not in cfg.RISK_PROFILES:
                return self._json({"error": "bad tier"}, 400)
            capital = float(body.get("capital") or 100000)
            with_backtest = bool(body.get("with_backtest"))
            mode = body.get("mode") or "auto"
            if mode not in ("auto", "build", "rebalance"):
                return self._json({"error": "bad mode"}, 400)
            sell_unknown = bool(body.get("sell_unknown", True))
            save_settings({"last_tier": tier, "last_capital": capital})
            tid = new_task("advise")
            threading.Thread(target=run_advise_task,
                             args=(tid, tier, capital, with_backtest, mode, sell_unknown),
                             daemon=True).start()
            return self._json({"task": tid})

        if path == "/api/holdings":
            rows = body.get("rows") or []
            cash = body.get("cash")
            try:
                store.replace_holdings(rows, cash=float(cash) if cash is not None else None)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
            return self._json({"ok": True, **_holdings_payload()})

        if path == "/api/backtest":
            tier = body.get("tier") or "balanced"
            start = body.get("start") or "2016-01-01"
            tid = new_task("backtest")
            threading.Thread(target=run_backtest_task, args=(tid, tier, start),
                             daemon=True).start()
            return self._json({"task": tid})

        if path == "/api/ai-review":
            result = body.get("result")
            if not result:
                return self._json({"ok": False, "error": "缺少组合数据，请先运行分析"}, 400)
            prompt = build_ai_prompt(result)
            out = call_deepseek(prompt, load_settings())
            if out.get("ok") and body.get("save_as_report"):
                try:
                    from .core import REPORT_DIR
                    p = os.path.join(REPORT_DIR, f"{pd.Timestamp.now():%Y-%m-%d}_{result.get('tier')}_AI风险复核.md")
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(f"# AI 风险复核（{result.get('tier_name')}）\n\n{out['text']}\n")
                    out["saved"] = p
                except Exception:  # noqa: BLE001
                    pass
            return self._json(out)

        if path == "/api/ai-prompt":
            result = body.get("result")
            if not result:
                return self._json({"ok": False, "error": "缺少组合数据"}, 400)
            return self._json({"ok": True, "prompt": build_ai_prompt(result)})

        return self._json({"error": "not found"}, 404)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"投资决策工作流界面已启动： {url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
