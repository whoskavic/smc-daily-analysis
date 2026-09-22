"""
CLI for long-range backtests — Phase 6.

The synchronous `/api/backtest/run` HTTP endpoint sits behind nginx and
isn't built for a request that can take minutes (e.g. a 2-year run); this
script calls the same engine.run_backtest() directly, writes the full
result plus a human-readable summary to backend/reports/, and prints the
summary.

Usage (from anywhere — output paths always resolve relative to backend/):
    python -m app.scripts.run_backtest --symbol BTC/USDT \\
        --since 2024-09-01 --until 2026-09-01 \\
        [--sim-mode event] [--sizing-mode risk_pct --risk-pct 1.0] \\
        [--order-ttl-bars 16] [--no-save]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = BACKEND_DIR / "reports"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.backtest.engine import run_backtest  # noqa: E402
from app.services.backtest.trade_simulator import SimConfig  # noqa: E402


def _parse_date(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a backtest and write a report to backend/reports/.")
    p.add_argument("--symbol", required=True, help='e.g. "BTC/USDT"')
    p.add_argument("--since", required=True, type=_parse_date, help="e.g. 2024-09-01")
    p.add_argument("--until", type=_parse_date, default=None, help="e.g. 2026-09-01 (default: now)")
    p.add_argument("--init-cash", type=float, default=1000.0)
    p.add_argument("--fees-pct", type=float, default=0.0004)
    p.add_argument("--slippage-pct", type=float, default=0.0005)
    p.add_argument("--claude-sample-pct", type=float, default=0.0)
    p.add_argument("--sim-mode", choices=["event", "vbt_legacy"], default="event")
    p.add_argument("--order-ttl-bars", type=int, default=SimConfig.order_ttl_bars)
    p.add_argument("--sizing-mode", choices=["risk_pct", "fixed_risk_usdt", "fixed_margin_usdt"],
                    default=SimConfig.sizing_mode)
    p.add_argument("--risk-pct", type=float, default=SimConfig.risk_pct)
    p.add_argument("--fixed-risk-usdt", type=float, default=SimConfig.fixed_risk_usdt)
    p.add_argument("--fixed-margin-usdt", type=float, default=SimConfig.fixed_margin_usdt)
    p.add_argument("--leverage", type=int, default=SimConfig.leverage)
    p.add_argument("--no-save", action="store_true", help="Don't persist the run to the BacktestRun table.")
    return p.parse_args(argv)


def _report_paths(symbol: str, since: datetime, until: datetime) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.replace("/", "-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"backtest_{safe_symbol}_{since.date()}_{until.date()}_{stamp}"
    return REPORTS_DIR / f"{base}.json", REPORTS_DIR / f"{base}.md"


def _fmt(value, digits=2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_summary_md(result: dict, args: argparse.Namespace) -> str:
    lines = [f"# Backtest — {result['symbol']} ({result['since']} → {result['until']})", ""]

    lines += ["## Config", ""]
    lines += [f"- sim_mode: `{result.get('sim_mode')}`"]
    if result.get("sim_config"):
        for k, v in result["sim_config"].items():
            lines.append(f"- {k}: `{v}`")
    lines.append("")

    lines += ["## Metrics", "", "| Metric | Value |", "|---|---|"]
    metric_keys = [
        "bars_analyzed", "signals_generated", "total_trades", "final_equity",
        "total_return_pct", "win_rate_pct", "expectancy_r", "avg_win_r", "avg_loss_r",
        "profit_factor", "sharpe_ratio", "max_drawdown_pct",
    ]
    for k in metric_keys:
        lines.append(f"| {k} | {_fmt(result.get(k))} |")
    lines.append("")

    orders = result.get("orders")
    if orders:
        lines += ["## Order summary", "", "| Field | Value |", "|---|---|"]
        lines.append(f"| placed | {orders['placed']} |")
        lines.append(f"| filled | {orders['filled']} |")
        lines.append(f"| fill_rate_pct | {_fmt(orders['fill_rate_pct'])} |")
        for reason, count in orders["cancelled"].items():
            lines.append(f"| cancelled.{reason} | {count} |")
        lines.append(f"| ignored_in_position | {orders['ignored_in_position']} |")
        lines.append("")

    trades = result.get("trades", [])
    if trades:
        outcome_counts: dict = {}
        for t in trades:
            outcome_counts[t["outcome"]] = outcome_counts.get(t["outcome"], 0) + 1
        lines += ["## Outcome breakdown", "", "| Outcome | Count |", "|---|---|"]
        for outcome, count in sorted(outcome_counts.items()):
            lines.append(f"| {outcome} | {count} |")
        lines.append("")

    if result.get("note"):
        lines += ["## Note", "", result["note"], ""]

    return "\n".join(lines)


def main(argv=None) -> None:
    args = _parse_args(argv)

    sim_config = None
    if args.sim_mode == "event":
        sim_config = SimConfig(
            init_cash=args.init_cash,
            fees_pct=args.fees_pct,
            slippage_pct=args.slippage_pct,
            order_ttl_bars=args.order_ttl_bars,
            sizing_mode=args.sizing_mode,
            risk_pct=args.risk_pct,
            fixed_risk_usdt=args.fixed_risk_usdt,
            fixed_margin_usdt=args.fixed_margin_usdt,
            leverage=args.leverage,
        )

    result = run_backtest(
        symbol=args.symbol,
        since=args.since,
        until=args.until,
        init_cash=args.init_cash,
        fees_pct=args.fees_pct,
        slippage_pct=args.slippage_pct,
        claude_sample_pct=args.claude_sample_pct,
        sim_mode=args.sim_mode,
        sim_config=sim_config,
    )

    until_dt = args.until or datetime.now(timezone.utc)
    json_path, md_path = _report_paths(args.symbol, args.since, until_dt)
    json_path.write_text(json.dumps(result, indent=2))
    summary = _render_summary_md(result, args)
    md_path.write_text(summary)

    if not args.no_save:
        from app.routers.backtest import BacktestRequest, _save_run
        req = BacktestRequest(
            symbol=args.symbol, since=args.since, until=args.until,
            init_cash=args.init_cash, fees_pct=args.fees_pct, slippage_pct=args.slippage_pct,
            claude_sample_pct=args.claude_sample_pct, save=True,
            sim_mode=args.sim_mode, order_ttl_bars=args.order_ttl_bars,
            sizing_mode=args.sizing_mode, risk_pct=args.risk_pct,
            fixed_risk_usdt=args.fixed_risk_usdt, fixed_margin_usdt=args.fixed_margin_usdt,
            leverage=args.leverage,
        )
        run_id = _save_run(req, result)
        print(f"Saved as BacktestRun id={run_id}")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}\n")
    print(summary)


if __name__ == "__main__":
    main()
