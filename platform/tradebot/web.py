"""Calm, server-rendered dashboard for the paper-trading ledger.

Simulated — no real money. This module only *reads* the SQLite ledger written
by the engine; it has no order-sending code and no write path to the ledger.

Routes
    GET /                         tests overview (?profile=base|stress, ?sort=, ?dir=)
    GET /test/<id>                one test: summary numbers and trade log
    GET /compare                  the 10 tests across both fee profiles
    GET /api/tests?profile=base   JSON: run header + the 10 test results
    GET /api/tests/<id>/trades    JSON: one test + its trades
    GET /static/style.css         the stylesheet

Usage
    server = make_server("trader.db", 8000)   # port 0 picks a free port
    server.serve_forever()
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

SIM_LABEL = "Simulated — no real money"
PROFILE_NAMES = ("base", "stress")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MINUS = "−"  # typographic minus sign
CENT = Decimal("0.01")

# Overview sort keys -> (column label, row getter). Getter returns a sortable value.
SORT_COLUMNS: Dict[str, Tuple[str, Any]] = {
    "number": ("#", lambda t: t["number"]),
    "name": ("Test", lambda t: t["name"].lower()),
    "final": ("Final balance", lambda t: _dec(t["final_balance"])),
    "pnl": ("P&L $", lambda t: _dec(t["pnl"])),
    "pnl_pct": ("P&L %", lambda t: _dec(t["pnl_pct"])),
    "trades": ("Trades", lambda t: t["trade_count"] or 0),
    "win": ("Win rate", lambda t: _dec(t["win_rate"], Decimal("-1"))),
    "dd": ("Max drawdown", lambda t: _dec(t["max_drawdown_pct"])),
}


# ============================================================ formatting helpers
def esc(value: Any) -> str:
    """HTML-escape anything (quotes included)."""
    return html.escape("" if value is None else str(value), quote=True)


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
    return d if d.is_finite() else default


def _q(d: Decimal, places: str = "0.01") -> Decimal:
    return d.quantize(Decimal(places), ROUND_HALF_EVEN)


def money(value: Any) -> str:
    """$1,234.56 (unsigned)."""
    return "$" + format(_q(_dec(value)), ",.2f")


def price(value: Any) -> str:
    return format(_q(_dec(value)), ",.2f")


def qty(value: Any) -> str:
    return format(_q(_dec(value), "0.00000001"), "f")


def pct(value: Any) -> str:
    if value is None:
        return "—"
    return format(_q(_dec(value)), "f") + "%"


def rate_pct(rate: Any) -> str:
    """Fraction to percent: 0.001 -> 0.10%."""
    return format(_q(_dec(rate) * 100), "f") + "%"


def signed_html(value: Any, kind: str = "money") -> str:
    """Signed, coloured number. Colour is never used without a +/− sign."""
    d = _q(_dec(value))
    body = format(abs(d), ",.2f")
    body = "$" + body if kind == "money" else body + "%"
    if d > 0:
        return '<span class="pos">+%s</span>' % esc(body)
    if d < 0:
        return '<span class="neg">%s%s</span>' % (MINUS, esc(body))
    return '<span class="flat">±%s</span>' % esc(body)


def utc(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def instrument_label(sym: str) -> str:
    sym = sym or ""
    if sym.upper().endswith("USDT") and "/" not in sym:
        return sym[:-4] + "/USDT"
    return sym


# ============================================================ data access (read-only)
class Store:
    """Read-only view of the ledger. One short-lived connection per request."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _connect(self) -> Optional[sqlite3.Connection]:
        path = Path(self.db_path).resolve()
        if not path.exists():
            return None
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        except sqlite3.OperationalError:  # e.g. empty file without our tables yet
            return []
        finally:
            conn.close()

    def latest_runs(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in self._query("SELECT * FROM runs WHERE status = 'complete' ORDER BY id"):
            out[r["profile"]] = r
        return out

    def run(self, run_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    def tests(self, run_id: int) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM tests WHERE run_id = ? ORDER BY number", (run_id,))

    def test(self, test_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM tests WHERE id = ?", (test_id,))
        return rows[0] if rows else None

    def trades(self, test_id: int) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM trades WHERE test_id = ? ORDER BY seq", (test_id,))


# ============================================================ JSON shapes
_TEST_FIELDS = ("id", "number", "name", "strategy_key", "description", "start_balance",
                "final_balance", "pnl", "pnl_pct", "trade_count", "win_rate",
                "max_drawdown_pct", "fees_paid")
_TRADE_FIELDS = ("seq", "entry_time_ms", "entry_price", "exit_time_ms", "exit_price", "qty",
                 "cost", "proceeds", "fees", "pnl", "pnl_pct", "exit_reason", "data_source")


def run_json(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": run["id"], "profile": run["profile"], "instrument": run["instrument"],
        "interval": run["interval"], "fee_rate": run["fee_rate"],
        "slippage_rate": run["slippage_rate"], "start_balance": run["start_balance"],
        "data_source": run["data_source"], "data_sha256": run["data_sha256"],
        "eval_start_ms": run["eval_start_ms"], "eval_end_ms": run["eval_end_ms"],
        "eval_start_utc": utc(run["eval_start_ms"]), "eval_end_utc": utc(run["eval_end_ms"]),
        "eval_bars": run["eval_bars"], "ledger_hash": run["ledger_hash"],
    }


def test_json(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: t.get(k) for k in _TEST_FIELDS}


def trade_json(tr: Dict[str, Any]) -> Dict[str, Any]:
    d = {k: tr.get(k) for k in _TRADE_FIELDS}
    d["entry_time_utc"] = utc(tr["entry_time_ms"])
    d["exit_time_utc"] = utc(tr["exit_time_ms"])
    return d


# ============================================================ HTML pieces
def page(title: str, body: str, active: str = "") -> str:
    def nav(href: str, label: str, key: str) -> str:
        cur = ' aria-current="page"' if key == active else ""
        return '<a href="%s"%s>%s</a>' % (esc(href), cur, esc(label))

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>%s · Calm Paper Trader</title>\n"
        "<link rel=\"stylesheet\" href=\"/static/style.css\">\n"
        "</head>\n<body>\n"
        "<header class=\"top\">\n"
        "<span class=\"brand\">Calm Paper Trader</span>\n"
        "<nav>%s%s</nav>\n"
        "<span class=\"sim\" role=\"note\">%s</span>\n"
        "</header>\n<main>\n%s\n</main>\n"
        "<footer>%s · paper fills only, no orders are ever sent.</footer>\n"
        "</body>\n</html>\n"
    ) % (esc(title), nav("/", "Tests", "overview"), nav("/compare", "Compare", "compare"),
         esc(SIM_LABEL), body, esc(SIM_LABEL))


def empty_state(profile: Optional[str] = None) -> str:
    what = "No completed %s run in the ledger yet." % profile if profile else \
        "No completed runs in the ledger yet."
    return ('<section class="empty"><p>%s</p><p class="muted">Run '
            '<code>python3 -m tradebot run --data data/sample_btcusdt_1h.csv --db &lt;db&gt;</code>'
            ' and reload.</p></section>' % esc(what))


def run_header(run: Dict[str, Any], profile: str, other_link: Optional[str],
               fees_text: Optional[str] = None) -> str:
    """The shared facts shown once: instrument, window, start, fees, data source."""
    source = run["data_source"] or "unknown"
    is_sample = "sample" in source.lower()
    items = [
        ("Instrument", "%s · %s" % (instrument_label(run["instrument"]), run["interval"])),
        ("Window", "%s → %s UTC" % (utc(run["eval_start_ms"]), utc(run["eval_end_ms"]))),
        ("Start", "%s per test" % money(run["start_balance"])),
        ("Fees", fees_text or "%s %s fee + %s slippage per side" % (
            profile.capitalize(), rate_pct(run["fee_rate"]), rate_pct(run["slippage_rate"]))),
        ("Data", source),
    ]
    dl = "".join('<div><dt>%s</dt><dd%s>%s</dd></div>' % (
        esc(k), ' class="sample"' if (k == "Data" and is_sample) else "", esc(v))
        for k, v in items)
    switch = ""
    if other_link:
        other = "stress" if profile == "base" else "base"
        switch = '<p class="switch">Showing <strong>%s</strong> fees · <a href="%s">Switch to %s</a></p>' % (
            esc(profile), esc(other_link), esc(other))
    return '<section class="facts"><dl>%s</dl>%s</section>' % (dl, switch)


def _sort_rows(rows: List[Dict[str, Any]], key: str, direction: str) -> List[Dict[str, Any]]:
    getter = SORT_COLUMNS[key][1]
    return sorted(rows, key=lambda r: (getter(r), r["number"]), reverse=(direction == "desc"))


def overview_html(store: Store, profile: str, sort: str, direction: str) -> str:
    runs = store.latest_runs()
    run = runs.get(profile)
    if run is None:
        return page("Tests", "<h1>Tests</h1>" + empty_state(profile), "overview")
    other = "stress" if profile == "base" else "base"
    other_link = "/?" + urlencode({"profile": other, "sort": sort, "dir": direction}) \
        if other in runs else None

    rows = _sort_rows(store.tests(run["id"]), sort, direction)
    heads = []
    numeric = {"number", "final", "pnl", "pnl_pct", "trades", "win", "dd"}
    for key, (label, _) in SORT_COLUMNS.items():
        if key == sort:
            nxt = "asc" if direction == "desc" else "desc"
            mark = " ↓" if direction == "desc" else " ↑"
            aria = ' aria-sort="%s"' % ("descending" if direction == "desc" else "ascending")
        else:
            nxt = "asc" if key in ("number", "name") else "desc"
            mark, aria = "", ""
        href = "/?" + urlencode({"profile": profile, "sort": key, "dir": nxt})
        heads.append('<th scope="col"%s%s><a href="%s">%s</a>%s</th>' % (
            ' class="num"' if key in numeric else "", aria, esc(href), esc(label), esc(mark)))

    body_rows = []
    for t in rows:
        body_rows.append(
            "<tr>"
            '<td class="num muted">%s</td>'
            '<td><a href="/test/%d">%s</a></td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            "</tr>" % (
                esc("%02d" % t["number"]), int(t["id"]), esc(t["name"]),
                esc(money(t["final_balance"])), signed_html(t["pnl"]),
                signed_html(t["pnl_pct"], "pct"), esc(t["trade_count"] or 0),
                esc(pct(t["win_rate"])), esc(pct(t["max_drawdown_pct"]))))
    table = ('<table class="data"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
             % ("".join(heads), "".join(body_rows)))
    body = ("<h1>Tests</h1>%s%s"
            '<p class="note muted">Same bars, clock, $50 start, fees, sizing (100%%, long only) '
            "and t+1 fills for every test; only the strategy differs. "
            "Click a column to sort. Ledger sha256 <code>%s</code></p>"
            % (run_header(run, profile, other_link), table, esc((run["ledger_hash"] or "")[:16])))
    return page("Tests · %s" % profile, body, "overview")


def detail_html(store: Store, test: Dict[str, Any]) -> str:
    run = store.run(int(test["run_id"])) or {}
    profile = run.get("profile", "base")
    trades = store.trades(int(test["id"]))

    # Link to the same test under the other profile, if that run exists.
    other_link = None
    runs = store.latest_runs()
    other = "stress" if profile == "base" else "base"
    if other in runs:
        for t in store.tests(runs[other]["id"]):
            if t["number"] == test["number"]:
                other_link = "/test/%d" % t["id"]
                break

    summary = [
        ("Final balance", esc(money(test["final_balance"]))),
        ("P&L", "%s · %s" % (signed_html(test["pnl"]), signed_html(test["pnl_pct"], "pct"))),
        ("Trades", esc(test["trade_count"] or 0)),
        ("Win rate", esc(pct(test["win_rate"]))),
        ("Max drawdown", esc(pct(test["max_drawdown_pct"]))),
        ("Fees paid", esc(money(test["fees_paid"]))),
    ]
    summary_html = '<section class="summary"><dl>%s</dl></section>' % "".join(
        "<div><dt>%s</dt><dd>%s</dd></div>" % (esc(k), v) for k, v in summary)

    if trades:
        rows = []
        for tr in trades:
            note = ' <span class="muted">(closed at end)</span>' \
                if tr["exit_reason"] == "final_close" else ""
            rows.append(
                "<tr>"
                '<td class="num muted">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s%s</td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                "</tr>" % (
                    esc(tr["seq"]), esc(utc(tr["entry_time_ms"])), esc(price(tr["entry_price"])),
                    esc(utc(tr["exit_time_ms"])), note, esc(price(tr["exit_price"])),
                    esc(qty(tr["qty"])), esc(money(tr["cost"])), esc(money(tr["fees"])),
                    signed_html(tr["pnl"]), signed_html(tr["pnl_pct"], "pct")))
        log = ('<table class="data"><thead><tr>'
               '<th scope="col" class="num">#</th>'
               '<th scope="col" class="num">Entry (UTC)</th><th scope="col" class="num">Entry price</th>'
               '<th scope="col" class="num">Exit (UTC)</th><th scope="col" class="num">Exit price</th>'
               '<th scope="col" class="num">Size (BTC)</th><th scope="col" class="num">Size ($)</th>'
               '<th scope="col" class="num">Fees</th>'
               '<th scope="col" class="num">Result $</th><th scope="col" class="num">Result %%</th>'
               "</tr></thead><tbody>%s</tbody></table>" % "".join(rows))
    else:
        log = '<p class="muted">No trades in this window.</p>'

    switch = ""
    if other_link:
        switch = ' · <a href="%s">Same test at %s fees</a>' % (esc(other_link), esc(other))
    body = (
        '<p class="crumb"><a href="/?%s">← All tests</a>%s</p>'
        "<h1>%s</h1>"
        '<p class="muted">%s</p>'
        "%s%s"
        "<h2>Trade log</h2>%s"
        '<p class="note muted">Prices include slippage. Fees are entry + exit. '
        "Fills happen at the next bar's open (t+1). "
        '<a href="/api/tests/%d/trades">JSON</a></p>'
    ) % (esc(urlencode({"profile": profile})), switch, esc(test["name"]),
         esc(test["description"]),
         run_header(run, profile, None) if run else "", summary_html, log, int(test["id"]))
    return page(test["name"], body, "overview")


def compare_html(store: Store) -> str:
    runs = store.latest_runs()
    if not runs:
        return page("Compare", "<h1>Compare</h1>" + empty_state(), "compare")
    profiles = [p for p in PROFILE_NAMES if p in runs]
    by_profile = {p: {t["number"]: t for t in store.tests(runs[p]["id"])} for p in profiles}
    numbers = sorted({n for d in by_profile.values() for n in d})
    first = runs[profiles[0]]

    head1 = '<th scope="col" rowspan="2">Test</th>' + "".join(
        '<th scope="colgroup" colspan="3" class="grp">%s · fee %s</th>' % (
            esc(p.capitalize()), esc(rate_pct(runs[p]["fee_rate"]))) for p in profiles)
    if len(profiles) == 2:
        head1 += '<th scope="col" rowspan="2" class="num grp">Fee drag</th>'
    head2 = "".join('<th scope="col" class="num">Final</th><th scope="col" class="num">P&amp;L %</th>'
                    '<th scope="col" class="num">Trades</th>' for _ in profiles)

    rows = []
    for n in numbers:
        name_t = next(by_profile[p][n] for p in profiles if n in by_profile[p])
        cells = ['<td><a href="/test/%d">%s</a></td>' % (int(name_t["id"]), esc(name_t["name"]))]
        for p in profiles:
            t = by_profile[p].get(n)
            if t is None:
                cells.append('<td class="num muted" colspan="3">—</td>')
                continue
            cells.append('<td class="num grp"><a href="/test/%d">%s</a></td><td class="num">%s</td>'
                         '<td class="num">%s</td>' % (
                             int(t["id"]), esc(money(t["final_balance"])),
                             signed_html(t["pnl_pct"], "pct"), esc(t["trade_count"] or 0)))
        if len(profiles) == 2:
            b, s = by_profile["base"].get(n), by_profile["stress"].get(n)
            if b and s:
                drag = _dec(s["final_balance"]) - _dec(b["final_balance"])
                cells.append('<td class="num grp">%s</td>' % signed_html(drag))
            else:
                cells.append('<td class="num grp muted">—</td>')
        rows.append("<tr>%s</tr>" % "".join(cells))

    table = ('<table class="data compare"><thead><tr>%s</tr><tr>%s</tr></thead>'
             "<tbody>%s</tbody></table>" % (head1, head2, "".join(rows)))
    body = ("<h1>Compare</h1>%s%s"
            '<p class="note muted">Fee drag is the stress final balance minus the base final '
            "balance. Slippage is %s per side in both profiles.</p>"
            % (run_header(first, profiles[0], None, fees_text="%s fee + %s slippage per side" % (
                " / ".join("%s %s" % (p.capitalize(), rate_pct(runs[p]["fee_rate"]))
                           for p in profiles), rate_pct(first["slippage_rate"]))),
               table, esc(rate_pct(first["slippage_rate"]))))
    return page("Compare", body, "compare")


def not_found_html(msg: str = "Not found.") -> str:
    return page("Not found", '<h1>Not found</h1><p class="muted">%s</p>'
                '<p><a href="/">Back to tests</a></p>' % esc(msg))


# ============================================================ HTTP handler
_TEST_RE = re.compile(r"^/test/(\d{1,12})/?$")
_API_TRADES_RE = re.compile(r"^/api/tests/(\d{1,12})/trades/?$")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CalmPaperTrader/1.0"
    sys_version = ""
    store: Store  # set on the subclass made by make_server
    quiet = True

    # ---------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.quiet:
            super().log_message(format, *args)

    def _send(self, status: int, body: bytes, ctype: str, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; img-src 'self'; "
                         "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Simulated", "no real money")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _html(self, status: int, text: str, head_only: bool) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8", head_only)

    def _json(self, status: int, obj: Any, head_only: bool) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", head_only)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def _method_not_allowed(self) -> None:
        body = b"Read-only dashboard. Simulated - no real money.\n"
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

    # ---------------------------------------------------------------- routing
    def _dispatch(self, head_only: bool) -> None:
        parts = urlsplit(self.path)
        path = parts.path or "/"
        qs = parse_qs(parts.query, keep_blank_values=False)

        def arg(name: str, default: str) -> str:
            vals = qs.get(name)
            return vals[0] if vals else default

        try:
            if path == "/static/style.css":
                return self._static("style.css", "text/css; charset=utf-8", head_only)

            if path in ("/", "/index.html"):
                profile = arg("profile", "base")
                if profile not in PROFILE_NAMES:
                    profile = "base"
                sort = arg("sort", "number")
                if sort not in SORT_COLUMNS:
                    sort = "number"
                direction = arg("dir", "asc")
                if direction not in ("asc", "desc"):
                    direction = "asc"
                return self._html(200, overview_html(self.store, profile, sort, direction),
                                  head_only)

            m = _TEST_RE.match(path)
            if m:
                test = self.store.test(int(m.group(1)))
                if test is None or test["final_balance"] is None:
                    return self._html(404, not_found_html("No finished test with that id."),
                                      head_only)
                return self._html(200, detail_html(self.store, test), head_only)

            if path in ("/compare", "/compare/"):
                return self._html(200, compare_html(self.store), head_only)

            if path in ("/api/tests", "/api/tests/"):
                profile = arg("profile", "base")
                if profile not in PROFILE_NAMES:
                    return self._json(400, {"error": "profile must be one of %s"
                                            % ", ".join(PROFILE_NAMES)}, head_only)
                run = self.store.latest_runs().get(profile)
                payload = {"simulated": True, "label": SIM_LABEL, "profile": profile,
                           "run": run_json(run) if run else None,
                           "tests": [test_json(t) for t in self.store.tests(run["id"])]
                           if run else []}
                return self._json(200, payload, head_only)

            m = _API_TRADES_RE.match(path)
            if m:
                test = self.store.test(int(m.group(1)))
                if test is None:
                    return self._json(404, {"error": "test not found"}, head_only)
                run = self.store.run(int(test["run_id"]))
                payload = {"simulated": True, "label": SIM_LABEL,
                           "profile": run["profile"] if run else None,
                           "test": test_json(test),
                           "trades": [trade_json(tr) for tr in self.store.trades(int(test["id"]))]}
                return self._json(200, payload, head_only)

            if path.startswith("/api/"):
                return self._json(404, {"error": "not found"}, head_only)
            return self._html(404, not_found_html(), head_only)
        except sqlite3.Error as exc:
            msg = "The ledger could not be read: %s" % exc
            if path.startswith("/api/"):
                return self._json(503, {"error": msg}, head_only)
            return self._html(503, page("Unavailable", "<h1>Unavailable</h1><p>%s</p>"
                                        % esc(msg)), head_only)

    def _static(self, name: str, ctype: str, head_only: bool) -> None:
        try:
            data = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self._send(404, b"/* not found */", ctype, head_only)
        self._send(200, data, ctype, head_only)


# ============================================================ server factory
def make_server(db_path: str, port: int = 8000, host: str = "127.0.0.1",
                quiet: bool = True) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard server.

    Binds to localhost by default; ``port=0`` picks a free port (read it back
    from ``server.server_address[1]``). Call ``serve_forever()`` to run and
    ``shutdown()`` + ``server_close()`` to stop.
    """
    handler = type("BoundDashboardHandler", (DashboardHandler,),
                   {"store": Store(os.fspath(db_path)), "quiet": quiet})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":  # pragma: no cover - convenience only
    import argparse

    ap = argparse.ArgumentParser(description="Calm Paper Trader dashboard (%s)." % SIM_LABEL)
    ap.add_argument("--db", required=True)
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    srv = make_server(a.db, a.port, quiet=False)
    print("%s. Dashboard on http://localhost:%d" % (SIM_LABEL, srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
