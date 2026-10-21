# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

from advisor import holdings as hd
from advisor import store


class TestNormalize(unittest.TestCase):
    def test_chinese_headers(self):
        row = {"代码": "510300", "名称": "沪深300ETF", "数量": "1,000", "成本价": "4.10"}
        out = hd.normalize_row(row)
        self.assertEqual(out["code"], "sh510300")
        self.assertEqual(out["asset"], "etf")
        self.assertEqual(out["quantity"], 1000.0)
        self.assertEqual(out["cost_price"], 4.10)

    def test_wrong_asset_hint_is_corrected(self):
        out = hd.normalize_row({"code": "510300", "quantity": 100, "cost_price": 4.0,
                                "asset": "stock"})
        self.assertEqual(out["asset"], "etf")
        self.assertEqual(out["code"], "sh510300")

    def test_cb_detection(self):
        out = hd.normalize_row({"code": "113046", "quantity": 40, "cost_price": 108.5})
        self.assertEqual(out["asset"], "cb")
        self.assertEqual(out["code"], "113046")


class TestStore(unittest.TestCase):
    def setUp(self):
        self._old = store.DB_PATH
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        store.DB_PATH = self.path

    def tearDown(self):
        store.DB_PATH = self._old
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_replace_and_positions(self):
        store.replace_holdings([
            {"code": "sh512800", "name": "银行ETF", "asset": "etf", "quantity": 1000, "cost_price": 0.8},
            {"code": "113046", "name": "金田转债", "asset": "cb", "quantity": 40, "cost_price": 108.5},
        ], cash=5000)
        ps = store.get_positions()
        self.assertEqual(len(ps), 2)
        self.assertEqual(store.get_account()["cash"], 5000)
        self.assertEqual({p.code for p in ps}, {"sh512800", "113046"})

    def test_trade_updates_cash(self):
        store.replace_holdings([], cash=10000)
        store.add_trade("sh512800", "BUY", 1000, 0.8, fee=5)
        self.assertEqual(store.get_account()["cash"], 10000 - 805)
        ps = store.get_positions()
        self.assertEqual(ps[0].quantity, 1000)
        store.add_trade("sh512800", "SELL", 1000, 1.0, fee=5)
        self.assertEqual(store.get_positions(), [])
        self.assertGreater(store.get_account()["cash"], 10000)
