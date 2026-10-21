# -*- coding: utf-8 -*-
"""策略与风险档位配置。所有阈值集中在这里，便于按自己的偏好调参。"""

# ---------------------------------------------------------------- 风险档位
# 资产类别: etf 宽基/行业ETF, cb 可转债, dividend 红利价值股, growth 成长动量股, cash 现金/货基
RISK_PROFILES: dict[str, dict] = {
    "conservative": {
        "name": "保守型",
        "desc": "本金安全优先，回撤容忍 ≤8%，追求跑赢货币基金与国债",
        "target_return": "年化 3%~6%",
        "max_drawdown": "≤8%",
        "weights": {"etf": 0.20, "cb": 0.50, "dividend": 0.10, "growth": 0.00, "cash": 0.20},
        "max_positions": {"etf": 4, "cb": 8, "dividend": 2, "growth": 0},
        "max_single": {"etf": 0.08, "cb": 0.08, "dividend": 0.06, "growth": 0.0},
        "cb_filters": {"price_max": 118, "premium_max": 35, "rating_min": "AA-",
                       "min_turnover": 3e6, "exclude_st": True},
        "etf_filters": {"min_amount": 3e7, "mom_window": 20, "require_positive_mom": False},
        "stop_loss": {"etf": 0.08, "cb": 0.08, "dividend": 0.12, "growth": 0.15},
        "take_profit": {"etf": 0.25, "cb": 0.20, "dividend": 0.30, "growth": 0.40},
        "rebalance": "每月首个交易日",
    },
    "balanced": {
        "name": "稳健型",
        "desc": "攻守均衡，回撤容忍 ≤15%，可转债打底 + 宽基/红利增强",
        "target_return": "年化 6%~10%",
        "max_drawdown": "≤15%",
        "weights": {"etf": 0.35, "cb": 0.30, "dividend": 0.20, "growth": 0.05, "cash": 0.10},
        "max_positions": {"etf": 3, "cb": 6, "dividend": 4, "growth": 1},
        "max_single": {"etf": 0.15, "cb": 0.08, "dividend": 0.08, "growth": 0.05},
        "cb_filters": {"price_max": 130, "premium_max": 50, "rating_min": "A+",
                       "min_turnover": 5e6, "exclude_st": True},
        "etf_filters": {"min_amount": 5e7, "mom_window": 60, "require_positive_mom": False},
        "stop_loss": {"etf": 0.12, "cb": 0.12, "dividend": 0.15, "growth": 0.18},
        "take_profit": {"etf": 0.35, "cb": 0.30, "dividend": 0.40, "growth": 0.50},
        "rebalance": "每月首个交易日",
    },
    "aggressive": {
        "name": "激进型",
        "desc": "追求高弹性，回撤容忍 ≤30%，主题/成长 + 低溢价可转债",
        "target_return": "年化 12%+",
        "max_drawdown": "≤30%",
        "weights": {"etf": 0.40, "cb": 0.20, "dividend": 0.05, "growth": 0.25, "cash": 0.10},
        "max_positions": {"etf": 3, "cb": 4, "dividend": 2, "growth": 4},
        "max_single": {"etf": 0.18, "cb": 0.08, "dividend": 0.05, "growth": 0.08},
        "cb_filters": {"price_max": 145, "premium_max": 60, "rating_min": "A",
                       "min_turnover": 1e7, "exclude_st": True},
        "etf_filters": {"min_amount": 5e7, "mom_window": 60, "require_positive_mom": True},
        "stop_loss": {"etf": 0.15, "cb": 0.15, "dividend": 0.15, "growth": 0.20},
        "take_profit": {"etf": 0.10, "cb": 0.15, "dividend": 0.20, "growth": 0.30},
        "rebalance": "每两周首个交易日",
    },
}

# ---------------------------------------------------------------- ETF 池
# 只做规模大、流动性好的主流品种，避免踩到迷你 ETF 的清盘风险
ETF_UNIVERSE: dict[str, dict] = {
    # 宽基
    "sh510300": {"name": "沪深300ETF", "tag": "宽基", "risk": "low"},
    "sh510500": {"name": "中证500ETF", "tag": "宽基", "risk": "mid"},
    "sh512100": {"name": "中证1000ETF", "tag": "宽基", "risk": "high"},
    "sh588000": {"name": "科创50ETF", "tag": "宽基", "risk": "high"},
    "sh510880": {"name": "红利ETF", "tag": "红利", "risk": "low"},
    "sh515180": {"name": "红利ETF易方达", "tag": "红利", "risk": "low"},
    "sh512890": {"name": "红利低波ETF", "tag": "红利", "risk": "low"},
    "sh513100": {"name": "纳指ETF", "tag": "海外", "risk": "high"},
    "sh513500": {"name": "标普500ETF", "tag": "海外", "risk": "mid"},
    "sh518880": {"name": "黄金ETF", "tag": "商品", "risk": "mid"},
    "sh511010": {"name": "国债ETF", "tag": "债券", "risk": "low"},
    "sh511260": {"name": "十年国债ETF", "tag": "债券", "risk": "low"},
    "sh511360": {"name": "短融ETF", "tag": "债券", "risk": "low"},
    "sh512880": {"name": "证券ETF", "tag": "金融", "risk": "high"},
    "sh512800": {"name": "银行ETF", "tag": "金融", "risk": "mid"},
    "sh512660": {"name": "军工ETF", "tag": "主题", "risk": "high"},
    "sh512010": {"name": "医药ETF", "tag": "主题", "risk": "mid"},
    "sh515030": {"name": "新能源车ETF", "tag": "主题", "risk": "high"},
    "sh515790": {"name": "光伏ETF", "tag": "主题", "risk": "high"},
    "sh512480": {"name": "半导体ETF", "tag": "主题", "risk": "high"},
    "sz159915": {"name": "创业板ETF", "tag": "宽基", "risk": "high"},
    "sh516160": {"name": "新能源ETF", "tag": "主题", "risk": "high"},
    "sh515220": {"name": "煤炭ETF", "tag": "周期", "risk": "mid"},
    "sh512400": {"name": "有色金属ETF", "tag": "周期", "risk": "mid"},
    "sz159928": {"name": "消费ETF", "tag": "主题", "risk": "mid"},
    "sh513050": {"name": "中概互联网ETF", "tag": "海外", "risk": "high"},
}

# 各种风险档位允许的 ETF 风险标签
ETF_RISK_ALLOW = {
    "conservative": {"low"},
    "balanced": {"low", "mid"},
    "aggressive": {"low", "mid", "high"},
}

# ---------------------------------------------------------------- 红利/价值股池
# 从沪深300 成分里筛，先限定为常见高股息大票，再按指标打分
DIVIDEND_CANDIDATES: list[str] = [
    # 银行（高股息主力，但单一行业过度集中会在报表里体现）
    "601398", "601939", "601288", "601988", "600036", "601166", "601328", "600016",
    # 能源/电力/公用
    "600028", "601857", "600019", "601088", "600900", "601985", "600011", "601899",
    # 建筑/建材/交运/地产链
    "600585", "601668", "601390", "601186", "600048", "601006", "600018", "600377",
    # 消费/家电/汽车
    "600104", "601633", "000651", "000333", "600690", "000895", "600887", "601098",
]

# 成长/主题股池（激进档使用，按动量+波动打分）
GROWTH_CANDIDATES: list[str] = [
    # 新能源/锂电/光伏
    "300750", "002594", "601127", "300124", "601012", "600438", "002460", "300274",
    "300014", "002709", "300450",
    # 半导体/电子/算力
    "688981", "688041", "688256", "002371", "300308", "300502", "002475", "603501",
    "300661", "600584", "603986", "002463", "002916", "000977", "603019",
    # 软件/通信/传媒
    "688111", "688036", "002230", "300496", "002415", "300413",
    # 医药/医疗
    "300760", "600276", "603259", "300347",
]

# 现金/货币基准（用于给"现金仓位"标一个收益参考）
CASH_BENCHMARK_ETF = "sh511990"  # 华宝添益

# ---------------------------------------------------------------- 评级序
RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
                "BB", "B", "CCC", "CC", "C", "D"]


def rating_score(rating: str) -> int:
    """评级越高分越高；未知评级给中间偏低分。"""
    if not isinstance(rating, str):
        return 0
    r = rating.strip().upper().replace("STI", "")
    if r in RATING_ORDER:
        return len(RATING_ORDER) - RATING_ORDER.index(r)
    return 3


def rating_at_least(rating: str, floor: str) -> bool:
    if not isinstance(rating, str):
        return False
    r = rating.strip().upper()
    f = floor.strip().upper()
    if r not in RATING_ORDER or f not in RATING_ORDER:
        return False
    return RATING_ORDER.index(r) <= RATING_ORDER.index(f)
