"""
Minimal signed Binance USD-M futures client. Standard library only.

Deliberately small: every endpoint the bot uses is here and nothing else,
so there is no vendored library between you and what actually gets sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("binance")

LIVE = "https://fapi.binance.com"

# Binance's documented testnet base. The old testnet.binancefuture.com still
# works, but it is a CloudFront alias (d1ttzas476mze3.cloudfront.net) that the
# docs no longer name, while demo-fapi resolves straight to the backend. Both
# hit the same venue today; the legacy alias is the one likely to be retired.
# Override with BINANCE_TESTNET_BASE if that ever inverts.
TESTNET = "https://demo-fapi.binance.com"
TESTNET_LEGACY = "https://testnet.binancefuture.com"

RECV_WINDOW = 5000


#: Binance's auth failures are terse and their causes are non-obvious, so each
#: one gets a plain explanation of what to actually do about it.
ERROR_HELP = {
    -2015: (
        "Invalid API key, IP, or permissions.\n"
        "  Almost always one of these, in order of likelihood:\n"
        "   1. Futures is not enabled on the key. Binance only lets you enable\n"
        "      Futures on a key that is ALREADY IP-restricted -- apply the IP\n"
        "      restriction first, save, then the Futures checkbox becomes\n"
        "      available.\n"
        "   2. The key predates your Futures account. A key created before you\n"
        "      opened Futures can never gain the permission; delete it and\n"
        "      create a new one after opening Futures.\n"
        "   3. The IP allow-list does not include this host. Check the server's\n"
        "      public IP, not your laptop's.\n"
        "   4. Portfolio Margin is enabled, which disables the classic Futures\n"
        "      API this bot uses.\n"
        "   5. Testnet keys used against live, or the reverse."),
    -2014: "Malformed API key. Check for a truncated or whitespace-padded paste.",
    -1022: "Signature invalid. Usually a wrong API SECRET, or a stray newline in .env.",
    -1021: ("Timestamp outside the recv window -- your clock has drifted.\n"
            "  Fix NTP: sudo systemctl restart systemd-timesyncd (or install chrony)."),
    -1002: "Unauthorised. The key is missing, revoked, or for the wrong environment.",
    -4046: "No need to change margin type -- already set. Harmless.",
    -1121: "Unknown symbol for this venue. Check the symbol exists on futures.",
    -1003: ("Rate limited, and the IP may be temporarily banned.\n"
            "  Wait it out -- the ban is time-boxed and retrying extends nothing.\n"
            "  If this appeared right after an auth failure, the auth error is\n"
            "  the real problem; something retried instead of stopping."),
}

#: Credential problems. Retrying these against a different endpoint cannot help
#: -- the key is wrong, not the endpoint -- and burns request weight that can
#: get the IP rate-limited or banned.
AUTH_ERRORS = frozenset({-1002, -1021, -1022, -2014, -2015})


class BinanceError(RuntimeError):
    def __init__(self, code, msg, endpoint):
        super().__init__(f"[{code}] {msg}  ({endpoint})")
        self.code = code
        self.msg = msg

    @property
    def help(self) -> str:
        return ERROR_HELP.get(self.code, "")

    def explain(self) -> str:
        h = self.help
        return f"{self}\n\n  {h}" if h else str(self)


class Binance:
    def __init__(self, key: str = "", secret: str = "", testnet: bool = True,
                 timeout: int = 10, base: str | None = None):
        import os

        self.key = key
        self.secret = secret.encode()
        if base:
            self.base = base.rstrip("/")
        elif testnet:
            self.base = os.environ.get("BINANCE_TESTNET_BASE", TESTNET).rstrip("/")
        else:
            self.base = LIVE
        self.testnet = testnet
        self.timeout = timeout
        self._offset_ms = 0

    # ---------------------------------------------------------------- plumbing
    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        if signed:
            if not self.key or not self.secret:
                raise BinanceError(-1, "signed call attempted without API credentials", path)
            params["timestamp"] = int(time.time() * 1000) + self._offset_ms
            params["recvWindow"] = RECV_WINDOW
            query = urllib.parse.urlencode(params, doseq=True)
            sig = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={sig}"
        else:
            query = urllib.parse.urlencode(params, doseq=True)

        url = f"{self.base}{path}"
        data = None
        if method in ("POST", "PUT", "DELETE"):
            data = query.encode()
        elif query:
            url = f"{url}?{query}"

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-MBX-APIKEY", self.key)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                payload = json.loads(body)
                raise BinanceError(payload.get("code", e.code), payload.get("msg", body), path) from None
            except json.JSONDecodeError:
                raise BinanceError(e.code, body[:200], path) from None
        except urllib.error.URLError as e:
            raise BinanceError(-2, f"network: {e.reason}", path) from None

    # ----------------------------------------------------------------- public
    def sync_clock(self) -> int:
        """Signed requests are rejected if the local clock drifts. Measure and correct."""
        local = int(time.time() * 1000)
        server = self._request("GET", "/fapi/v1/time")["serverTime"]
        self._offset_ms = server - local
        if abs(self._offset_ms) > 1000:
            log.warning("clock drift %d ms -- correcting locally, but fix NTP", self._offset_ms)
        return self._offset_ms

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(self, symbol: str, interval: str, limit: int = 200,
               end_ms: int | None = None):
        """end_ms fetches an EARLIER window -- needed for out-of-sample testing."""
        p = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_ms is not None:
            p["endTime"] = end_ms
        return self._request("GET", "/fapi/v1/klines", p)

    def mark_price(self, symbol: str):
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    def funding_history(self, symbol: str, limit: int = 500):
        return self._request("GET", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})

    def book_ticker(self, symbol: str):
        return self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    # ---------------------------------------------------------------- private
    def balances(self):
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def account(self):
        return self._request("GET", "/fapi/v2/account", signed=True)

    def usdt_equity(self) -> float:
        """
        Margin balance: wallet plus unrealised P&L across BOTH margin modes.

        The obvious `balance + crossUnPnl` from /fapi/v2/balance excludes
        isolated positions by definition -- and the engine sets ISOLATED at
        startup, so that reading treated an open losing trade as if it did not
        exist, disabling the daily loss limit and the equity floor. (QA F2)
        """
        try:
            acct = self.account()
            for a in acct.get("assets", []):
                if a["asset"] == "USDT":
                    return float(a["marginBalance"])
            return float(acct["totalMarginBalance"])
        except BinanceError as e:
            # Never fall back on a credential failure. The key is wrong, not the
            # endpoint, so a second request cannot succeed -- it only spends
            # request weight and buries the real error under a rate-limit one.
            if e.code in AUTH_ERRORS:
                raise
            log.warning("account endpoint unusable (%s); falling back to "
                        "balance + positionRisk", e)
        except (KeyError, ValueError) as e:
            log.warning("account response malformed (%s); falling back to "
                        "balance + positionRisk", e)
        try:
            wallet = 0.0
            for b in self.balances():
                if b["asset"] == "USDT":
                    wallet = float(b["balance"])
                    break
            unrealised = sum(float(r["unRealizedProfit"])
                             for r in self._request("GET", "/fapi/v2/positionRisk",
                                                    {}, signed=True))
            return wallet + unrealised
        except BinanceError:
            raise

    def positions(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        rows = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        return [r for r in rows if float(r["positionAmt"]) != 0.0]

    def open_orders(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def set_leverage(self, symbol: str, leverage: int):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            return self._request("POST", "/fapi/v1/marginType",
                                 {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code == -4046:      # "No need to change margin type"
                return {"msg": "unchanged"}
            raise

    def order(self, **params):
        """Place an order. Always pass a newClientOrderId so retries are idempotent."""
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, client_order_id: str):
        """Cancel ONE order. Needed to pull a resting entry while leaving the
        protective stop on the book. (QA R1)"""
        return self._request("DELETE", "/fapi/v1/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)

    def cancel_all(self, symbol: str):
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def query_order(self, symbol: str, client_order_id: str):
        return self._request("GET", "/fapi/v1/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)

    # ----------------------------------------------------------- user stream
    def listen_key(self) -> str:
        """Open a user-data stream. Delivers fills and account updates in real time."""
        return self._request("POST", "/fapi/v1/listenKey", signed=True)["listenKey"]

    def keepalive_listen_key(self) -> None:
        """Must be called at least every 60 minutes or the stream goes silent."""
        self._request("PUT", "/fapi/v1/listenKey", signed=True)

    def close_listen_key(self) -> None:
        self._request("DELETE", "/fapi/v1/listenKey", signed=True)

    def user_trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000):
        """Actual fills, as the exchange recorded them."""
        p = {"symbol": symbol, "limit": limit}
        if start_ms:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/userTrades", p, signed=True)

    def all_orders(self, symbol: str, start_ms: int | None = None, limit: int = 1000):
        """Every order including cancelled and expired ones."""
        p = {"symbol": symbol, "limit": limit}
        if start_ms:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/allOrders", p, signed=True)

    def income(self, income_type: str | None = None, start_ms: int | None = None, limit: int = 100):
        p = {"limit": limit}
        if income_type:
            p["incomeType"] = income_type
        if start_ms:
            p["startTime"] = start_ms
        return self._request("GET", "/fapi/v1/income", p, signed=True)
