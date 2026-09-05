"""
Dashboard: auth, command safety, and the snapshot contract.

The dashboard can close positions, so these tests are about access control as
much as behaviour.
"""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.dashboard import Dashboard, generate_token      # noqa: E402


class ServerCase(unittest.TestCase):
    port = 8097

    def setUp(self):
        self.token = generate_token()
        self.dash = Dashboard(self.token, "127.0.0.1", self.port)
        self.dash.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.dash.stop()

    def call(self, path, method="GET", body=None, token="__valid__"):
        tok = self.token if token == "__valid__" else token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body or b"{}")
            except json.JSONDecodeError:
                return e.code, {}


class TestAuth(ServerCase):
    port = 8096

    def test_no_token_is_rejected(self):
        self.assertEqual(self.call("/api/state", token=None)[0], 401)

    def test_wrong_token_is_rejected(self):
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)

    def test_valid_token_is_accepted(self):
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_healthz_needs_no_token(self):
        self.assertEqual(self.call("/healthz", token=None)[0], 200)

    def test_valid_token_still_works_while_someone_guesses(self):
        """A flood of bad guesses must not lock the owner out of the close button."""
        for _ in range(RATE := 12):
            self.call("/api/state", token=f"guess-{RATE}")
        self.assertGreaterEqual(self.dash.recent_auth_failures, 10)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_unauthenticated_requests_cannot_queue_commands(self):
        self.call("/api/close", "POST", {"confirm": True}, token=None)
        self.call("/api/close", "POST", {"confirm": True}, token="wrong")
        self.assertEqual(self.dash.pop_commands(), [])


class TestCommands(ServerCase):
    port = 8095

    def test_close_requires_explicit_confirmation(self):
        code, payload = self.call("/api/close", "POST", {})
        self.assertEqual(code, 400)
        self.assertIn("confirmation", payload["error"])
        self.assertEqual(self.dash.pop_commands(), [])

    def test_confirmed_close_is_queued_not_executed(self):
        """The HTTP thread must never touch the exchange; it only queues."""
        code, _ = self.call("/api/close", "POST", {"confirm": True})
        self.assertEqual(code, 202)
        cmds = self.dash.pop_commands()
        self.assertEqual([c.action for c in cmds], ["close"])

    def test_halt_requires_confirmation_too(self):
        self.assertEqual(self.call("/api/halt", "POST", {})[0], 400)
        self.assertEqual(self.call("/api/halt", "POST", {"confirm": True})[0], 202)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.call("/api/liquidate", "POST", {"confirm": True})[0], 404)

    def test_oversized_body_is_refused(self):
        code, _ = self.call("/api/close", "POST", {"confirm": True, "note": "x" * 8000})
        self.assertEqual(code, 413)

    def test_malformed_json_is_refused(self):
        req = urllib.request.Request(self.base + "/api/close", data=b"{not json",
                                     method="POST")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("should have raised")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


class TestSnapshot(ServerCase):
    port = 8094

    def test_published_snapshot_is_served_verbatim(self):
        self.dash.publish({"symbol": "BTCUSDT", "equity": 43.0, "position": None})
        code, payload = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["equity"], 43.0)
        self.assertIsNone(payload["position"])

    def test_page_is_served_with_security_headers(self):
        req = urllib.request.Request(f"{self.base}/?t={self.token}")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("Content-Security-Policy", r.headers)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn(b"Bot Monitor", r.read()[:2000])


class TestEngineSnapshotShape(unittest.TestCase):
    """The page reads specific keys; a rename would silently blank the UI."""

    REQUIRED = {"symbol", "mode", "strategy", "dry_run", "equity", "price",
                "realized_today", "day", "target", "target_pct", "target_reached",
                "stop_when_reached", "target_note", "halted", "halt_reason",
                "trades_today", "position", "events", "stream_ok"}

    def test_engine_snapshot_declares_every_key_the_page_uses(self):
        import inspect
        from bot import engine
        src = inspect.getsource(engine.Engine.snapshot)
        missing = {k for k in self.REQUIRED if f'"{k}"' not in src}
        self.assertEqual(missing, set(), f"snapshot() is missing keys: {missing}")

    def test_position_block_declares_its_keys(self):
        import inspect
        from bot import engine
        src = inspect.getsource(engine.Engine.snapshot)
        for k in ("side", "qty", "entry", "stop", "take_profit",
                  "unrealized", "to_tp", "to_sl"):
            self.assertIn(f'"{k}"', src)
