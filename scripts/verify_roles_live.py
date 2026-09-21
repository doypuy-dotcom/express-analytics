"""The required role tests, run against the LIVE deployed API.

The pytest suite stubs the JWT decode, which is the right trade for a test
that has to run in CI -- but it means the suite never proves that the
deployed service, with real Supabase tokens over the real network, enforces
any of this. That is the claim being made to the owner, so it gets checked
where the claim applies. Every user here signs in for real.

CREDENTIALS COME FROM THE ENVIRONMENT AND ARE NEVER PRINTED.

An earlier version of this file carried the test passwords as literals. That
is wrong twice over: a password in a file is a password in git history and in
every backup of the working tree, and it silently rots the moment the owner
rotates it -- which is exactly what happened. Set them per run:

    ROLE_TEST_SALES03_PASSWORD=...  (email: ROLE_TEST_SALES03_EMAIL)
    ROLE_TEST_MANAGER_PASSWORD=...  (email: ROLE_TEST_MANAGER_EMAIL)
    ROLE_TEST_CEO_PASSWORD=...      (email: ROLE_TEST_CEO_EMAIL)

A role with no password is SKIPPED, loudly, and the run reports
"PASS WITH SKIPS" -- never "ALL PASS". A skipped check is not a passed check,
and a harness that blurs the two is worse than no harness. Correctness for a
skipped role is still covered by tests/test_row_level_visibility.py, which
stubs identity and so needs no password at all.

Exit code is 0 only when everything ran and everything passed.
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# backend/.env holds SUPABASE_URL / SUPABASE_ANON_KEY. setdefault, so a value
# already exported for this run wins over the file.
env_file = ROOT / "backend" / ".env"
if env_file.exists():
    for line in io.open(env_file, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SB = os.environ["SUPABASE_URL"].rstrip("/")
ANON = os.environ["SUPABASE_ANON_KEY"]
API = os.environ.get("API_URL", "https://backend-production-f146.up.railway.app")

# The ceo grant is held by exactly one real login; the throwaway
# ceo@express.local was deleted by the owner.
ACCOUNTS = {
    "sales03": ("ROLE_TEST_SALES03_EMAIL", "sales03@express.local",
                "ROLE_TEST_SALES03_PASSWORD"),
    "manager": ("ROLE_TEST_MANAGER_EMAIL", "manager@express.local",
                "ROLE_TEST_MANAGER_PASSWORD"),
    "ceo":     ("ROLE_TEST_CEO_EMAIL",     "doykong1369@gmail.com",
                "ROLE_TEST_CEO_PASSWORD"),
}

REVENUE_ALL = 44_493_479.89
REVENUE_03 = 12_534_217.13
REVENUE_TEAM = 26_567_914.93

out, failures, skips = [], [], []
TIMINGS = []


def signin(email, password):
    body = json.dumps({"email": email, "password": password}).encode()
    r = urllib.request.Request(f"{SB}/auth/v1/token?grant_type=password",
                               data=body, method="POST")
    r.add_header("apikey", ANON)
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())["access_token"]


def call(tok, path, method="GET", data=None):
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Authorization", f"Bearer {tok}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            status, body = resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {"raw": raw[:200].decode("utf-8", "replace")}
    TIMINGS.append((path, status, time.time() - t0))
    return status, body


def check(label, got, want):
    ok = got == want
    out.append(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    out.append(f"          got {got!r}   want {want!r}")
    if not ok:
        failures.append(label)
    return ok


def skip(label, why):
    out.append(f"  [SKIP] {label}")
    out.append(f"          {why}")
    skips.append(label)


# --- sign in -------------------------------------------------------------
tok = {}
for role, (email_var, email_default, pw_var) in ACCOUNTS.items():
    email = os.environ.get(email_var, email_default)
    pw = os.environ.get(pw_var, "")
    if not pw:
        skips.append(f"sign-in: {role}")
        out.append(f"NO CREDENTIAL for {role} ({email}) -- set {pw_var}. "
                   f"Every check needing {role} is skipped.")
        continue
    try:
        tok[role] = signin(email, pw)
    except urllib.error.HTTPError as e:
        # Deliberately does not echo the response body: Supabase repeats the
        # submitted email, and a bad-password body is not worth the risk.
        failures.append(f"sign-in: {role}")
        out.append(f"SIGN-IN FAILED for {role} ({email}): HTTP {e.code}")

out.append(f"signed in: {', '.join(tok) or '(none)'}")
out.append(f"API: {API}")
out.append("")

# --- 1 -------------------------------------------------------------------
out.append("TEST 1  sales 03 totals = sum of code 03 only")
if "sales03" in tok:
    s, b = call(tok["sales03"], "/api/overview")
    check("HTTP", s, 200)
    check("revenue_ex_vat", round(b["totals"]["revenue_ex_vat"], 2), REVENUE_03)
    check("scope codes", b["scope"]["codes"], ["03"])
else:
    skip("sales 03 totals", "no sales03 credential")
out.append("")

# --- 2 -------------------------------------------------------------------
out.append("TEST 2  manager totals = 03 + 04 only")
if "manager" in tok:
    s, b = call(tok["manager"], "/api/overview")
    check("HTTP", s, 200)
    check("revenue_ex_vat", round(b["totals"]["revenue_ex_vat"], 2), REVENUE_TEAM)
    check("scope codes", sorted(b["scope"]["codes"]), ["03", "04"])
else:
    skip("manager totals", "no manager credential")
out.append("")

# --- 3 -------------------------------------------------------------------
out.append("TEST 3  ceo totals = 44,493,479.89")
if "ceo" in tok:
    s, b = call(tok["ceo"], "/api/overview")
    check("HTTP", s, 200)
    check("revenue_ex_vat", round(b["totals"]["revenue_ex_vat"], 2), REVENUE_ALL)
    check("scope unrestricted", b["scope"]["codes"], None)
else:
    skip("ceo totals", "no ceo credential")
out.append("")

# --- 4 -------------------------------------------------------------------
out.append("TEST 4  sales 03 sees no row, customer revenue or total "
           "from another code")
if "sales03" in tok:
    s, b = call(tok["sales03"], "/api/sales")
    codes = sorted({r["salesperson_code"] for r in b["by_person_month"]})
    check("salesperson codes on the sales page", codes, ["03"])
    check("their monthly figures re-add to their own total",
          round(sum(float(r["revenue_ex_vat"]) for r in b["by_person_month"]), 2),
          REVENUE_03)

    leaks = []
    for path in ("/api/overview", "/api/sales", "/api/demand",
                 "/api/customers?limit=2000"):
        s2, b2 = call(tok["sales03"], path)
        blob = json.dumps(b2)
        for needle in ("44493479.89", "26567914.93", "14033697.8"):
            if needle in blob:
                leaks.append(f"{path} contains {needle}")
    check("no company/other-code total in any payload", leaks, [])

    # A customer both 03 and someone else sold to: the served figure must be
    # 03's invoices only, which is strictly less than the customer's total.
    # This one needs the ceo as the company-wide baseline.
    if "ceo" in tok:
        s3, cust = call(tok["sales03"], "/api/customers?limit=2000")
        s4, ceocust = call(tok["ceo"], "/api/customers?limit=2000")
        mine = {r["customer_code"]: float(r["monetary"])
                for r in cust["top_customers"]}
        theirs = {r["customer_code"]: float(r["monetary"])
                  for r in ceocust["top_customers"]}
        shared = [c for c in mine if c in theirs and theirs[c] > mine[c] + 0.01]
        check("at least one shared customer exists (test is not vacuous)",
              len(shared) > 0, True)
        over = [c for c in mine if c in theirs and mine[c] > theirs[c] + 0.01]
        check("no customer served above their company-wide total", over, [])
        if shared:
            c = shared[0]
            out.append(f"          e.g. customer {c}: sales03 sees {mine[c]:,.2f}, "
                       f"company total is {theirs[c]:,.2f}")
        check("sales03 customer count <= company customer count",
              len(mine) <= len(theirs), True)
    else:
        skip("per-customer revenue vs the company baseline",
             "needs the ceo token for the company-wide comparison; "
             "covered offline by test_no_company_figure_on_any_page_"
             "the_sales_role_can_reach")
else:
    skip("sales 03 cross-code visibility", "no sales03 credential")
out.append("")

# --- 5 -------------------------------------------------------------------
out.append("TEST 5  sales 03 gets 403 on upload and accuracy")
if "sales03" in tok:
    s, b = call(tok["sales03"], "/api/accuracy")
    check("GET /api/accuracy", s, 403)
    body = (b"--x\r\nContent-Disposition: form-data; name=\"files\"; "
            b"filename=\"a.csv\"\r\nContent-Type: text/csv\r\n\r\n"
            b"x,y\n1,2\n\r\n--x--\r\n")
    r = urllib.request.Request(API + "/api/upload", data=body, method="POST")
    r.add_header("Authorization", f"Bearer {tok['sales03']}")
    r.add_header("Content-Type", "multipart/form-data; boundary=x")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            up = resp.status
    except urllib.error.HTTPError as e:
        up = e.code
    check("POST /api/upload", up, 403)
    s, b = call(tok["sales03"], "/api/admin/users")
    check("GET /api/admin/users", s, 403)
else:
    skip("sales 03 denied on upload/accuracy/admin", "no sales03 credential")
out.append("")

# --- extras from the brief ----------------------------------------------
out.append("ALSO  page access and the default-off flags")
if "manager" in tok:
    check("manager CAN open accuracy", call(tok["manager"], "/api/accuracy")[0], 200)
    check("manager CANNOT open admin", call(tok["manager"], "/api/admin/users")[0], 403)
    check("manager may see forecast", call(tok["manager"], "/api/forecast")[0], 200)
else:
    skip("manager page access", "no manager credential")
if "sales03" in tok:
    check("sales forecast off by default", call(tok["sales03"], "/api/forecast")[0], 403)
    check("sales stock off by default", call(tok["sales03"], "/api/stock")[0], 403)
    s, me = call(tok["sales03"], "/api/me")
    check("/api/me role", me.get("role"), "sales")
    check("/api/me hides upload in nav", me.get("pages", {}).get("upload"), False)
else:
    skip("sales page access", "no sales03 credential")
if "ceo" in tok:
    check("ceo may open admin", call(tok["ceo"], "/api/admin/users")[0], 200)
else:
    skip("ceo admin access", "no ceo credential")
out.append("")

out.append("SLOWEST REQUESTS  (was 20-146s before the SQL-aggregation fix)")
for path, status, secs in sorted(TIMINGS, key=lambda t: -t[2])[:10]:
    out.append(f"  {secs:6.1f}s  {status}  {path}")
out.append("")

out.append("ALL TIMINGS")
for path, status, secs in TIMINGS:
    out.append(f"  {secs:6.2f}s  {status}  {path}")
out.append("")

out.append("=" * 62)
if failures:
    verdict = f"FAILURES: {failures}"
elif skips:
    verdict = f"PASS WITH SKIPS -- {len(skips)} check(s) never ran: {skips}"
else:
    verdict = "ALL PASS"
out.append(verdict)

dest = ROOT / ".tmp" / "verify_roles_live.txt"
dest.parent.mkdir(exist_ok=True)
io.open(dest, "w", encoding="utf-8").write("\n".join(out))
print(verdict)
print(f"written to {dest}")
sys.exit(0 if not failures and not skips else 1)
