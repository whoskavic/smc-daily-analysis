"""
SMC engine variant experiment runner — Phase 6.

Compares smc_engine.SmcConfig variants (break_mode, ob_max_scan) against
master's defaults (break_mode="close", ob_max_scan=None) on a FIXED
backtest harness, using bootstrap confidence intervals so a difference in
expectancy R isn't mistaken for noise. This script only ever READS the
result of engine.run_backtest() — it cannot change any live default; see
"Criteria for changing the live default" below for what would actually be
required to propose that, in a separate PR.

Fixed harness (identical for every variant AND the baseline — never
overridable per variant within one run; --symbol/--since/--until below
override the harness for the WHOLE run, applied uniformly to every
variant, not per grid cell):
    decision_schedule = "every_bar"
    min_sl_pct        = 0.3
    fees              = MEXC (maker 0.0%, taker 0.02%) — SimConfig()'s own
                        defaults, so the harness sim_config is SimConfig()
                        unmodified
    slippage          = SimConfig()'s default (0.05%)
    symbol            = BTC/USDT
    range             = 2024-09-01 -> 2026-09-01

Why min_sl_pct=0.3 (rationale for the fixed harness, not a claim about live
performance):
    min_sl_pct=0.3 comes from a 50-sample Claude calibration run: Claude
    never chose an SL tighter than 0.27% of entry (p25 0.42%, median
    0.70%), while the rule proxy chose below 0.3% on 10 of 15 TRADE bars
    (median 0.13%). On this harness the baseline gives 594 trades and
    expectancy -0.07R (95% CI -0.23 to +0.10), with 24-96 trades per
    quarterly fold. Daily cadence yields only ~39 trades in two years, far
    too few for fold-level comparison. This harness is for RELATIVE
    comparison between engine variants, NOT a prediction of live
    performance, since live currently decides once per day.

Method:
    1. Grid: break_mode in {close, wick} x ob_max_scan in {None, 10} — 4
       variants, one of which (close, None) IS the baseline (master's
       defaults).
    2. Sensitivity pass: ob_max_scan in {5, 20}, for whichever break_mode
       had the better ob_max_scan=None expectancy R in step 1.
    3. Each variant runs once over the full range (never per-fold — a
       per-fold sub-backtest would need its own warmup lookback and change
       results); the resulting trade list is then bucketed into calendar-
       quarter folds by entry_time for fold-level comparison.
    4. Each non-baseline variant is compared to the baseline via a paired
       (by fold, where both have trades) bootstrap 95% CI of the
       difference in per-fold expectancy R — seeded, deterministic,
       10000 resamples by default.
    5. The single best-performing variant (by overall expectancy R, across
       grid + sensitivity) is re-run once on ETH/USDT, alongside the
       baseline, as a robustness check only — this NEVER feeds back into
       the verdict.

Candles are fetched ONCE per symbol (via engine.run_backtest -> data_loader,
which caches to CandleCache) — the first variant's run populates the cache
for that symbol/range, so every later variant for the same symbol reuses it
instead of re-hitting the exchange. See the report's "design decisions" for
why this (rather than an in-memory prefetch bypassing run_backtest) was the
chosen mechanism.

Criteria for changing the live default (ALL must hold — this script only
reports the verdict; changing a default is a separate PR):
    - better expectancy R, with the 95% CI of the difference excluding 0;
    - better in the majority of (paired) folds;
    - max drawdown not worse by more than 20% relative;
    - >= 30 trades for the variant.

Usage:
    cd backend
    python -m app.scripts.run_experiments \\
        [--symbol BTC/USDT] [--since 2024-09-01] [--until 2026-09-01] \\
        [--out my_run_name] [--skip-eth] [--bootstrap-n 10000] [--seed 20260101]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = BACKEND_DIR / "reports"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.backtest.engine import run_backtest  # noqa: E402
from app.services.backtest.signal_simulator import SignalConfig  # noqa: E402
from app.services.backtest.trade_simulator import SimConfig  # noqa: E402
from app.services.smc_engine import SmcConfig  # noqa: E402

# ── Fixed harness ────────────────────────────────────────────────────────────
HARNESS_DECISION_SCHEDULE = "every_bar"
HARNESS_MIN_SL_PCT = 0.3
HARNESS_SYMBOL = "BTC/USDT"
HARNESS_SINCE = datetime(2024, 9, 1, tzinfo=timezone.utc)
HARNESS_UNTIL = datetime(2026, 9, 1, tzinfo=timezone.utc)
ROBUSTNESS_SYMBOL = "ETH/USDT"

_GRID_BREAK_MODES = ("close", "wick")
_GRID_OB_MAX_SCAN = (None, 10)
_SENSITIVITY_OB_MAX_SCAN = (5, 20)

_CRITERIA_MIN_TRADES = 30
_CRITERIA_MAX_DD_RELATIVE_TOLERANCE = 1.20  # variant_dd <= baseline_dd * this


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare smc_engine SmcConfig variants against master's defaults "
                    "on a fixed backtest harness (see module docstring)."
    )
    p.add_argument("--symbol", default=HARNESS_SYMBOL,
                    help="Applied uniformly to every variant in this run (not per-variant)")
    p.add_argument("--since", type=_parse_date, default=HARNESS_SINCE)
    p.add_argument("--until", type=_parse_date, default=HARNESS_UNTIL)
    p.add_argument("--out", default=None,
                    help="Base filename (no extension) under backend/reports/; auto-generated if omitted")
    p.add_argument("--skip-eth", action="store_true", help="Skip the ETH/USDT robustness check")
    p.add_argument("--bootstrap-n", type=int, default=10000, help="Bootstrap resamples for the CI")
    p.add_argument("--seed", type=int, default=20260101, help="Seed for the bootstrap RNG (determinism)")
    return p.parse_args(argv)


# ─────────────────────────────────────────────────────────────────────────────
# Folds — calendar quarters of [since, until)
# ─────────────────────────────────────────────────────────────────────────────

def quarter_folds(since: datetime, until: datetime) -> List[Dict]:
    """Calendar-quarter fold boundaries within [since, until), clipped at
    both ends (the first/last fold may be a partial quarter). Pure/
    deterministic — no randomness, json.dumps-able ISO strings."""
    q_start_month = ((since.month - 1) // 3) * 3 + 1
    cursor = since.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    folds: List[Dict] = []
    while cursor < until:
        nxt = cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 10 else cursor.replace(month=cursor.month + 3)
        fold_start = max(cursor, since)
        fold_end = min(nxt, until)
        if fold_start < fold_end:
            folds.append({"start": fold_start.isoformat(), "end": fold_end.isoformat()})
        cursor = nxt
    return folds


def bucket_trades_by_fold(trades: List[Dict], folds: List[Dict], time_key: str = "entry_time") -> List[Dict]:
    """Per-fold trade_count and expectancy_r (mean r_multiple); expectancy_r
    is None for a fold with zero trades — that fold can't be paired for the
    bootstrap CI ("paired by fold where possible")."""
    out = []
    for fold in folds:
        in_fold = [t for t in trades if t.get(time_key) is not None and fold["start"] <= t[time_key] < fold["end"]]
        r_values = [t["r_multiple"] for t in in_fold]
        expectancy = (sum(r_values) / len(r_values)) if r_values else None
        out.append({
            "start": fold["start"], "end": fold["end"],
            "trade_count": len(in_fold), "expectancy_r": expectancy,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap CI of the difference in (fold) expectancy R
# ─────────────────────────────────────────────────────────────────────────────

def paired_fold_diffs(variant_folds: List[Dict], baseline_folds: List[Dict]) -> List[float]:
    """variant.expectancy_r - baseline.expectancy_r for every fold where
    BOTH have at least one trade; folds carrying zero trades on either side
    can't be paired and are skipped."""
    diffs = []
    for v, b in zip(variant_folds, baseline_folds):
        if v["expectancy_r"] is not None and b["expectancy_r"] is not None:
            diffs.append(v["expectancy_r"] - b["expectancy_r"])
    return diffs


def bootstrap_ci(diffs: List[float], n_resamples: int, seed: int) -> Dict:
    """Percentile bootstrap 95% CI of the mean of `diffs`. Seeded with
    Python's stdlib random.Random (not numpy) so this is reproducible
    across environments without a numpy-version dependency. Deterministic
    for a fixed (diffs, n_resamples, seed) — same output every call."""
    if not diffs:
        return {"point_estimate": None, "ci_low": None, "ci_high": None, "n_folds": 0, "n_resamples": n_resamples}
    rng = random.Random(seed)
    n = len(diffs)
    point_estimate = sum(diffs) / n
    means = []
    for _ in range(n_resamples):
        resample_sum = 0.0
        for _ in range(n):
            resample_sum += diffs[rng.randrange(n)]
        means.append(resample_sum / n)
    means.sort()
    lo_idx = min(n_resamples - 1, max(0, int(0.025 * n_resamples)))
    hi_idx = min(n_resamples - 1, max(0, int(0.975 * n_resamples)))
    return {
        "point_estimate": point_estimate,
        "ci_low": means[lo_idx],
        "ci_high": means[hi_idx],
        "n_folds": n,
        "n_resamples": n_resamples,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Running one variant
# ─────────────────────────────────────────────────────────────────────────────

def variant_label(cfg: SmcConfig) -> str:
    scan = cfg.ob_max_scan if cfg.ob_max_scan is not None else "none"
    return f"break={cfg.break_mode},ob_max_scan={scan}"


def run_variant(symbol: str, since: datetime, until: datetime, smc_config: SmcConfig) -> Dict:
    """One full-range backtest for one SmcConfig variant, on the fixed
    harness. Candles are fetched by run_backtest -> data_loader, which
    caches to CandleCache — the first call for a given symbol/range hits
    the exchange (or an already-warm cache); every subsequent variant call
    for that same symbol/range reuses the cache, never refetching."""
    return run_backtest(
        symbol=symbol,
        since=since,
        until=until,
        sim_mode="event",
        sim_config=SimConfig(),  # MEXC fees + default slippage are SimConfig's own defaults
        signal_config=SignalConfig(min_sl_pct=HARNESS_MIN_SL_PCT),
        decision_schedule=HARNESS_DECISION_SCHEDULE,
        smc_config=smc_config,
    )


def variant_report(result: Dict, folds: List[Dict]) -> Dict:
    return {
        "smc_config": result.get("smc_config"),
        "expectancy_r": result.get("expectancy_r"),
        "total_trades": result.get("total_trades"),
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "win_rate_pct": result.get("win_rate_pct"),
        "profit_factor": result.get("profit_factor"),
        "fold_stats": bucket_trades_by_fold(result.get("trades", []), folds),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Criteria for changing the live default (section 5) — report only
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_criteria(variant: Dict, baseline: Dict, ci: Dict) -> Dict:
    ve, be = variant["expectancy_r"], baseline["expectancy_r"]
    better_expectancy_r = ve is not None and be is not None and ve > be
    ci_excludes_zero = (
        ci["ci_low"] is not None and ci["ci_high"] is not None
        and (ci["ci_low"] > 0 or ci["ci_high"] < 0)
    )

    paired = [
        (v["expectancy_r"], b["expectancy_r"])
        for v, b in zip(variant["fold_stats"], baseline["fold_stats"])
        if v["expectancy_r"] is not None and b["expectancy_r"] is not None
    ]
    folds_compared = len(paired)
    folds_better = sum(1 for v_e, b_e in paired if v_e > b_e)
    majority_folds_better = folds_compared > 0 and folds_better > folds_compared / 2

    v_dd, b_dd = variant["max_drawdown_pct"], baseline["max_drawdown_pct"]
    drawdown_ok = (
        v_dd is not None and b_dd is not None
        and (b_dd == 0 or v_dd <= b_dd * _CRITERIA_MAX_DD_RELATIVE_TOLERANCE)
    )

    enough_trades = (variant["total_trades"] or 0) >= _CRITERIA_MIN_TRADES

    criteria = {
        "better_expectancy_r": better_expectancy_r,
        "ci_excludes_zero": ci_excludes_zero,
        "majority_folds_better": majority_folds_better,
        "drawdown_not_worse_than_20pct_relative": drawdown_ok,
        "at_least_30_trades": enough_trades,
    }
    return {
        **criteria,
        "folds_better": folds_better,
        "folds_compared": folds_compared,
        "verdict_change_default": all(criteria.values()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report rendering
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(value, digits=4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_summary_md(
    symbol: str, since: datetime, until: datetime,
    baseline_label: str, variants: Dict[str, Dict], verdicts: Dict[str, Dict],
    best_label: str, eth_section: Optional[Dict],
) -> str:
    lines = [f"# SMC engine variant experiments — {symbol} ({since.date()} -> {until.date()})", ""]
    lines += [
        "Fixed harness: decision_schedule=every_bar, min_sl_pct=0.3, MEXC fees "
        "(maker 0.0%, taker 0.02%), default slippage. RELATIVE comparison only — "
        "not a prediction of live performance (live decides once per day).", "",
    ]

    lines += ["## Variants", "", "| Variant | expectancy_R | trades | max_dd% | win_rate% | profit_factor |",
              "|---|---|---|---|---|---|"]
    for label, v in variants.items():
        tag = f"{label} (baseline)" if label == baseline_label else label
        lines.append(
            f"| {tag} | {_fmt(v['expectancy_r'])} | {v['total_trades']} | "
            f"{_fmt(v['max_drawdown_pct'], 2)} | {_fmt(v['win_rate_pct'], 2)} | {_fmt(v['profit_factor'], 3)} |"
        )
    lines.append("")

    lines += ["## Verdict vs baseline", "",
              "| Variant | diff (point est.) | 95% CI | better_R | CI excl. 0 | majority folds | dd ok | >=30 trades | CHANGE DEFAULT |",
              "|---|---|---|---|---|---|---|---|---|"]
    for label, v in verdicts.items():
        ci, c = v["ci"], v["criteria"]
        ci_str = f"[{_fmt(ci['ci_low'])}, {_fmt(ci['ci_high'])}]" if ci["ci_low"] is not None else "n/a"
        lines.append(
            f"| {label} | {_fmt(ci['point_estimate'])} | {ci_str} | "
            f"{_fmt(c['better_expectancy_r'])} | {_fmt(c['ci_excludes_zero'])} | "
            f"{c['folds_better']}/{c['folds_compared']} | {_fmt(c['drawdown_not_worse_than_20pct_relative'])} | "
            f"{_fmt(c['at_least_30_trades'])} | **{_fmt(c['verdict_change_default'])}** |"
        )
    lines.append("")
    lines.append(f"Best-performing variant overall (by expectancy R): `{best_label}`")
    lines.append("")

    if eth_section:
        lines += ["## ETH/USDT robustness check (informational only — does not enter the decision)", ""]
        lines.append(f"- baseline: expectancy_r=`{_fmt(eth_section['baseline']['expectancy_r'])}` "
                      f"trades=`{eth_section['baseline']['total_trades']}`")
        lines.append(f"- {eth_section['best_variant']['label']}: expectancy_r=`{_fmt(eth_section['best_variant']['expectancy_r'])}` "
                      f"trades=`{eth_section['best_variant']['total_trades']}`")
        lines.append("")

    lines.append(
        "This report does not change any live default. See "
        "\"Criteria for changing the live default\" in this script's module docstring."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _report_paths(out: Optional[str], symbol: str) -> Tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if out:
        base = out
    else:
        safe_symbol = symbol.replace("/", "-")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"experiments_{safe_symbol}_{stamp}"
    return REPORTS_DIR / f"{base}.json", REPORTS_DIR / f"{base}.md"


def main(argv=None) -> None:
    args = _parse_args(argv)
    since, until = args.since, args.until
    folds = quarter_folds(since, until)

    baseline_config = SmcConfig()  # break_mode="close", ob_max_scan=None — master's defaults
    grid_configs = [
        SmcConfig(break_mode=bm, ob_max_scan=scan)
        for bm in _GRID_BREAK_MODES for scan in _GRID_OB_MAX_SCAN
    ]

    print(f"[run_experiments] {args.symbol} {since.date()} -> {until.date()}, "
          f"{len(folds)} quarterly folds, {len(grid_configs)} grid variants")

    configs: Dict[str, SmcConfig] = {}
    variants: Dict[str, Dict] = {}
    for cfg in grid_configs:
        label = variant_label(cfg)
        print(f"[run_experiments] running {label} ...")
        result = run_variant(args.symbol, since, until, cfg)
        configs[label] = cfg
        variants[label] = variant_report(result, folds)

    baseline_label = variant_label(baseline_config)
    baseline_report = variants[baseline_label]

    close_primary = variants[variant_label(SmcConfig(break_mode="close", ob_max_scan=None))]
    wick_primary = variants[variant_label(SmcConfig(break_mode="wick", ob_max_scan=None))]
    close_r = close_primary["expectancy_r"] if close_primary["expectancy_r"] is not None else float("-inf")
    wick_r = wick_primary["expectancy_r"] if wick_primary["expectancy_r"] is not None else float("-inf")
    winning_break_mode = "wick" if wick_r > close_r else "close"
    print(f"[run_experiments] better-performing break_mode at ob_max_scan=None: {winning_break_mode}")

    for scan in _SENSITIVITY_OB_MAX_SCAN:
        cfg = SmcConfig(break_mode=winning_break_mode, ob_max_scan=scan)
        label = variant_label(cfg)
        print(f"[run_experiments] running sensitivity variant {label} ...")
        result = run_variant(args.symbol, since, until, cfg)
        configs[label] = cfg
        variants[label] = variant_report(result, folds)

    verdicts: Dict[str, Dict] = {}
    for label, report in variants.items():
        if label == baseline_label:
            continue
        diffs = paired_fold_diffs(report["fold_stats"], baseline_report["fold_stats"])
        ci = bootstrap_ci(diffs, args.bootstrap_n, args.seed)
        verdicts[label] = {"ci": ci, "criteria": evaluate_criteria(report, baseline_report, ci)}

    best_label = max(
        variants,
        key=lambda k: variants[k]["expectancy_r"] if variants[k]["expectancy_r"] is not None else float("-inf"),
    )

    eth_section = None
    if not args.skip_eth:
        print(f"[run_experiments] ETH/USDT robustness check (baseline + {best_label}) ...")
        eth_baseline = run_variant(ROBUSTNESS_SYMBOL, since, until, baseline_config)
        eth_best = run_variant(ROBUSTNESS_SYMBOL, since, until, configs[best_label])
        eth_section = {
            "baseline": {"expectancy_r": eth_baseline.get("expectancy_r"), "total_trades": eth_baseline.get("total_trades")},
            "best_variant": {
                "label": best_label,
                "expectancy_r": eth_best.get("expectancy_r"),
                "total_trades": eth_best.get("total_trades"),
            },
            "note": "Robustness check only — does not enter the decision.",
        }

    report = {
        "symbol": args.symbol, "since": since.isoformat(), "until": until.isoformat(),
        "folds": folds,
        "baseline_label": baseline_label,
        "variants": variants,
        "verdicts": verdicts,
        "best_label": best_label,
        "eth_robustness_check": eth_section,
        "bootstrap_n": args.bootstrap_n, "seed": args.seed,
    }

    json_path, md_path = _report_paths(args.out, args.symbol)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = render_summary_md(
        args.symbol, since, until, baseline_label, variants, verdicts, best_label, eth_section,
    )
    md_path.write_text(summary, encoding="utf-8")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}\n")
    print(summary)


if __name__ == "__main__":
    main()
