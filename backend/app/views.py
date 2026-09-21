"""Role-scoped views, rebuilt from sales_lines.

The company-wide tables -- kpi_monthly, sales_by_group_month, customer_rfm,
customer_segments -- are pre-aggregated with no salesperson_code on them.
There is no correct way to serve a slice of a total that has already been
summed, so for anyone who is not the ceo they are not served at all. Their
equivalents are recomputed here from the rows that user is allowed to see.

The recomputation calls the SAME builder functions the pipeline uses
(export_app.build_kpi_monthly, customer_rfm.build_rfm, ...) with a filtered
input. That is the point: a salesperson's "revenue per selling day" is then
the same quantity as the ceo's, computed by the same code, and not a
second definition that happens to have the same label. Nothing here reimplements
a metric.

The ceo does not come through this module. The ceo reads the pre-aggregated
tables directly, which is both faster and the exact path that produces the
accepted 44,493,479.89.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from . import db
from .scope import Scope

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
CLEAN = ROOT / "data" / "clean"
APP_DATA = ROOT / "data" / "app"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CODE_DTYPES = {
    "sku_prefix": str, "salesperson_code": str, "customer_code": str,
    "doc_no": str, "sku": str, "voucher_no": str,
}

# Columns actually needed downstream. Pulling `select *` on sales_lines is
# 31,578 rows x 21 columns over the public internet on every page load.
LINE_COLS = [
    "doc_no", "line_no", "sku", "product_name", "qty", "unit", "amount",
    "amount_ex_vat", "is_cancelled", "sku_prefix", "category", "group_name",
    "doc_date_iso", "salesperson_code", "customer_code",
]
HEADER_COLS = [
    "doc_no", "sale_type", "doc_date_iso", "customer_code", "customer_name",
    "salesperson_code", "goods_value", "is_cancelled",
]


def _use_db() -> bool:
    import os
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _read_scoped(table: str, cols: list[str], csv_name: str,
                 codes: list[str]) -> pd.DataFrame:
    """Rows of `table` whose salesperson_code is in `codes`.

    An empty `codes` short-circuits to an empty frame rather than issuing
    `= any('{}')`. Same result, but it also means a user with no codes cannot
    cause a full table scan by logging in.
    """
    if not codes:
        return pd.DataFrame(columns=cols)
    if _use_db():
        collist = ", ".join(f'"{c}"' for c in cols)
        rows = db.fetch(
            f"select {collist} from {table} "
            f"where salesperson_code = any(%s) "
            f"and coalesce(is_cancelled, false) = false",
            (list(codes),),
        )
        return pd.DataFrame(rows, columns=cols)
    folder = CLEAN
    df = pd.read_csv(folder / csv_name, dtype=CODE_DTYPES)
    df = df[df["salesperson_code"].isin(codes)]
    return df[~df["is_cancelled"].fillna(False)][cols].reset_index(drop=True)


def scoped_frames(scope: Scope) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(headers, lines) the user may see. Never called for the ceo."""
    codes = scope.codes or []
    headers = _read_scoped("sales_header", HEADER_COLS, "sales_header.csv", codes)
    lines = _read_scoped("sales_lines", LINE_COLS, "sales_lines.csv", codes)
    for df in (headers, lines):
        if "is_cancelled" not in df.columns:
            df["is_cancelled"] = False
        df["is_cancelled"] = df["is_cancelled"].fillna(False).astype(bool)
    for c in ("amount_ex_vat", "qty", "amount"):
        if c in lines.columns:
            lines[c] = pd.to_numeric(lines[c], errors="coerce")
    if "goods_value" in headers.columns:
        headers["goods_value"] = pd.to_numeric(headers["goods_value"], errors="coerce")
    return headers, lines


def company_as_of() -> str | None:
    """Last document date in the whole export, for recency scoring.

    Read unscoped on purpose. It is a single date, not anybody's revenue, and
    measuring every user's recency from a different day would make two users'
    segment labels incomparable.
    """
    try:
        if _use_db():
            rows = db.fetch("select max(doc_date_iso) as d from sales_header")
            return rows[0]["d"] if rows else None
        df = pd.read_csv(CLEAN / "sales_header.csv", usecols=["doc_date_iso"])
        return str(df["doc_date_iso"].max())
    except Exception:
        return None


# ------------------------------------------------------------------ views

def kpi_monthly(headers: pd.DataFrame, lines: pd.DataFrame) -> list[dict]:
    import export_app
    if lines.empty or headers.empty:
        return []
    return _records(export_app.build_kpi_monthly(lines, headers))


def by_group_month(lines: pd.DataFrame) -> list[dict]:
    """Revenue per product group per month, for the allowed rows only.

    Built straight off the lines rather than by re-aggregating
    monthly_sales.csv the way export_app does for the company table, because
    monthly_sales has no salesperson_code to filter on. n_invoices counts a
    document once per group it touches -- the same convention the company
    table uses, so the two columns mean the same thing.
    """
    if lines.empty:
        return []
    ln = lines.copy()
    ln["month"] = ln["doc_date_iso"].str.slice(0, 7)
    out = (ln.groupby(["month", "category", "group_name"], as_index=False)
             .agg(revenue_ex_vat=("amount_ex_vat", "sum"),
                  n_invoices=("doc_no", "nunique"),
                  n_lines=("doc_no", "size")))
    out["revenue_ex_vat"] = out["revenue_ex_vat"].round(2)
    return _records(out.sort_values(["month", "revenue_ex_vat"],
                                    ascending=[True, False]))


def by_person_month(headers: pd.DataFrame, lines: pd.DataFrame) -> list[dict]:
    import export_app
    if lines.empty:
        return []
    return _records(export_app.build_by_person(lines, headers))


def weekly_demand(lines: pd.DataFrame) -> list[dict]:
    """Weekly quantity per group, on the same dense zero-filled axis.

    The company table is built from data/clean/weekly_demand.csv, which is a
    panel with no salesperson_code. The panel is rebuilt here from the allowed
    lines and then handed to the SAME build_weekly_demand, so the axis rules
    (zero-fill from first sale, drop the partial final week) are applied once
    and identically.
    """
    import export_app
    if lines.empty:
        return []
    ln = lines.dropna(subset=["doc_date_iso"]).copy()
    d = pd.to_datetime(ln["doc_date_iso"], errors="coerce")
    ln = ln[d.notna()]
    d = d[d.notna()]
    # Week starting Monday, matching the pipeline's weekly panel.
    ln["week_start"] = (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    last = d.max()
    panel = (ln.groupby(["week_start", "sku_prefix", "group_name", "unit"],
                        as_index=False)["qty"].sum())
    # The final week of an export stops mid-week; its total is a fraction of a
    # real week and plots as a cliff. Same rule as the pipeline.
    last_week = (last - pd.Timedelta(days=int(last.weekday()))).strftime("%Y-%m-%d")
    partial = last != (last + pd.offsets.Week(weekday=6) if last.weekday() != 6 else last)
    panel["is_complete_week"] = ~((panel["week_start"] == last_week) & partial)
    panel["forecast_scope"] = True
    return _records(export_app.build_weekly_demand(panel))


def product_groups(lines: pd.DataFrame) -> list[dict]:
    """The group dimension, limited to groups this user has actually sold."""
    if lines.empty:
        return []
    out = (lines[["sku_prefix", "category", "group_name", "unit"]]
           .dropna(subset=["sku_prefix"]).drop_duplicates("sku_prefix"))
    out["forecast_scope"] = True
    out["is_intermittent"] = False
    return _records(out.sort_values("sku_prefix"))


def customers(headers: pd.DataFrame, lines: pd.DataFrame,
              as_of: str | None, limit: int = 200) -> dict:
    """RFM over the user's OWN invoices only.

    Two things are deliberately true here:

      * monetary is the revenue on THIS user's invoices to that customer, not
        the customer's company-wide total. A customer who buys 10m a year but
        1m from this rep shows 1m. Showing the company total would tell a rep
        what their colleagues sold, which is the thing being prevented.
      * the R/F/M bands are ranked within the user's own customer list, so a
        "Champion" means a champion of that rep's book. Ranking against the
        company distribution would leak the company distribution.

    Both are noted on the page, because a number that silently means something
    narrower than its label is worse than no number.
    """
    import customer_rfm as rfm_mod
    if headers.empty or lines.empty:
        return {"segments": [], "top_customers": [], "as_of": as_of}
    rfm, used = rfm_mod.build_rfm(headers, lines, as_of=as_of)
    segments = rfm_mod.build_segments(rfm)
    top = rfm.sort_values("monetary", ascending=False).head(limit)
    return {
        "segments": _records(segments.sort_values("revenue", ascending=False)),
        "top_customers": _records(top),
        "as_of": used,
    }


def _records(df: pd.DataFrame) -> list[dict]:
    import numpy as np
    out = df.replace([np.inf, -np.inf], np.nan)
    return out.astype(object).where(pd.notna(out), None).to_dict("records")
