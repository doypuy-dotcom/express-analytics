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
from pydantic import BaseModel

from . import db, views
from .auth import User, current_user
from .detect import detect_set
from .ingest import ingest
from .scope import ROLES, Scope, current_scope, require_ceo

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
def me(scope: Scope = Depends(current_scope)) -> dict:
    """Identity, role, and which pages the role may open.

    The frontend uses `pages` to decide what to put in the navigation. That is
    cosmetic only -- every endpoint enforces the same rule server-side, so
    hand-typing a URL for a hidden page returns 403 rather than data.
    """
    return scope.as_json()


# ---------------------------------------------------------------- upload

@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    scope: Scope = Depends(current_scope),
) -> JSONResponse:
    """Upload the three Express reports. Type is detected from content.

    ceo only. An upload replaces the whole dataset for every user, so it is
    the one action where a wrong role is not a privacy problem but a data
    problem.
    """
    scope.require("upload")
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

    result = ingest(routed, write_db=use_db(), uploaded_by=scope.email)

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
def overview(scope: Scope = Depends(current_scope)) -> dict:
    scope.require("overview")
    if scope.unrestricted:
        kpi = table("kpi_monthly", APP_DATA, "kpi_monthly.csv", order_by="month")
        groups = table("sales_by_group_month", APP_DATA,
                       "sales_by_group_month.csv", order_by="month")
    else:
        kpi = views.kpi_monthly(scope)
        groups = views.by_group_month(scope)
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
        "scope": _scope_note(scope),
    }


# --------------------------------------------------------------- 2. sales

@app.get("/api/sales")
def sales(scope: Scope = Depends(current_scope)) -> dict:
    scope.require("sales")
    if scope.unrestricted:
        return {
            "by_group_month": table("sales_by_group_month", APP_DATA,
                                    "sales_by_group_month.csv", order_by="month"),
            "by_person_month": table("sales_by_person_month", APP_DATA,
                                     "sales_by_person_month.csv", order_by="month"),
            "scope": _scope_note(scope),
        }
    return {
        "by_group_month": views.by_group_month(scope),
        "by_person_month": views.by_person_month(scope),
        "scope": _scope_note(scope),
    }


# -------------------------------------------------------------- 3. demand

@app.get("/api/demand")
def demand(scope: Scope = Depends(current_scope)) -> dict:
    scope.require("demand")
    if scope.unrestricted:
        return {
            "weekly": table("weekly_demand", APP_DATA, "weekly_demand.csv",
                            order_by="week_start"),
            "groups": table("dim_product_group", APP_DATA,
                            "dim_product_group.csv", order_by="sku_prefix"),
            "scope": _scope_note(scope),
        }
    weekly, groups = views.weekly_demand(scope)
    return {
        "weekly": weekly,
        "groups": groups,
        "scope": _scope_note(scope),
    }


# ------------------------------------------------------------ 4. forecast

@app.get("/api/forecast")
def forecast(scope: Scope = Depends(current_scope)) -> dict:
    """Company-level forecast.

    Unlike revenue, this cannot be sliced by salesperson: the models were
    fitted on total demand per product group, and a per-rep share of a fitted
    forecast is not a forecast of anything. So it is served whole or not at
    all, and `scope.level` says which -- the page prints "ทั้งบริษัท" so that
    a rep does not read a company number as their own.

    For the sales role this is behind SALES_CAN_SEE_FORECAST, default off.
    """
    scope.require("forecast")
    return {
        "next_4_weeks": table("forecast_next_4_weeks", APP_DATA,
                              "forecast_next_4_weeks.csv", order_by="sku_prefix"),
        "trend_alerts": table("trend_alerts", APP_DATA, "trend_alerts.csv",
                              order_by="sku_prefix"),
        "scope": _scope_note(scope, level="company"),
    }


# --------------------------------------------------------------- 5. stock

@app.get("/api/stock")
def stock(scope: Scope = Depends(current_scope)) -> dict:
    """Company stock position. Same reasoning as forecast, same flag."""
    scope.require("stock")
    return {
        "reorder_points": table("reorder_points", APP_DATA, "reorder_points.csv",
                                order_by="sku_prefix"),
        "stock_check": table("stock_check", APP_DATA, "stock_check.csv",
                             order_by="days_since_last_sale desc"),
        "scope": _scope_note(scope, level="company"),
    }


# ------------------------------------------------------------ 6. accuracy

@app.get("/api/accuracy")
def accuracy(scope: Scope = Depends(current_scope)) -> dict:
    """Per-group detail plus the pooled headline. Manager and ceo only.

    The pooled figures are computed in the pipeline, not here and not in the
    browser. WAPE is a ratio of sums, so pooling it means re-summing the
    numerator and denominator across groups -- averaging the per-group
    percentages, which is what the page used to do, reported 50.9% where the
    real ma8 error is 14.2%.
    """
    scope.require("accuracy")
    return {
        "model_accuracy": table("model_accuracy", APP_DATA, "model_accuracy.csv"),
        "pooled": table("model_accuracy_pooled", APP_DATA,
                        "model_accuracy_pooled.csv", order_by="scenario"),
        "scope": _scope_note(scope, level="company"),
    }


# ----------------------------------------------------------- 7. customers

@app.get("/api/customers")
def customers(limit: int = 200, scope: Scope = Depends(current_scope)) -> dict:
    scope.require("customers")
    if scope.unrestricted:
        return {
            "segments": table("customer_segments", CLEAN,
                              "customer_segments.csv", order_by="revenue desc"),
            "top_customers": table("customer_rfm", CLEAN, "customer_rfm.csv",
                                   order_by="monetary desc", limit=limit),
            "scope": _scope_note(scope),
        }
    out = views.customers(scope, views.company_as_of(), limit=limit)
    out["scope"] = _scope_note(scope)
    return out


def _scope_note(scope: Scope, level: str | None = None) -> dict:
    """What the numbers in this payload cover.

    Sent with every page so the UI can label a scoped figure as scoped. A
    salesperson's overview total is not the company's, and a page that shows
    12,534,217.13 under the heading "รายได้รวม" with nothing else on it is
    a number that means something different from what it says.
    """
    return {
        "role": scope.role,
        "level": level or ("company" if scope.unrestricted else "own"),
        "codes": scope.codes,
        "label_th": (
            "ทั้งบริษัท" if (level == "company" or scope.unrestricted)
            else ("ทีมของคุณ (" + ", ".join(scope.codes or []) + ")"
                  if scope.role == "sales_manager"
                  else "เฉพาะยอดของคุณ (" + (scope.own_code or "-") + ")")
        ),
    }


# ------------------------------------------------------- 8. admin (ceo only)

class RoleAssignment(BaseModel):
    user_id: str
    role: str
    salesperson_code: str | None = None
    team: list[str] = []
    email: str | None = None


@app.get("/api/admin/users")
def admin_users(scope: Scope = Depends(require_ceo)) -> dict:
    """Every account, with its role, code and team.

    Driven from auth.users so that a person who has signed up but never been
    given a role still appears -- otherwise the ceo cannot grant one and the
    new user sees an empty site with no explanation.
    """
    if not use_db():
        raise HTTPException(400, "ต้องมีฐานข้อมูล (DATABASE_URL) จึงจะจัดการสิทธิ์ได้")
    try:
        users = db.fetch(
            "select u.id::text as user_id, u.email, u.created_at, "
            "       p.role, p.salesperson_code "
            "from   auth.users u left join user_profiles p on p.user_id = u.id "
            "order by u.created_at"
        )
    except Exception:
        # No auth schema (plain Postgres): fall back to the profile table.
        users = db.fetch(
            "select user_id::text as user_id, email, created_at, role, "
            "salesperson_code from user_profiles order by created_at")
    teams: dict[str, list[str]] = {}
    for r in db.fetch("select manager_user_id::text as m, salesperson_code "
                      "from team_members order by salesperson_code"):
        teams.setdefault(r["m"], []).append(r["salesperson_code"])
    for u in users:
        u["team"] = teams.get(u["user_id"], [])
        u["role"] = u.get("role") or "none"
        u["created_at"] = str(u.get("created_at") or "")
    codes = [r["salesperson_code"] for r in db.fetch(
        "select distinct salesperson_code from sales_header "
        "where salesperson_code is not null order by salesperson_code")]
    return {"users": users, "salesperson_codes": codes,
            "roles": list(ROLES), "you": scope.user_id}


@app.post("/api/admin/users")
def admin_set_role(body: RoleAssignment,
                   scope: Scope = Depends(require_ceo)) -> dict:
    """Assign a role, a code and a team, in one transaction.

    Guards, in order of how badly they would bite:

      * the ceo cannot demote themselves -- with no ceo left, nobody can ever
        grant the role back, and the only fix is a manual SQL statement
        against production.
      * a sales user must have a code. Without one their scope is [] and they
        get a working login onto an entirely empty dashboard, which reads as
        "the site is broken" rather than "you are not set up".
      * a manager's team replaces wholesale rather than merging, so removing
        somebody from a team is possible at all.
    """
    if not use_db():
        raise HTTPException(400, "ต้องมีฐานข้อมูล (DATABASE_URL) จึงจะจัดการสิทธิ์ได้")
    if body.role not in ROLES:
        raise HTTPException(400, f"บทบาทไม่ถูกต้อง: {body.role}")
    if body.user_id == scope.user_id and body.role != "ceo":
        raise HTTPException(
            400, "ไม่สามารถลดสิทธิ์ของตัวเองได้ "
                 "(ต้องให้บัญชีอื่นเป็น ceo ก่อน มิฉะนั้นจะไม่มีใครแก้สิทธิ์ได้อีก)")
    code = (body.salesperson_code or "").strip() or None
    if body.role == "sales" and not code:
        raise HTTPException(400, "บทบาท sales ต้องระบุรหัสพนักงานขาย")
    if body.role != "sales":
        code = None
    team = sorted({c.strip() for c in body.team if c.strip()}) if body.role == "sales_manager" else []
    if body.role == "sales_manager" and not team:
        raise HTTPException(400, "บทบาท sales_manager ต้องระบุรหัสพนักงานขายในทีมอย่างน้อย 1 รหัส")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into user_profiles (user_id, email, role, salesperson_code) "
                "values (%s, %s, %s, %s) "
                "on conflict (user_id) do update set role = excluded.role, "
                "  email = coalesce(excluded.email, user_profiles.email), "
                "  salesperson_code = excluded.salesperson_code, updated_at = now()",
                (body.user_id, body.email, body.role, code),
            )
            cur.execute("delete from team_members where manager_user_id = %s",
                        (body.user_id,))
            for c in team:
                cur.execute(
                    "insert into team_members (manager_user_id, salesperson_code) "
                    "values (%s, %s) on conflict do nothing", (body.user_id, c))
        conn.commit()
    return {"ok": True, "user_id": body.user_id, "role": body.role,
            "salesperson_code": code, "team": team}


@app.get("/")
def root() -> dict:
    return {
        "service": "Express Analytics API",
        "docs": "/docs",
        "health": "/health",
        "pages": ["overview", "sales", "demand", "forecast", "stock",
                  "accuracy", "customers"],
    }
