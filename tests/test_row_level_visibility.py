"""Row-level visibility by role -- the tests that must pass before deploy.

Three test users, three answers to the same question:

    sales03@express.local   role sales,          code 03
    manager@express.local   role sales_manager,  team 03 + 04
    doykong1369@gmail.com   role ceo   <- the owner's real and only ceo login

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
# The owner's real account, and deliberately not a fixture: the ceo grant is
# held by exactly one login and the throwaway ceo@express.local was deleted.
# Identity is stubbed here (see the module docstring), so these tests need the
# user_id and never the password.
CEO_EMAIL = "doykong1369@gmail.com"

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


# ------------------------------------------- leak check: the RFM labelling
#
# A segment label is derived data, and derived data leaks. "แชมเปี้ยน" is
# awarded on a rank within a population; if that population is every customer
# in the company, then the label on a rep's own screen is a statement about
# how their customer compares to customers they are not allowed to see. The
# revenue column could be perfectly scoped and the labels would still leak.

def test_rfm_segments_are_ranked_within_the_reps_own_book(client_for):
    """3. Segment labels come from the rep's own invoices, not the company's.

    The proof is a disagreement: if the rep's labels were copied from a
    company-wide ranking they would match the company label for every
    customer. The test asserts they do NOT match for at least one customer,
    so it cannot pass by accident on a dataset where the two happen to agree.
    """
    mine = get(client_for(SALES_EMAIL), "/api/customers?limit=5000")["top_customers"]
    company = get(client_for(CEO_EMAIL), "/api/customers?limit=5000")["top_customers"]
    assert mine, "the sales user has no customers -- test would be vacuous"

    company_seg = {r["customer_code"]: r["segment"] for r in company}
    shared = [r for r in mine if r["customer_code"] in company_seg]
    assert len(shared) > 20, (
        f"only {len(shared)} customers overlap the company list -- too few to "
        f"tell a scoped ranking from a copied one")

    differ = [(r["customer_code"], r["segment"], company_seg[r["customer_code"]])
              for r in shared if r["segment"] != company_seg[r["customer_code"]]]
    assert differ, (
        "every one of this rep's customers carries the same segment label as "
        "the company-wide ranking gives it. That is what copying the "
        "company-wide labels would look like.")


def test_rfm_bands_span_the_reps_own_population(client_for):
    """3b. The bands are quintiles OF THIS REP'S customers.

    Ranking inside the rep's own book puts ~20% of THEIR customers in each
    band. A slice of a company-wide ranking does not: measured against this
    data, 03's customers sit at 14/15/17/23/31% of the company m_score bands,
    because 03 sells to the larger accounts. So the 20% assertion is what
    distinguishes a scoped ranking from a leaked one, and it has teeth --
    band 5 alone would miss by 11 points.

    Only recency and monetary are checked. Frequency ties far too heavily to
    be a quintile of anything (~60% of customers bought exactly once and the
    whole tied block lands in one band); that is documented in customer_rfm
    and asserted separately below.
    """
    mine = get(client_for(SALES_EMAIL), "/api/customers?limit=5000")["top_customers"]
    n = len(mine)
    assert n >= 100, f"only {n} customers -- too few for a quintile claim"

    for col in ("r_score", "m_score"):
        bands = [int(r[col]) for r in mine]
        assert set(bands) == {1, 2, 3, 4, 5}, (
            f"{col} uses bands {sorted(set(bands))}, not all five -- these are "
            f"not quintiles of this rep's own customers")
        for b in (1, 2, 3, 4, 5):
            share = sum(1 for x in bands if x == b) / n
            assert abs(share - 0.20) <= 0.05, (
                f"{col} band {b} holds {share:.1%} of this rep's customers, "
                f"not ~20%. Either the ranking population is not this rep's "
                f"own book, or the banding changed.")


def test_rfm_frequency_bands_are_ties_not_quintiles(client_for):
    """3c. Pin the frequency-tie behaviour so it cannot change silently.

    f_score is not a quintile and the module says so. What must stay true is
    that the band is monotone in frequency -- a customer who bought more never
    scores lower than one who bought less.
    """
    mine = get(client_for(SALES_EMAIL), "/api/customers?limit=5000")["top_customers"]
    by_freq: dict[int, set[int]] = {}
    for r in mine:
        by_freq.setdefault(int(r["frequency"]), set()).add(int(r["f_score"]))

    for freq, scores in by_freq.items():
        assert len(scores) == 1, (
            f"customers with frequency {freq} were given different f_scores "
            f"{sorted(scores)} -- equal values must band equally")

    ordered = sorted(by_freq)
    scores = [next(iter(by_freq[f])) for f in ordered]
    assert scores == sorted(scores), (
        f"f_score is not monotone in frequency: {list(zip(ordered, scores))}")


def test_rfm_segment_revenue_adds_up_to_the_reps_own_total(client_for):
    """3c. The segment summary totals the rep's revenue, not the company's."""
    out = get(client_for(SALES_EMAIL), "/api/customers?limit=5000")
    total = round(sum(float(s["revenue"]) for s in out["segments"]), 2)
    # Customers with no customer_code are excluded from RFM, so this is a
    # ceiling rather than an equality.
    assert total <= REVENUE_03 + 0.02, (
        f"segments total {total:,.2f}, above this rep's own {REVENUE_03:,.2f}")
    assert total > REVENUE_03 * 0.90, (
        f"segments total {total:,.2f} -- too far below the rep's revenue, "
        f"the grouping is dropping rows")


# ----------------------------------- leak check: every reachable page, every
#                                      figure, against the company-wide value
#
# test_sales_totals_never_reach_the_company_figure greps for four numbers I
# happened to think of. That catches a headline total and nothing else: a
# per-month figure, a per-group figure or a per-week quantity taken from the
# company table would sail past it. This compares EVERY served figure against
# both the company-wide value and the correctly-scoped value for the same key,
# and demands it equal the scoped one.
#
# The distinction matters because the two are often equal -- where 03 is the
# only rep who sold a SKU, the scoped and company quantities agree and no
# assertion can tell a filtered query from an unfiltered one. So each metric
# also asserts that the two disagree somewhere, which is where the real
# evidence is.

SALES_PAGES = ("/api/overview", "/api/sales", "/api/demand",
               "/api/customers?limit=5000")


def _scoped_and_company(sql_scoped: str, sql_company: str) -> tuple[dict, dict]:
    """Both sides of the comparison, keyed identically.

    A group whose values are all NULL sums to NULL, not 0 -- there are
    customers with no amount on any line. Treated as 0.0 so the comparison
    does not blow up on them.
    """
    from app import db

    def load(sql: str) -> dict:
        out = {}
        for r in db.fetch(sql):
            key = tuple(r["k"]) if isinstance(r["k"], list) else r["k"]
            out[key] = float(r["v"]) if r["v"] is not None else 0.0
        return out

    return load(sql_scoped), load(sql_company)


def _assert_scoped_not_company(label, served: dict, scoped: dict, company: dict):
    """served[key] must equal scoped[key]; and somewhere the two must differ."""
    assert served, f"{label}: nothing served -- the test would be vacuous"

    leaked, wrong = [], []
    for key, value in served.items():
        want = scoped.get(key, 0.0)
        if abs(value - want) < 0.02:
            continue
        theirs = company.get(key)
        if theirs is not None and abs(value - theirs) < 0.02:
            leaked.append((key, value, want))
        else:
            wrong.append((key, value, want))
    assert not leaked, (
        f"{label}: served the COMPANY-WIDE figure for {len(leaked)} key(s), "
        f"e.g. {leaked[0][0]} -> {leaked[0][1]:,.2f} "
        f"(this rep's own is {leaked[0][2]:,.2f})")
    assert not wrong, (
        f"{label}: {len(wrong)} figure(s) match neither this rep nor the "
        f"company, e.g. {wrong[0][0]} -> {wrong[0][1]:,.2f} "
        f"(expected {wrong[0][2]:,.2f})")

    discriminating = [k for k in served
                      if k in company and abs(company[k] - scoped.get(k, 0.0)) > 0.02]
    assert discriminating, (
        f"{label}: this rep's figures equal the company's for every key on the "
        f"page, so the assertion above cannot distinguish a scoped query from "
        f"an unscoped one. The test proves nothing as written.")


def test_no_company_figure_on_any_page_the_sales_role_can_reach(client_for):
    """4. Every figure on all four sales pages is this rep's own.

    Covers monthly revenue, monthly document counts, revenue per product
    group per month, weekly quantity per SKU, and per-customer revenue.
    """
    c = client_for(SALES_EMAIL)
    live = "coalesce(is_cancelled, false) = false"

    # --- overview: revenue per month ------------------------------------
    served = {r["month"]: float(r["revenue_ex_vat"])
              for r in get(c, "/api/overview")["kpi_monthly"]}
    scoped, company = _scoped_and_company(
        f"select substr(doc_date_iso,1,7) as k, sum(amount_ex_vat) as v "
        f"from sales_lines where {live} and salesperson_code = '03' group by 1",
        f"select substr(doc_date_iso,1,7) as k, sum(amount_ex_vat) as v "
        f"from sales_lines where {live} group by 1")
    _assert_scoped_not_company("overview / revenue per month", served, scoped, company)

    # --- overview: documents per month ----------------------------------
    served = {r["month"]: float(r["n_documents"])
              for r in get(c, "/api/overview")["kpi_monthly"]}
    scoped, company = _scoped_and_company(
        f"select substr(doc_date_iso,1,7) as k, count(distinct doc_no) as v "
        f"from sales_header where {live} and salesperson_code = '03' group by 1",
        f"select substr(doc_date_iso,1,7) as k, count(distinct doc_no) as v "
        f"from sales_header where {live} group by 1")
    _assert_scoped_not_company("overview / documents per month", served, scoped, company)

    # --- sales: revenue per product group per month ---------------------
    served = {(r["month"], r["group_name"]): float(r["revenue_ex_vat"])
              for r in get(c, "/api/sales")["by_group_month"]}
    scoped, company = _scoped_and_company(
        f"select array[substr(doc_date_iso,1,7), group_name] as k, "
        f"       sum(amount_ex_vat) as v from sales_lines "
        f"where {live} and salesperson_code = '03' group by 1",
        f"select array[substr(doc_date_iso,1,7), group_name] as k, "
        f"       sum(amount_ex_vat) as v from sales_lines where {live} group by 1")
    _assert_scoped_not_company("sales / revenue per group per month",
                               served, scoped, company)

    # --- demand: quantity per SKU per week ------------------------------
    # Keyed by UNIT as well, because one sku_prefix is sold in more than one
    # unit and the panel keeps them apart -- 180.11 of prefix 01 in a week is
    # 179.11 of one unit plus 1.00 of another, and summing across them would
    # add metres to lengths. build_weekly_demand also zero-fills gaps, so a
    # served 0 against a non-zero company quantity is exactly the leak this
    # is looking for.
    served = {(r["week_start"], r["sku_prefix"], r["unit"]): float(r["qty"])
              for r in get(c, "/api/demand")["weekly"]}
    week = "to_char(date_trunc('week', doc_date_iso::date),'YYYY-MM-DD')"
    where = (f"{live} and doc_date_iso is not null and sku_prefix is not null "
             f"and unit is not null")
    scoped, company = _scoped_and_company(
        f"select array[{week}, sku_prefix, unit] as k, sum(qty) as v "
        f"from sales_lines where {where} and salesperson_code = '03' group by 1",
        f"select array[{week}, sku_prefix, unit] as k, sum(qty) as v "
        f"from sales_lines where {where} group by 1")
    _assert_scoped_not_company("demand / quantity per SKU per week",
                               served, scoped, company)

    # --- customers: revenue per customer --------------------------------
    served = {r["customer_code"]: float(r["monetary"])
              for r in get(c, "/api/customers?limit=5000")["top_customers"]}
    scoped, company = _scoped_and_company(
        f"select customer_code as k, sum(amount_ex_vat) as v from sales_lines "
        f"where {live} and salesperson_code = '03' and customer_code is not null "
        f"group by 1",
        f"select customer_code as k, sum(amount_ex_vat) as v from sales_lines "
        f"where {live} and customer_code is not null group by 1")
    _assert_scoped_not_company("customers / revenue per customer",
                               served, scoped, company)


def test_sales_pages_never_mention_another_salesperson(client_for):
    """4b. No other rep's code appears anywhere in a sales user's payloads."""
    from app import db
    others = [r["salesperson_code"] for r in db.fetch(
        "select distinct salesperson_code from sales_lines "
        "where salesperson_code is not null and salesperson_code <> '03'")]
    assert others, "only one salesperson in the data -- test is vacuous"

    c = client_for(SALES_EMAIL)
    for url in SALES_PAGES:
        payload = get(c, url)
        for row in _walk(payload):
            for key, value in row.items():
                if "salesperson" in key and value not in (None, "03"):
                    pytest.fail(f"{url} exposed salesperson_code {value!r} "
                                f"in field {key!r}")


def _walk(node):
    """Every dict in a nested payload."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
