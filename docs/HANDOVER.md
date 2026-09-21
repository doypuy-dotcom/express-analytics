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

**Outstanding:** the two test-account passwords (below) were shared in plain
text during development and have **not been rotated**. Rotate them in Supabase
Auth before anyone outside the project is given access.

### Accounts

| Email | Role | Notes |
|---|---|---|
| `doykong1369@gmail.com` | ceo | The owner. Only account that may upload data or change permissions. |
| `manager@express.local` | sales_manager | Test account. Password not rotated. |
| `sales03@express.local` | sales | Test account, scoped to code `03`. Password not rotated. |

---

## 2. What it costs

Roughly **$5 per month (about 180 baht)**, and the reason is a flat plan fee
rather than usage — the actual compute is close to free.

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

**Supabase** is assumed to be on the free tier. ⚠️ **Verify this in the
dashboard** — it is the one cost fact here that was not confirmed from an API.
It matters for a second reason: the Supabase free tier **pauses a project after
7 days with no activity**, and a paused project takes the whole site down. If
the dashboard will genuinely sit unused for a week at a time, either upgrade
Supabase or arrange something to touch the database weekly.

### Trial expiry

Railway reports the project as a trial and reports the $5 included-usage
allowance, but **does not expose a trial end date through its API**, so no
date is asserted here. The project was created **2026-09-20**.

Check `Project → Settings → Usage` in the Railway dashboard for the actual
remaining balance and expiry, and set a calendar reminder. **When the trial
ends the service stops and the whole site goes down** — the frontend stays up
on Vercel but every page will fail to load data. Upgrading to Hobby before
that happens avoids any outage.

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

Before deploying, run the tests — they are what stops one role's data reaching
another:

```bash
python -m pytest tests/ -q             # expect 26 passed
```

`scripts/verify_roles_live.py` re-checks the same boundaries against the
deployed site using real logins. `scripts/preview_roles.py` renders each role's
real payload into a static local copy of the site, so frontend changes can be
checked in a browser without touching production and without any password.

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

1. **Rotate the two test-account passwords** (section 1).
2. **Confirm the Supabase plan** and decide about the 7-day pause (section 2).
3. **Set a reminder for the Railway trial** (section 2).
4. **Two-factor authentication is not implemented.** Supabase Auth supports it;
   it was deferred, not rejected.
5. **There is no audit log.** Nothing records who uploaded what, or who changed
   whose role.
6. **Five pairs of customers with near-identical names have not been merged.**
   They are deliberately left separate pending the owner's confirmation —
   merging the wrong pair mixes two customers' purchase histories and is hard
   to undo. See `docs/METHODS.md` §4.4.
7. **Credit-note style vouchers are not netted out of revenue** (79 vouchers,
   166,752.42 baht, ≈0.4%), pending confirmation that they are genuine returns.
   See `docs/METHODS.md` §3.2.
8. **The July→August drop is unexplained** beyond the part attributable to
   customers switching product groups. See `docs/METHODS.md` §7.3.
