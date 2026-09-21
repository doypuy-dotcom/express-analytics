# Handover

Everything needed to keep this running, or to hand it to somebody else.
For how to *use* the system, see `docs/USER_GUIDE_TH.md`. For how it was first
deployed, see `docs/DEPLOY.md`. For why the numbers are calculated the way they
are, see `docs/METHODS.md`.

---

## 1. What is where

| Thing | Where | Account |
|---|---|---|
| Website (what users open) | https://express-analytics.vercel.app | Vercel, `doypuy@gmail.com` |
| API | https://backend-production-f146.up.railway.app | Railway, `doypuy@gmail.com` |
| Database + login | Supabase project, region **Singapore** | Supabase, `doypuy@gmail.com` |
| Source code | https://github.com/doypuy-dotcom/express-analytics (**private**) | GitHub, `doypuy-dotcom` |

The repo is private on purpose: `data/clean/` contains 1,483 real customer
names. Do not make it public without stripping that directory first.

Identifiers, for when a dashboard asks:

```
Railway project   f576e27e-26b4-48a0-a314-7ce46576bf67
  environment     b730ce1b-f17c-49c2-88c7-14af7673fef3   (production)
  service         8706f1c6-f8d5-4f5f-a2ed-062031da58b6   (backend)
Vercel project    prj_cQOQbE1Gbyc0g48VHHWSCW17JurX
Database host     aws-0-ap-southeast-1.pooler.supabase.com:5432
```

### Passwords and keys

**No password or key is written down in this repo or in this document.**
They live in two places only:

- `backend/.env` on the owner's machine — git-ignored, never committed. Holds
  `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `GITHUB_TOKEN`,
  `RAILWAY_TOKEN`, `VERCEL_TOKEN`.
- The Railway and Vercel dashboards, as environment variables on the
  deployed services.

If `backend/.env` is lost, every value in it can be re-issued from the
Supabase, Railway, Vercel and GitHub dashboards. Nothing in it is irreplaceable.

The two test-account passwords were shared in plain text during development.
Both accounts have since been **deleted and recreated in Supabase Auth with
new passwords set by the owner**, so the exposed passwords no longer exist.
`scripts/verify_roles_live.py` was then run against the deployed site with all
three passwords and **passed every check**, which confirms both the new logins
and their role scoping.

The new passwords are known only to the owner. They are not in this repo, not
in `backend/.env`, and were never seen by anyone else. To run the live role
checks, supply them at the point of use and do not persist them:

```
ROLE_TEST_SALES03_PASSWORD, ROLE_TEST_MANAGER_PASSWORD, ROLE_TEST_CEO_PASSWORD
```

### Accounts

| Email | Role | Notes |
|---|---|---|
| `doykong1369@gmail.com` | ceo | The owner. Only account that may upload data or change permissions. |
| `manager@express.local` | sales_manager | Test account. Recreated; password rotated. |
| `sales03@express.local` | sales | Test account, scoped to code `03`. Recreated; password rotated. |

---

## 2. What it costs

Roughly **$5 per month (about 180 baht)** once Railway is on a paid plan, and
the reason is a flat plan fee rather than usage — the actual compute is close
to free. Right now it is $0, because Railway is still on the trial, which ends
**2026-10-20** and must be upgraded (see below).

**Railway** is the only thing that will ever charge. The project is currently on
`subscriptionType: "trial"`, which includes **$5 of usage**. Measured over a
clean 24-hour window (20–21 Sep 2026) the backend used 3.50 vCPU-minutes,
212 GB-minutes of memory (≈151 MB resident, ≈0.2% of one CPU) and 0.09 GB of
egress. At Railway's published rates — $20 per vCPU-month, $10 per GB-month,
$0.05 per GB egress — that is about **$1.64 per 30 days**:

```
cpu     3.50 vCPU-min/day × 30 ÷ 43,800 × $20 = $0.05
memory   212 GB-min/day  × 30 ÷ 43,800 × $10 = $1.45
egress  0.09 GB/day      × 30           × $0.05 = $0.14
```

That 24 hours included deployments and testing, so real steady-state egress
will be lower, not higher. The practical consequence: **usage is not the thing
to watch, the plan fee is.** When the trial ends, Railway's Hobby plan is
$5/month and includes $5 of usage — which this service does not come close to
exhausting. So the bill is $5/month whether the dashboard is used heavily or
not at all.

**Vercel** is on the free Hobby plan (the account has no paid plan attached).
A static site with no build step will not leave that tier.

**Supabase** is on the **Free plan** (confirmed in the dashboard). That costs
nothing, but it carries one operational risk worth more attention than the
money: the Free plan **pauses a project after 7 days with no activity**, and a
paused project takes the whole site down. If the dashboard will genuinely sit
unused for a week at a time — over a long holiday, say — either upgrade
Supabase or arrange something to touch the database weekly.

### Trial expiry — action required by 2026-10-20

Railway is on the **trial**, not a paid plan. The trial is **$5 of credit or
30 days, whichever runs out first**. The project was created **2026-09-20**,
so the trial ends on or about **2026-10-20**.

**The date expires before the money does.** At the measured burn of about
$1.64 per 30 days, the $5 credit would last roughly three months — but the
30-day clock does not care, and it is the binding constraint. Do not read a
healthy credit balance as time in hand.

**The service must be upgraded to Hobby ($5/month) to stay online.** When the
trial ends the backend stops. The frontend stays up on Vercel and users can
still sign in, which makes the failure look stranger than it is: every page
loads and then fails to fetch its data. Nothing is lost — the database is
Supabase, not Railway — and upgrading restores service, but the site is down
until someone notices and acts.

Upgrade at `Project → Settings → Usage` in the Railway dashboard. Do it before
2026-10-20 and there is no outage at all. Set a calendar reminder now.

---

## 3. Adding and removing users

### Adding

Two steps. Step 1 is in Supabase, step 2 is in this app — a person who has done
only step 1 can sign in but will see nothing at all.

1. **Supabase → Authentication → Users → Add user.** Enter the email, set a
   password, and tick **Auto Confirm User** (otherwise they wait on a
   confirmation email that may not arrive).
2. **Sign in to the site as the ceo → ผู้ใช้และสิทธิ์.** The new person is
   already listed, with role `none`. Give them a role:
   - `sales` — **must** be given a salesperson code. The form enforces this.
   - `sales_manager` — **must** be given at least one code in their team. The
     team list *replaces* whatever was there before; it does not merge.
   - `ceo` — sees everything, and can upload data and change permissions.

The ceo cannot demote themselves. That guard is deliberate: with no ceo left,
nobody can grant the role back and the only repair is hand-written SQL against
production.

### Removing

There is **no delete button, and no "none" option in the role dropdown** — the
API only accepts the three real roles. To revoke access, do it in
**Supabase → Authentication → Users**: delete the user, or ban them.
That removes the login itself, which is the thing that actually grants access.

Their row may remain in `user_profiles`. It is harmless — without a Supabase
login nothing can authenticate as them — but to clear it fully:

```sql
delete from team_members  where manager_user_id = '<user_id>';
delete from user_profiles where user_id = '<user_id>';
```

The `user_id` is shown on the ผู้ใช้และสิทธิ์ page.

**When somebody leaves, check the manager teams.** A departing salesperson's
code may still be listed in a manager's team. That is not a security hole —
the code still maps to real historical sales that the manager is entitled to
see — but re-assigning the code to a new hire hands that new person's data to
that manager. Re-save the manager's team to fix it.

### Letting sales staff see พยากรณ์ and สต็อก

Both are hidden from the `sales` role by default, pending the owner's decision.
No code change is needed to flip either one. Set on the Railway service:

```
SALES_CAN_SEE_FORECAST = 1
SALES_CAN_SEE_STOCK    = 1
```

The service restarts and the pages appear for sales users. Removing the
variables hides them again. These are role-wide switches, not per-person.

---

## 4. Backup and restore

**The three Express CSV exports are the backup.** Everything else in the
database is derived from them and can be rebuilt.

To restore onto an empty database: apply `backend/schema.sql`, then sign in as
the ceo and upload the three files through **อัปโหลดข้อมูล**, exactly as for a
normal update. Takes about 20 seconds. Then re-create the user accounts
(section 3) — user accounts are the one thing the upload does **not** restore.

A restore is correct when the upload reports all three checks:

```
revenue_ex_vat    44,493,479.89
cash_documents             6,088
credit_documents             168
```

These are the accepted figures for the current data set. If a restore produces
anything else, the file set is wrong or incomplete — do not proceed.

**Keep the raw exports somewhere outside this system**, with the whole date
range in each file, not one file per month. Re-uploading is keyed on document
number, so a full-range export uploaded over the top is always safe and always
complete. That is the only backup discipline this project needs.

---

## 5. Deploying a change

The Railway service has no GitHub source attached, so **`git push` does not
deploy anything.** Both halves are manual:

```bash
./.tmp/railway.exe up                  # backend
python scripts/deploy.py frontend      # frontend (Vercel)
```

The token in `backend/.env` is named `RAILWAY_TOKEN`, but it is an **account**
token, and the CLI reads account tokens from `RAILWAY_API_TOKEN` —
`RAILWAY_TOKEN` is where it looks for a *project* token. Exporting it under the
name it has in the file gets "Not signed in"; exporting it under both names
gets "Invalid RAILWAY_TOKEN". Set `RAILWAY_API_TOKEN` and leave `RAILWAY_TOKEN`
unset:

```bash
RAILWAY_API_TOKEN="$RAILWAY_TOKEN" RAILWAY_TOKEN= ./.tmp/railway.exe up
```

`scripts/deploy.py` talks to the REST APIs directly and is unaffected — it
needs `VERCEL_TOKEN`, `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `API_URL`.

Before deploying, run the tests — they are what stops one role's data reaching
another:

```bash
python -m pytest tests/ -q             # expect 26 passed
```

`scripts/verify_roles_live.py` re-checks the same boundaries against the
deployed site using real logins; it was last run against all three accounts
after the password rotation and passed every check. `scripts/preview_roles.py`
renders each role's real payload into a static local copy of the site, so
frontend changes can be checked in a browser without touching production and
without any password.

Two things that have bitten before, both recorded in `docs/DEPLOY.md`:

- The Railway root directory must be the **repository root**, not `backend/`,
  because the upload pipeline shells out to `src/*.py`.
- The Railway region must be **southeast-asia**. It cannot be set in
  `railway.json`; it is a CLI command, and both halves of it are required.
  On the US West default, requests took 21 seconds instead of 0.5.

Never set `ALLOW_ANONYMOUS` on the deployed service. It opens every endpoint
to the internet with no login.

---

## 6. Known gaps, in the order they should be closed

1. **Upgrade Railway to Hobby before 2026-10-20.** The only item here with a
   deadline, and the only one that takes the site down if missed (section 2).
2. **Decide about the Supabase 7-day idle pause** (section 2). No cost, but it
   is the second way this site can go down while nothing is wrong with it.
3. **Two-factor authentication is not implemented.** Supabase Auth supports it;
   it was deferred, not rejected.
4. **There is no audit log.** Nothing records who uploaded what, or who changed
   whose role.
5. **Five pairs of customers with near-identical names have not been merged.**
   They are deliberately left separate pending the owner's confirmation —
   merging the wrong pair mixes two customers' purchase histories and is hard
   to undo. See `docs/METHODS.md` §4.4.
6. **Credit-note style vouchers are not netted out of revenue** (79 vouchers,
   166,752.42 baht, ≈0.4%), pending confirmation that they are genuine returns.
   See `docs/METHODS.md` §3.2.
7. **The July→August drop is unexplained** beyond the part attributable to
   customers switching product groups. See `docs/METHODS.md` §7.3.
8. **`railway.json` stops working on 2026-12-01.** Railway has deprecated
   config-as-code in favour of `.railway/railway.ts`; the CLI warns on every
   deploy. Run `railway config migrate` before that date. Nothing breaks
   today, and the region setting does not live in that file anyway.
