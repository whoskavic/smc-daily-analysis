# Backtest reports

Output of `python -m app.scripts.run_backtest` (see that module's docstring
for CLI flags). Long-range runs (e.g. multi-year) are meant to be run from
here rather than through the synchronous `/api/backtest/run` HTTP endpoint,
which sits behind nginx and isn't built for requests that take minutes.

Each run writes two files, named `backtest_<SYMBOL>_<since>_<until>_<utc-timestamp>.json/.md`,
e.g.:

```
backtest_BTC-USDT_2024-09-01_2026-09-01_20260922T101500Z.json   # full result (same shape as the API response)
backtest_BTC-USDT_2024-09-01_2026-09-01_20260922T101500Z.md     # human-readable summary
```

Everything in this folder except this file is generated output and is
git-ignored — do not rely on it being committed. When a run's result is
worth keeping as a deliverable (e.g. to back a strategy decision), copy the
relevant `.json`/`.md` pair somewhere it's meant to be committed instead of
relying on this folder.
