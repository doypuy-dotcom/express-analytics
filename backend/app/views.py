"""Role-scoped views, aggregated in the database.

The company-wide tables -- kpi_monthly, sales_by_group_month, customer_rfm,
customer_segments -- are pre-aggregated with no salesperson_code on them.
There is no correct way to serve a slice of a total that has already been
summed, so for anyone who is not the ceo they are not served at all. Their
equivalents are recomputed here from the rows that user is allowed to see.

WHERE the work happens matters as much as what it computes. The first version
of this module pulled the allowed rows out of Postgres and grouped them in
pandas. It was correct and it was unusable: the API runs on Railway and the
database is in ap-southeast-1, so a manager's overview page dragged 18,713
line items across the Pacific and took 29 seconds; /api/sales took 146 and
died. Every query here now groups in SQL and returns tens of rows, not tens
of thousands.

What that costs is a second place where a metric could be defined, so the
split is deliberate:

    the GROUPING (a sum, a count, a count-distinct) is in the SQL below
    the DEFINITIONS (revenue per selling day, the RFM bands, the dense week
    axis, the partial-month flag) stay in the pipeline functions and are
    CALLED from here

So a salesperson's "revenue per selling day" is computed by the same line of
code as the ceo's. Nothing in this file decides what a metric means.

The ceo does not come through this module at all: the ceo reads the
pre-aggregated tables directly, which is the exact path that produces the
accepted 44,493,479.89.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

from . import db
from .scope import Scope

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
CLEAN = ROOT / "data" / "clean"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CODE_DTYPES = {
    "sku_prefix": str, "salesperson_code": str, "customer_code": str,
    "doc_no": str, "sku": str, "voucher_no": str,
}

# Every scoped query carries these two conditions. Written once so that a new
# query cannot forget the cancellation filter and report cancelled invoices as
# revenue -- which is a 1.2m error on this dataset.
LIVE = "coalesce(is_cancelled, false) = false"
MINE = "salesperson_code = any(%s)"


def _use_db() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _csv(name: str, codes: list[str]) -> pd.DataFrame:
    """CSV-fallback equivalent of a scoped read. Local development only."""
    df = pd.read_csv(CLEAN / name, dtype=CODE_DTYPES)
    df = df[df["salesperson_code"].isin(codes)]
    return df[~df["is_cancelled"].fillna(False)].reset_index(drop=True)


def _frame(sql: str, codes: list[str], columns: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=columns)
    rows = db.fetch(sql, (list(codes),))
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows)


def company_as_of() -> str | None:
    """Last document date in the whole export.

    Read unscoped on purpose. It is one date, not anybody's revenue, and it is
    what recency and the partial-month flag are measured against; measuring
    each user from a different day would make two users' numbers
    incomparable.
    """
    try:
        if _use_db():
            rows = db.fetch("select max(doc_date_iso) as d from sales_header")
            return rows[0]["d"] if rows else None
        return str(pd.read_csv(CLEAN / "sales_header.csv",
                               usecols=["doc_date_iso"])["doc_date_iso"].max())
    except Exception:
        return None


# ------------------------------------------------------------------ views

def kpi_monthly(scope: Scope) -> list[dict]:
    """Monthly headline figures for the allowed rows.

    Revenue comes from the LINES and the document/day/customer counts from the
    HEADERS -- the same split build_kpi_monthly uses, and it is not
    cosmetic: counting documents on the line table multiplies every invoice by
    its number of line items.
    """
    import export_app
    codes = scope.codes or []
    if not codes:
        return []
    if not _use_db():
        return _records(export_app.build_kpi_monthly(
            _csv("sales_lines.csv", codes), _csv("sales_header.csv", codes)))

    rev = _frame(
        f"select substr(doc_date_iso, 1, 7) as month, "
        f"       sum(amount_ex_vat) as revenue_ex_vat "
        f"from sales_lines where {MINE} and {LIVE} group by 1",
        codes, ["month", "revenue_ex_vat"])
    docs = _frame(
        f"select substr(doc_date_iso, 1, 7) as month, "
        f"       count(distinct doc_no) as n_documents, "
        f"       count(distinct doc_date_iso) as selling_days, "
        f"       count(distinct customer_code) as n_customers "
        f"from sales_header where {MINE} and {LIVE} group by 1",
        codes, ["month", "n_documents", "selling_days", "n_customers"])
    if rev.empty or docs.empty:
        return []
    agg = rev.merge(docs, on="month", how="outer")
    agg["revenue_ex_vat"] = agg["revenue_ex_vat"].astype(float).fillna(0.0)
    return _records(export_app.kpi_from_monthly_aggregates(
        agg, company_as_of() or agg["month"].max() + "-28"))


def by_group_month(scope: Scope) -> list[dict]:
    """Revenue per product group per month.

    n_invoices counts a document once per group it touches -- the same
    convention the company-wide table uses, so the column means the same
    thing on both. It is therefore NOT summable into a document count, which
    is why the overview page takes its document count from kpi_monthly.
    """
    codes = scope.codes or []
    if not codes:
        return []
    if not _use_db():
        ln = _csv("sales_lines.csv", codes)
        if ln.empty:
            return []
        ln["month"] = ln["doc_date_iso"].str.slice(0, 7)
        out = (ln.groupby(["month", "category", "group_name"], as_index=False)
                 .agg(revenue_ex_vat=("amount_ex_vat", "sum"),
                      n_invoices=("doc_no", "nunique"),
                      n_lines=("doc_no", "size")))
    else:
        out = _frame(
            f"select substr(doc_date_iso, 1, 7) as month, category, group_name, "
            f"       round(sum(amount_ex_vat)::numeric, 2)::float8 as revenue_ex_vat, "
            f"       count(distinct doc_no) as n_invoices, count(*) as n_lines "
            f"from sales_lines where {MINE} and {LIVE} "
            f"group by 1, 2, 3 order by 1, 4 desc",
            codes, ["month", "category", "group_name", "revenue_ex_vat",
                    "n_invoices", "n_lines"])
    return _records(out)


def by_person_month(scope: Scope) -> list[dict]:
    """Revenue and document count per salesperson per month.

    No UNASSIGNED bucket here, unlike the company table: a scoped user is
    filtered to a list of real codes, so a null code cannot be in range. Only
    the ceo ever sees the unattributed documents, and the ceo reads the
    pre-aggregated table.
    """
    codes = scope.codes or []
    if not codes:
        return []
    if not _use_db():
        import export_app
        return _records(export_app.build_by_person(
            _csv("sales_lines.csv", codes), _csv("sales_header.csv", codes)))
    rev = _frame(
        f"select substr(doc_date_iso, 1, 7) as month, salesperson_code, "
        f"       round(sum(amount_ex_vat)::numeric, 2)::float8 as revenue_ex_vat "
        f"from sales_lines where {MINE} and {LIVE} group by 1, 2",
        codes, ["month", "salesperson_code", "revenue_ex_vat"])
    docs = _frame(
        f"select substr(doc_date_iso, 1, 7) as month, salesperson_code, "
        f"       count(distinct doc_no) as n_documents "
        f"from sales_header where {MINE} and {LIVE} group by 1, 2",
        codes, ["month", "salesperson_code", "n_documents"])
    if rev.empty:
        return []
    out = rev.merge(docs, on=["month", "salesperson_code"], how="outer")
    out["revenue_ex_vat"] = out["revenue_ex_vat"].astype(float).fillna(0.0).round(2)
    out["n_documents"] = out["n_documents"].fillna(0).astype(int)
    return _records(out.sort_values(["month", "revenue_ex_vat"],
                                    ascending=[True, False]))


def weekly_demand(scope: Scope) -> tuple[list[dict], list[dict]]:
    """(weekly panel, group dimension) for the allowed rows.

    The weekly panel is grouped in SQL and then handed to the pipeline's
    build_weekly_demand, so the axis rules -- zero-fill from each group's
    first sale, drop the partial final week -- are applied by one
    implementation rather than two. Those rules are the reason the chart does
    not draw a straight line across a gap where nothing sold.
    """
    import export_app
    codes = scope.codes or []
    if not codes:
        return [], []
    if not _use_db():
        ln = _csv("sales_lines.csv", codes)
        if ln.empty:
            return [], []
        d = pd.to_datetime(ln["doc_date_iso"], errors="coerce")
        ln = ln[d.notna()].copy()
        d = d[d.notna()]
        ln["week_start"] = (d - pd.to_timedelta(d.dt.weekday, unit="D")
                            ).dt.strftime("%Y-%m-%d")
        panel = (ln.groupby(["week_start", "sku_prefix", "group_name", "unit"],
                            as_index=False)["qty"].sum())
        last = str(d.max().date())
        dim = (ln[["sku_prefix", "category", "group_name", "unit"]]
               .drop_duplicates("sku_prefix"))
    else:
        panel = _frame(
            f"select to_char(date_trunc('week', doc_date_iso::date), 'YYYY-MM-DD') "
            f"         as week_start, "
            f"       sku_prefix, group_name, unit, sum(qty)::float8 as qty "
            f"from sales_lines where {MINE} and {LIVE} and doc_date_iso is not null "
            f"group by 1, 2, 3, 4",
            codes, ["week_start", "sku_prefix", "group_name", "unit", "qty"])
        dim = _frame(
            f"select sku_prefix, min(category) as category, "
            f"       min(group_name) as group_name, min(unit) as unit "
            f"from sales_lines where {MINE} and {LIVE} and sku_prefix is not null "
            f"group by 1 order by 1",
            codes, ["sku_prefix", "category", "group_name", "unit"])
        last = company_as_of()
    if panel.empty:
        return [], []

    # date_trunc('week') is Monday-based in Postgres, matching the pipeline.
    d = pd.to_datetime(last)
    last_week = (d - pd.Timedelta(days=int(d.weekday()))).strftime("%Y-%m-%d")
    # A final week that does not reach Sunday holds a fraction of a week's
    # sales and plots as a cliff.
    partial = d.weekday() != 6
    panel["is_complete_week"] = ~((panel["week_start"] == last_week) & partial)
    panel["forecast_scope"] = True
    panel["qty"] = panel["qty"].astype(float)

    dim = dim.copy()
    dim["forecast_scope"] = True
    dim["is_intermittent"] = False
    return _records(export_app.build_weekly_demand(panel)), _records(dim)


def customers(scope: Scope, as_of: str | None, limit: int = 200) -> dict:
    """RFM over the user's OWN invoices only.

    Two things are deliberately true here:

      * monetary is the revenue on THIS user's invoices to that customer, not
        the customer's company-wide total. A customer who buys 10m a year but
        1m from this rep shows 1m. Showing the company total would tell a rep
        what their colleagues sold, which is the thing being prevented.
      * the R/F/M bands are ranked within the user's own customer list, so
        "Champion" means a champion of that rep's book. Ranking against the
        company distribution would leak the company distribution.

    Recency is still measured from the last day in the WHOLE export, not the
    rep's last sale -- otherwise a rep who has been quiet for two months sees
    every one of their customers scored as freshly active.
    """
    import customer_rfm as rfm_mod
    codes = scope.codes or []
    if not codes:
        return {"segments": [], "top_customers": [], "as_of": as_of}

    if not _use_db():
        hd, ln = _csv("sales_header.csv", codes), _csv("sales_lines.csv", codes)
        if hd.empty or ln.empty:
            return {"segments": [], "top_customers": [], "as_of": as_of}
        rfm, used = rfm_mod.build_rfm(hd, ln, as_of=as_of)
    else:
        # Monetary from the LINES, frequency and dates from the HEADERS: a
        # document is one visit however many line items it carries.
        money = _frame(
            f"select customer_code, sum(amount_ex_vat)::float8 as monetary "
            f"from sales_lines where {MINE} and {LIVE} "
            f"and customer_code is not null group by 1",
            codes, ["customer_code", "monetary"])
        visits = _frame(
            f"select customer_code, max(customer_name) as customer_name, "
            f"       count(distinct doc_no) as frequency, "
            f"       max(doc_date_iso) as last_purchase, "
            f"       min(doc_date_iso) as first_purchase "
            f"from sales_header where {MINE} and {LIVE} "
            f"and customer_code is not null group by 1",
            codes, ["customer_code", "customer_name", "frequency",
                    "last_purchase", "first_purchase"])
        if money.empty or visits.empty:
            return {"segments": [], "top_customers": [], "as_of": as_of}
        per_cust = visits.merge(money, on="customer_code", how="left")
        per_cust["monetary"] = per_cust["monetary"].astype(float).fillna(0.0)
        rfm, used = rfm_mod.score_rfm(per_cust, as_of or per_cust["last_purchase"].max())

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
