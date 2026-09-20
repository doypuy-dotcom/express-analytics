"""Operational outputs: what to order, what changed, and what to keep on the shelf.

This is the "act on it" half of the forecasting work. src/forecast_baseline.py
decides whether the models are trustworthy; this file assumes that decision is
made and produces the artefacts the owner actually uses:

  next_4_weeks.csv    forecast quantity per group for the coming 4 weeks, with a
                      range taken from how wrong the same model was in backtest.
  trend_alerts.csv    which groups are moving, for every in-scope group.
  reorder_points.csv  for the intermittent groups, where forecasting does not
                      work, a reorder level instead.
  dead_stock_risk.csv SKUs that have stopped selling and want a physical check.

Model choice is fixed at ma8 for the twelve non-intermittent groups. It is not
the lowest-WAPE model on every group, but the gaps between ma4/ma8/ses were
0.4-1.6 points on 5 origins -- noise -- and a mean of the last eight weeks is
something the owner can verify by hand. Explainability wins a tie.

Plain Python + pandas, no new dependencies.
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
# Findings a human has to sign off on before they change how anything is
# ordered -- same convention as parse_express.py: never in clean/.
REVIEW = ROOT / "data" / "review"

FILE_WEEKLY = CLEAN / "weekly_demand.csv"
FILE_LINES = CLEAN / "sales_lines.csv"
FILE_PRODUCTS = CLEAN / "products.csv"
FILE_GROUPS = REFERENCE / "product_groups.csv"
FILE_BACKTEST = FORECAST / "backtest_results.csv"
FILE_INTERMITTENCY = FORECAST / "intermittency.csv"

OUTPUT_ENCODING = "utf-8-sig"

PLAN_MODEL = "ma8"      # the adopted model
MA_WINDOW = 8           # must match PLAN_MODEL
HORIZON = 4             # weeks ahead to plan for

# Owner-confirmed intermittency classification, overriding the raw zero-week
# share computed in forecast_baseline.py. Groups 06, 07, 08 and 13 launched in
# February 2026; their leading zeros are the product's absence, not weeks of no
# demand, and on the interior test they run 3.6-14.8% zero weeks. They are
# forecastable with a short history. Only these three are genuinely lumpy:
#   04  53.8% interior zero weeks
#   16  42.1%
#   05  41.7%  (and no sales since 2026-07-13 -- see dead stock output)
# Confirmed by the owner rather than inferred, same convention as
# CONFIRMED_CUSTOMER_MERGES in parse_express.py.
CONFIRMED_INTERMITTENT = {"04", "16", "05"}

# Groups whose demand level has shifted so far that a mean over the full active
# window no longer describes them. Group 04 stepped up ~3.3x in mid-June (active
# mean 127/wk vs last-8 mean 417/wk), so its reorder point is computed from the
# recent window only. Owner-confirmed; the trade-off is that 8 weeks is a thin
# base for a variance estimate on a lumpy series.
ROP_WINDOW_OVERRIDE = {"04": 8}

# Dormancy tiers, in days since a SKU last sold.
#
# Naming: these outputs say "stock check recommended", never "dead stock". We
# have SALES data only -- there is no inventory feed in this pipeline. A SKU that
# has not sold in 90 days may be sitting in the warehouse as dead capital, or the
# shelf may be empty and that is precisely WHY it has not sold. The data cannot
# tell those apart, and they call for opposite actions. What the data supports is
# "go and look at this one", so that is what the column says.
DORMANCY_TIERS = (
    (90, "risk", "stock check recommended"),
    (60, "watch", "monitor"),
)
# Retained in the analyst-facing detail file for continuity, but deliberately
# absent from the dashboard export: at 30 days the list runs to 319 of 615 SKUs,
# which is over half the catalogue and not an actionable dashboard page.
DETAIL_ONLY_DAYS = 30

# Percentiles of the backtest error distribution used as the forecast range.
LOW_PCT, HIGH_PCT = 10, 90
# A backtest window only contributes an actual/forecast ratio if its forecast is
# at least this fraction of the group's median window forecast -- see
# build_next_4_weeks. Guards against divide-by-almost-zero blowing up the range.
RATIO_FLOOR_FRAC = 0.20
# Below this many surviving windows the percentiles are not worth printing; the
# range is left blank rather than published at false precision.
MIN_WINDOWS_FOR_RANGE = 8

TREND_ALERT_THRESHOLD = 15.0   # |% change| that raises a flag

# --- reorder point assumptions --------------------------------------------
# Lead time from placing an order to goods being sellable, in weeks. One week
# is the owner's stated figure; it is the single most important input here, so
# it is named rather than buried in the arithmetic.
LEAD_TIME_WEEKS = 1
# Service level -> safety factor. 1.65 is the standard normal z for 95%, i.e.
# accept a stockout in roughly 1 replenishment cycle in 20.
SERVICE_LEVEL = 0.95
SAFETY_Z = 1.65


# --------------------------------------------------------------------------
# Shared loading
# --------------------------------------------------------------------------


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Complete group x week grid of in-scope, complete weeks.

    Identical construction to forecast_baseline.load_panel -- absent rows are
    real zeros, and the incomplete trailing week is excluded. Kept as its own
    copy rather than imported so each script can be read and run on its own.
    """
    w = pd.read_csv(FILE_WEEKLY, dtype={"sku_prefix": str}, encoding="utf-8-sig")
    groups = pd.read_csv(FILE_GROUPS, dtype=str, encoding="utf-8-sig")
    groups["sku_prefix"] = groups["sku_prefix"].str.strip().str.zfill(2)

    w = w[w["forecast_scope"] & w["is_complete_week"]].copy()
    weeks = sorted(w["week_start"].unique())
    prefixes = sorted(w["sku_prefix"].unique())
    grid = pd.MultiIndex.from_product(
        [prefixes, weeks], names=["sku_prefix", "week_start"]
    )
    panel = (
        w.groupby(["sku_prefix", "week_start"])["qty"].sum()
        .reindex(grid, fill_value=0.0)
        .reset_index()
    )
    meta = groups.set_index("sku_prefix")
    panel["group_name"] = panel["sku_prefix"].map(meta["group_name"])
    panel["unit"] = panel["sku_prefix"].map(meta["main_unit"])

    # Mark weeks before a group's first recorded sale. Those rows are zeros only
    # because the product did not exist yet, so every mean, standard deviation
    # and moving average below must skip them -- including them is what made
    # groups 06/07/08/13 look intermittent in the first place. Kept as a flag on
    # the panel rather than dropped, so the grid stays rectangular and the
    # exclusion is visible to anything that reads it.
    first_sale = (
        panel[panel["qty"] > 0].groupby("sku_prefix")["week_start"].min()
    )
    panel["first_sale_week"] = panel["sku_prefix"].map(first_sale)
    panel["is_pre_launch"] = panel["week_start"] < panel["first_sale_week"]
    return panel, groups


def active(panel: pd.DataFrame) -> pd.DataFrame:
    """Panel rows from each group's launch onward -- the only rows that are demand."""
    return panel[~panel["is_pre_launch"]]


# --------------------------------------------------------------------------
# 1. Next 4 weeks
# --------------------------------------------------------------------------


def build_next_4_weeks(panel: pd.DataFrame, backtest: pd.DataFrame,
                       intermittent: set[str]) -> pd.DataFrame:
    """Forecast the coming 4 weeks per group, with an empirical range.

    Point forecast
    --------------
    ma8 on the most recent 8 complete weeks, held flat across all 4 weeks. The
    4-week total is simply 4x the weekly rate, because none of the baselines
    carry a trend -- stating the weekly rate separately keeps that honest
    rather than implying a shaped profile we did not model.

    Range
    -----
    NOT a statistical confidence interval. It is the observed spread of
    actual/forecast ratios for the SAME model over every backtest window, at
    the 4-week-total level, read off at the 10th and 90th percentile. So
    "low" means: in 8 of 10 backtested windows this group came in above this
    number.

    Both backtest scenarios are pooled (10 windows per group) because they
    bracket the two regimes seen in the data -- a stable spring and a declining
    summer. A range built only on the stable window would be reassuring and
    wrong. Ten observations is thin; the column n_windows makes that visible
    rather than hiding it behind a smooth-looking interval.
    """
    weeks = sorted(panel["week_start"].unique())
    recent = weeks[-MA_WINDOW:]
    last_week = weeks[-1]
    next_week = (pd.Timestamp(last_week) + pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    end_week = (pd.Timestamp(last_week) + pd.Timedelta(days=7 * HORIZON)).strftime("%Y-%m-%d")

    # active() drops pre-launch weeks. For the current data every in-scope group
    # has at least 8 weeks of trading history, so this changes no number today --
    # it is here so a group launched in the last 8 weeks is not forecast at a
    # fraction of its real rate by averaging in the weeks it did not exist.
    base = (
        active(panel)[lambda d: d["week_start"].isin(recent)]
        .groupby(["sku_prefix", "group_name", "unit"])["qty"]
        .mean()
        .rename("forecast_qty_per_week")
        .reset_index()
    )
    n_active = (
        active(panel)[lambda d: d["week_start"].isin(recent)]
        .groupby("sku_prefix").size().rename("n_weeks_in_mean")
    )
    base["n_weeks_in_mean"] = base["sku_prefix"].map(n_active)

    # Empirical error ratios from the backtest, 4-week-window level.
    bt = backtest[backtest["model"] == PLAN_MODEL]
    windows = (
        bt.groupby(["sku_prefix", "scenario", "origin_week"])
        .agg(actual=("actual", "sum"), forecast=("forecast", "sum"))
        .reset_index()
    )
    # Windows where the model predicted zero carry no information about
    # proportional error and would divide by zero; drop them explicitly.
    windows = windows[windows["forecast"] > 0]
    # A near-zero denominator is barely better than a zero one. Group 04 had a
    # window forecasting 33.6 m against 814 m actual -- a ratio of 24x that is
    # an artefact of the divisor, not a measurement of error, and it dragged the
    # 90th percentile to 11.7x (a "high" of 19,567 m on a 1,669 m forecast).
    # Require a window's forecast to be at least RATIO_FLOOR_FRAC of that
    # group's own median window forecast before its ratio is allowed to vote.
    med_fc = windows.groupby("sku_prefix")["forecast"].transform("median")
    windows = windows[windows["forecast"] >= RATIO_FLOOR_FRAC * med_fc]
    windows["ratio"] = windows["actual"] / windows["forecast"]

    stats = (
        windows.groupby("sku_prefix")["ratio"]
        .agg(
            n_windows="size",
            ratio_low=lambda s: s.quantile(LOW_PCT / 100),
            ratio_high=lambda s: s.quantile(HIGH_PCT / 100),
            ratio_median="median",
        )
        .reset_index()
    )

    out = base.merge(stats, on="sku_prefix", how="left")
    out["forecast_qty_4wk"] = (out["forecast_qty_per_week"] * HORIZON).round(0)
    # Too few surviving windows -> no range at all. A blank cell is a truthful
    # "we do not know"; a number computed from 3 observations is not.
    thin = out["n_windows"].fillna(0) < MIN_WINDOWS_FOR_RANGE
    for c in ("ratio_low", "ratio_high", "ratio_median"):
        out.loc[thin, c] = pd.NA
    out["low_qty_4wk"] = (out["forecast_qty_4wk"] * out["ratio_low"]).round(0)
    out["high_qty_4wk"] = (out["forecast_qty_4wk"] * out["ratio_high"]).round(0)
    # The range is read off actual/forecast ratios, so when the model was biased
    # high in most backtest windows the whole range sits BELOW the point
    # forecast. That is a real signal, not a glitch -- ma8 over-ordered these
    # groups in 9 windows out of 10 -- so it is labelled rather than clipped.
    out["range_below_forecast"] = out["ratio_high"] < 1.0
    out["forecast_qty_per_week"] = out["forecast_qty_per_week"].round(1)
    out["model"] = PLAN_MODEL
    out["is_intermittent"] = out["sku_prefix"].isin(intermittent)
    out["forecast_from_week"] = next_week
    out["forecast_to_week"] = end_week
    # Intermittency is flagged, not filtered: the owner still wants a number for
    # these groups, but it should never be read with the same confidence.
    out["confidence"] = out["is_intermittent"].map(
        {False: "use for ordering", True: "indicative only - use reorder point"}
    )
    for c in ("ratio_low", "ratio_high", "ratio_median"):
        out[c] = out[c].round(3)

    cols = [
        "sku_prefix", "group_name", "unit", "model",
        "forecast_from_week", "forecast_to_week",
        "forecast_qty_per_week", "n_weeks_in_mean", "forecast_qty_4wk",
        "low_qty_4wk", "high_qty_4wk",
        "ratio_low", "ratio_median", "ratio_high", "n_windows",
        "range_below_forecast", "is_intermittent", "confidence",
    ]
    return out[cols].sort_values("forecast_qty_4wk", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------
# 2. Trend alerts
# --------------------------------------------------------------------------


def build_trend_alerts(panel: pd.DataFrame, intermittent: set[str]) -> pd.DataFrame:
    """Flag groups whose recent demand has moved.

    Headline measure: MEDIAN of the last 4 weeks against the median of the last 8.

    Why the median. The mean version of this alert put group 07 at -88.2%, driven
    by a single 1,173 m order sitting in the 8-week denominator against a normal
    week of 100-170 m. One large order is exactly what this business does -- a
    roofing job is lumpy -- so a measure that a single order can dominate will
    cry wolf every time a big customer buys. The median asks the question the
    owner actually means: has the TYPICAL week changed? A single outlier moves it
    barely at all, while a genuine shift in level moves every week and so moves
    the median with it.

    The mean-based numbers are kept alongside as secondary columns, because the
    gap between the two is itself informative: median flat + mean up means one
    big order, not a trend.

    Note the last 8 CONTAINS the last 4, so the comparison is deliberately damped
    -- a 4-week surge can move it by at most half its true size. That makes it
    slow to false-alarm, which is what you want for something read weekly.
    pct_change_vs_prior_4 is the undamped version (last 4 vs the 4 before, no
    overlap) for sanity-checking a flag before acting on it.
    """
    weeks = sorted(panel["week_start"].unique())
    last4, last8 = weeks[-4:], weeks[-8:]
    prior4 = weeks[-8:-4]

    def stat_over(ws, how):
        sub = active(panel)[lambda d: d["week_start"].isin(ws)]
        return sub.groupby("sku_prefix")["qty"].agg(how)

    med4, med8, medp4 = (stat_over(w, "median") for w in (last4, last8, prior4))
    m4, m8, mp4 = (stat_over(w, "mean") for w in (last4, last8, prior4))
    meta = panel.groupby("sku_prefix")[["group_name", "unit"]].first()

    out = pd.DataFrame({
        "group_name": meta["group_name"],
        "unit": meta["unit"],
        "median_qty_last_4wk": med4.round(1),
        "median_qty_last_8wk": med8.round(1),
        "median_qty_prior_4wk": medp4.round(1),
        "avg_qty_last_4wk": m4.round(1),
        "avg_qty_last_8wk": m8.round(1),
        "avg_qty_prior_4wk": mp4.round(1),
    }).reset_index()

    def pct(new, old):
        # Guard the zero denominator: a group whose typical week was already zero
        # has no percentage change to report, only a level. Left null rather than
        # rendered as inf or a misleading 0%.
        return ((new - old) / old * 100).where(old > 0)

    out["pct_change_median"] = pct(
        out["median_qty_last_4wk"], out["median_qty_last_8wk"]).round(1)
    out["pct_change_mean"] = pct(
        out["avg_qty_last_4wk"], out["avg_qty_last_8wk"]).round(1)
    out["pct_change_vs_prior_4"] = pct(
        out["median_qty_last_4wk"], out["median_qty_prior_4wk"]).round(1)
    # A group that has stopped selling outright needs its own test, because the
    # median cannot see it. Group 05 sells in under a fifth of weeks, so its
    # median was ALREADY zero before it stopped -- a median-vs-median comparison
    # reads 0 -> 0 and reports no change, silently dropping the single most
    # important alert in the set. Testing the total instead catches it whatever
    # the median was doing.
    s4 = stat_over(last4, "sum")
    s8 = stat_over(last8, "sum")
    out["qty_last_4wk"] = out["sku_prefix"].map(s4).round(1)
    out["qty_last_8wk"] = out["sku_prefix"].map(s8).round(1)
    out["stopped"] = (out["qty_last_4wk"] == 0) & (out["qty_last_8wk"] > 0)

    out["alert"] = (
        (out["pct_change_median"].abs() > TREND_ALERT_THRESHOLD) | out["stopped"]
    )
    # Divergence between the two measures = one big order rather than a trend.
    out["outlier_driven"] = (
        (out["pct_change_median"].abs() <= TREND_ALERT_THRESHOLD)
        & (out["pct_change_mean"].abs() > TREND_ALERT_THRESHOLD)
    )
    out["direction"] = pd.cut(
        out["pct_change_median"],
        bins=[-float("inf"), -TREND_ALERT_THRESHOLD, TREND_ALERT_THRESHOLD, float("inf")],
        labels=["falling", "stable", "rising"],
    )
    out.loc[out["stopped"], "direction"] = "falling"
    # An alert on a group that is zero half the time is usually noise, not news.
    out["is_intermittent"] = out["sku_prefix"].isin(intermittent)
    cols = [
        "sku_prefix", "group_name", "unit",
        "median_qty_last_4wk", "median_qty_last_8wk", "median_qty_prior_4wk",
        "pct_change_median", "pct_change_vs_prior_4",
        "qty_last_4wk", "qty_last_8wk", "stopped",
        "avg_qty_last_4wk", "avg_qty_last_8wk", "avg_qty_prior_4wk",
        "pct_change_mean", "outlier_driven",
        "alert", "direction", "is_intermittent",
    ]
    return out[cols].sort_values(
        ["stopped", "pct_change_median"], ascending=[False, True]
    ).reset_index(drop=True)


# --------------------------------------------------------------------------
# 3. Reorder points for intermittent groups
# --------------------------------------------------------------------------


def build_reorder_points(panel: pd.DataFrame, intermittent: set[str]) -> pd.DataFrame:
    """Reorder level for groups too intermittent to forecast.

    Formula
    -------
        reorder_point = (mean weekly demand x lead time)
                      + (z x weekly std dev x sqrt(lead time))
          ^ demand expected to arise while waiting     ^ safety stock

    The first term covers what sells during the LEAD_TIME_WEEKS wait. The second
    is the buffer: sqrt(lead time) because variance accumulates linearly over
    independent weeks, so standard deviation grows with the square root. With a
    1-week lead time sqrt(1) = 1, so the term collapses to z x sigma -- the
    sqrt is written out anyway so the formula stays correct if the lead time
    ever changes.

    z = 1.65 targets a 95% service level: roughly one stockout per twenty
    replenishment cycles.

    Honest caveat
    -------------
    This uses the normal approximation, which assumes demand is symmetric around
    its mean. For a group selling in 18% of weeks (group 05) that is plainly
    false -- the real distribution is a spike at zero with an occasional large
    order. The normal approximation OVERSTATES the buffer for such series, so
    these numbers are conservative: they will tie up more cash than necessary
    rather than risk a stockout. That is the correct direction to be wrong in,
    but it is why Croston / bootstrapping is the right next step if the cash
    matters.

    weeks_of_cover shows the same number as a duration, which is the sanity
    check: if it says 9 weeks of stock for a slow mover, the answer is probably
    "do not stock it, order on demand".

    Active window
    -------------
    Statistics are computed from each group's FIRST SALE WEEK onward, not over
    the whole 39-week panel. Groups 06, 07, 08 and 13 did not exist before
    mid-February 2026; the 11-12 leading zeros in their rows are the product's
    absence, not weeks of zero demand. Averaging over them halves the mean and
    inflates the apparent intermittency, which would set the reorder point far
    too low on exactly the products that are still ramping. Weeks before the
    first sale are therefore excluded and counted in n_weeks_pre_launch so the
    exclusion is auditable.
    """
    df = panel[panel["sku_prefix"].isin(intermittent)].sort_values("week_start")

    rows = []
    for (pfx, gname, unit), s in df.groupby(["sku_prefix", "group_name", "unit"]):
        qty = s.set_index("week_start")["qty"]
        nonzero = qty[qty > 0]
        if nonzero.empty:
            continue
        first_sale, last_sale = nonzero.index[0], nonzero.index[-1]
        act = qty.loc[first_sale:]                    # launch -> end of panel
        trailing_zeros = int((qty.loc[last_sale:] == 0).sum())

        # Owner-confirmed override: a group whose demand level has stepped gets
        # its statistics from the recent window instead of the whole active one,
        # because the older weeks describe a product that was selling at a
        # different rate. Recorded per row in basis_window so the reorder point
        # can never be read without knowing what it was built from.
        win = ROP_WINDOW_OVERRIDE.get(pfx)
        basis = act.iloc[-win:] if win else act
        basis_window = f"last {win} weeks" if win else "full active window"

        rows.append({
            "sku_prefix": pfx, "group_name": gname, "unit": unit,
            "first_sale_week": first_sale,
            "last_sale_week": last_sale,
            "n_weeks_panel": int(len(qty)),
            "n_weeks_pre_launch": int(len(qty) - len(act)),
            "n_weeks_active": int(len(act)),
            "basis_window": basis_window,
            "n_weeks": int(len(basis)),
            "n_zero_weeks": int((basis == 0).sum()),
            "trailing_zero_weeks": trailing_zeros,
            "mean_weekly_qty": basis.mean(),
            "std_weekly_qty": basis.std(),
            "max_weekly_qty": basis.max(),
            "mean_weekly_qty_active": round(float(act.mean()), 1),
        })
    g = pd.DataFrame(rows)
    g["pct_zero_weeks"] = (g["n_zero_weeks"] / g["n_weeks"] * 100).round(1)
    # Four consecutive zero weeks at the end of the series is not intermittency,
    # it is a product that has stopped selling. Holding safety stock against a
    # mean that no longer applies is how dead inventory accumulates.
    g["maybe_discontinued"] = g["trailing_zero_weeks"] >= 4
    # A reorder point is only as good as the mean it is built on, and a mean over
    # the whole active window assumes the demand level has not moved. Group 04
    # breaks that assumption: sporadic small orders until mid-June, then a step
    # up to ~400/wk. Its active mean of 127/wk would set the buffer at roughly a
    # third of true lead-time demand. Surfacing the ratio rather than silently
    # switching to the recent mean, because on a genuinely lumpy series the
    # recent 8 weeks can just as easily be a run of luck as a new level.
    g["mean_weekly_qty_last8"] = g["sku_prefix"].map(
        panel.sort_values("week_start").groupby("sku_prefix")["qty"].apply(
            lambda s: s.iloc[-8:].mean()
        )
    ).round(1)
    # Compared against the FULL active mean, not the basis mean -- otherwise a
    # group already switched to the recent window would always read 1.0x and
    # hide the very shift that justified the switch.
    g["recent_vs_active"] = (
        g["mean_weekly_qty_last8"] / g["mean_weekly_qty_active"]
    ).round(2)
    g["regime_shift"] = (g["recent_vs_active"] >= 2.0) | (g["recent_vs_active"] <= 0.5)

    lead_demand = g["mean_weekly_qty"] * LEAD_TIME_WEEKS
    safety = SAFETY_Z * g["std_weekly_qty"] * (LEAD_TIME_WEEKS ** 0.5)

    g["lead_time_weeks"] = LEAD_TIME_WEEKS
    g["service_level"] = SERVICE_LEVEL
    g["safety_z"] = SAFETY_Z
    g["lead_time_demand"] = lead_demand.round(1)
    g["safety_stock"] = safety.round(1)
    g["reorder_point"] = (lead_demand + safety).round(0)
    g["weeks_of_cover"] = (g["reorder_point"] / g["mean_weekly_qty"]).round(1)
    for c in ("mean_weekly_qty", "std_weekly_qty"):
        g[c] = g[c].round(1)

    cols = [
        "sku_prefix", "group_name", "unit",
        "first_sale_week", "last_sale_week",
        "n_weeks_panel", "n_weeks_pre_launch", "n_weeks_active",
        "basis_window", "n_weeks",
        "mean_weekly_qty", "std_weekly_qty", "max_weekly_qty", "pct_zero_weeks",
        "mean_weekly_qty_active", "mean_weekly_qty_last8",
        "recent_vs_active", "regime_shift",
        "trailing_zero_weeks", "maybe_discontinued",
        "lead_time_weeks", "service_level", "safety_z",
        "lead_time_demand", "safety_stock", "reorder_point", "weeks_of_cover",
    ]
    return g[cols].sort_values("reorder_point", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def build_dead_stock_risk() -> tuple[pd.DataFrame, str]:
    """SKU-level list of things that have stopped selling.

    Group 05 (สแน็ปล็อค) is the reason this exists: the group-level reorder point
    was happily proposing a 323 m buffer against a product whose last sale was
    2026-07-13. Group averages hide this, because one live SKU keeps a group
    looking alive while the rest of it goes dead. So this is computed per SKU.

    As-of date
    ----------
    Days since last sale are measured from the LAST DATE IN THE EXPORT, not from
    today. The export ends 2026-08-31; running this in late September would
    otherwise report every SKU as three weeks more dormant than it is and silently
    turn a reporting lag into a stock problem. as_of is returned so the caller can
    state it rather than leave it implied.

    Inventory items only -- freight, giveaways and service lines (is_inventory
    false) have no stock to be dead.
    """
    lines = pd.read_csv(
        FILE_LINES, dtype={"sku_prefix": str}, encoding="utf-8-sig",
        usecols=["sku", "qty", "amount_ex_vat", "doc_no", "doc_date_iso", "is_cancelled"],
    )
    products = pd.read_csv(
        FILE_PRODUCTS, dtype={"sku_prefix": str}, encoding="utf-8-sig",
        usecols=["sku", "product_name", "unit", "sku_prefix", "category",
                 "group_name", "is_inventory"],
    )

    lines = lines[~lines["is_cancelled"].fillna(False)]
    lines["doc_date_iso"] = pd.to_datetime(lines["doc_date_iso"], errors="coerce")
    lines = lines.dropna(subset=["doc_date_iso"])
    as_of = lines["doc_date_iso"].max()

    # Only quantity-bearing lines count as a sale. A zero-qty line is usually a
    # descriptive or corrective row and must not keep a dead SKU looking alive.
    sold = lines[lines["qty"] > 0]

    agg = sold.groupby("sku").agg(
        last_sale_date=("doc_date_iso", "max"),
        first_sale_date=("doc_date_iso", "min"),
        total_qty_sold=("qty", "sum"),
        total_revenue_ex_vat=("amount_ex_vat", "sum"),
        n_lines=("doc_no", "size"),
        n_documents=("doc_no", "nunique"),
    )

    inv = products[products["is_inventory"].fillna(False)].copy()
    out = inv.merge(agg, on="sku", how="left")

    # A stocked SKU with no qty-bearing sale at all is the worst case, not an
    # absent one: give it the maximum dormancy rather than dropping it.
    out["days_since_last_sale"] = (as_of - out["last_sale_date"]).dt.days
    out["never_sold"] = out["last_sale_date"].isna()
    span = (as_of - lines["doc_date_iso"].min()).days
    out["days_since_last_sale"] = out["days_since_last_sale"].fillna(span).astype(int)

    days = out["days_since_last_sale"]
    for d in (DETAIL_ONLY_DAYS, *[t[0] for t in DORMANCY_TIERS]):
        out[f"no_sale_{d}d"] = days >= d

    # risk_level / recommendation, assigned from the highest tier that applies.
    out["risk_level"] = "active"
    out["recommendation"] = "-"
    for threshold, level, action in sorted(DORMANCY_TIERS):
        hit = days >= threshold
        out.loc[hit, "risk_level"] = level
        out.loc[hit, "recommendation"] = action
    out["as_of_date"] = as_of.strftime("%Y-%m-%d")
    for c in ("last_sale_date", "first_sale_date"):
        out[c] = out[c].dt.strftime("%Y-%m-%d")
    for c in ("total_qty_sold", "total_revenue_ex_vat"):
        out[c] = out[c].fillna(0).round(2)
    for c in ("n_lines", "n_documents"):
        out[c] = out[c].fillna(0).astype(int)

    cols = [
        "sku", "product_name", "unit", "sku_prefix", "category", "group_name",
        "as_of_date", "last_sale_date", "first_sale_date", "days_since_last_sale",
        "never_sold", "risk_level", "recommendation",
        *[f"no_sale_{d}d" for d in (DETAIL_ONLY_DAYS, *[t[0] for t in DORMANCY_TIERS])],
        "total_qty_sold", "total_revenue_ex_vat", "n_lines", "n_documents",
    ]
    out = out[cols].sort_values(
        ["days_since_last_sale", "total_revenue_ex_vat"], ascending=[False, False]
    )
    return out.reset_index(drop=True), as_of.strftime("%Y-%m-%d")


def diagnose_intermittency(panel: pd.DataFrame, intermittent: set[str]) -> pd.DataFrame:
    """Re-test the intermittency label by asking WHERE the zero weeks sit.

    forecast_baseline.py classified a group as intermittent on the raw share of
    zero weeks over the whole 39-week panel. That measure cannot tell three very
    different situations apart:

      leading zeros   the product had not launched yet  -> not intermittent,
                      just short history
      trailing zeros  the product has stopped selling   -> not intermittent,
                      possibly discontinued
      interior zeros  the product is on the shelf and
                      some weeks nobody buys it         -> genuinely intermittent

    Only the interior share is evidence of intermittent demand. This function
    recomputes it and lands in data/review/ rather than data/clean/, because
    changing a group's classification changes how it gets ordered -- that is the
    owner's call to confirm, not a silent reclassification by the script.
    """
    rows = []
    for (pfx, gname, unit), s in panel.sort_values("week_start").groupby(
        ["sku_prefix", "group_name", "unit"]
    ):
        qty = s.set_index("week_start")["qty"]
        nonzero = qty[qty > 0]
        if nonzero.empty:
            continue
        first, last = nonzero.index[0], nonzero.index[-1]
        active = qty.loc[first:]
        interior = qty.loc[first:last]
        interior_pct = float((interior == 0).mean() * 100)
        was = pfx in intermittent
        now = interior_pct >= 25.0
        rows.append({
            "sku_prefix": pfx, "group_name": gname, "unit": unit,
            "first_sale_week": first, "last_sale_week": last,
            "n_weeks_panel": int(len(qty)),
            "n_weeks_pre_launch": int(len(qty) - len(active)),
            "n_weeks_active": int(len(active)),
            "pct_zero_panel": round(float((qty == 0).mean() * 100), 1),
            "pct_zero_interior": round(interior_pct, 1),
            "trailing_zero_weeks": int((qty.loc[last:] == 0).sum()),
            "classified_intermittent": was,
            "interior_test_intermittent": now,
            "reclassify": was != now,
        })
    out = pd.DataFrame(rows)
    return out.sort_values("pct_zero_interior", ascending=False).reset_index(drop=True)


def report_dead_stock(dead: pd.DataFrame, as_of: str) -> None:
    """Summarise dormant SKUs, then name the worst by revenue at risk."""
    print("\n" + "-" * 78)
    print(f"4. STOCK CHECK  ({len(dead)} stocked SKUs, as of {as_of})")
    print("-" * 78)
    print("  Dormancy = days since last SALE. There is no inventory feed here, so")
    print("  this cannot distinguish dead stock from an empty shelf -- it says")
    print("  which SKUs to go and look at, not which are overstocked.")
    print("  Measured from the last date in the export, not from today.\n")
    for level in ("watch", "risk"):
        sub = dead[dead["risk_level"] == level]
        rev = sub["total_revenue_ex_vat"].sum()
        lo = min(t[0] for t in DORMANCY_TIERS if t[1] == level)
        hi = "+" if level == "risk" else "-89"
        print(f"  {level:<6} ({lo}{hi} days) : {len(sub):>4} SKUs   "
              f"lifetime revenue {rev:>14,.0f}")
    active_n = int((dead["risk_level"] == "active").sum())
    print(f"  {'active':<6} (<60 days)   : {active_n:>4} SKUs")
    print()
    worst = dead[dead["risk_level"] == "risk"].nlargest(10, "total_revenue_ex_vat")
    print("  largest 'risk' SKUs by lifetime revenue:")
    print(f"  {'sku':<12} {'last sale':<11} {'days':>5} {'qty':>10} "
          f"{'revenue':>13}  product")
    for r in worst.itertuples():
        print(f"  {r.sku:<12} {str(r.last_sale_date):<11} "
              f"{r.days_since_last_sale:>5} {r.total_qty_sold:>10,.0f} "
              f"{r.total_revenue_ex_vat:>13,.0f}  {str(r.product_name)[:24]}")


def report_intermittency_review(diag: pd.DataFrame) -> None:
    """Print the zero-week decomposition, loudest for groups that change side."""
    print("\n" + "-" * 78)
    print("5. INTERMITTENCY RE-CHECK  (where do the zero weeks actually sit?)")
    print("-" * 78)
    print("  grp  launch      pre  active   %zero      %zero   was ->  now")
    print("                   wks     wks   panel   interior")
    for _, r in diag.iterrows():
        mark = "  <-- RECLASSIFY" if r["reclassify"] else ""
        was = "INT" if r["classified_intermittent"] else "fcst"
        now = "INT" if r["interior_test_intermittent"] else "fcst"
        print(
            f"  {r['sku_prefix']:<4} {r['first_sale_week']}  "
            f"{r['n_weeks_pre_launch']:>4} {r['n_weeks_active']:>6}  "
            f"{r['pct_zero_panel']:>6.1f}%  {r['pct_zero_interior']:>8.1f}%  "
            f"{was:>4} -> {now:<4}{mark}"
        )
    n = int(diag["reclassify"].sum())
    print(f"\n  {n} of {len(diag)} groups would change side on the interior test.")
    print("  Written to data/review/ for confirmation -- NOT applied automatically.")


def report(plan, alerts, rop, intermittent) -> None:
    p = print
    p("=" * 78)
    p("FORECAST PLAN")
    p("=" * 78)
    p(f"  model            : {PLAN_MODEL} (mean of last {MA_WINDOW} complete weeks)")
    p(f"  horizon          : {HORIZON} weeks "
      f"({plan['forecast_from_week'].iloc[0]} .. {plan['forecast_to_week'].iloc[0]})")
    p(f"  range            : {LOW_PCT}th-{HIGH_PCT}th pct of backtest actual/forecast ratios")

    p("\n" + "-" * 78)
    p(f"1. NEXT {HORIZON} WEEKS  ({(~plan['is_intermittent']).sum()} forecastable "
      f"groups, {plan['is_intermittent'].sum()} intermittent)")
    p("-" * 78)
    p(f"  {'grp':<4} {'unit':<8} {'per wk':>10} {'4wk fcst':>11} "
      f"{'low':>10} {'high':>10}   group")
    def qty(v):
        # A suppressed range prints as "--", never as "nan": the reader should
        # see "we did not have enough windows to say" and not a broken number.
        return "--" if pd.isna(v) else f"{v:,.0f}"

    def line(r, note=""):
        p(f"  {r.sku_prefix:<4} {r.unit:<8} {r.forecast_qty_per_week:>10,.1f} "
          f"{r.forecast_qty_4wk:>11,.0f} {qty(r.low_qty_4wk):>10} "
          f"{qty(r.high_qty_4wk):>10}   {r.group_name[:26]}{note}")

    for r in plan[~plan["is_intermittent"]].itertuples():
        line(r, "  <-- range sits below forecast" if r.range_below_forecast else "")

    p(f"\n  intermittent groups (indicative only -- order via reorder point):")
    for r in plan[plan["is_intermittent"]].itertuples():
        line(r)

    p("\n" + "-" * 78)
    p(f"2. TREND ALERTS  (MEDIAN week, last 4 vs last 8; "
      f"flag at +/-{TREND_ALERT_THRESHOLD:.0f}%)")
    p("-" * 78)
    p(f"  {'grp':<4} {'med last4':>10} {'med last8':>10} {'median':>9} "
      f"{'mean':>9}  {'flag':<8} group")
    for r in alerts.itertuples():
        if not r.alert:
            flag = ""
        elif r.stopped:
            flag = "STOPPED"
        else:
            flag = "FALLING" if r.pct_change_median < 0 else "RISING"
        mark = "*" if r.is_intermittent else " "
        med = "--" if pd.isna(r.pct_change_median) else f"{r.pct_change_median:+.1f}%"
        avg = "--" if pd.isna(r.pct_change_mean) else f"{r.pct_change_mean:+.1f}%"
        note = "  <-- one large order, not a trend" if r.outlier_driven else ""
        p(f"  {r.sku_prefix:<4} {r.median_qty_last_4wk:>10,.1f} "
          f"{r.median_qty_last_8wk:>10,.1f} {med:>9} {avg:>9}  "
          f"{flag:<8}{mark}{r.group_name[:22]}{note}")
    n_alert = int(alerts["alert"].sum())
    n_out = int(alerts["outlier_driven"].sum())
    p(f"\n  {n_alert} of {len(alerts)} groups flagged.  * = intermittent, treat with caution")
    if n_out:
        p(f"  {n_out} group(s) move on the mean but not the median: a single large")
        p("  order, not a change in the typical week. Deliberately not flagged.")

    p("\n" + "-" * 78)
    p(f"3. REORDER POINTS  ({len(rop)} intermittent groups, "
      f"lead time {LEAD_TIME_WEEKS}wk, service {SERVICE_LEVEL:.0%})")
    p("-" * 78)
    p("  statistics use each group's active window (first sale week onward),")
    p("  except where basis_window says otherwise -- see ROP_WINDOW_OVERRIDE")
    p(f"  {'grp':<4} {'unit':<8} {'mean/wk':>9} {'sd/wk':>9} {'%zero':>7} "
      f"{'last8/wk':>9} {'ROP':>8} {'cover':>7}  group")
    for r in rop.itertuples():
        note = ""
        if r.regime_shift and r.basis_window.startswith("last "):
            # Shift detected AND already corrected for -- say so, rather than
            # warning about a number that was just rebuilt to handle it.
            note = f"  <-- level moved {r.recent_vs_active:.1f}x, using {r.basis_window}"
        elif r.regime_shift:
            note = f"  <-- level moved {r.recent_vs_active:.1f}x, review basis window"
        elif r.maybe_discontinued:
            note = f"  <-- no sales for {r.trailing_zero_weeks} wks, check before stocking"
        p(f"  {r.sku_prefix:<4} {r.unit:<8} {r.mean_weekly_qty:>9,.1f} "
          f"{r.std_weekly_qty:>9,.1f} {r.pct_zero_weeks:>6.1f}% "
          f"{r.mean_weekly_qty_last8:>9,.1f} "
          f"{r.reorder_point:>8,.0f} {r.weeks_of_cover:>6.1f}w  {r.group_name[:18]}{note}")
    p("\n  ROP = mean weekly demand x lead time  +  z x weekly sd x sqrt(lead time)")
    p("  Normal approximation overstates the buffer on very intermittent series;")
    p("  these are deliberately conservative. See build_reorder_points() docstring.")
    p("=" * 78)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    for f in (FILE_WEEKLY, FILE_BACKTEST, FILE_INTERMITTENCY):
        if not f.exists():
            raise FileNotFoundError(
                f"{f} not found. Run src/parse_express.py then "
                "src/forecast_baseline.py first."
            )

    panel, _groups = load_panel()
    backtest = pd.read_csv(FILE_BACKTEST, dtype={"sku_prefix": str}, encoding="utf-8-sig")
    inter_df = pd.read_csv(FILE_INTERMITTENCY, dtype={"sku_prefix": str}, encoding="utf-8-sig")
    baseline_inter = set(inter_df.loc[inter_df["is_intermittent"], "sku_prefix"])
    # The confirmed list wins. Print what it changed so the override can never
    # drift silently away from what forecast_baseline.py still reports.
    intermittent = set(CONFIRMED_INTERMITTENT)
    moved = sorted(baseline_inter - intermittent)
    if moved:
        print(f"  reclassified as forecastable (owner-confirmed): {', '.join(moved)}")

    plan = build_next_4_weeks(panel, backtest, intermittent)
    alerts = build_trend_alerts(panel, intermittent)
    rop = build_reorder_points(panel, intermittent)
    diag = diagnose_intermittency(panel, baseline_inter)
    dead, as_of = build_dead_stock_risk()

    FORECAST.mkdir(parents=True, exist_ok=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    outputs = {
        FORECAST / "next_4_weeks.csv": plan,
        FORECAST / "trend_alerts.csv": alerts,
        FORECAST / "reorder_points.csv": rop,
        FORECAST / "dead_stock_risk.csv": dead,
        REVIEW / "intermittency_review.csv": diag,
    }
    locked = []
    for path, df in outputs.items():
        try:
            df.to_csv(path, index=False, encoding=OUTPUT_ENCODING)
        except PermissionError:
            locked.append(path.name)
    if locked:
        print(
            "ERROR: could not write these files because another program has them "
            "open (usually Excel):\n"
            + "\n".join(f"   {n}" for n in locked)
            + "\nClose them and re-run.",
            file=sys.stderr,
        )

    report(plan, alerts, rop, intermittent)
    report_dead_stock(dead, as_of)
    report_intermittency_review(diag)

    print("\nWrote:")
    for path, df in outputs.items():
        rel = path.relative_to(ROOT).as_posix()
        print(f"   {rel:<42} {len(df):>7,} rows x {len(df.columns)} cols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
