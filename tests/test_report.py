"""The run report. It must be complete, safe to share, and never crash."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import report as rp   # noqa: E402

MINIMAL = {"symbol": "BTCUSDT", "days": 8, "generated": "2026-09-05T10:00:00+00:00",
           "equity": 5000.0}


class TestRobustness(unittest.TestCase):
    """A report that crashes on an empty account is useless on day one."""

    def test_renders_with_no_activity_at_all(self):
        out = rp.render(dict(MINIMAL))
        self.assertIn("RUN REPORT", out)
        self.assertIn("EQUITY NOW", out)

    def test_renders_when_every_fetch_failed(self):
        out = rp.render(dict(MINIMAL, errors=["income: BinanceError: -2015"]))
        self.assertIn("COULD NOT FETCH", out)
        self.assertIn("-2015", out)

    def test_tolerates_malformed_rows(self):
        data = dict(MINIMAL, income=[
            {"incomeType": "REALIZED_PNL", "income": "not-a-number", "time": 0},
            {"incomeType": "REALIZED_PNL", "income": "1.5", "time": 1787000000000},
        ])
        out = rp.render(data)
        self.assertIn("+1.5000", out)

    def test_handles_orders_without_prices(self):
        data = dict(MINIMAL, orders=[{"status": "NEW", "type": "STOP_MARKET"}])
        self.assertIn("ORDERS PLACED", rp.render(data))


class TestContent(unittest.TestCase):
    def _full(self):
        day = 86_400_000
        base = 1_787_000_000_000
        return dict(MINIMAL,
                    income=[{"incomeType": "REALIZED_PNL", "income": "2.0", "time": base},
                            {"incomeType": "COMMISSION", "income": "-0.5", "time": base}],
                    trades=[{"side": "BUY", "qty": "1", "price": "100",
                             "realizedPnl": "2.0", "commission": "0.5",
                             "maker": True, "time": base + day}],
                    orders=[{"status": "FILLED", "type": "LIMIT",
                             "price": "100", "avgPrice": "101"},
                            {"status": "EXPIRED", "type": "LIMIT",
                             "price": "90", "avgPrice": "0"}],
                    state={"total_trades": 1, "halted": False})

    def test_separates_pnl_from_fees(self):
        out = rp.render(self._full())
        self.assertIn("REALIZED_PNL", out)
        self.assertIn("COMMISSION", out)

    def test_reports_limit_fill_rate(self):
        """The number that tells us whether the backtest's fill model is right."""
        out = rp.render(self._full())
        self.assertIn("limit entry fill rate", out)
        self.assertIn("1/2", out)

    def test_reports_slippage(self):
        out = rp.render(self._full())
        self.assertIn("slippage", out)

    def test_reports_maker_share(self):
        out = rp.render(self._full())
        self.assertIn("maker fills", out)

    def test_compares_local_count_against_the_exchange(self):
        out = rp.render(self._full())
        self.assertIn("RECONCILIATION", out)
        self.assertIn("agree", out)

    def test_flags_a_reconciliation_mismatch(self):
        data = self._full()
        data["state"]["total_trades"] = 7
        self.assertIn("MISMATCH", rp.render(data))


class TestSafeToShare(unittest.TestCase):
    """The whole point is that the user can paste it without redacting."""

    def test_render_never_emits_credentials(self):
        import inspect
        src = inspect.getsource(rp)
        for forbidden in ("api_key", "api_secret", "telegram_token",
                          "dashboard_token", "smtp_password"):
            self.assertNotIn(forbidden, src, f"report touches {forbidden}")

    def test_gather_requests_only_history_endpoints(self):
        import inspect
        src = inspect.getsource(rp.gather)
        for method in ("usdt_equity", "income", "user_trades", "all_orders"):
            self.assertIn(method, src)
        self.assertNotIn("order(", src, "the report must never place an order")
        self.assertNotIn("cancel", src, "the report must never cancel anything")

    def test_report_is_read_only(self):
        import inspect
        src = inspect.getsource(rp)
        for mutating in ("cancel_all", "set_leverage", "close_position"):
            self.assertNotIn(mutating, src)
