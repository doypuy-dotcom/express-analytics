"""Row-level visibility by role -- the tests that must pass before deploy.

Three test users, three answers to the same question:

    sales03@express.local   role sales,          code 03
    manager@express.local   role sales_manager,  team 03 + 04
    ceo@express.local       role ceo

The totals are not chosen, they are the data:

    03                12,534,217.13
    04                14,033,697.80
    03 + 04           26,567,914.93
    every code        44,459,976.39
    + no code at all  44,493,479.89   <- only the ceo sees this

That last line is the whole point of the `codes is None` case in scope.py.
Thirty line items worth 33,503.50 carry no salesperson_code. A filter spelled
`code = any(<every code>)` drops them, so an implementation that treats "ceo"
as "a list of all the codes" passes the first four assertions and fails the
accepted figure by 33,503.50 -- a rounding-sized error that hides a whole
class of documents. test_ceo_total_is_the_accepted_figure is what catches it.

These go through the real FastAPI app and the real database. Only the JWT
decode is stubbed (current_user is overridden with a fixed `sub`), because
verifying a Supabase signature is not what is under test here and a live token
expires in an hour. Everything downstream of identity -- the profile lookup,
the scope, the filtering, the recomputation -- is the real code path.

Run:  python -m pytest tests/test_row_level_visibility.py -v
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backend"))

ENV = ROOT / "backend" / ".env"
if ENV.exists():
    for _line in io.open(ENV, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

REVENUE_ALL = 44_493_479.89        # the accepted acceptance figure
REVENUE_03 = 12_534_217.13
REVENUE_04 = 14_033_697.80
REVENUE_TEAM = 26_567_914.93       # 03 + 04
UNASSIGNED = 33_503.50             # documents with no salesperson code

SALES_EMAIL = "sales03@express.local"
MANAGER_EMAIL = "manager@express.local"
CEO_EMAIL = "ceo@express.local"

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").strip(),
    reason="DATABASE_URL not set -- these tests read roles from the database",
)


# ------------------------------------------------------------- harness

@pytest.fixture(scope="module")
def profiles() -> dict:
    """user_id for each test account, read from user_profiles.

    Fails with instructions rather than skipping: a missing test user means
    the fixtures were never provisioned, and silently skipping the security
    tests is exactly the failure mode that lets an unfiltered endpoint ship.
    """
    from app import db
    rows = db.fetch("select email, user_id::text as user_id, role, salesperson_code "
                    "from user_profiles where email = any(%s)",
                    ([SALES_EMAIL, MANAGER_EMAIL, CEO_EMAIL],))
    got = {r["email"]: r for r in rows}
    missing = [e for e in (SALES_EMAIL, MANAGER_EMAIL, CEO_EMAIL) if e not in got]
    if missing:
        pytest.fail(f"test accounts not provisioned: {missing}. "
                    f"Run: python .tmp/setup_roles.py")
    return got


@pytest.fixture(scope="module")
def client_for(profiles):
    """A TestClient whose requests authenticate as the named account."""
    from fastapi.testclient import TestClient
    from app.auth import User, current_user
    from app.main import app

    def make(email: str) -> TestClient:
        row = profiles[email]

        async def _fake_user() -> User:
            return User(id=row["user_id"], email=email, role="authenticated")

        app.dependency_overrides[current_user] = _fake_user
        return TestClient(app, raise_server_exceptions=False)

    yield make
    app.dependency_overrides.clear()


def total_revenue(payload: dict) -> float:
    return round(float(payload["totals"]["revenue_ex_vat"]), 2)


def get(client, url: str) -> dict:
    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text[:300]}"
    return r.json()


# ------------------------------------------------------ the five criteria

def test_sales_totals_are_their_own_code_only(client_for):
    """1. sales 03 totals = sum of code 03 only."""
    body = get(client_for(SALES_EMAIL), "/api/overview")
    assert total_revenue(body) == REVENUE_03
    assert body["scope"]["role"] == "sales"
    assert body["scope"]["codes"] == ["03"]


def test_manager_totals_are_the_team_only(client_for):
    """2. manager totals = 03 + 04 only."""
    body = get(client_for(MANAGER_EMAIL), "/api/overview")
    assert total_revenue(body) == REVENUE_TEAM
    assert sorted(body["scope"]["codes"]) == ["03", "04"]
    # Not a coincidence of two wrongs: it is exactly the two parts.
    assert round(REVENUE_03 + REVENUE_04, 2) == REVENUE_TEAM


def test_ceo_total_is_the_accepted_figure(client_for):
    """3. ceo totals = 44,493,479.89, including the unattributed documents."""
    body = get(client_for(CEO_EMAIL), "/api/overview")
    assert total_revenue(body) == REVENUE_ALL
    assert body["scope"]["codes"] is None, "ceo must be unrestricted, not a code list"
    # The ceo total is strictly larger than every code added up, by exactly
    # the documents nobody owns.
    assert round(REVENUE_ALL - UNASSIGNED, 2) == 44_459_976.39


def test_sales_cannot_see_another_persons_rows(client_for):
    """4a. No row from another salesperson code, on any page."""
    c = client_for(SALES_EMAIL)

    people = get(c, "/api/sales")["by_person_month"]
    assert people, "the sales page returned nothing at all -- check the fixture"
    codes = {r["salesperson_code"] for r in people}
    assert codes == {"03"}, f"leaked salesperson codes: {sorted(codes - {'03'})}"

    # The per-month figures must add back up to their own total and no more.
    assert round(sum(float(r["revenue_ex_vat"]) for r in people), 2) == REVENUE_03


def test_sales_sees_only_their_own_revenue_for_a_shared_customer(client_for):
    """4b. Customer revenue is this rep's invoices, not the customer's total.

    Picks a customer that BOTH 03 and someone else sold to -- if there is no
    such customer the assertion would pass vacuously, so the test asserts one
    exists first.
    """
    from app import db
    shared = db.fetch(
        "select customer_code, "
        "       sum(amount_ex_vat) filter (where salesperson_code = '03') as own, "
        "       sum(amount_ex_vat) as company "
        "from   sales_lines "
        "where  coalesce(is_cancelled, false) = false and customer_code is not null "
        "group  by customer_code "
        "having count(distinct salesperson_code) > 1 "
        "   and sum(amount_ex_vat) filter (where salesperson_code = '03') > 0 "
        "order  by sum(amount_ex_vat) desc limit 5")
    assert shared, "no customer is shared between salespeople -- test is vacuous"

    rows = get(client_for(SALES_EMAIL), "/api/customers?limit=2000")["top_customers"]
    seen = {r["customer_code"]: float(r["monetary"]) for r in rows}

    for s in shared:
        code = s["customer_code"]
        if code not in seen:
            continue
        own, company = round(float(s["own"]), 2), round(float(s["company"]), 2)
        assert abs(seen[code] - own) < 0.02, (
            f"customer {code}: served {seen[code]:,.2f}, own invoices are {own:,.2f}")
        assert seen[code] < company, (
            f"customer {code}: served the company-wide total {company:,.2f}")


def test_sales_totals_never_reach_the_company_figure(client_for):
    """4c. No page hands a sales user a company-wide revenue total."""
    c = client_for(SALES_EMAIL)
    for url in ("/api/overview", "/api/sales", "/api/demand", "/api/customers"):
        r = c.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        blob = r.text
        for forbidden in ("44493479.89", "44,493,479.89", "14033697.8", "26567914.93"):
            assert forbidden not in blob, f"{url} leaked {forbidden}"


def test_sales_is_forbidden_from_upload_and_accuracy(client_for):
    """5. sales 03 gets 403 on upload and accuracy."""
    c = client_for(SALES_EMAIL)
    assert c.get("/api/accuracy").status_code == 403
    assert c.post("/api/upload", files={"files": ("a.csv", b"x,y\n1,2\n")}
                  ).status_code == 403
    assert c.get("/api/admin/users").status_code == 403


# ------------------------------------------------- the rest of the brief

def test_manager_may_see_accuracy_but_not_upload(client_for):
    """(e) accuracy = manager + ceo; upload = ceo only."""
    c = client_for(MANAGER_EMAIL)
    assert c.get("/api/accuracy").status_code == 200
    assert c.post("/api/upload", files={"files": ("a.csv", b"x,y\n1,2\n")}
                  ).status_code == 403


def test_ceo_may_see_everything(client_for):
    c = client_for(CEO_EMAIL)
    for url in ("/api/overview", "/api/sales", "/api/demand", "/api/forecast",
                "/api/stock", "/api/accuracy", "/api/customers",
                "/api/admin/users"):
        assert c.get(url).status_code == 200, f"ceo denied {url}"


def test_forecast_and_stock_are_off_for_sales_by_default(client_for, monkeypatch):
    """(e) config flag, default off."""
    monkeypatch.delenv("SALES_CAN_SEE_FORECAST", raising=False)
    monkeypatch.delenv("SALES_CAN_SEE_STOCK", raising=False)
    c = client_for(SALES_EMAIL)
    assert c.get("/api/forecast").status_code == 403
    assert c.get("/api/stock").status_code == 403

    monkeypatch.setenv("SALES_CAN_SEE_FORECAST", "true")
    assert c.get("/api/forecast").status_code == 200
    assert c.get("/api/stock").status_code == 403, "the two flags must be independent"


def test_user_with_no_profile_sees_nothing(client_for, profiles):
    """Fails closed. An account nobody has provisioned is not a company-wide account."""
    from fastapi.testclient import TestClient
    from app.auth import User, current_user
    from app.main import app

    async def _stranger() -> User:
        return User(id="00000000-0000-0000-0000-0000000000ff",
                    email="nobody@example.com", role="authenticated")

    app.dependency_overrides[current_user] = _stranger
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/overview").status_code == 403
    assert c.get("/api/me").json()["role"] == "none"
    app.dependency_overrides.clear()


def test_role_comes_from_the_database_not_the_token(client_for, profiles):
    """(a) never trust the role from the client.

    Hands the API a token claiming role=ceo for the sales user's identity and
    checks the answer is still the sales user's 12.5m. The `role` claim in a
    Supabase JWT is user-influenceable metadata; identity (`sub`) is not.
    """
    from fastapi.testclient import TestClient
    from app.auth import User, current_user
    from app.main import app

    async def _liar() -> User:
        return User(id=profiles[SALES_EMAIL]["user_id"], email=SALES_EMAIL,
                    role="ceo")           # <- the lie

    app.dependency_overrides[current_user] = _liar
    c = TestClient(app, raise_server_exceptions=False)
    assert total_revenue(get(c, "/api/overview")) == REVENUE_03
    assert c.get("/api/accuracy").status_code == 403
    app.dependency_overrides.clear()


def test_rls_predicate_matches_api_scope(profiles):
    """(f) the second layer agrees with the first.

    Runs app_can_see() in the database with each test user's jwt claim set,
    and checks the row count it admits equals the row count the API serves
    them. Two layers that disagree are worse than one layer, because the safe
    one gets removed as redundant.
    """
    import psycopg
    from app import db

    expected = {SALES_EMAIL: ["03"], MANAGER_EMAIL: ["03", "04"], CEO_EMAIL: None}
    with psycopg.connect(db.dsn()) as conn:
        with conn.cursor() as cur:
            for email, codes in expected.items():
                uid = profiles[email]["user_id"]
                cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (uid,))
                cur.execute("select app_role(), app_allowed_codes()")
                role, allowed = cur.fetchone()
                assert role == profiles[email]["role"], f"{email}: {role}"
                if codes is None:
                    assert allowed is None, f"{email}: ceo must be unrestricted, got {allowed}"
                else:
                    assert sorted(allowed) == codes, f"{email}: {allowed}"

                cur.execute("select count(*) from sales_lines "
                            "where app_can_see(salesperson_code) "
                            "and coalesce(is_cancelled, false) = false")
                by_policy = cur.fetchone()[0]
                if codes is None:
                    cur.execute("select count(*) from sales_lines "
                                "where coalesce(is_cancelled, false) = false")
                else:
                    cur.execute("select count(*) from sales_lines "
                                "where salesperson_code = any(%s) "
                                "and coalesce(is_cancelled, false) = false", (codes,))
                by_filter = cur.fetchone()[0]
                assert by_policy == by_filter, (
                    f"{email}: policy admits {by_policy} rows, scope filter "
                    f"admits {by_filter}")


def test_rls_denies_a_user_with_no_profile():
    """The policy, not just the API, fails closed."""
    import psycopg
    from app import db
    with psycopg.connect(db.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)",
                        ("00000000-0000-0000-0000-0000000000ff",))
            cur.execute("select app_role(), app_allowed_codes(), app_can_see('03'), "
                        "app_can_see(null)")
            role, allowed, can03, cannull = cur.fetchone()
            assert role == "none"
            assert list(allowed) == []
            assert can03 is False and cannull is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
