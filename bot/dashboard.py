"""
Web dashboard: monitor from a phone or laptop, and close a trade if needed.

Design constraints that shaped this module:

  * It can close positions, so it is an authenticated control plane, not a
    status page. Every request carries a bearer token, compared in constant
    time. No token, no access -- there is no "read-only anonymous" mode.
  * It binds to 127.0.0.1 by default. Exposing an order-cancelling endpoint
    on a public cloud IP is a bad trade; use an SSH tunnel or Tailscale.
  * The HTTP thread NEVER calls the exchange. It reads a snapshot the engine
    publishes, and pushes commands onto a queue the engine drains in its own
    loop. That keeps all order placement single-threaded.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("dashboard")

PAGE = Path(__file__).parent / "dashboard.html"
MAX_BODY = 4096
RATE_WINDOW = 60
RATE_MAX_FAILURES = 10


@dataclass
class Command:
    action: str                       # close | halt | resume | strategy
    requested_at: float = field(default_factory=time.time)
    note: str = ""
    value: str = ""                   # payload, e.g. the strategy to switch to


class Dashboard:
    """Owns the HTTP server. The engine owns the data."""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8080):
        self.token = token
        self.host = host
        self.port = port
        self.commands: queue.Queue[Command] = queue.Queue()
        self._snapshot: dict = {"status": "starting"}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._failures: list[float] = []
        self._fail_lock = threading.Lock()

    # --------------------------------------------------------------- data
    def publish(self, snapshot: dict) -> None:
        with self._lock:
            self._snapshot = snapshot

    def read(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def pop_commands(self) -> list[Command]:
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                return out

    # ------------------------------------------------------------- auth
    def authorised(self, header: str | None, query_token: str | None) -> bool:
        """
        Token may arrive as `Authorization: Bearer <t>` or as ?t=<t>, because
        opening a link on a phone cannot set a header. Both are compared in
        constant time.

        A VALID token is always accepted, even while the throttle is active.
        Locking out the correct token because someone else is guessing would
        mean an attacker could stop you reaching your own close button -- the
        throttle exists to slow guessing, not to deny you control of your
        position.
        """
        supplied = ""
        if header and header.startswith("Bearer "):
            supplied = header[7:]
        elif query_token:
            supplied = query_token

        if supplied and hmac.compare_digest(supplied, self.token):
            return True

        self._throttle()
        return False

    def _throttle(self) -> None:
        """
        Delay once guessing starts. ThreadingHTTPServer runs handlers in
        parallel, so the counter needs a lock, and the delay is held WHILE
        locked -- otherwise a parallel flood is not slowed at all, it just
        spawns more threads. (QA F17)
        """
        with self._fail_lock:
            now = time.time()
            self._failures = [t for t in self._failures if t > now - RATE_WINDOW]
            self._failures.append(now)
            over = len(self._failures) >= RATE_MAX_FAILURES
            if over:
                log.warning("dashboard: %d failed auth attempts in %ds",
                            len(self._failures), RATE_WINDOW)
                time.sleep(1.0)

    @property
    def recent_auth_failures(self) -> int:
        with self._fail_lock:
            cutoff = time.time() - RATE_WINDOW
            return len([t for t in self._failures if t > cutoff])

    # -------------------------------------------------------------- serve
    def start(self) -> str:
        dash = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "bot/1.0"

            def log_message(self, fmt, *a):          # quiet; we have our own logs
                log.debug("%s - %s", self.address_string(), fmt % a)

            # ------------------------------------------------------ helpers
            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload).encode(), "application/json")

            def _auth(self, query: dict) -> bool:
                tok = (query.get("t") or [None])[0]
                if dash.authorised(self.headers.get("Authorization"), tok):
                    return True
                self._json(401, {"error": "unauthorised"})
                return False

            # --------------------------------------------------------- HEAD
            def do_HEAD(self) -> None:
                """Uptime monitors probe with HEAD; 501 looked like an outage. (QA F18)"""
                u = urlparse(self.path)
                if u.path == "/healthz":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._auth(parse_qs(u.query)):
                    return
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            # ---------------------------------------------------------- GET
            def do_GET(self) -> None:
                u = urlparse(self.path)
                q = parse_qs(u.query)

                if u.path == "/healthz":                    # no auth: liveness only
                    self._json(200, {"ok": True})
                    return
                if not self._auth(q):
                    return

                if u.path == "/":
                    try:
                        html = PAGE.read_bytes()
                    except OSError as e:
                        self._json(500, {"error": f"page missing: {e}"})
                        return
                    self._send(200, html, "text/html; charset=utf-8")
                elif u.path == "/api/state":
                    self._json(200, dash.read())
                else:
                    self._json(404, {"error": "not found"})

            # --------------------------------------------------------- POST
            def do_POST(self) -> None:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._auth(q):
                    return

                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._json(413, {"error": "body too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "bad json"})
                    return

                action = {"/api/close": "close", "/api/halt": "halt",
                          "/api/resume": "resume",
                          "/api/strategy": "strategy"}.get(u.path)
                if action is None:
                    self._json(404, {"error": "not found"})
                    return

                # Every state change needs explicit confirmation -- including
                # resume, which is what undoes a daily-loss-limit halt. It was
                # previously the one unconfirmed route into the most dangerous
                # state change in the system. (QA F6)
                if body.get("confirm") is not True:
                    self._json(400, {"error": "confirmation required"})
                    return

                dash.commands.put(Command(action,
                                          note=str(body.get("note", ""))[:120],
                                          value=str(body.get("value", ""))[:40]))
                log.info("dashboard command queued: %s", action)
                self._json(202, {"queued": action})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="dashboard", daemon=True)
        self._thread.start()
        url = f"http://{self.host}:{self.port}/?t={self.token}"
        log.info("dashboard listening on http://%s:%d", self.host, self.port)
        return url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def generate_token() -> str:
    return secrets.token_urlsafe(24)
