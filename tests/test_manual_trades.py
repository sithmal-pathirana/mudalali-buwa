"""manual_trades.symbols: the user's own positions are never touched."""

import unittest

from tests.test_r5_regressions import INJ_LIVE, _cleanup, _restarted


class ManualTradesAreLeftAlone(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_not_adopted_at_startup(self):
        e = _restarted()
        e.cfg.manual_trades.symbols = ["INJUSDT"]
        self.assertEqual(e.adopt_open_positions(), 0)
        self.assertNotIn("INJUSDT", e.book)

    def test_not_adopted_or_protected_by_the_watchdog(self):
        e = _restarted()
        e.cfg.manual_trades.symbols = ["INJUSDT"]
        live = e.reconcile_book()
        self.assertNotIn("INJUSDT", live)
        self.assertNotIn("INJUSDT", e.book)

    def test_other_symbols_are_still_adopted(self):
        e = _restarted()
        e.cfg.manual_trades.symbols = ["HBARUSDT"]
        self.assertEqual(e.adopt_open_positions(), 1)
        self.assertIn("INJUSDT", e.book)

    def test_is_manual(self):
        e = _restarted(live=(INJ_LIVE,))
        e.cfg.manual_trades.symbols = ["INJUSDT"]
        self.assertTrue(e.is_manual("INJUSDT"))
        self.assertFalse(e.is_manual("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
