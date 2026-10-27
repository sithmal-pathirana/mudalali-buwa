import unittest

from tools.shadow_logger import IcebergMeter, ReadOnly, book_usd, tiers_of


class IcebergMeterTest(unittest.TestCase):
    def test_buying_beyond_shown_size_is_hidden(self):
        m = IcebergMeter()
        m.book([["10", "5"], ["10.1", "5"]])
        m.book([["10", "2"], ["10.1", "6"]])         # most ever shown: 5 at 10, 6 at 10.1
        m.trade(10.0, 12, False)                     # 7 more than ever shown
        m.trade(10.1, 3, False)                      # within the shown size
        m.trade(10.0, 4, True)                       # a sell: not counted as buying
        self.assertAlmostEqual(m.hidden_usd(), 70.0)
        self.assertAlmostEqual(m.seen_usd(), 150.3)
        self.assertAlmostEqual(m.sell_usd, 40.0)

    def test_prices_never_seen_are_left_out(self):
        m = IcebergMeter()
        m.book([["10", "5"]])
        m.trade(11.0, 100, False)
        self.assertEqual(m.hidden_usd(), 0.0)
        self.assertEqual(m.seen_usd(), 0.0)


class HelpersTest(unittest.TestCase):
    def test_book_usd(self):
        self.assertAlmostEqual(book_usd([["10", "1"], ["10.1", "2"]]), 30.2)

    def test_tiers_of(self):
        e = {"brackets": [{"bracket": 2, "initialLeverage": 10, "notionalCap": 50000},
                          {"bracket": 1, "initialLeverage": 20, "notionalCap": 5000}]}
        self.assertEqual(tiers_of(e), dict(max_lev=20, cap1=5000, cap_last=50000, n=2))

    def test_read_only_has_no_write_path(self):
        calls = []

        class Fake:
            def _request(self, method, path, params=None, signed=False):
                calls.append(method)

        ReadOnly(Fake()).get("/x")
        self.assertEqual(calls, ["GET"])
        self.assertFalse(hasattr(ReadOnly(Fake()), "_request"))


if __name__ == "__main__":
    unittest.main()
