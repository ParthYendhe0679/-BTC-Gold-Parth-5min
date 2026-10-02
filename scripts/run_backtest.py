"""
run_backtest.py — Backtest every configured strategy on historical CSVs and
(optionally) run a bounded, reproducible parameter search.

Usage
  python scripts/run_backtest.py data/binance_BTCUSDT_5m.csv --symbol BTC/USD --cost 0.10
  python scripts/run_backtest.py data/twelvedata_XAUUSD_5m.csv --symbol XAU/USD --cost 0.03 --optimize

--cost is the ROUND-TRIP cost in percent of price (fees + spread + slippage).
Outputs reports/backtest_<symbol>.json and prints a summary table.

Methodology
  • chronological split: 60 % train / 20 % validation / 20 % test (untouched)
  • baseline = default parameters, reported on every split
  • --optimize: small fixed grid per new strategy (≤ 12 combos). A combo must
    have >= MIN_TRADES trades and positive net expectancy on TRAIN; the winner
    maximises VALIDATION t-stat. The test split is reported once for baseline
    and winner — it is never used for selection. Winners are NOT applied to the
    live config automatically.
  • cost sensitivity: 0×, 1×, 2× the stated cost on the full sample
"""

import argparse
import itertools
import json
import os
import sys
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import metrics, regime_split, simulate, split_bounds, trades_in  # noqa: E402
from app.candles import validate_candles  # noqa: E402
from app.config import settings  # noqa: E402
from app.strategies import MarketContext, build_strategy  # noqa: E402

MIN_TRADES = 30

GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "FIB_GOLDEN_ZONE": {"min_impulse_atr": [2.0, 3.0, 4.0], "pivot_left": [3, 5], "min_rr": [1.0, 1.5]},
    "POS_5EMA": {"target_r": [[1.0, 2.0], [2.0, 3.0], [3.0, 4.0]], "trend_filter": [False, True],
                 "min_risk_atr": [0.25, 0.5]},
    "RSI2_MEAN_REVERSION": {"oversold": [5.0, 10.0], "overbought": [90.0, 95.0],
                            "stop_atr": [1.0, 1.5, 2.5]},
    "LIQUIDITY_SWEEP": {"min_wick_ratio": [0.0, 0.3, 0.5], "target_r": [[1.0, 2.0], [1.5, 3.0]],
                        "trend_filter": [False, True]},
}


def load(path: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    rows = raw.to_dict("records")
    df, rep = validate_candles(rows, pd.Timedelta(minutes=5), pd.Timestamp.now(tz="UTC").to_pydatetime())
    print(f"Loaded {path}: {rep.closed} bars ({df['datetime'].min()} -> {df['datetime'].max()}), "
          f"rejected malformed={rep.malformed} inconsistent={rep.inconsistent} dup={rep.duplicates}, gaps={rep.gaps}")
    return df


def evaluate(ctx: MarketContext, cfg: Dict[str, Any], cost: float) -> Dict[str, Any]:
    strat = build_strategy(cfg)
    sigs = strat.generate(ctx)
    trades = simulate(ctx, strat, cost, signals=sigs)
    splits = split_bounds(len(ctx))
    res = {"params": strat.p, "full": metrics(trades),
           "splits": {k: metrics(trades_in(trades, ctx, sl)) for k, sl in splits.items()},
           "cost_sensitivity": {f"{m}x": metrics(simulate(ctx, strat, cost * m, signals=sigs))
                                for m in (0, 2)},
           "regime": regime_split(ctx, trades)}
    return res


def optimise(ctx: MarketContext, cfg: Dict[str, Any], cost: float) -> Dict[str, Any]:
    grid = GRIDS.get(cfg["id"])
    if not grid:
        return {}
    keys = list(grid)
    splits = split_bounds(len(ctx))
    tried = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = {**cfg.get("params", {}), **dict(zip(keys, combo))}
        strat = build_strategy({**cfg, "params": params})
        trades = simulate(ctx, strat, cost)
        tr = metrics(trades_in(trades, ctx, splits["train"]))
        va = metrics(trades_in(trades, ctx, splits["validation"]))
        te = metrics(trades_in(trades, ctx, splits["test"]))
        tried.append({"params": dict(zip(keys, combo)), "train": tr, "validation": va, "test": te})
    eligible = [t for t in tried if t["train"].get("trades", 0) >= MIN_TRADES
                and t["train"].get("expectancy_pct", -1) > 0
                and t["validation"].get("trades", 0) >= MIN_TRADES // 3
                and t["validation"].get("t_stat") is not None]
    best = max(eligible, key=lambda t: t["validation"]["t_stat"]) if eligible else None
    return {"grid": grid, "combos": len(tried), "eligible": len(eligible),
            "selected": best, "rejected": [t for t in tried if t is not best]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--cost", type=float, required=True, help="round-trip cost in %% of price")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    df = load(a.csv)
    ctx = MarketContext(a.symbol, df)
    report: Dict[str, Any] = {"symbol": a.symbol, "source": a.csv, "label": a.label, "cost_round_trip_pct": a.cost,
                              "bars": len(df), "start": str(df["datetime"].min()), "end": str(df["datetime"].max()),
                              "splits": {k: [str(ctx.t[v.start]), str(ctx.t[v.stop - 1])]
                                         for k, v in split_bounds(len(ctx)).items()},
                              "strategies": {}}
    hdr = f"{'strategy':22} {'trades':>6} {'win%':>6} {'exp%':>8} {'PF':>5} {'net%':>8} {'maxDD%':>7} | {'TEST n':>6} {'exp%':>8} {'PF':>5} | {'exp% 0x':>8} {'exp% 2x':>8}"
    print(hdr)
    for cfg in settings.STRATEGIES:
        res = evaluate(ctx, cfg, a.cost)
        if a.optimize:
            res["optimisation"] = optimise(ctx, cfg, a.cost)
        report["strategies"][cfg["id"]] = res
        f, t = res["full"], res["splits"]["test"]
        c0, c2 = res["cost_sensitivity"]["0x"], res["cost_sensitivity"]["2x"]
        print(f"{cfg['id']:22} {f.get('trades', 0):>6} {f.get('win_rate_pct', 0):>6} {f.get('expectancy_pct', 0):>8} "
              f"{str(f.get('profit_factor')):>5} {f.get('net_return_pct', 0):>8} {f.get('max_drawdown_pct', 0):>7} | "
              f"{t.get('trades', 0):>6} {t.get('expectancy_pct', 0):>8} {str(t.get('profit_factor')):>5} | "
              f"{c0.get('expectancy_pct', 0):>8} {c2.get('expectancy_pct', 0):>8}")
        if a.optimize and res.get("optimisation", {}).get("selected"):
            s = res["optimisation"]["selected"]
            print(f"   -> optimised {s['params']}: val t={s['validation']['t_stat']} "
                  f"test n={s['test'].get('trades', 0)} exp={s['test'].get('expectancy_pct')} PF={s['test'].get('profit_factor')}")
        elif a.optimize and cfg["id"] in GRIDS:
            print("   -> optimisation: no combination passed the train/validation gates")
    os.makedirs("reports", exist_ok=True)
    out = f"reports/backtest_{a.label or a.symbol.replace('/', '')}.json"
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print("Saved", out)


if __name__ == "__main__":
    main()
