"""Upload -> parse -> derive -> Postgres.

The upload endpoint must reproduce the numbers the local pipeline produces:
revenue 44,493,479.89, 6,088 cash documents, 168 credit. It achieves that by
calling exactly the same code -- parse_express.run_pipeline for the parse, and
the existing scripts for the derived layers -- rather than reimplementing any
of it against the database. Anything else would drift.

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
    "weekly_demand.csv": "weekly_demand",
    "monthly_sales.csv": "monthly_sales",
}
CLEAN_EXTRA = {
    "customer_rfm.csv": "customer_rfm",
    "customer_segments.csv": "customer_segments",
}
APP_TO_DB = {
    "forecast_next_4_weeks.csv": "forecast_next_4_weeks",
    "trend_alerts.csv": "trend_alerts",
    "reorder_points.csv": "reorder_points",
    "stock_check.csv": "stock_check",
    "model_accuracy.csv": "model_accuracy",
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


def reference_checks(counts: dict, revenue: float) -> list[dict]:
    """The acceptance criteria, asserted on every upload.

    These are the figures the whole project is validated against. Checking
    them here means a regression shows up in the upload response rather than
    silently populating the dashboard with wrong numbers.
    """
    expected = [
        ("revenue_ex_vat", revenue, 44_493_479.89, 0.01, "รายได้ (ไม่รวม VAT)"),
        ("cash_documents", counts.get("cash", 0), 6088, 0, "เอกสารขายเงินสด"),
        ("credit_documents", counts.get("credit", 0), 168, 0, "เอกสารขายเงินเชื่อ"),
    ]
    out = []
    for key, actual, want, tol, label_th in expected:
        passed = abs(float(actual) - float(want)) <= tol
        out.append({
            "check": key, "label_th": label_th,
            "actual": actual, "expected": want, "passed": passed,
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

    res.counts = {
        "cash": int((headers["sale_type"] == "cash").sum()),
        "credit": int((headers["sale_type"] == "credit").sum()),
        "deposit": int(len(tables["deposits.csv"])),
        "sales_lines": int(len(lines)),
        "customers": int(len(tables["customers.csv"])),
        "products": int(len(tables["products.csv"])),
        "unparsed": int(len(parsed["unparsed"])),
    }
    live = lines[~lines["is_cancelled"].fillna(False)]
    res.revenue_ex_vat = round(float(live["amount_ex_vat"].sum()), 2)
    res.checks = reference_checks(res.counts, res.revenue_ex_vat)

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
