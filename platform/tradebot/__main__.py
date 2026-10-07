"""Command line: python -m tradebot run|serve

    python -m tradebot run   --data data/sample_btcusdt_1h.csv --db trader.db
    python -m tradebot serve --db trader.db --port 8000

Simulated — no real money. No command here can send an order anywhere.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import List, Optional

from .agents import DataAgent
from .engine import PROFILES, SimulationResult, simulate
from .ledger import connect

CENT = Decimal("0.01")


def _money(d: Decimal) -> str:
    return "$" + format(d.quantize(CENT, ROUND_HALF_EVEN), ",.2f")


def _signed(d: Decimal, suffix: str = "") -> str:
    q = d.quantize(CENT, ROUND_HALF_EVEN)
    sign = "+" if q > 0 else ("-" if q < 0 else " ")
    return "%s%s%s" % (sign, format(abs(q), ",.2f"), suffix)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_table(res: SimulationResult) -> str:
    p = res.profile
    lines = [
        "%s profile - fee %.2f%% + slippage %.2f%% per side"
        % (p.name.capitalize(), p.fee_rate * 100, p.slippage_rate * 100),
        "BTCUSDT 1h | %s -> %s UTC | data: %s | run %d | ledger sha256 %s"
        % (_date(res.eval_start), _date(res.eval_end), res.data_source, res.run_id,
           res.ledger_hash[:16]),
    ]
    header = "%-22s %9s %10s %10s %9s %7s %7s %8s" % (
        "Test", "Start", "Final", "P&L $", "P&L %", "Trades", "Win %", "Max DD")
    lines.append(header)
    lines.append("-" * len(header))
    for t in res.tests:
        win = "-" if t.win_rate is None else "%.0f%%" % t.win_rate
        lines.append("%-22s %9s %10s %10s %9s %7d %7s %8s" % (
            t.name, _money(t.start_balance), _money(t.final_balance), _signed(t.pnl),
            _signed(t.pnl_pct, "%"), t.trade_count, win,
            "%.2f%%" % t.max_drawdown_pct))
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        data = DataAgent.from_csv(args.data)
    except (OSError, ValueError) as exc:
        print("error: cannot load %s: %s" % (args.data, exc), file=sys.stderr)
        return 2
    try:
        # Fail fast with a readable message before any simulation work.
        connect(args.db).close()
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    profiles = list(PROFILES) if args.profile == "both" else [args.profile]
    print("Simulated - no real money. Paper fills only.\n")
    for i, name in enumerate(profiles):
        res = simulate(data, name, db=args.db, data_path=args.data)
        if i:
            print()
        print(format_table(res))
    print("\nLedger written to %s" % args.db)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from . import web  # dashboard module (owned by the frontend package)
    except ImportError as exc:
        print("error: dashboard module tradebot.web is not available: %s" % exc, file=sys.stderr)
        return 2
    server = web.make_server(args.db, args.port)
    host, port = server.server_address[:2]
    print("Simulated - no real money. Dashboard on http://%s:%d  (Ctrl+C to stop)"
          % ("localhost" if host in ("", "0.0.0.0", "127.0.0.1") else host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tradebot",
                                 description="Calm paper trader (simulated, no real money).")
    sub = ap.add_subparsers(dest="command")
    sub.required = True

    run = sub.add_parser("run", help="run the 10 tests at base and stress fees")
    run.add_argument("--data", required=True, help="CSV of open_time_ms,open,high,low,close,volume")
    run.add_argument("--db", required=True, help="SQLite ledger path (created if missing)")
    run.add_argument("--profile", choices=["both"] + list(PROFILES), default="both")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="serve the dashboard")
    serve.add_argument("--db", required=True)
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
