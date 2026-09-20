"""Customer RFM scoring and segmentation for the customers page.

Recency / Frequency / Monetary, scored 1-5, then mapped to named segments.

Two design decisions worth knowing before reading the numbers:

1. Scores are RANK-based (quintiles of the rank), not value-based. This
   business has a long tail of one-visit cash customers, so a value-based cut
   (pd.qcut) collapses: the frequency distribution has so many ties at 1 that
   the bin edges are not unique and the call raises. Ranking survives ties and
   always yields five populated bands.

2. Recency is measured from the LAST DATE IN THE EXPORT, not from today, for
   the same reason as the stock check (METHODS 8.4) -- measuring from today
   would silently convert a reporting lag into an apparent customer problem.

Plain pandas, no external services. Outputs:
    data/clean/customer_rfm.csv       one row per customer
    data/clean/customer_segments.csv  one row per segment (the page summary)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CLEAN = ROOT / "data" / "clean"
OUTPUT_ENCODING = "utf-8-sig"

SCORE_BANDS = 5

# R, F, M each 1-5. Rules are evaluated TOP-DOWN and the first match wins, so
# the order of this list is part of the definition. `fm` is the mean of the
# frequency and monetary scores -- the standard reduction that keeps the grid
# two-dimensional (how recently vs how valuable) instead of 125 cells.
SEGMENT_RULES = [
    ("แชมเปี้ยน",        "Champions",           lambda r, fm: (r >= 4) & (fm >= 4)),
    ("ลูกค้าประจำ",       "Loyal",               lambda r, fm: (r >= 3) & (fm >= 3)),
    ("มีแนวโน้มประจำ",    "Potential Loyalist",  lambda r, fm: (r >= 4) & (fm >= 2)),
    ("ลูกค้าใหม่",        "New",                 lambda r, fm: (r >= 4) & (fm < 2)),
    ("ต้องรักษาไว้",      "Cannot Lose",         lambda r, fm: (r <= 2) & (fm >= 4)),
    ("เสี่ยงหาย",         "At Risk",             lambda r, fm: (r <= 2) & (fm >= 3)),
    ("ใกล้หลับ",          "Hibernating",         lambda r, fm: (r <= 2) & (fm >= 2)),
    ("หลับแล้ว",          "Lost",                lambda r, fm: (r <= 2) & (fm < 2)),
    ("ทั่วไป",            "Needs Attention",     lambda r, fm: pd.Series(True, index=r.index)),
]


def band(series: pd.Series, higher_is_better: bool = True, k: int = SCORE_BANDS) -> pd.Series:
    """Score a series 1..k by rank percentile.

    Rank-based so that heavy ties (frequency == 1 for most walk-in customers)
    cannot produce duplicate bin edges the way value-based quintiles do.
    """
    pct = series.rank(method="average", pct=True)
    if not higher_is_better:
        pct = 1.0 - pct + 1e-12  # epsilon keeps the best value inside band k
    return np.clip(np.ceil(pct * k), 1, k).astype(int)


def build_rfm(headers: pd.DataFrame, lines: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """One row per customer, with R/F/M values, 1-5 scores and a segment."""
    headers = headers[~headers["is_cancelled"].fillna(False)].copy()
    lines = lines[~lines["is_cancelled"].fillna(False)].copy()

    headers["doc_date_iso"] = pd.to_datetime(headers["doc_date_iso"])
    as_of = headers["doc_date_iso"].max()

    # Monetary from LINES (amount_ex_vat is the agreed revenue metric, METHODS
    # 2.1); frequency and recency from HEADERS, because a document is one visit
    # however many lines it carries.
    money = lines.groupby("customer_code")["amount_ex_vat"].sum()
    per_cust = headers.groupby("customer_code").agg(
        customer_name=("customer_name", "last"),
        frequency=("doc_no", "nunique"),
        last_purchase=("doc_date_iso", "max"),
        first_purchase=("doc_date_iso", "min"),
    )
    per_cust["monetary"] = per_cust.index.map(money).astype(float).round(2)
    per_cust["recency_days"] = (as_of - per_cust["last_purchase"]).dt.days
    per_cust["tenure_days"] = (as_of - per_cust["first_purchase"]).dt.days
    per_cust["avg_order_value"] = (
        per_cust["monetary"] / per_cust["frequency"]
    ).round(2)

    out = per_cust.reset_index()
    out["r_score"] = band(out["recency_days"], higher_is_better=False)
    out["f_score"] = band(out["frequency"], higher_is_better=True)
    out["m_score"] = band(out["monetary"], higher_is_better=True)
    out["rfm_score"] = (
        out["r_score"].astype(str) + out["f_score"].astype(str) + out["m_score"].astype(str)
    )
    out["fm_score"] = ((out["f_score"] + out["m_score"]) / 2).round(1)

    out["segment"] = pd.NA
    out["segment_th"] = pd.NA
    unassigned = out["segment"].isna()
    for th, en, rule in SEGMENT_RULES:
        hit = unassigned & rule(out["r_score"], out["fm_score"])
        out.loc[hit, "segment"] = en
        out.loc[hit, "segment_th"] = th
        unassigned = out["segment"].isna()

    out["revenue_share_pct"] = (
        out["monetary"] / out["monetary"].sum() * 100
    ).round(3)
    out = out.sort_values("monetary", ascending=False).reset_index(drop=True)
    out["revenue_rank"] = out.index + 1
    out["cumulative_revenue_pct"] = out["revenue_share_pct"].cumsum().round(2)

    for c in ("last_purchase", "first_purchase"):
        out[c] = out[c].dt.strftime("%Y-%m-%d")

    cols = [
        "customer_code", "customer_name", "segment", "segment_th",
        "recency_days", "frequency", "monetary", "avg_order_value",
        "r_score", "f_score", "m_score", "fm_score", "rfm_score",
        "last_purchase", "first_purchase", "tenure_days",
        "revenue_rank", "revenue_share_pct", "cumulative_revenue_pct",
    ]
    return out[cols], as_of.strftime("%Y-%m-%d")


def build_segments(rfm: pd.DataFrame) -> pd.DataFrame:
    """One row per segment -- what the customers page charts."""
    g = rfm.groupby(["segment", "segment_th"], dropna=False).agg(
        n_customers=("customer_code", "count"),
        revenue=("monetary", "sum"),
        avg_recency_days=("recency_days", "mean"),
        avg_frequency=("frequency", "mean"),
        avg_order_value=("avg_order_value", "mean"),
    ).reset_index()

    g["revenue"] = g["revenue"].round(2)
    g["avg_recency_days"] = g["avg_recency_days"].round(0).astype(int)
    g["avg_frequency"] = g["avg_frequency"].round(1)
    g["avg_order_value"] = g["avg_order_value"].round(2)
    g["customer_share_pct"] = (g["n_customers"] / g["n_customers"].sum() * 100).round(1)
    g["revenue_share_pct"] = (g["revenue"] / g["revenue"].sum() * 100).round(1)
    # Revenue concentration is the reason to look at this table at all: a
    # segment holding 5% of customers and 40% of revenue is the whole story.
    g["revenue_per_customer"] = (g["revenue"] / g["n_customers"]).round(2)
    return g.sort_values("revenue", ascending=False).reset_index(drop=True)


def main() -> int:
    headers = pd.read_csv(CLEAN / "sales_header.csv", dtype={"customer_code": str})
    lines = pd.read_csv(CLEAN / "sales_lines.csv", dtype={"customer_code": str})

    rfm, as_of = build_rfm(headers, lines)
    segments = build_segments(rfm)

    locked = []
    for name, df in (("customer_rfm.csv", rfm), ("customer_segments.csv", segments)):
        try:
            df.to_csv(CLEAN / name, index=False, encoding=OUTPUT_ENCODING)
        except PermissionError:
            locked.append(name)
    if locked:
        print(f"ERROR: close these in Excel and re-run: {', '.join(locked)}", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"CUSTOMER RFM   ({len(rfm):,} customers, as of {as_of})")
    print("=" * 78)
    print("  Recency measured from the last date in the export, not from today.")
    print("  Scores are rank quintiles, so each band holds ~20% of customers.\n")
    print(f"  {'segment':<20}{'n':>6}{'cust%':>7}{'revenue':>14}{'rev%':>7}"
          f"{'rec':>6}{'freq':>6}")
    for r in segments.itertuples():
        print(f"  {r.segment:<20}{r.n_customers:>6,}{r.customer_share_pct:>6.1f}%"
              f"{r.revenue:>14,.0f}{r.revenue_share_pct:>6.1f}%"
              f"{r.avg_recency_days:>6}{r.avg_frequency:>6.1f}")

    top = rfm.head(10)
    print(f"\n  top 10 customers = {top['revenue_share_pct'].sum():.1f}% of revenue")
    n20 = max(1, int(len(rfm) * 0.20))
    print(f"  top 20% ({n20:,} customers) = "
          f"{rfm.head(n20)['revenue_share_pct'].sum():.1f}% of revenue")

    print("\nWrote to data/clean/:")
    print(f"   customer_rfm.csv           {len(rfm):>7,} rows x {len(rfm.columns)} cols")
    print(f"   customer_segments.csv      {len(segments):>7,} rows x {len(segments.columns)} cols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
