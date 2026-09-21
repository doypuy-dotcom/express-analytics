"""The acceptance criteria, asserted against the reference export.

These three figures --

    revenue ex VAT   44,493,479.89
    cash documents        6,088
    credit documents        168

-- are the agreed definition of "the pipeline is correct". They used to be
checked on every upload, which was wrong in a specific way: they are facts
about ONE export. The first upload of next month's data would have failed them
and painted the อัปโหลดข้อมูล page red while the import was perfectly healthy,
which teaches the operator that red means nothing.

Here they keep their meaning, because here the input is fixed. A test that
runs against a known file and expects a known total is a regression test; the
same numbers on a live upload page are a guess about the future.

Run:  python -m pytest tests/ -v
      (or: python tests/test_reference_figures.py  for a no-pytest run)

Skips rather than fails when data/raw/ is absent -- the raw Express exports are
gitignored, so CI and a fresh clone do not have them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

RAW = ROOT / "data" / "raw"
FILE_CASH = RAW / "ขายเงินสด.csv"
FILE_CREDIT = RAW / "ขายเงินเชื่อ.csv"
FILE_DEPOSIT = RAW / "รับมัดจำ.csv"

REVENUE_EX_VAT = 44_493_479.89
N_CASH = 6_088
N_CREDIT = 168

pytestmark = pytest.mark.skipif(
    not (FILE_CASH.exists() and FILE_CREDIT.exists() and FILE_DEPOSIT.exists()),
    reason="data/raw/ Express exports not present (they are gitignored)",
)


@pytest.fixture(scope="module")
def parsed():
    import parse_express
    return parse_express.run_pipeline(FILE_CASH, FILE_CREDIT, FILE_DEPOSIT)


def test_revenue_ex_vat(parsed):
    """Revenue is sum(amount_ex_vat) over non-cancelled lines.

    NOT header goods_value, which is net of applied deposit vouchers.
    """
    lines = parsed["tables"]["sales_lines.csv"]
    live = lines[~lines["is_cancelled"].fillna(False)]
    assert round(float(live["amount_ex_vat"].sum()), 2) == REVENUE_EX_VAT


def test_cash_document_count(parsed):
    headers = parsed["tables"]["sales_header.csv"]
    assert int((headers["sale_type"] == "cash").sum()) == N_CASH


def test_credit_document_count(parsed):
    headers = parsed["tables"]["sales_header.csv"]
    assert int((headers["sale_type"] == "credit").sum()) == N_CREDIT


def test_no_unparsed_rows(parsed):
    """Every line of all three reports was understood by the parser."""
    assert parsed["unparsed"] == []


def test_documents_reconcile(parsed):
    """Line items add up to their header, for every document.

    This is the check the upload page now shows, so a regression here would
    also show up live. Asserting it against the reference export means the
    threshold on the page is grounded in a known-good number.
    """
    import parse_express
    t = parsed["tables"]
    chk = parse_express.reconcile_documents(
        t["sales_header.csv"], t["sales_lines.csv"], t["deposit_applications.csv"]
    )
    bad = chk[~chk["status"].isin(("ok", "ok_vat_rounding"))]
    assert len(bad) == 0, f"{len(bad)} documents do not reconcile:\n{bad.head(10)}"


def test_customer_code_count_matches_rfm_grain(parsed):
    """The upload page and the customers page must count the same thing.

    customers.csv is keyed by customer_name and so drops codes with a blank
    name -- that is the 1,478 vs 1,483 split. The upload page reports distinct
    customer CODES on non-cancelled headers, which is exactly the grain
    customer_rfm is built at.
    """
    headers = parsed["tables"]["sales_header.csv"]
    live = headers[~headers["is_cancelled"].fillna(False)]
    assert live["customer_code"].nunique() == 1_483


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
