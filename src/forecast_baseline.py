"""Baseline forecasting backtest for weekly product-group demand.

Purpose
-------
Establish how accurate DUMB forecasts are before anyone reaches for a clever
model. If a 4-week moving average already hits the owner's 10-15% target on the
groups that matter, a seasonal/ML model has to beat that to justify itself --
and on 39 weeks of history it very likely cannot.

Everything here is deliberately untuned: no parameter search, no feature
engineering, no seasonality. Plain Python + pandas, no new dependencies.

Design decisions that materially affect the numbers
---------------------------------------------------
1. Incomplete weeks are dropped. The export ends mid-week, so the final
   week_start holds one day and ~1/5 of normal volume. Scoring against it would
   make every model look broken. weekly_demand.csv carries is_complete_week.

2. Missing (group, week) pairs are filled with 0, not skipped. weekly_demand
   only has rows where something sold, so a zero-demand week is an ABSENT row.
   Leaving it absent would shorten the series, silently shift the moving-average
   windows onto non-adjacent weeks, and hide intermittency.

3. Rolling-origin, expanding window. At each origin the model sees weeks 1..o
   and forecasts o+1..o+4. Origins are chosen so all four forecast weeks land
   inside the 8-week holdout.

4. WAPE, not MAPE. MAPE divides by the actual, so a week with 3 metres of demand
   dominates a week with 3,000. WAPE weights by volume, which is what a
   purchasing decision cares about.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CLEAN = ROOT / "data" / "clean"
REFERENCE = ROOT / "data" / "reference"
FORECAST = ROOT / "data" / "forecast"

FILE_WEEKLY = CLEAN / "weekly_demand.csv"
FILE_GROUPS = REFERENCE / "product_groups.csv"

OUTPUT_ENCODING = "utf-8-sig"

HOLDOUT_WEEKS = 8   # size of the test period
HORIZON = 4         # weeks forecast from each origin

# Fixed smoothing constant. NOT fitted -- fitting alpha per group would be
# tuning, which this baseline deliberately avoids. 0.3 is the conventional
# default: responsive enough to follow a trend, damped enough to ignore a spike.
SES_ALPHA = 0.3

# The comparison holdout: the last 8 complete weeks that finish on or before
# this date. Deliberately chosen to sit in the stable Apr-Jun stretch, BEFORE
# the Jul-Aug decline, so the same models can be scored under normal conditions.
# This is the one hard-coded analytical choice in the file; everything else is
# derived from the data.
EARLIER_HOLDOUT_ENDS_BY = "2026-06-30"

# A group is called intermittent if this share of its weeks had zero demand.
# Below it, a mean-based model is reasonable; above it, the series is mostly
# zeros and WAPE becomes unstable regardless of model.
INTERMITTENT_THRESHOLD = 0.25


# --------------------------------------------------------------------------
# Data preparation
# --------------------------------------------------------------------------


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load weekly demand as a complete group x week grid of quantities.

    Returns (panel, groups) where panel is one row per (sku_prefix, week_start)
    with no gaps -- absent weeks materialised as qty 0.
    """
    if not FILE_WEEKLY.exists():
        raise FileNotFoundError(
            f"{FILE_WEEKLY} not found. Run src/parse_express.py first."
        )
    w = pd.read_csv(FILE_WEEKLY, dtype={"sku_prefix": str}, encoding="utf-8-sig")
    groups = pd.read_csv(FILE_GROUPS, dtype=str, encoding="utf-8-sig")
    groups["sku_prefix"] = groups["sku_prefix"].str.strip().str.zfill(2)

    if "forecast_scope" not in groups.columns:
        raise ValueError(
            "product_groups.csv has no forecast_scope column -- re-run the parser "
            "after adding it, or the backtest will silently cover the wrong groups."
        )

    # Scope and completeness filters, in that order.
    w = w[w["forecast_scope"] & w["is_complete_week"]].copy()
    if w.empty:
        raise ValueError("No in-scope complete weeks found in weekly_demand.csv.")

    # Materialise the full grid. Reindexing on the cross product is what turns
    # "no row" into "zero demand"; without it the week index is not contiguous
    # and every window-based model quietly averages the wrong weeks.
    weeks = sorted(w["week_start"].unique())
    prefixes = sorted(w["sku_prefix"].unique())
    grid = pd.MultiIndex.from_product([prefixes, weeks], names=["sku_prefix", "week_start"])

    panel = (
        w.groupby(["sku_prefix", "week_start"])["qty"].sum()
        .reindex(grid, fill_value=0.0)
        .reset_index()
    )

    meta = groups.set_index("sku_prefix")
    panel["group_name"] = panel["sku_prefix"].map(meta["group_name"])
    panel["unit"] = panel["sku_prefix"].map(meta["main_unit"])
    panel["week_index"] = panel.groupby("sku_prefix").cumcount()
    return panel, groups


def zero_week_profile(panel: pd.DataFrame) -> pd.DataFrame:
    """Share of weeks with no demand at all, per group.

    Intermittency is the single best predictor of whether these baselines will
    work. A group selling in 95% of weeks is a smoothing problem; a group
    selling in 40% is a different problem entirely (Croston-style intermittent
    demand), and no amount of moving-average tuning will fix it.
    """
    out = (
        panel.groupby(["sku_prefix", "group_name"])
        .agg(
            n_weeks=("qty", "size"),
            n_zero_weeks=("qty", lambda s: int((s == 0).sum())),
            mean_qty=("qty", "mean"),
            median_qty=("qty", "median"),
            max_qty=("qty", "max"),
        )
        .reset_index()
    )
    out["pct_zero_weeks"] = (out["n_zero_weeks"] / out["n_weeks"] * 100).round(1)
    # Coefficient of variation: how spiky the series is, independent of scale.
    cv = panel.groupby("sku_prefix")["qty"].agg(lambda s: s.std() / s.mean() if s.mean() else 0)
    out["cv"] = out["sku_prefix"].map(cv).round(2)
    out["is_intermittent"] = out["pct_zero_weeks"] >= INTERMITTENT_THRESHOLD * 100
    return out.sort_values("pct_zero_weeks", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
#
# Every model has the same contract: given the observed history up to and
# including the origin, return a single number used for all HORIZON weeks
# ahead. None of them extrapolate a trend -- that is the point of a baseline.


def f_naive(history: pd.Series) -> float:
    """Last observed week, repeated."""
    return float(history.iloc[-1])


def f_ma(history: pd.Series, window: int) -> float:
    """Mean of the last `window` weeks.

    If the history is shorter than the window, average what exists rather than
    returning NaN -- at the earliest origins this only affects the long window.
    """
    return float(history.iloc[-window:].mean())


def f_ses(history: pd.Series, alpha: float = SES_ALPHA) -> float:
    """Simple exponential smoothing, level only.

    Implemented directly rather than via statsmodels to avoid a dependency for
    what is three lines of arithmetic. Initialised at the first observation,
    which is standard and matters little over 30+ weeks.
    """
    level = float(history.iloc[0])
    for y in history.iloc[1:]:
        level = alpha * float(y) + (1 - alpha) * level
    return level


MODELS = {
    "naive": f_naive,
    "ma4": lambda h: f_ma(h, 4),
    "ma8": lambda h: f_ma(h, 8),
    f"ses_a{SES_ALPHA}": f_ses,
}


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------


def backtest(panel: pd.DataFrame, holdout_end: int | None = None,
             scenario: str = "recent") -> pd.DataFrame:
    """Rolling-origin backtest over a holdout period ending at `holdout_end`.

    Origin `o` means "weeks 0..o are known". We forecast o+1..o+HORIZON. Origins
    run from the last week before the holdout up to the point where a full
    HORIZON still fits inside it, so every forecast is scored against a real
    actual and no window is padded.

    `holdout_end` is parameterised so the identical procedure can be pointed at
    an earlier, non-declining stretch of history. Accuracy measured over a
    period when demand was stepping down says more about the period than about
    the model, so the comparison is the only way to separate the two.
    """
    n_weeks = panel["week_index"].max() + 1
    if holdout_end is None:
        holdout_end = n_weeks - 1
    if holdout_end - HOLDOUT_WEEKS - HORIZON < 0:
        raise ValueError(
            f"holdout ending at week {holdout_end} leaves too little history; "
            f"need at least {HOLDOUT_WEEKS + HORIZON} weeks before it."
        )

    first_origin = holdout_end - HOLDOUT_WEEKS
    last_origin = holdout_end - HORIZON
    origins = list(range(first_origin, last_origin + 1))

    rows = []
    for prefix, g in panel.groupby("sku_prefix"):
        g = g.sort_values("week_index").reset_index(drop=True)
        qty = g["qty"]
        group_name = g["group_name"].iloc[0]

        for o in origins:
            history = qty.iloc[: o + 1]
            origin_week = g["week_start"].iloc[o]
            for model_name, fn in MODELS.items():
                fc = fn(history)
                for h in range(1, HORIZON + 1):
                    t = o + h
                    rows.append(
                        {
                            "scenario": scenario,
                            "sku_prefix": prefix,
                            "group_name": group_name,
                            "model": model_name,
                            "origin_week": origin_week,
                            "horizon": h,
                            "target_week": g["week_start"].iloc[t],
                            "actual": round(float(qty.iloc[t]), 2),
                            "forecast": round(float(fc), 2),
                        }
                    )
    out = pd.DataFrame(rows)
    out["abs_error"] = (out["actual"] - out["forecast"]).abs().round(2)
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def wape(actual: pd.Series, forecast: pd.Series) -> float:
    """Weighted absolute percentage error: sum|a-f| / sum(a).

    Returns NaN when the denominator is zero -- a group with no demand at all in
    the window has no meaningful percentage error, and returning 0 or inf would
    both be lies that propagate into the summary.
    """
    denom = actual.sum()
    if denom == 0:
        return float("nan")
    return float((actual - forecast).abs().sum() / denom)


def summarise(results: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """WAPE per group per model, at weekly and 4-week-total level.

    The two levels answer different questions:

      weekly      - can we predict an individual week? Punishes getting the
                    right total in the wrong week.
      4wk total   - can we predict what to ORDER? Over- and under-shoots inside
                    the window cancel, which is legitimate: stock ordered for
                    week 2 still covers demand that lands in week 3.

    The owner orders against a 4-week window, so the total-level number is the
    one to judge against the 10-15% target. The weekly number is diagnostic.
    """
    weekly = (
        results.groupby(["sku_prefix", "group_name", "model"])
        .apply(lambda d: wape(d["actual"], d["forecast"]), include_groups=False)
        .rename("wape_weekly")
        .reset_index()
    )

    # Collapse each origin's 4 weeks into one window total, then score.
    windows = (
        results.groupby(["sku_prefix", "group_name", "model", "origin_week"])
        .agg(actual=("actual", "sum"), forecast=("forecast", "sum"))
        .reset_index()
    )
    total = (
        windows.groupby(["sku_prefix", "group_name", "model"])
        .apply(lambda d: wape(d["actual"], d["forecast"]), include_groups=False)
        .rename("wape_4wk_total")
        .reset_index()
    )

    # True demand over the holdout, taken from the panel rather than from
    # `results`. In `results` each holdout week appears a DIFFERENT number of
    # times (the first test week is only ever forecast at horizon 1, the middle
    # ones at all four), so summing there and dividing by HORIZON undercounts by
    # a group-specific factor. Only the panel gives the real quantity.
    hold_weeks = sorted(results["target_week"].unique())
    vol = (
        panel[panel["week_start"].isin(hold_weeks)]
        .groupby("sku_prefix")["qty"].sum()
    )

    # Signed bias on the 4-week windows: negative means the model forecast MORE
    # than sold. Separates "noisy but centred" from "systematically wrong",
    # which WAPE alone cannot show.
    bias = (
        windows.groupby(["sku_prefix", "group_name", "model"])
        .apply(
            lambda d: (d["actual"].sum() - d["forecast"].sum()) / d["actual"].sum()
            if d["actual"].sum() else float("nan"),
            include_groups=False,
        )
        .rename("bias_4wk")
        .reset_index()
    )

    out = weekly.merge(total, on=["sku_prefix", "group_name", "model"])
    out = out.merge(bias, on=["sku_prefix", "group_name", "model"])
    out["holdout_qty"] = out["sku_prefix"].map(vol).round(0)
    out.insert(0, "scenario", results["scenario"].iloc[0])
    for c in ("wape_weekly", "wape_4wk_total", "bias_4wk"):
        out[c] = (out[c] * 100).round(1)
    return out.sort_values(["sku_prefix", "wape_4wk_total"]).reset_index(drop=True)


def best_per_group(summary: pd.DataFrame) -> pd.DataFrame:
    """Pick the winning model per group on the 4-week-total metric.

    Ties and near-ties matter here: with only 5 origins, a 1-point WAPE gap is
    noise. `margin_vs_next` exposes how much of a real winner it is, so nobody
    reads a coin-flip as a finding.
    """
    rows = []
    for (prefix, name), g in summary.groupby(["sku_prefix", "group_name"]):
        g = g.sort_values("wape_4wk_total")
        if g["wape_4wk_total"].isna().all():
            continue
        best = g.iloc[0]
        nxt = g.iloc[1] if len(g) > 1 else None
        rows.append(
            {
                "sku_prefix": prefix,
                "group_name": name,
                "best_model": best["model"],
                "wape_4wk_total": best["wape_4wk_total"],
                "wape_weekly": best["wape_weekly"],
                "bias_4wk": best["bias_4wk"],
                "runner_up": None if nxt is None else nxt["model"],
                "margin_vs_next": None if nxt is None
                else round(float(nxt["wape_4wk_total"] - best["wape_4wk_total"]), 1),
                "holdout_qty": best["holdout_qty"],
                "meets_target_15pct": bool(best["wape_4wk_total"] <= 15),
            }
        )
    return pd.DataFrame(rows).sort_values("holdout_qty", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def report(panel, results, summary, best, zeros) -> None:
    p = print
    weeks = sorted(panel["week_start"].unique())
    n = len(weeks)
    hold_weeks = sorted(results["target_week"].unique())
    p("=" * 78)
    p("BASELINE FORECAST BACKTEST")
    p("=" * 78)
    p(f"\n  groups in scope    : {panel['sku_prefix'].nunique()}")
    p(f"  complete weeks     : {n}  ({weeks[0]} .. {weeks[-1]})")
    p(f"  holdout            : {len(hold_weeks)} weeks ({hold_weeks[0]} .. {hold_weeks[-1]})")
    p(f"  horizon            : {HORIZON} weeks from each origin")
    origins = sorted(results["origin_week"].unique())
    p(f"  rolling origins    : {len(origins)}  ({origins[0]} .. {origins[-1]})")
    p(f"  models             : {', '.join(MODELS)}")
    p(f"  forecast points    : {len(results):,}")

    p("\n" + "-" * 78)
    p("1. INTERMITTENCY  (share of weeks with zero demand)")
    p("-" * 78)
    p(f"  {'grp':<4} {'%zero':>6} {'cv':>6} {'mean/wk':>10} {'max/wk':>10}  group")
    for r in zeros.itertuples():
        flag = "  <-- intermittent" if r.is_intermittent else ""
        p(f"  {r.sku_prefix:<4} {r.pct_zero_weeks:>5.1f}% {r.cv:>6.2f} "
          f"{r.mean_qty:>10,.0f} {r.max_qty:>10,.0f}  {r.group_name}{flag}")

    p("\n" + "-" * 78)
    p("2. WAPE BY GROUP AND MODEL  (%, lower is better)")
    p("-" * 78)
    p(f"  {'grp':<4} {'model':<10} {'weekly':>8} {'4wk tot':>9}   group")
    for prefix, g in summary.groupby("sku_prefix"):
        for r in g.itertuples():
            wk = "n/a" if pd.isna(r.wape_weekly) else f"{r.wape_weekly:.1f}"
            tt = "n/a" if pd.isna(r.wape_4wk_total) else f"{r.wape_4wk_total:.1f}"
            p(f"  {r.sku_prefix:<4} {r.model:<10} {wk:>8} {tt:>9}   {r.group_name[:34]}")
        p("")

    p("-" * 78)
    p("3. BEST MODEL PER GROUP  (on 4-week total WAPE)")
    p("-" * 78)
    p(f"  {'grp':<4} {'best':<10} {'4wk':>6} {'wkly':>6} {'bias':>7} {'margin':>7} "
      f"{'hold qty':>11}  target")
    for r in best.itertuples():
        m = "-" if r.margin_vs_next is None else f"{r.margin_vs_next:+.1f}"
        tgt = "MEETS <=15%" if r.meets_target_15pct else ""
        p(f"  {r.sku_prefix:<4} {r.best_model:<10} {r.wape_4wk_total:>6.1f} "
          f"{r.wape_weekly:>6.1f} {r.bias_4wk:>+6.1f}% {m:>7} {r.holdout_qty:>11,.0f}  {tgt}")
    p("\n  bias: negative = model forecast MORE than actually sold.")

    # Pooled view: one model has to be chosen for the whole business unless the
    # per-group winners are decisive, so show how each model does overall.
    p("\n" + "-" * 78)
    p("4. POOLED ACROSS ALL IN-SCOPE GROUPS")
    p("-" * 78)
    windows = (
        results.groupby(["model", "sku_prefix", "origin_week"])
        .agg(actual=("actual", "sum"), forecast=("forecast", "sum"))
        .reset_index()
    )
    p(f"  {'model':<10} {'weekly':>8} {'4wk tot':>9}   (volume-weighted over every group)")
    for model in MODELS:
        r = results[results["model"] == model]
        w = windows[windows["model"] == model]
        p(f"  {model:<10} {wape(r['actual'], r['forecast']) * 100:>7.1f}% "
          f"{wape(w['actual'], w['forecast']) * 100:>8.1f}%")

    n_meet = int(best["meets_target_15pct"].sum())
    p(f"\n  groups meeting the 10-15% target on 4-week totals: {n_meet} of {len(best)}")

    # Decompose the error. Every model here is backward-looking, so if the
    # demand LEVEL shifted between train and holdout they all inherit the same
    # systematic miss and no choice among them can fix it. Separating that from
    # genuine timing error decides whether the next step is a better model or a
    # conversation about what changed in the business.
    p("\n" + "-" * 78)
    p("5. LEVEL SHIFT  (is the error timing, or a change in demand?)")
    p("-" * 78)
    train = panel[~panel["week_start"].isin(hold_weeks)]
    hold = panel[panel["week_start"].isin(hold_weeks)]
    tr_mean = train.groupby("sku_prefix")["qty"].mean()
    ho_mean = hold.groupby("sku_prefix")["qty"].mean()

    p(f"  {'grp':<4} {'train/wk':>10} {'hold/wk':>10} {'change':>8} "
      f"{'WAPE':>7} {'|bias|':>7} {'systematic':>11}  group")
    for r in best.itertuples():
        t, ho_ = tr_mean.get(r.sku_prefix, 0), ho_mean.get(r.sku_prefix, 0)
        chg = (ho_ - t) / t * 100 if t else float("nan")
        share = abs(r.bias_4wk) / r.wape_4wk_total * 100 if r.wape_4wk_total else float("nan")
        p(f"  {r.sku_prefix:<4} {t:>10,.0f} {ho_:>10,.0f} {chg:>+7.1f}% "
          f"{r.wape_4wk_total:>6.1f}% {abs(r.bias_4wk):>6.1f}% {share:>10.0f}%  "
          f"{r.group_name[:26]}")

    tot_t = train.groupby("week_start")["qty"].sum().mean()
    tot_h = hold.groupby("week_start")["qty"].sum().mean()
    p(f"\n  all in-scope groups: {tot_t:,.0f}/wk in train -> {tot_h:,.0f}/wk in holdout "
      f"({(tot_h - tot_t) / tot_t * 100:+.1f}%)")
    p("  'systematic' = share of WAPE explained by forecasting the wrong LEVEL")
    p("  rather than the wrong week. High values mean the model is not the problem.")
    p("=" * 78)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def earlier_holdout_index(panel: pd.DataFrame) -> int:
    """Index of the last complete week finishing on or before the cutoff date."""
    weeks = sorted(panel["week_start"].unique())
    ends = [pd.Timestamp(w) + pd.Timedelta(days=6) for w in weeks]
    cutoff = pd.Timestamp(EARLIER_HOLDOUT_ENDS_BY)
    eligible = [i for i, e in enumerate(ends) if e <= cutoff]
    if not eligible:
        raise ValueError(f"No complete week ends on or before {EARLIER_HOLDOUT_ENDS_BY}.")
    return eligible[-1]


def compare_scenarios(sum_recent: pd.DataFrame, sum_earlier: pd.DataFrame,
                      res_recent: pd.DataFrame, res_earlier: pd.DataFrame) -> None:
    """Print the two holdouts side by side.

    The point of this table is to answer one question: are the baselines weak,
    or was the recent period simply hard? If WAPE is materially lower on the
    earlier window with the SAME models and the SAME procedure, the models are
    fine and the Jul-Aug decline is the story.
    """
    p = print
    p("\n" + "=" * 78)
    p("6. SCENARIO COMPARISON  (identical models and procedure, two holdouts)")
    p("=" * 78)
    hr = sorted(res_recent["target_week"].unique())
    he = sorted(res_earlier["target_week"].unique())
    p(f"  earlier holdout : {he[0]} .. {he[-1]}   (stable period)")
    p(f"  recent  holdout : {hr[0]} .. {hr[-1]}   (declining period)")

    p(f"\n  POOLED 4-WEEK-TOTAL WAPE")
    p(f"  {'model':<10} {'earlier':>9} {'recent':>9} {'delta':>9}")
    for model in MODELS:
        vals = []
        for res in (res_earlier, res_recent):
            w = (
                res[res["model"] == model]
                .groupby(["sku_prefix", "origin_week"])
                .agg(actual=("actual", "sum"), forecast=("forecast", "sum"))
            )
            vals.append(wape(w["actual"], w["forecast"]) * 100)
        p(f"  {model:<10} {vals[0]:>8.1f}% {vals[1]:>8.1f}% {vals[1] - vals[0]:>+8.1f}")

    # Per group, ma8 only -- the model actually being adopted.
    p(f"\n  ma8 BY GROUP (4-week total WAPE)")
    p(f"  {'grp':<4} {'earlier':>9} {'recent':>9} {'delta':>9}   group")
    e = sum_earlier[sum_earlier["model"] == "ma8"].set_index("sku_prefix")
    r = sum_recent[sum_recent["model"] == "ma8"].set_index("sku_prefix")
    for prefix in sorted(set(e.index) & set(r.index)):
        ev, rv = e.at[prefix, "wape_4wk_total"], r.at[prefix, "wape_4wk_total"]
        if pd.isna(ev) or pd.isna(rv):
            continue
        p(f"  {prefix:<4} {ev:>8.1f}% {rv:>8.1f}% {rv - ev:>+8.1f}   "
          f"{r.at[prefix, 'group_name'][:32]}")
    p("=" * 78)


def main() -> int:
    panel, _groups = load_panel()
    zeros = zero_week_profile(panel)

    results = backtest(panel, scenario="recent")
    summary = summarise(results, panel)
    best = best_per_group(summary)

    e_idx = earlier_holdout_index(panel)
    res_earlier = backtest(panel, holdout_end=e_idx, scenario="earlier")
    sum_earlier = summarise(res_earlier, panel)

    all_results = pd.concat([results, res_earlier], ignore_index=True)
    all_summary = pd.concat([summary, sum_earlier], ignore_index=True)

    FORECAST.mkdir(parents=True, exist_ok=True)
    outputs = {
        "backtest_results.csv": all_results,
        "backtest_summary.csv": all_summary,
        "best_model_per_group.csv": best,
        "intermittency.csv": zeros,
    }
    locked = []
    for name, df in outputs.items():
        try:
            df.to_csv(FORECAST / name, index=False, encoding=OUTPUT_ENCODING)
        except PermissionError:
            locked.append(name)
    if locked:
        print(
            "ERROR: could not write these files because another program has them "
            "open (usually Excel):\n"
            + "\n".join(f"   data/forecast/{n}" for n in locked)
            + "\nClose them and re-run.",
            file=sys.stderr,
        )

    report(panel, results, summary, best, zeros)
    compare_scenarios(summary, sum_earlier, results, res_earlier)

    print("\nWrote to data/forecast/:")
    for name, df in outputs.items():
        print(f"   {name:<28} {len(df):>7,} rows x {len(df.columns)} cols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
