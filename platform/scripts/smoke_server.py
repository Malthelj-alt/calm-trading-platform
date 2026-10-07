#!/usr/bin/env python3
"""Dashboard smoke test. Standard library only, offline, no lingering server.

1. Runs the engine on data/sample_btcusdt_1h.csv (base + stress) into a temp DB.
2. Starts tradebot.web.make_server on a free localhost port in a thread.
3. GETs /, /test/1, /compare, /api/tests and /static/style.css; asserts 200,
   the 'Simulated — no real money' label and all 10 test names.
4. Shuts the server down and exits 0 (1 on any failure).

    python3 scripts/smoke_server.py
"""
import errno
import html
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tradebot.engine import PROFILES, run_all  # noqa: E402
from tradebot.strategies import TEST_NAMES  # noqa: E402
import tradebot.web as web  # noqa: E402
from tradebot.web import make_server  # noqa: E402

SAMPLE_CSV = os.path.join(ROOT, "data", "sample_btcusdt_1h.csv")
LABEL = "Simulated — no real money"
TIMEOUT = 10


# Set SMOKE_REQUIRE_PORT=1 to forbid the socketpair fallback (strict TCP-only mode).
REQUIRE_PORT = os.environ.get("SMOKE_REQUIRE_PORT") == "1"
BIND_BLOCKED = (errno.EPERM, errno.EACCES)


def tcp_fetcher(base):
    def fetch(path):
        # Only ever talks to our own server on 127.0.0.1; no proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + path, timeout=TIMEOUT) as resp:
            return (resp.status, resp.headers.get("Content-Type", ""),
                    resp.read().decode("utf-8"))
    return fetch


def socketpair_fetcher(server):
    """Drive the real server object over an in-process socket pair.

    Used only when the OS refuses to bind a loopback port (e.g. a sandbox).
    The full HTTP request parsing, routing and response writing of the
    dashboard handler still run; only the TCP listener is skipped.
    """
    def fetch(path):
        client, served = socket.socketpair()
        client.settimeout(TIMEOUT)
        worker = threading.Thread(target=server.finish_request,
                                  args=(served, ("127.0.0.1", 0)), daemon=True)
        worker.start()
        try:
            client.sendall(("GET %s HTTP/1.1\r\nHost: smoke.local\r\n"
                            "Connection: close\r\n\r\n" % path).encode("ascii"))
            resp = http.client.HTTPResponse(client)
            resp.begin()
            body = resp.read().decode("utf-8")
            return resp.status, resp.getheader("Content-Type", ""), body
        finally:
            worker.join(timeout=TIMEOUT)
            served.close()
            client.close()
    return fetch


def make_unbound_server(db):
    """Call the real make_server, but with bind/listen deferred."""
    real_cls = web.ThreadingHTTPServer

    class Unbound(real_cls):
        def __init__(self, addr, handler, bind_and_activate=True):
            super().__init__(addr, handler, bind_and_activate=False)

    with mock.patch.object(web, "ThreadingHTTPServer", Unbound):
        return make_server(db, 0, "127.0.0.1")


def contains_name(text, name):
    return name in text or html.escape(name) in text or html.escape(name, quote=False) in text


def main():
    failures = []

    def check(cond, msg):
        print(("  ok    " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory(prefix="calm_trader_smoke_") as tmp:
        db = os.path.join(tmp, "smoke.db")
        for profile in PROFILES:
            run_id, digest = run_all(SAMPLE_CSV, db, profile)
            print("engine: %s run %d, ledger sha256 %s" % (profile, run_id, digest[:16]))

        thread = None
        try:
            server = make_server(db, 0, "127.0.0.1")
        except OSError as exc:
            if exc.errno not in BIND_BLOCKED or REQUIRE_PORT:
                print("FAIL: cannot bind a local port (%s)." % exc)
                return 1
            print("server: NOTE loopback port binding is blocked here (%s);" % exc)
            print("        falling back to an in-process socket pair. The TCP listener is")
            print("        NOT exercised; allow local binding to test it (SMOKE_REQUIRE_PORT=1).")
            server = make_unbound_server(db)
            fetch_path = socketpair_fetcher(server)
        else:
            port = server.server_address[1]
            base = "http://127.0.0.1:%d" % port
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            print("server: %s" % base)
            fetch_path = tcp_fetcher(base)
        try:
            pages = {}
            for path in ("/", "/test/1", "/compare", "/api/tests", "/static/style.css"):
                try:
                    status, ctype, body = fetch_path(path)
                except Exception as exc:  # noqa: BLE001 - report every kind of failure
                    check(False, "GET %s raised %r" % (path, exc))
                    continue
                check(status == 200, "GET %s -> %d (%s)" % (path, status, ctype.split(";")[0]))
                pages[path] = (ctype, body)

            for path in ("/", "/test/1", "/compare"):
                if path in pages:
                    ctype, body = pages[path]
                    check("text/html" in ctype, "%s is HTML" % path)
                    check(LABEL in body, "%s shows '%s'" % (path, LABEL))

            for path in ("/", "/compare"):
                if path in pages:
                    body = pages[path][1]
                    missing = [n for n in TEST_NAMES if not contains_name(body, n)]
                    check(not missing, "%s lists all 10 test names%s"
                          % (path, "" if not missing else " (missing: %s)" % missing))

            if "/test/1" in pages:
                check(contains_name(pages["/test/1"][1], TEST_NAMES[0]),
                      "/test/1 is '%s'" % TEST_NAMES[0])

            if "/api/tests" in pages:
                try:
                    data = json.loads(pages["/api/tests"][1])
                except ValueError as exc:
                    check(False, "/api/tests is JSON (%s)" % exc)
                else:
                    names = [t.get("name") for t in data.get("tests", [])]
                    check(names == TEST_NAMES, "/api/tests returns the 10 tests in order")
                    check(data.get("simulated") is True and data.get("label") == LABEL,
                          "/api/tests is flagged simulated")
                    starts = {str(t.get("start_balance")) for t in data.get("tests", [])}
                    check(all(s.replace(".00", "") == "50" for s in starts),
                          "/api/tests start balances are $50 (%s)" % sorted(starts))

            if "/static/style.css" in pages:
                ctype, body = pages["/static/style.css"]
                check("text/css" in ctype and len(body) > 0, "/static/style.css is CSS")
        finally:
            if thread is not None:
                server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=5)
        if thread is not None:
            check(not thread.is_alive(), "server thread stopped")

    if failures:
        print("\nsmoke test FAILED (%d problem%s)" % (len(failures), "" if len(failures) == 1 else "s"))
        return 1
    print("\nsmoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
