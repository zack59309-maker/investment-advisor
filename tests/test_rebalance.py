# -*- coding: utf-8 -*-
import unittest

import pandas as pd

from advisor.domain import Position
from advisor.rebalance import plan_rebalance


def target_df(rows):
    return pd.DataFrame(rows)


class TestRebalance(unittest.TestCase):
    def test_buy_sell_and_hold(self):
        target = target_df([
            {"类别": "ETF", "代码": "sh512800", "名称": "银行ETF", "asset": "etf", "排名": 1,
             "买入数量": 2000, "买入挂单价": 0.9, "最新价": 1.0, "止损价": 0.8,
             "止盈价": 1.2, "理由": "x"},
        ])
        pos = [
            Position("sh512800", "银行ETF", "etf", 1500, 0.85, last_price=1.0),
            Position("600011", "华能国际", "dividend", 700, 6.0, last_price=7.0),
        ]
        cash = 1000.0
        equity = cash + 1500 * 1.0 + 700 * 7.0
        plan = plan_rebalance(target, pos, cash, equity, {"cash": 0.0})
        sides = {o.code: o.side for o in plan.orders}
        self.assertEqual(sides["sh512800"], "BUY")
        self.assertEqual(sides["600011"], "SELL")
        buy = next(o for o in plan.orders if o.code == "sh512800")
        self.assertEqual(buy.quantity, 500)
        self.assertGreater(plan.cash_after, 0)

    def test_cash_cap_scales_buys(self):
        target = target_df([
            {"类别": "ETF", "代码": "sh510300", "名称": "沪深300ETF", "asset": "etf", "排名": 1,
             "买入数量": 10000, "买入挂单价": 10.0, "最新价": 10.0, "止损价": 8.0,
             "止盈价": 12.0, "理由": "x"},
        ])
        plan = plan_rebalance(target, [], 1100.0, 1100.0, {"cash": 0.0})
        buy = plan.buys[0]
        self.assertLessEqual(buy.amount + buy.fee, 1100.0 + 1e-6)
        self.assertEqual(buy.quantity, 100)

    def test_keep_unknown_reduces_buy_budget(self):
        target = target_df([
            {"类别": "ETF", "代码": "sh510300", "名称": "沪深300ETF", "asset": "etf", "排名": 1,
             "买入数量": 4000, "买入挂单价": 10.0, "最新价": 10.0, "止损价": 8.0,
             "止盈价": 12.0, "理由": "x"},
        ])
        pos = [Position("600011", "华能国际", "stock", 5000, 10.0, last_price=10.0)]
        plan = plan_rebalance(target, pos, 10000.0, 60000.0, {"cash": 0.0},
                              sell_unknown=False)
        self.assertEqual(plan.buys, [])
        self.assertEqual(plan.holds[0].code, "600011")
        self.assertTrue(any("保留" in w for w in plan.warnings))

    def test_no_target_keeps_positions(self):
        pos = [Position("sh512800", "银行ETF", "etf", 1000, 0.85, last_price=0.9)]
        plan = plan_rebalance(pd.DataFrame(), pos, 100.0, 1000.0, {"cash": 0.1})
        self.assertEqual(plan.buys, [])
        self.assertEqual(plan.sells, [])
        self.assertEqual(plan.holds[0].code, "sh512800")
        self.assertTrue(plan.warnings)
