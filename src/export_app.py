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
        "Revenue by month and salesperson.",
    "weekly_demand.csv":
        "Weekly quantity history per group, in the group's main unit. Complete "
        "weeks only. The series the forecast is built from.",
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
    out = pd.concat([rev, docs], axis=1).reset_index()
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
    out["is_partial_month"] = out["month"] == last if _is_partial(hd, last) else False
    return out.sort_values("month").reset_index(drop=True)


def _is_partial(headers: pd.DataFrame, month: str) -> bool:
    """True if the last month in the data stops before its calendar month end."""
    last_date = pd.to_datetime(headers["doc_date_iso"].max())
    return last_date != (last_date + pd.offsets.MonthEnd(0))


def main() -> int:
    required = [
        CLEAN / "sales_lines.csv", CLEAN / "sales_header.csv",
        CLEAN / "monthly_sales.csv", CLEAN / "weekly_demand.csv",
        REFERENCE / "product_groups.csv",
        FORECAST / "next_4_weeks.csv", FORECAST / "trend_alerts.csv",
        FORECAST / "reorder_points.csv", FORECAST / "dead_stock_risk.csv",
        FORECAST / "backtest_summary.csv",
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
    by_person = (
        monthly.groupby(["month", "salesperson_code"], as_index=False)
        .agg(revenue_ex_vat=("revenue_ex_vat", "sum"),
             n_invoices=("n_invoices", "sum"))
    )

    # -- demand ------------------------------------------------------------
    wk = weekly[
        (weekly["is_complete_week"]) & (weekly["forecast_scope"])
    ][["week_start", "sku_prefix", "group_name", "unit", "qty"]].copy()

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
        "forecast_next_4_weeks.csv": plan,
        "trend_alerts.csv": alerts,
        "reorder_points.csv": rop,
        "stock_check.csv": stock,
        "model_accuracy.csv": acc,
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
