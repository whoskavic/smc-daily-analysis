"""
CLI for long-range backtests — Phase 6.

The synchronous `/api/backtest/run` HTTP endpoint sits behind nginx and
isn't built for a request that can take minutes (e.g. a 2-year run); this
script calls the same engine.run_backtest() directly, writes the full
result plus a human-readable summary to backend/reports/, and prints the
summary.

Must be run from the backend/ directory: `.env` (env_file=".env") and the
default SQLite URL (sqlite:///./smc_trading.db) both resolve relative to the
current working directory, not to this file's location.

Usage:
    cd backend
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
from app.services.backtest.signal_simulator import SignalConfig  # noqa: E402
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
    p.add_argument("--fees-pct", type=float, default=0.0004, help='sim_mode="vbt_legacy" only')
    p.add_argument("--slippage-pct", type=float, default=0.0005)
    p.add_argument("--claude-sample-pct", type=float, default=0.0,
                    help="Random fraction of decision bars to cross-check against Claude. "
                         "Mutually exclusive with --claude-sample-max.")
    p.add_argument("--claude-sample-max", type=int, default=0,
                    help="Sample up to N decision bars, evenly spaced and deterministic "
                         "(includes NO_TRADE bars). Mutually exclusive with --claude-sample-pct.")
    p.add_argument("--sim-mode", choices=["event", "vbt_legacy"], default="event")
    p.add_argument("--maker-fee-pct", type=float, default=SimConfig.maker_fee_pct)
    p.add_argument("--taker-fee-pct", type=float, default=SimConfig.taker_fee_pct)
    p.add_argument("--order-ttl-bars", type=int, default=SimConfig.order_ttl_bars)
    p.add_argument("--sizing-mode", choices=["risk_pct", "fixed_risk_usdt", "fixed_margin_usdt"],
                    default=SimConfig.sizing_mode)
    p.add_argument("--risk-pct", type=float, default=None,
                    help="Default: settings.risk_per_trade_pct (live-mirroring)")
    p.add_argument("--fixed-risk-usdt", type=float, default=SimConfig.fixed_risk_usdt)
    p.add_argument("--fixed-margin-usdt", type=float, default=SimConfig.fixed_margin_usdt)
    p.add_argument("--leverage", type=int, default=None,
                    help="Default: settings.max_leverage (live-mirroring)")
    p.add_argument("--min-sl-pct", type=float, default=None,
                    help="Skip setups whose risk distance is below this %% of entry price")
    p.add_argument("--min-sl-atr", type=float, default=None,
                    help="Skip setups whose risk distance is below this multiple of ATR(14)")
    p.add_argument("--decision-schedule", choices=["every_bar", "daily"], default="every_bar",
                    help='"daily" decides once/day at settings.daily_analysis_time, matching live cadence')
    p.add_argument("--no-save", action="store_true", help="Don't persist the run to the BacktestRun table.")
    return p.parse_args(argv)


def _ensure_running_from_backend() -> None:
    cwd = Path.cwd().resolve()
    if cwd != BACKEND_DIR:
        print(
            f"error: run this from the backend/ directory — cwd is {cwd}, expected {BACKEND_DIR}.\n"
            f".env and the SQLite DB URL resolve relative to the current working directory, "
            f"so running from elsewhere silently picks up the wrong config/database.\n"
            f"Fix: cd {BACKEND_DIR} && python -m app.scripts.run_backtest ...",
            file=sys.stderr,
        )
        sys.exit(1)


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
    lines.append(f"- decision_schedule: `{result.get('decision_schedule')}`")
    lines.append(f"- decision_bars: `{result.get('decision_bars', 0)}`")
    if result.get("signal_config"):
        for k, v in result["signal_config"].items():
            lines.append(f"- signal_config.{k}: `{v}`")
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
        lines.append(f"| duplicate_signals | {orders.get('duplicate_signals', 0)} |")
        lines.append(f"| margin_capped_trades | {orders.get('margin_capped_trades', 0)} |")
        lines.append(f"| setup_reused_blocked | {orders.get('setup_reused_blocked', 0)} |")
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

    lines += _render_diagnostics_md(result)
    lines += _render_claude_vs_proxy_md(result)

    if result.get("note"):
        lines += ["## Note", "", result["note"], ""]

    return "\n".join(lines)


def _render_diagnostics_md(result: dict) -> list:
    sl_stats = result.get("sl_pct_stats")
    cost_stats = result.get("cost_r_stats")
    fill_bar_sl = result.get("fill_bar_sl_exits")
    buckets = result.get("r_by_sl_bucket")
    if not (sl_stats or cost_stats or fill_bar_sl or buckets):
        return []

    lines = ["## Diagnostics", ""]
    if sl_stats:
        lines += ["**sl_pct_stats** (% of entry price)", "", "| min | p25 | median | p75 | max |", "|---|---|---|---|---|"]
        lines.append(
            f"| {_fmt(sl_stats['min'], 4)} | {_fmt(sl_stats['p25'], 4)} | {_fmt(sl_stats['median'], 4)} | "
            f"{_fmt(sl_stats['p75'], 4)} | {_fmt(sl_stats['max'], 4)} |"
        )
        lines.append("")
    if cost_stats:
        lines += ["**cost_r_stats** (fees + slippage, in R)", "", "| median | mean |", "|---|---|"]
        lines.append(f"| {_fmt(cost_stats['median'], 4)} | {_fmt(cost_stats['mean'], 4)} |")
        lines.append("")
    lines.append(f"- fill_bar_sl_exits: `{fill_bar_sl or 0}`")
    lines.append("")
    if buckets:
        lines += ["**r_by_sl_bucket**", "", "| Bucket | Count | Expectancy R |", "|---|---|---|"]
        for label, stats in buckets.items():
            lines.append(f"| {label} | {stats['count']} | {_fmt(stats['expectancy_r'], 4)} |")
        lines.append("")
    return lines


def _render_claude_vs_proxy_md(result: dict) -> list:
    samples = [s for s in result.get("claude_sample", []) if "error" not in s]
    if not samples:
        return []

    both_trade = [s for s in samples if s.get("rule_decision") == "TRADE" and s.get("claude_decision") == "TRADE"]
    decision_agree = sum(1 for s in samples if s.get("agree_decision")) / len(samples) * 100.0
    direction_agree = (
        sum(1 for s in both_trade if s.get("agree_direction")) / len(both_trade) * 100.0 if both_trade else None
    )
    claude_trade_count = sum(1 for s in samples if s.get("claude_decision") == "TRADE")

    rule_sl_pcts = [s["rule_sl_pct"] for s in samples if s.get("rule_sl_pct") is not None]
    claude_sl_pcts = [s["claude_sl_pct"] for s in samples if s.get("claude_sl_pct") is not None]

    def _mean(vals):
        return sum(vals) / len(vals) if vals else None

    lines = ["## Claude vs proxy", ""]
    lines.append(f"- samples: `{len(result.get('claude_sample', []))}` (`{len(samples)}` without error)")
    lines.append(f"- decision agreement: `{_fmt(decision_agree)}%`")
    lines.append(
        f"- direction agreement (both TRADE, n={len(both_trade)}): "
        f"`{_fmt(direction_agree) if direction_agree is not None else 'n/a'}%`"
    )
    lines.append(f"- Claude TRADE count: `{claude_trade_count}`")
    lines.append(f"- rule sl_pct mean (n={len(rule_sl_pcts)}): `{_fmt(_mean(rule_sl_pcts), 4)}%`")
    lines.append(f"- Claude sl_pct mean (n={len(claude_sl_pcts)}): `{_fmt(_mean(claude_sl_pcts), 4)}%`")
    lines.append("")

    lines += [
        "| timestamp | rule | claude | agree | rule_sl% | claude_sl% |",
        "|---|---|---|---|---|---|",
    ]
    for s in result.get("claude_sample", []):
        if "error" in s:
            lines.append(f"| {s.get('timestamp')} | error: {s['error']} | | | | |")
            continue
        lines.append(
            f"| {s.get('timestamp')} | {s.get('rule_decision')}/{s.get('rule_direction') or '-'} | "
            f"{s.get('claude_decision')}/{s.get('claude_direction') or '-'} | "
            f"{'yes' if s.get('agree_decision') else 'no'} | "
            f"{_fmt(s.get('rule_sl_pct'), 4) if s.get('rule_sl_pct') is not None else 'n/a'} | "
            f"{_fmt(s.get('claude_sl_pct'), 4) if s.get('claude_sl_pct') is not None else 'n/a'} |"
        )
    lines.append("")
    return lines


def main(argv=None) -> None:
    _ensure_running_from_backend()
    args = _parse_args(argv)

    sim_config = None
    risk_pct = args.risk_pct
    leverage = args.leverage
    if args.sim_mode == "event":
        if risk_pct is None or leverage is None:
            from app.config import settings
            if risk_pct is None:
                risk_pct = getattr(settings, "risk_per_trade_pct", 1.0)
            if leverage is None:
                leverage = getattr(settings, "max_leverage", 10)
        sim_config = SimConfig(
            init_cash=args.init_cash,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_pct=args.taker_fee_pct,
            slippage_pct=args.slippage_pct,
            order_ttl_bars=args.order_ttl_bars,
            sizing_mode=args.sizing_mode,
            risk_pct=risk_pct,
            fixed_risk_usdt=args.fixed_risk_usdt,
            fixed_margin_usdt=args.fixed_margin_usdt,
            leverage=leverage,
        )

    signal_config = SignalConfig(min_sl_pct=args.min_sl_pct, min_sl_atr=args.min_sl_atr)

    result = run_backtest(
        symbol=args.symbol,
        since=args.since,
        until=args.until,
        init_cash=args.init_cash,
        fees_pct=args.fees_pct,
        slippage_pct=args.slippage_pct,
        claude_sample_pct=args.claude_sample_pct,
        claude_sample_max=args.claude_sample_max,
        sim_mode=args.sim_mode,
        sim_config=sim_config,
        signal_config=signal_config,
        decision_schedule=args.decision_schedule,
    )

    until_dt = args.until or datetime.now(timezone.utc)
    json_path, md_path = _report_paths(args.symbol, args.since, until_dt)
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary = _render_summary_md(result, args)
    md_path.write_text(summary, encoding="utf-8")

    if not args.no_save:
        from app.routers.backtest import BacktestRequest, _save_run
        req = BacktestRequest(
            symbol=args.symbol, since=args.since, until=args.until,
            init_cash=args.init_cash, fees_pct=args.fees_pct, slippage_pct=args.slippage_pct,
            claude_sample_pct=args.claude_sample_pct, claude_sample_max=args.claude_sample_max, save=True,
            sim_mode=args.sim_mode, maker_fee_pct=args.maker_fee_pct, taker_fee_pct=args.taker_fee_pct,
            order_ttl_bars=args.order_ttl_bars,
            sizing_mode=args.sizing_mode, risk_pct=risk_pct,
            fixed_risk_usdt=args.fixed_risk_usdt, fixed_margin_usdt=args.fixed_margin_usdt,
            leverage=leverage,
            min_sl_pct=args.min_sl_pct, min_sl_atr=args.min_sl_atr,
            decision_schedule=args.decision_schedule,
        )
        run_id = _save_run(req, result)
        print(f"Saved as BacktestRun id={run_id}")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}\n")
    print(summary)


if __name__ == "__main__":
    main()
