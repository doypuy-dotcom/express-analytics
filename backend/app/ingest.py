"""Upload -> parse -> derive -> Postgres.

The upload endpoint must reproduce the numbers the local pipeline produces. It
achieves that by calling exactly the same code -- parse_express.run_pipeline
for the parse, and the existing scripts for the derived layers -- rather than
reimplementing any of it against the database. Anything else would drift.

That equivalence is asserted in tests/test_reference_figures.py against the
reference export (44,493,479.89 / 6,088 / 168). It is deliberately NOT asserted
here: those totals are facts about one month's file, and checking them on every
upload would fail the first import of new data while the import was fine. What
this module checks on upload is internal consistency -- see data_checks().

The derived scripts (RFM, backtest, forecast, app export) read and write CSVs
on disk, so the uploaded files are staged into data/raw/ and the scripts are
run in their normal order. On Railway the filesystem is ephemeral, which is
fine: Postgres is the durable store and the CSVs are rebuilt on every upload.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
RAW = ROOT / "data" / "raw"
CLEAN = ROOT / "data" / "clean"
APP = ROOT / "data" / "app"

sys.path.insert(0, str(SRC))

# Filenames parse_express expects in data/raw/.
STAGE_NAMES = {
    "cash": "ขายเงินสด.csv",
    "credit": "ขายเงินเชื่อ.csv",
    "deposit": "รับมัดจำ.csv",
}

# Run in dependency order. forecast_baseline produces the backtest that
# forecast_plan reads; export_app reads everything.
DERIVED_SCRIPTS = [
    ("customer_rfm.py", "จัดกลุ่มลูกค้า (RFM)"),
    ("forecast_baseline.py", "ทดสอบความแม่นยำของโมเดล"),
    ("forecast_plan.py", "พยากรณ์ความต้องการ"),
    ("export_app.py", "เตรียมข้อมูลสำหรับเว็บ"),
]

# clean-table name -> database table
CLEAN_TO_DB = {
    "sales_header.csv": "sales_header",
    "sales_lines.csv": "sales_lines",
    "deposits.csv": "deposits",
    "deposit_applications.csv": "deposit_applications",
    "products.csv": "products",
    "customers.csv": "customers",
    "monthly_sales.csv": "monthly_sales",
}
CLEAN_EXTRA = {
    "customer_rfm.csv": "customer_rfm",
    "customer_segments.csv": "customer_segments",
}
APP_TO_DB = {
    # weekly_demand belongs HERE, not in CLEAN_TO_DB, even though a file of the
    # same name exists in data/clean. The clean one is the analyst's panel: it
    # keeps partial weeks and out-of-scope groups and flags them with columns
    # the dashboard does not read. Loading that into the table the /api/demand
    # endpoint serves meant production showed a one-day final week and gap-
    # ridden series while the local CSV fallback -- which reads data/app --
    # looked perfect. Two files, one name, opposite contents: the bug was
    # invisible on this machine by construction. One source now feeds both.
    "weekly_demand.csv": "weekly_demand",
    "forecast_next_4_weeks.csv": "forecast_next_4_weeks",
    "trend_alerts.csv": "trend_alerts",
    "reorder_points.csv": "reorder_points",
    "stock_check.csv": "stock_check",
    "model_accuracy.csv": "model_accuracy",
    "model_accuracy_pooled.csv": "model_accuracy_pooled",
    "dim_product_group.csv": "dim_product_group",
    "kpi_monthly.csv": "kpi_monthly",
    "sales_by_group_month.csv": "sales_by_group_month",
    "sales_by_person_month.csv": "sales_by_person_month",
}

# Tables merged by primary key (dedupe by doc_no). Everything else is a
# derived table and is replaced wholesale on each run.
UPSERT_TABLES = {
    "sales_header", "sales_lines", "deposits", "deposit_applications",
    "products", "customers",
}

CODE_DTYPES = {
    "sku_prefix": str, "salesperson_code": str, "customer_code": str,
    "doc_no": str, "sku": str, "voucher_no": str,
}


@dataclass
class Step:
    name: str
    name_th: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class IngestResult:
    ok: bool
    steps: list[Step] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    revenue_ex_vat: float = 0.0
    load: list[dict] = field(default_factory=list)
    errors_th: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    seconds: float = 0.0


def stage_files(routed: dict[str, tuple[str, bytes]]) -> dict[str, Path]:
    """Write the uploaded bytes into data/raw/ under the expected names."""
    RAW.mkdir(parents=True, exist_ok=True)
    paths = {}
    for kind, (_, raw) in routed.items():
        p = RAW / STAGE_NAMES[kind]
        p.write_bytes(raw)
        paths[kind] = p
    return paths


def run_derived() -> list[Step]:
    steps = []
    for script, name_th in DERIVED_SCRIPTS:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(SRC / script)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=900,
        )
        ok = proc.returncode == 0
        detail = "" if ok else (proc.stderr or proc.stdout)[-1500:]
        steps.append(Step(script, name_th, ok, round(time.time() - t0, 1), detail))
        if not ok:
            break
    return steps


def data_checks(tables: dict, unparsed: list, dup_docs: int) -> list[dict]:
    """Integrity checks that are valid for ANY upload, not just this dataset.

    The previous version of this function asserted the three acceptance
    figures (44,493,479.89 / 6,088 / 168). Those are correct for the current
    export and WRONG for every future one: the first upload of next month's
    data would have painted the page red while the import was perfectly fine,
    which trains the operator to ignore the one panel that is supposed to mean
    something. They now live in tests/test_reference_figures.py, where a fixed
    input has a fixed expected output and the assertion keeps its meaning.

    What replaces them are four checks that compare the upload against ITSELF:

      1. line totals reconcile to the document headers (a percentage)
      2. no unparsed rows -- every line of every report was understood
      3. duplicate documents collapsed, none left behind
      4. the date range is covered with no missing month

    Each returns a human-readable value plus a Thai detail string, because a
    bare "pass" tells the operator nothing about what was actually verified.
    """
    import parse_express  # noqa: E402  (sys.path set at module import)

    headers = tables["sales_header.csv"]
    lines = tables["sales_lines.csv"]
    apps = tables["deposit_applications.csv"]

    out: list[dict] = []

    # --- 1. lines vs headers ---------------------------------------------
    # Same implementation the CLI validation report uses, so the page and the
    # report can never disagree.
    chk = parse_express.reconcile_documents(headers, lines, apps)
    n = len(chk)
    ok = int(chk["status"].isin(("ok", "ok_vat_rounding")).sum())
    pct = round(100.0 * ok / n, 2) if n else 0.0
    bad = n - ok
    out.append({
        "check": "line_reconciliation",
        "label_th": "ยอดรวมรายการสินค้า ตรงกับหัวเอกสาร",
        "value": f"{pct:.2f}%",
        # 99.5% rather than 100%: a genuine Express export can carry a few
        # documents with blank line amounts, and failing the whole import for
        # that would be wrong. Anything below this is a parser problem.
        "passed": pct >= 99.5,
        "detail_th": f"ตรงกัน {ok:,} จาก {n:,} เอกสาร"
                     + (f" · ไม่ตรง {bad:,}" if bad else ""),
    })

    # --- 2. unparsed rows -------------------------------------------------
    n_unparsed = len(unparsed)
    out.append({
        "check": "unparsed_rows",
        "label_th": "บรรทัดที่อ่านไม่ได้",
        "value": f"{n_unparsed:,}",
        "passed": n_unparsed == 0,
        "detail_th": "อ่านได้ครบทุกบรรทัด" if n_unparsed == 0
                     else f"มี {n_unparsed:,} บรรทัดที่อ่านไม่ได้ ดู _unparsed.csv",
    })

    # --- 3. duplicates ----------------------------------------------------
    # The upsert is keyed by doc_no, so a duplicate that survived to here
    # would silently overwrite a real document rather than raise.
    left = int(headers["doc_no"].duplicated().sum())
    out.append({
        "check": "duplicates_handled",
        "label_th": "เอกสารซ้ำ",
        "value": f"{dup_docs:,}",
        "passed": left == 0,
        "detail_th": (f"รวมเอกสารซ้ำ {dup_docs:,} รายการแล้ว " if dup_docs
                      else "ไม่พบเอกสารซ้ำ ")
                     + f"· เลขที่เอกสารไม่ซ้ำกัน {headers['doc_no'].nunique():,} รายการ",
    })

    # --- 4. date coverage -------------------------------------------------
    dates = headers["doc_date_iso"].dropna()
    if len(dates):
        months = sorted(dates.str.slice(0, 7).unique())
        span = pd.period_range(months[0], months[-1], freq="M").astype(str).tolist()
        missing = [m for m in span if m not in months]
        out.append({
            "check": "date_coverage",
            "label_th": "ช่วงวันที่ของข้อมูล",
            "value": f"{dates.min()} – {dates.max()}",
            "passed": not missing,
            "detail_th": f"{len(months)} เดือน"
                         + (f" · ขาดเดือน {', '.join(missing)}" if missing
                            else " · ต่อเนื่องไม่ขาดเดือน"),
        })
    else:
        out.append({
            "check": "date_coverage", "label_th": "ช่วงวันที่ของข้อมูล",
            "value": "-", "passed": False, "detail_th": "ไม่พบวันที่ในเอกสาร",
        })

    return out


def record_batch(conn, res: "IngestResult", routed: dict, uploaded_by: str | None) -> None:
    """Write one upload_batches row -- the audit trail.

    Every upsert overwrites rows keyed by doc_no, so after the fact there is
    nothing in the data itself to say which upload produced it or who ran it.
    This row is that record. It is written inside the same transaction as the
    load, so a failed load leaves no batch claiming success.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into upload_batches "
            "(uploaded_by, filenames, n_cash, n_credit, n_deposit, "
            " revenue_ex_vat, status, message) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uploaded_by,
                ", ".join(sorted(name for name, _ in routed.values())),
                res.counts.get("cash"),
                res.counts.get("credit"),
                res.counts.get("deposit"),
                res.revenue_ex_vat,
                "ok" if all(c["passed"] for c in res.checks) else "check_failed",
                "; ".join(res.errors_th) or None,
            ),
        )


def ingest(routed: dict[str, tuple[str, bytes]], write_db: bool = True,
           uploaded_by: str | None = None) -> IngestResult:
    import parse_express  # noqa: E402  (path set above)

    t_all = time.time()
    res = IngestResult(ok=True)

    # --- 1. stage + parse -------------------------------------------------
    t0 = time.time()
    try:
        paths = stage_files(routed)
        parsed = parse_express.run_pipeline(
            paths["cash"], paths["credit"], paths["deposit"]
        )
    except Exception as exc:
        res.ok = False
        res.steps.append(Step("parse", "อ่านไฟล์รายงาน", False, round(time.time() - t0, 1), str(exc)))
        res.errors_th.append(f"อ่านไฟล์ไม่สำเร็จ: {exc}")
        return res
    res.steps.append(Step("parse", "อ่านไฟล์รายงาน", True, round(time.time() - t0, 1)))

    tables = parsed["tables"]
    headers = tables["sales_header.csv"]
    lines = tables["sales_lines.csv"]

    live = lines[~lines["is_cancelled"].fillna(False)]
    live_headers = headers[~headers["is_cancelled"].fillna(False)]

    res.counts = {
        "cash": int((headers["sale_type"] == "cash").sum()),
        "credit": int((headers["sale_type"] == "credit").sum()),
        "deposit": int(len(tables["deposits.csv"])),
        "sales_lines": int(len(lines)),
        # Customer CODES, not rows of customers.csv. That table is keyed by
        # customer_name, so it lost the handful of codes whose name is blank
        # in the export and reported 1,478 where the customers page -- which
        # is built per code -- showed 1,483. One entity, two numbers, and the
        # smaller one was on the page that claimed to say what was imported.
        # Counting codes on non-cancelled headers is the same grain that
        # customer_rfm uses, so the two pages now agree by construction.
        "customer_codes": int(live_headers["customer_code"].nunique()),
        "products": int(len(tables["products.csv"])),
        "unparsed": int(len(parsed["unparsed"])),
    }
    res.revenue_ex_vat = round(float(live["amount_ex_vat"].sum()), 2)
    res.checks = data_checks(tables, parsed["unparsed"],
                             int(parsed.get("dupes_collapsed", 0)))

    # --- 2. write clean CSVs (the derived scripts read them) --------------
    t0 = time.time()
    CLEAN.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(CLEAN / name, index=False, encoding="utf-8-sig")
    if parsed["unparsed"]:
        pd.DataFrame(
            parsed["unparsed"], columns=["source_file", "line_no", "reason", "text"]
        ).to_csv(CLEAN / "_unparsed.csv", index=False, encoding="utf-8-sig")
    res.steps.append(Step("write_clean", "บันทึกตารางที่สะอาดแล้ว", True, round(time.time() - t0, 1)))

    # --- 3. derived layers ------------------------------------------------
    derived = run_derived()
    res.steps.extend(derived)
    if any(not s.ok for s in derived):
        failed = next(s for s in derived if not s.ok)
        res.ok = False
        res.errors_th.append(f"ขั้นตอน «{failed.name_th}» ไม่สำเร็จ")
        return res

    # --- 4. load to Postgres ---------------------------------------------
    if write_db:
        from . import db
        t0 = time.time()
        try:
            with db.connect() as conn:
                for fname, table in {**CLEAN_TO_DB, **CLEAN_EXTRA}.items():
                    path = CLEAN / fname
                    if not path.exists():
                        continue
                    df = pd.read_csv(path, dtype=CODE_DTYPES)
                    fn = db.upsert if table in UPSERT_TABLES else db.replace
                    res.load.append(fn(conn, table, df))
                for fname, table in APP_TO_DB.items():
                    path = APP / fname
                    if not path.exists():
                        continue
                    df = pd.read_csv(path, dtype=CODE_DTYPES)
                    res.load.append(db.replace(conn, table, df))
                record_batch(conn, res, routed, uploaded_by)
                conn.commit()
        except Exception as exc:
            res.ok = False
            res.steps.append(Step("load_db", "บันทึกลงฐานข้อมูล", False, round(time.time() - t0, 1), str(exc)))
            res.errors_th.append(f"บันทึกลงฐานข้อมูลไม่สำเร็จ: {exc}")
            return res
        res.steps.append(Step("load_db", "บันทึกลงฐานข้อมูล", True, round(time.time() - t0, 1)))

    res.seconds = round(time.time() - t_all, 1)
    res.ok = all(c["passed"] for c in res.checks) and res.ok
    if not all(c["passed"] for c in res.checks):
        bad = [c["label_th"] for c in res.checks if not c["passed"]]
        res.errors_th.append(
            "ตัวเลขตรวจสอบไม่ตรงกับค่าอ้างอิง: " + ", ".join(bad)
        )
    return res
