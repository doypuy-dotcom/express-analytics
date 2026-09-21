"""Build data/app/ -- the only directory the dashboard reads.

Why a separate export rather than pointing the dashboard at data/clean/ and
data/forecast/:

  * Those directories are the analyst's working set. They contain diagnostic
    columns, review files, intermediate tables and two backtest scenarios. A
    dashboard that reads them would break every time one of them gained a column.
  * The dashboard needs a small, stable, documented contract. This file IS that
    contract: whatever it writes, plus the README it generates, is what the app
    may rely on. Everything else is free to change.
  * It is the right place to enforce presentation rules once -- zero-padded
    codes, ISO dates, no NaN literals -- instead of in every dashboard page.

Run after src/parse_express.py, src/forecast_baseline.py and
src/forecast_plan.py. Plain Python + pandas.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CLEAN = ROOT / "data" / "clean"
REFERENCE = ROOT / "data" / "reference"
FORECAST = ROOT / "data" / "forecast"
APP = ROOT / "data" / "app"

# utf-8-sig: the BOM is what makes Thai open correctly on a double-click in
# Excel. Every consumer here is either Excel or a dashboard library that strips
# the BOM automatically.
OUTPUT_ENCODING = "utf-8-sig"

# Columns that are CODES, not numbers. Stored zero-padded as strings. Any
# consumer that type-infers will turn "01" into 1 and silently break joins
# against the group reference -- this cost a debugging session once already, and
# the README warns about it in the loudest terms available.
CODE_COLUMNS = ("sku_prefix", "salesperson_code", "customer_code")

# Money columns. README.md is the only file in data/app/ that is committed --
# the CSVs are gitignored because they hold real revenue. The README took its
# "Example" values from the first row of real data, which put real monthly
# revenue into the one file that IS published. These columns get a placeholder
# instead. Shape and type are what the contract needs to convey; the actual
# figure is not.
MONEY_COLUMNS = (
    "revenue_ex_vat", "revenue_per_selling_day", "avg_document_value",
    "total_revenue_ex_vat",
)
MONEY_PLACEHOLDER = "1234567.89"

# Shown in place of a salesperson code when the Express document carries none.
# Dropping those documents instead -- which is what a plain groupby does, since
# it discards NaN keys -- silently lost 33,503.50 baht across six months and made
# the salesperson table disagree with the headline revenue. A visible bucket is
# the only version of this that reconciles.
UNASSIGNED_SALESPERSON = "ไม่ระบุ"

# Dashboard page each file backs. Drives the generated README so that the page
# mapping cannot drift out of sync with what is actually written.
PAGES = {
    "dim_product_group.csv": "shared lookup (all pages)",
    "kpi_monthly.csv": "1. Overview",
    "sales_by_group_month.csv": "1. Overview, 2. Sales",
    "sales_by_person_month.csv": "2. Sales",
    "weekly_demand.csv": "3. Demand",
    "forecast_next_4_weeks.csv": "4. Forecast",
    "trend_alerts.csv": "4. Forecast",
    "reorder_points.csv": "5. Stock",
    "stock_check.csv": "5. Stock",
    "model_accuracy.csv": "6. Accuracy",
    "model_accuracy_pooled.csv": "6. Accuracy",
}

# One-line purpose per file for the README.
PURPOSE = {
    "dim_product_group.csv":
        "Product group dimension: the 19 SKU prefixes with category, unit and "
        "forecast scope. Join key for every other file.",
    "kpi_monthly.csv":
        "Headline monthly numbers. Revenue per SELLING DAY is the honest "
        "month-on-month comparison -- months differ in trading days.",
    "sales_by_group_month.csv":
        "Revenue by month and product group. Additive across groups within a "
        "month; n_invoices is NOT (one invoice spans several groups).",
    "sales_by_person_month.csv":
        "Revenue and document count by month and salesperson. Documents with no "
        "salesperson code appear under 'ไม่ระบุ'.",
    "weekly_demand.csv":
        "Weekly quantity history per group, in the group's main unit. Complete "
        "weeks only, on a dense axis. The series the forecast is built from.",
    "forecast_next_4_weeks.csv":
        "The order plan: ma8 forecast for the coming 4 weeks with an empirical "
        "range. Filter to confidence='use for ordering' for the 12 reliable groups.",
    "trend_alerts.csv":
        "Which groups have moved, on the MEDIAN week so one large order cannot "
        "trigger a false alarm.",
    "reorder_points.csv":
        "Reorder level for the 3 intermittent groups, where forecasting does "
        "not work.",
    "stock_check.csv":
        "SKUs that have not sold recently and are worth physically checking. "
        "NOT confirmed dead stock -- there is no inventory feed.",
    "model_accuracy.csv":
        "Backtest WAPE per group and model, for both holdout periods. Evidence "
        "for how far the forecast can be trusted.",
    "model_accuracy_pooled.csv":
        "The same backtest scored once across all groups together -- the "
        "headline accuracy figure. ma8 is 14.2% on the earlier holdout and "
        "22.5% on the recent one.",
}

# Caveats that MUST travel with a file, because reading it without them
# produces a wrong conclusion rather than an incomplete one.
CAVEATS = {
    "sales_by_group_month.csv":
        "Do not sum n_invoices across groups -- an invoice containing three "
        "product groups is counted once in each.",
    "weekly_demand.csv":
        "Only each group's main unit is included, so this is demand volume, not "
        "a complete line count. Covers 97.1% of revenue.",
    "forecast_next_4_weeks.csv":
        "low_qty_4wk / high_qty_4wk are the 10th-90th percentile of past "
        "forecast error, NOT a confidence interval, and rest on 10 windows. "
        "Where range_below_forecast is true the model has over-forecast this "
        "group in 9 of 10 backtests -- order toward the low end. Blank range "
        "means too few usable windows to say.",
    "trend_alerts.csv":
        "pct_change_median is the headline. pct_change_mean is shown only so "
        "the two can be compared: when outlier_driven is true the mean moved "
        "and the median did not, which means one large order rather than a "
        "trend. stopped=true means no sales at all in the last 4 weeks.",
    "reorder_points.csv":
        "Covers 3 groups only. Uses a normal approximation that overstates the "
        "buffer on intermittent demand, so these are deliberately conservative. "
        "Check basis_window before quoting the mean.",
    "stock_check.csv":
        "Dormancy is measured from the last date in the data (as_of_date), not "
        "today. There is NO inventory data here: a SKU may be dormant because "
        "it is overstocked or because it is out of stock. This flags what to "
        "go and look at.",
    "model_accuracy.csv":
        "Two scenarios. 'earlier' is a stable period, 'recent' a declining one. "
        "Quote both -- quoting only one misrepresents the accuracy.",
    "model_accuracy_pooled.csv":
        "Do NOT reproduce these by averaging the per-group WAPEs in "
        "model_accuracy.csv. WAPE is a ratio of sums: averaging the "
        "percentages weights a group selling 68 units the same as one selling "
        "90,000 and overstates the error by more than three times.",
    "weekly_demand.csv":
        "Weeks with no sales are present as qty 0, so the series is safe to "
        "plot directly. Each group starts at its own first sale week rather "
        "than at the start of the axis -- zeros before a group existed would "
        "be indistinguishable from a group that stopped selling.",
    "sales_by_person_month.csv":
        "n_documents is a true distinct document count. Do not compare it with "
        "n_invoices in sales_by_group_month.csv, which counts an invoice once "
        "per product group it touches.",
}


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the presentation rules that every exported file must satisfy."""
    df = df.copy()
    for c in CODE_COLUMNS:
        if c in df.columns:
            # Re-pad defensively. If an upstream step ever lost the padding this
            # restores it; if it did not, this is a no-op.
            df[c] = (
                df[c].astype("string").str.strip().str.zfill(2)
                if c == "sku_prefix"
                else df[c].astype("string").str.strip()
            )
    # Booleans as true/false rather than True/False: consistent across every
    # dashboard library, and unambiguous in a CSV.
    for c in df.columns:
        if pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].map({True: "true", False: "false"})
    return df


def kpi_from_monthly_aggregates(agg: pd.DataFrame, last_date: str) -> pd.DataFrame:
    """The derived monthly columns, given the four sums they are derived from.

    Split out from build_kpi_monthly so that the API can do the grouping in
    SQL -- a role-scoped page that groups in pandas has to drag every line
    item across the network first, which on a Railway-to-Singapore hop cost
    30-100 seconds a page. The GROUPING moves; the DEFINITIONS stay here, in
    one place, so the ceo's "revenue per selling day" and a salesperson's are
    the same quantity and not two functions that agree today.

    `agg` needs: month, revenue_ex_vat, n_documents, selling_days, n_customers.
    `last_date` is the last document date in the underlying data, used only to
    decide whether the final month is partial.
    """
    out = agg.sort_values("month").reset_index(drop=True).copy()
    out["revenue_per_selling_day"] = (
        out["revenue_ex_vat"] / out["selling_days"]).round(2)
    out["avg_document_value"] = (
        out["revenue_ex_vat"] / out["n_documents"]).round(2)
    out["revenue_ex_vat"] = out["revenue_ex_vat"].round(2)
    out["pct_change_per_selling_day"] = (
        out["revenue_per_selling_day"].pct_change() * 100).round(1)
    # The final month is a partial export, not a real decline. Flag it, because
    # an unflagged partial month on a dashboard reads as a collapse.
    last = out["month"].max()
    d = pd.to_datetime(last_date)
    partial = d != (d + pd.offsets.MonthEnd(0))
    out["is_partial_month"] = (out["month"] == last) if partial else False
    return out


def build_kpi_monthly(lines: pd.DataFrame, headers: pd.DataFrame) -> pd.DataFrame:
    """Monthly headline figures, including revenue per selling day.

    Selling days matter: August 2026 had 26 trading days against July's 27, so a
    raw month-on-month comparison overstates the decline by about three points.
    The dashboard should lead with revenue_per_selling_day.
    """
    ln = lines[~lines["is_cancelled"].fillna(False)].copy()
    ln["month"] = ln["doc_date_iso"].str.slice(0, 7)
    hd = headers[~headers["is_cancelled"].fillna(False)].copy()
    hd["month"] = hd["doc_date_iso"].str.slice(0, 7)

    rev = ln.groupby("month")["amount_ex_vat"].sum().rename("revenue_ex_vat")
    docs = hd.groupby("month").agg(
        n_documents=("doc_no", "nunique"),
        selling_days=("doc_date_iso", "nunique"),
        n_customers=("customer_code", "nunique"),
    )
    agg = pd.concat([rev, docs], axis=1).reset_index()
    return kpi_from_monthly_aggregates(agg, hd["doc_date_iso"].max())


def _is_partial(headers: pd.DataFrame, month: str) -> bool:
    """True if the last month in the data stops before its calendar month end."""
    last_date = pd.to_datetime(headers["doc_date_iso"].max())
    return last_date != (last_date + pd.offsets.MonthEnd(0))


def build_by_person(lines: pd.DataFrame, headers: pd.DataFrame) -> pd.DataFrame:
    """Revenue and document count per salesperson per month.

    Built from the line and header tables, NOT by re-aggregating
    monthly_sales.csv, for two reasons that both produced wrong numbers:

      * monthly_sales is keyed by group as well as salesperson, and a groupby
        drops rows whose salesperson code is null. The 17 such rows carried
        33,503.50 baht that simply vanished -- August was short 11,031.50 and
        June short 15,246.00 against the overview total. They are now bucketed
        under UNASSIGNED_SALESPERSON, so every month reconciles exactly.

      * n_invoices in monthly_sales counts an invoice once per product group it
        touches. Summing it gave 1,444 "documents" for December against 611
        real ones. Document counts have to come from the header table, where an
        invoice exists exactly once.

    Revenue is summed from the same line-level amount_ex_vat that
    build_kpi_monthly uses, so the two agree by construction rather than by
    coincidence.
    """
    ln = lines[~lines["is_cancelled"].fillna(False)].copy()
    ln["month"] = ln["doc_date_iso"].str.slice(0, 7)
    ln["salesperson_code"] = _fill_salesperson(ln["salesperson_code"])

    hd = headers[~headers["is_cancelled"].fillna(False)].copy()
    hd["month"] = hd["doc_date_iso"].str.slice(0, 7)
    hd["salesperson_code"] = _fill_salesperson(hd["salesperson_code"])

    rev = (ln.groupby(["month", "salesperson_code"], as_index=False)["amount_ex_vat"]
             .sum().rename(columns={"amount_ex_vat": "revenue_ex_vat"}))
    docs = (hd.groupby(["month", "salesperson_code"], as_index=False)["doc_no"]
              .nunique().rename(columns={"doc_no": "n_documents"}))

    out = rev.merge(docs, on=["month", "salesperson_code"], how="outer")
    out["revenue_ex_vat"] = out["revenue_ex_vat"].fillna(0).round(2)
    out["n_documents"] = out["n_documents"].fillna(0).astype(int)
    return out.sort_values(["month", "revenue_ex_vat"],
                           ascending=[True, False]).reset_index(drop=True)


def _fill_salesperson(s: pd.Series) -> pd.Series:
    """Null or blank salesperson code -> the visible 'unassigned' bucket."""
    out = s.astype("string").str.strip()
    return out.mask(out.isna() | (out == ""), UNASSIGNED_SALESPERSON)


def build_weekly_demand(weekly: pd.DataFrame) -> pd.DataFrame:
    """Weekly quantity per group on a dense, gap-free axis.

    Two problems with the raw panel, both of which the line chart renders as a
    lie rather than as missing data:

      * A week in which a group sold nothing has no row at all. The chart then
        draws a straight segment from the week before to the week after, which
        reads as steady demand across a gap where there was none. Every group is
        reindexed onto the full week axis and missing weeks become a real zero.

      * The final week of an export is almost always partial -- here 2026-08-31
        holds a single day -- so its total is a fraction of a real week and
        shows up as a cliff. is_complete_week already marks those; they are
        dropped rather than plotted.

    Zero-filling starts at each group's FIRST SALE, not at the start of the
    axis. Group 05 launched in May; padding it back to December would draw 22
    weeks of zero demand for a product that did not exist, which looks exactly
    like a product that stopped selling -- the opposite conclusion. Trailing
    zeros after the last sale ARE filled, because those are real: that is what a
    group going quiet looks like, and it is the signal the stopped flag is
    built on.
    """
    wk = weekly[weekly["is_complete_week"] & weekly["forecast_scope"]][
        ["week_start", "sku_prefix", "group_name", "unit", "qty"]
    ].copy()
    weeks = sorted(wk["week_start"].unique())

    filled = []
    for (prefix, name, unit), g in wk.groupby(["sku_prefix", "group_name", "unit"]):
        axis = [w for w in weeks if w >= g["week_start"].min()]
        s = (g.set_index("week_start")["qty"]
              .reindex(axis, fill_value=0.0).rename("qty").reset_index())
        s = s.rename(columns={"index": "week_start"})
        s["sku_prefix"], s["group_name"], s["unit"] = prefix, name, unit
        filled.append(s)

    out = pd.concat(filled, ignore_index=True)
    # Kept so the column list matches the weekly_demand table the database
    # already has. Both are true of every surviving row by construction.
    out["is_complete_week"] = True
    out["forecast_scope"] = True
    return out[["week_start", "sku_prefix", "group_name", "unit", "qty",
                "is_complete_week", "forecast_scope"]].sort_values(
        ["sku_prefix", "week_start"]).reset_index(drop=True)


def wape(actual: pd.Series, forecast: pd.Series) -> float:
    """sum|a-f| / sum(a) -- identical to the definition in forecast_baseline."""
    denom = actual.sum()
    if denom == 0:
        return float("nan")
    return float((actual - forecast).abs().sum() / denom)


def build_pooled_accuracy(results: pd.DataFrame) -> pd.DataFrame:
    """One WAPE per scenario and model, pooled over every group.

    Pooled WAPE cannot be obtained by averaging the per-group WAPEs, which is
    what the dashboard was doing: that treats a group selling 90,000 units and a
    group selling 68 as equally important and reported ma8 at 50.9% when the
    real figure is 14.2%. WAPE is a ratio of sums, so pooling means re-summing
    the numerator and the denominator across groups and dividing once.

    Computed from data/forecast/backtest_results.csv -- the raw per-week actual
    and forecast -- because backtest_summary.csv stores percentages already
    rounded to one decimal, and reconstructing the totals from those is off by
    about 0.2 points.
    """
    rows = []
    for (scenario, model), d in results.groupby(["scenario", "model"]):
        # Weekly: can we call an individual week. 4-week total: can we call what
        # to order. Sum each origin's four weeks first, then score.
        windows = (d.groupby(["sku_prefix", "origin_week"])
                    .agg(actual=("actual", "sum"), forecast=("forecast", "sum")))
        rows.append({
            "scenario": scenario,
            "model": model,
            "wape_weekly": round(wape(d["actual"], d["forecast"]) * 100, 1),
            "wape_4wk_total": round(wape(windows["actual"], windows["forecast"]) * 100, 1),
            "holdout_qty": round(float(d.groupby(["sku_prefix", "target_week"])
                                        ["actual"].first().sum()), 0),
            "n_groups": int(d["sku_prefix"].nunique()),
        })
    return (pd.DataFrame(rows)
            .sort_values(["scenario", "wape_4wk_total"]).reset_index(drop=True))


def attach_stopped(df: pd.DataFrame, alerts: pd.DataFrame) -> pd.DataFrame:
    """Carry the stopped flag onto any table the dashboard orders from.

    stopped is computed once, in forecast_plan, and lives in trend_alerts. The
    forecast and reorder tables had no idea about it, so group 05 -- which has
    sold nothing since mid-July -- was still advertising a forecast of 251 m and
    a reorder point of 323 m. Both pages now have the flag and suppress the
    number instead of presenting it.
    """
    flag = (alerts.assign(
        stopped=alerts["stopped"].astype(str).str.lower() == "true")
        [["sku_prefix", "stopped"]])
    out = df.merge(flag, on="sku_prefix", how="left")
    out["stopped"] = out["stopped"].fillna(False)
    return out


def main() -> int:
    required = [
        CLEAN / "sales_lines.csv", CLEAN / "sales_header.csv",
        CLEAN / "monthly_sales.csv", CLEAN / "weekly_demand.csv",
        REFERENCE / "product_groups.csv",
        FORECAST / "next_4_weeks.csv", FORECAST / "trend_alerts.csv",
        FORECAST / "reorder_points.csv", FORECAST / "dead_stock_risk.csv",
        FORECAST / "backtest_summary.csv", FORECAST / "backtest_results.csv",
    ]
    missing = [f for f in required if not f.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing inputs -- run parse_express.py, forecast_baseline.py and "
            "forecast_plan.py first:\n"
            + "\n".join(f"   {f.relative_to(ROOT).as_posix()}" for f in missing)
        )

    S = dict(dtype={c: str for c in CODE_COLUMNS}, encoding="utf-8-sig")
    lines = pd.read_csv(CLEAN / "sales_lines.csv", **S)
    headers = pd.read_csv(CLEAN / "sales_header.csv", **S)
    monthly = pd.read_csv(CLEAN / "monthly_sales.csv", **S)
    weekly = pd.read_csv(CLEAN / "weekly_demand.csv", **S)
    groups = pd.read_csv(REFERENCE / "product_groups.csv", dtype=str,
                         encoding="utf-8-sig")
    plan = pd.read_csv(FORECAST / "next_4_weeks.csv", **S)
    alerts = pd.read_csv(FORECAST / "trend_alerts.csv", **S)
    rop = pd.read_csv(FORECAST / "reorder_points.csv", **S)
    dead = pd.read_csv(FORECAST / "dead_stock_risk.csv", **S)
    acc = pd.read_csv(FORECAST / "backtest_summary.csv", **S)
    bt = pd.read_csv(FORECAST / "backtest_results.csv", **S)

    groups["sku_prefix"] = groups["sku_prefix"].str.strip().str.zfill(2)

    # -- dimension ---------------------------------------------------------
    dim = groups.rename(columns={"main_unit": "unit"})[
        ["sku_prefix", "category", "group_name", "unit", "forecast_scope"]
    ].copy()
    dim["forecast_scope"] = dim["forecast_scope"].str.lower() == "true"
    dim["is_intermittent"] = dim["sku_prefix"].isin(
        plan.loc[plan["is_intermittent"].astype(str).str.lower() == "true", "sku_prefix"]
    )

    # -- sales -------------------------------------------------------------
    by_group = (
        monthly.groupby(["month", "category", "group_name"], as_index=False)
        .agg(revenue_ex_vat=("revenue_ex_vat", "sum"),
             n_invoices=("n_invoices", "sum"),
             n_lines=("n_lines", "sum"))
    )
    by_person = build_by_person(lines, headers)

    # -- demand ------------------------------------------------------------
    wk = build_weekly_demand(weekly)

    # -- stock check -------------------------------------------------------
    # Dashboard page carries only the two actionable tiers; the 30-day column
    # stays behind in data/forecast/dead_stock_risk.csv for analysis.
    stock = dead[dead["risk_level"].isin(["watch", "risk"])][[
        "sku", "product_name", "unit", "sku_prefix", "category", "group_name",
        "as_of_date", "last_sale_date", "days_since_last_sale",
        "risk_level", "recommendation",
        "total_qty_sold", "total_revenue_ex_vat", "n_documents",
    ]].copy()

    outputs = {
        "dim_product_group.csv": dim,
        "kpi_monthly.csv": build_kpi_monthly(lines, headers),
        "sales_by_group_month.csv": by_group,
        "sales_by_person_month.csv": by_person,
        "weekly_demand.csv": wk,
        "forecast_next_4_weeks.csv": attach_stopped(plan, alerts),
        "trend_alerts.csv": alerts,
        "reorder_points.csv": attach_stopped(rop, alerts),
        "stock_check.csv": stock,
        "model_accuracy.csv": acc,
        "model_accuracy_pooled.csv": build_pooled_accuracy(bt),
    }

    APP.mkdir(parents=True, exist_ok=True)
    written, locked = {}, []
    for name, df in outputs.items():
        df = _clean(df)
        try:
            df.to_csv(APP / name, index=False, encoding=OUTPUT_ENCODING)
            written[name] = df
        except PermissionError:
            locked.append(name)
    if locked:
        print("ERROR: could not write (file open in Excel?):\n"
              + "\n".join(f"   data/app/{n}" for n in locked), file=sys.stderr)
        return 1

    write_readme(written)

    print(f"Wrote {len(written)} files + README.md to data/app/\n")
    for name, df in written.items():
        print(f"   {name:<30} {len(df):>6,} rows x {len(df.columns):>2} cols"
              f"   [{PAGES.get(name, '?')}]")

    unmapped = set(written) - set(PAGES)
    if unmapped:
        print(f"\nWARNING: no dashboard page declared for: {sorted(unmapped)}",
              file=sys.stderr)
    return 0


def write_readme(written: dict[str, pd.DataFrame]) -> None:
    """Generate data/app/README.md from what was actually written.

    Generated rather than hand-written so the column lists cannot drift away
    from the files. The prose -- purpose, page mapping, caveats -- lives in the
    dicts at the top of this module, where it is version-controlled next to the
    code that produces each file.
    """
    as_of = ""
    if "stock_check.csv" in written and len(written["stock_check.csv"]):
        as_of = written["stock_check.csv"]["as_of_date"].iloc[0]

    L: list[str] = []
    L.append("# data/app/ -- dashboard data contract\n")
    L.append("Generated by `src/export_app.py`. **Do not edit by hand** -- it is")
    L.append("overwritten on every run. To change a file's contents or its")
    L.append("description, edit `src/export_app.py`.\n")
    if as_of:
        L.append(f"Data as of **{as_of}** (last document date in the Express export).\n")
    L.append("> **The CSVs are not committed** -- they hold real revenue, and the")
    L.append("> database is the durable store. This contract is committed; the files")
    L.append("> are rebuilt locally by every pipeline run. Example values for money")
    L.append(f"> columns are shown as `{MONEY_PLACEHOLDER}`, not the real figure.\n")
    L.append("## Reading these files\n")
    L.append("All files are UTF-8 with BOM (`utf-8-sig`), comma-separated, with an")
    L.append("ISO `YYYY-MM-DD` date format and `true`/`false` booleans.\n")
    L.append("> **Read code columns as text.**")
    L.append(f"> `{'`, `'.join(CODE_COLUMNS)}` are zero-padded identifiers, not numbers.")
    L.append("> Any loader that infers types will read `\"01\"` as `1`, and every join")
    L.append("> against `dim_product_group.csv` will then silently return nothing.")
    L.append("> In pandas: `pd.read_csv(path, dtype={'sku_prefix': str})`.")
    L.append("> In Power BI: set the column type to Text *before* the first join.\n")
    L.append("## Files\n")
    L.append("| File | Rows | Dashboard page |")
    L.append("|---|---|---|")
    for name, df in written.items():
        L.append(f"| `{name}` | {len(df):,} | {PAGES.get(name, '(unassigned)')} |")
    L.append("")

    for name, df in written.items():
        L.append(f"### `{name}`\n")
        L.append(f"**Page:** {PAGES.get(name, '(unassigned)')}  ")
        L.append(f"**Rows:** {len(df):,}\n")
        L.append(PURPOSE.get(name, "") + "\n")
        if name in CAVEATS:
            L.append(f"> **Caveat.** {CAVEATS[name]}\n")
        L.append("| Column | Type | Example |")
        L.append("|---|---|---|")
        for c in df.columns:
            s = df[c].dropna()
            ex = str(s.iloc[0]) if len(s) else ""
            if c in MONEY_COLUMNS and ex:
                ex = MONEY_PLACEHOLDER
            if len(ex) > 32:
                ex = ex[:29] + "..."
            ex = ex.replace("|", "\\|")
            L.append(f"| `{c}` | {_kind(df[c], c)} | {ex} |")
        L.append("")

    L.append("## Regenerating\n")
    L.append("```")
    L.append("python src/parse_express.py       # data/clean/")
    L.append("python src/forecast_baseline.py   # data/forecast/ backtest")
    L.append("python src/forecast_plan.py       # data/forecast/ plan")
    L.append("python src/export_app.py          # data/app/  (this contract)")
    L.append("```\n")
    L.append("Method and justification for every decision behind these numbers:")
    L.append("[`docs/METHODS.md`](../../docs/METHODS.md).")

    (APP / "README.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def _kind(series: pd.Series, name: str) -> str:
    if name in CODE_COLUMNS:
        return "text (code)"
    if series.dropna().isin(["true", "false"]).all() and len(series.dropna()):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if name.endswith(("_date", "_week", "week_start")) or name == "month":
        return "date"
    return "text"


if __name__ == "__main__":
    sys.exit(main())
