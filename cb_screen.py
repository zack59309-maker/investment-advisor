# -*- coding: utf-8 -*-
"""
可转债筛查 demo —— 数据全部来自 akshare 公开数据源（本机网络实测可用）
  1) bond_zh_hs_cov_spot()  新浪：全市场可转债实时行情（328 只，含现价/成交量）
  2) bond_zh_cov()          东财：可转债发行明细（转股价 / 正股价 / 信用评级 / 发行规模）
  3) bond_zh_us_rate()      中债：中美国债收益率曲线（债性性价比的宏观基准）

派生指标（无第三方依赖，自算）：
  转股价值 = 100 / 转股价 * 正股价
  转股溢价率 = 现价 / 转股价值 - 1
  双低值   = 现价 + 转股溢价率(%)

输出：cb_screen_result.csv（分档建议）。仅供数据管道演示，不构成投资建议。
"""
import sys

import akshare as ak
import pandas as pd

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 40)
pd.set_option("display.unicode.east_asian_width", True)


def classify(r) -> str:
    """规则化分档：只描述风险收益特征，不预测涨跌。"""
    if r["现价"] < 100 and r["转股溢价率"] < 40:
        return "关注-低价低溢价"
    if r["现价"] <= 120 and r["转股溢价率"] <= 30:
        return "关注-双低"
    if r["转股溢价率"] > 80:
        return "规避-高溢价"
    if r["现价"] < 105:
        return "防守-债性为主"
    return "中性"


def main() -> int:
    print("akshare", ak.__version__)

    spot = ak.bond_zh_hs_cov_spot()
    basic = ak.bond_zh_cov()

    spot = spot.rename(columns={"code": "债券代码", "name": "转债名称", "trade": "现价",
                                "volume": "成交量", "amount": "成交额"})
    spot["债券代码"] = spot["债券代码"].astype(str).str.zfill(6)
    basic["债券代码"] = basic["债券代码"].astype(str).str.zfill(6)
    basic = basic[["债券代码", "正股代码", "正股简称", "正股价", "转股价",
                   "信用评级", "发行规模"]]

    df = spot.merge(basic, on="债券代码", how="left")
    for c in ["现价", "正股价", "转股价", "成交量", "成交额"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[(df["现价"] > 0) & (df["转股价"] > 0) & (df["正股价"] > 0)].copy()
    df["转股价值"] = 100 / df["转股价"] * df["正股价"]
    df["转股溢价率"] = (df["现价"] / df["转股价值"] - 1) * 100
    df["双低值"] = df["现价"] + df["转股溢价率"]
    df["规则建议"] = df.apply(classify, axis=1)
    df = df.sort_values("双低值")

    cols = ["债券代码", "转债名称", "现价", "转股价值", "转股溢价率", "双低值",
            "正股简称", "信用评级", "成交额", "规则建议"]
    cols = [c for c in cols if c in df.columns]

    print(f"\n=== 有效样本 {len(df)} 只（新浪行情 ∩ 东财发行明细）===")
    print("\n--- 双低值最低 15 只 ---")
    print(df[cols].head(15).to_string(index=False))

    print("\n--- 分档统计 ---")
    print(df["规则建议"].value_counts().to_string())

    print("\n--- 各档代表（每档 3 只）---")
    for tag in df["规则建议"].unique():
        print(f"\n[{tag}]")
        print(df[df["规则建议"] == tag][cols].head(3).to_string(index=False))

    y = ak.bond_zh_us_rate().dropna(subset=["中国国债收益率10年"]).tail(1)
    if not y.empty:
        print(f"\n中债 10 年期国债收益率: {y['中国国债收益率10年'].iloc[-1]}%  "
              f"(日期 {y['日期'].iloc[-1]})")

    out = "cb_screen_result.csv"
    df[cols].to_csv(out, index=False, encoding="utf-8-sig")
    print("\n已保存:", out, "行数:", len(df))
    return 0


if __name__ == "__main__":
    sys.exit(main())
