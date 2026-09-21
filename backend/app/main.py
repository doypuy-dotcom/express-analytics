"""FastAPI backend for the Express analytics dashboard.

Seven pages, one endpoint group each, plus upload and auth.

Data source: Postgres when DATABASE_URL is set, otherwise the CSVs in
data/app/ and data/clean/. The fallback is deliberate -- it means the site can
be deployed and demonstrated before Supabase is provisioned, which is what
lets step 1 (a working URL) happen independently of step 2 (the database).
/health always reports which source is live, so nobody is misled about
whether they are looking at uploaded data or the shipped snapshot.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .auth import User, current_user
from .detect import detect_set
from .ingest import ingest

ROOT = Path(__file__).resolve().parent.parent.parent
APP_DATA = ROOT / "data" / "app"
CLEAN = ROOT / "data" / "clean"

CODE_DTYPES = {
    "sku_prefix": str, "salesperson_code": str, "customer_code": str,
    "doc_no": str, "sku": str, "voucher_no": str,
}

MAX_UPLOAD_MB = 64

app = FastAPI(title="Express Analytics API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- helpers

def use_db() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def clean_records(df: pd.DataFrame) -> list[dict]:
    """JSON-safe records.

    NaN/NaT/Inf are not valid JSON. Left alone they serialise as the literal
    NaN, which every strict JSON parser in the browser rejects -- an empty
    page with a console error, which is a miserable thing to debug live.
    """
    out = df.replace([np.inf, -np.inf], np.nan)
    return out.astype(object).where(pd.notna(out), None).to_dict("records")


def from_csv(folder: Path, name: str) -> pd.DataFrame:
    path = folder / name
    if not path.exists():
        raise HTTPException(503, f"ยังไม่มีข้อมูล ({name}) กรุณาอัปโหลดไฟล์ก่อน")
    return pd.read_csv(path, dtype=CODE_DTYPES)


def table(name: str, csv_folder: Path, csv_name: str,
          order_by: str | None = None, limit: int | None = None) -> list[dict]:
    """Read a table from Postgres, or the CSV snapshot when there is no DB."""
    if use_db():
        q = f"select * from {name}"
        if order_by:
            q += f" order by {order_by}"
        if limit:
            q += f" limit {int(limit)}"
        try:
            return db.fetch(q)
        except Exception as exc:
            raise HTTPException(503, f"อ่านฐานข้อมูลไม่สำเร็จ: {exc}")
    df = from_csv(csv_folder, csv_name)
    if order_by:
        col = order_by.split()[0]
        if col in df.columns:
            df = df.sort_values(col, ascending="desc" not in order_by.lower())
    if limit:
        df = df.head(limit)
    return clean_records(df)


# ---------------------------------------------------------------- system

@app.get("/health")
def health() -> dict:
    ok, msg = (True, "csv-snapshot")
    if use_db():
        ok, msg = db.healthy()
    return {
        "status": "ok" if ok else "degraded",
        "source": "postgres" if use_db() else "csv",
        "database": msg,
        "anonymous_access": os.environ.get("ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes"),
        "version": app.version,
    }


@app.post("/admin/init-db")
def init_db(user: User = Depends(current_user)) -> dict:
    if not use_db():
        raise HTTPException(400, "DATABASE_URL is not set")
    db.ensure_schema()
    return {"ok": True, "message": "schema applied"}


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role}


# ---------------------------------------------------------------- upload

@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    user: User = Depends(current_user),
) -> JSONResponse:
    """Upload the three Express reports. Type is detected from content."""
    if not files:
        raise HTTPException(400, "ไม่พบไฟล์ที่อัปโหลด")

    payload: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        raw = await f.read()
        total += len(raw)
        if total > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"ไฟล์รวมกันใหญ่เกิน {MAX_UPLOAD_MB} MB")
        payload.append((f.filename or "unnamed.csv", raw))

    routed, errors = detect_set(payload)
    if errors:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "stage": "detect", "errors_th": errors,
                     "detected": sorted(routed.keys())},
        )

    result = ingest(routed, write_db=use_db(), uploaded_by=user.email)

    body: dict[str, Any] = {
        "ok": result.ok,
        "seconds": result.seconds,
        "counts": result.counts,
        "revenue_ex_vat": result.revenue_ex_vat,
        "checks": result.checks,
        "steps": [s.__dict__ for s in result.steps],
        "load": result.load,
        "errors_th": result.errors_th,
        "detected": {k: v[0] for k, v in routed.items()},
    }
    return JSONResponse(status_code=200 if result.ok else 422, content=body)


# ------------------------------------------------------------ 1. overview

@app.get("/api/overview")
def overview(user: User = Depends(current_user)) -> dict:
    kpi = table("kpi_monthly", APP_DATA, "kpi_monthly.csv", order_by="month")
    groups = table("sales_by_group_month", APP_DATA, "sales_by_group_month.csv",
                   order_by="month")
    total_rev = sum(float(r.get("revenue_ex_vat") or 0) for r in kpi)
    latest = kpi[-1] if kpi else {}
    return {
        "kpi_monthly": kpi,
        "sales_by_group_month": groups,
        "totals": {
            "revenue_ex_vat": round(total_rev, 2),
            "months": len(kpi),
            "documents": sum(int(r.get("n_documents") or 0) for r in kpi),
            "latest_month": latest.get("month"),
            "latest_revenue": latest.get("revenue_ex_vat"),
            "latest_pct_change_per_selling_day": latest.get("pct_change_per_selling_day"),
        },
    }


# --------------------------------------------------------------- 2. sales

@app.get("/api/sales")
def sales(user: User = Depends(current_user)) -> dict:
    return {
        "by_group_month": table("sales_by_group_month", APP_DATA,
                                "sales_by_group_month.csv", order_by="month"),
        "by_person_month": table("sales_by_person_month", APP_DATA,
                                 "sales_by_person_month.csv", order_by="month"),
    }


# -------------------------------------------------------------- 3. demand

@app.get("/api/demand")
def demand(user: User = Depends(current_user)) -> dict:
    return {
        "weekly": table("weekly_demand", APP_DATA, "weekly_demand.csv",
                        order_by="week_start"),
        "groups": table("dim_product_group", APP_DATA, "dim_product_group.csv",
                        order_by="sku_prefix"),
    }


# ------------------------------------------------------------ 4. forecast

@app.get("/api/forecast")
def forecast(user: User = Depends(current_user)) -> dict:
    return {
        "next_4_weeks": table("forecast_next_4_weeks", APP_DATA,
                              "forecast_next_4_weeks.csv", order_by="sku_prefix"),
        "trend_alerts": table("trend_alerts", APP_DATA, "trend_alerts.csv",
                              order_by="sku_prefix"),
    }


# --------------------------------------------------------------- 5. stock

@app.get("/api/stock")
def stock(user: User = Depends(current_user)) -> dict:
    return {
        "reorder_points": table("reorder_points", APP_DATA, "reorder_points.csv",
                                order_by="sku_prefix"),
        "stock_check": table("stock_check", APP_DATA, "stock_check.csv",
                             order_by="days_since_last_sale desc"),
    }


# ------------------------------------------------------------ 6. accuracy

@app.get("/api/accuracy")
def accuracy(user: User = Depends(current_user)) -> dict:
    """Per-group detail plus the pooled headline.

    The pooled figures are computed in the pipeline, not here and not in the
    browser. WAPE is a ratio of sums, so pooling it means re-summing the
    numerator and denominator across groups -- averaging the per-group
    percentages, which is what the page used to do, reported 50.9% where the
    real ma8 error is 14.2%.
    """
    return {
        "model_accuracy": table("model_accuracy", APP_DATA, "model_accuracy.csv"),
        "pooled": table("model_accuracy_pooled", APP_DATA,
                        "model_accuracy_pooled.csv", order_by="scenario"),
    }


# ----------------------------------------------------------- 7. customers

@app.get("/api/customers")
def customers(limit: int = 200, user: User = Depends(current_user)) -> dict:
    return {
        "segments": table("customer_segments", CLEAN, "customer_segments.csv",
                          order_by="revenue desc"),
        "top_customers": table("customer_rfm", CLEAN, "customer_rfm.csv",
                               order_by="monetary desc", limit=limit),
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "Express Analytics API",
        "docs": "/docs",
        "health": "/health",
        "pages": ["overview", "sales", "demand", "forecast", "stock",
                  "accuracy", "customers"],
    }
