# -*- coding: utf-8 -*-
import unittest

from advisor.domain import Trade, canonical_code, compute_positions


class TestLedger(unittest.TestCase):
    def test_avg_cost_and_realized(self):
        trades = [
            Trade("sh512800", "BUY", 1000, 0.80, 5, "2026-01-01", "银行ETF", "etf"),
            Trade("sh512800", "BUY", 1000, 1.00, 5, "2026-01-02", "银行ETF", "etf"),
            Trade("sh512800", "SELL", 500, 1.10, 5, "2026-01-03", "银行ETF", "etf"),
        ]
        ps = compute_positions(trades)
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertAlmostEqual(p.quantity, 1500)
        self.assertAlmostEqual(p.avg_cost, 0.905)
        self.assertAlmostEqual(p.realized_pnl, 92.5)

    def test_sell_more_than_held_raises(self):
        with self.assertRaises(ValueError):
            compute_positions([Trade("113046", "SELL", 10, 100, 0, "2026-01-01", "x", "cb")])

    def test_canonical_codes(self):
        self.assertEqual(canonical_code("512800", "etf"), "sh512800")
        self.assertEqual(canonical_code("159915", "etf"), "sz159915")
        self.assertEqual(canonical_code("sh512800", "stock"), "sh512800")
        self.assertEqual(canonical_code("113046", "cb"), "113046")
        self.assertEqual(canonical_code("600011", None), "600011")
