# Deployment

Everything below is built, tested locally and committed. The only thing
missing is credentials — this machine has no Vercel, Railway or Supabase
account, and no `npm` with which to install their CLIs. Deployment is
therefore done over each platform's REST API.

**Do these in order.** The backend must exist before the frontend, because the
frontend needs the backend URL baked into `config.js` at deploy time (Vercel
serves static files; there is no runtime env var to read).

---

## 0. What you need to create (15 minutes)

| # | Platform | What to get | Where |
|---|---|---|---|
| 1 | Supabase | Project → connection string, anon key, service_role key, JWT secret | Settings → Database / API |
| 2 | Railway | API token | Account → Tokens |
| 3 | Vercel | API token | Account → Settings → Tokens |
| 4 | GitHub | A **private** repo (optional but recommended) | github.com/new |

Sign into Railway and Vercel with GitHub — quickest, and it enables
push-to-deploy later.

> **Make the GitHub repo private.** `data/app/` is committed so the site shows
> real numbers before the first upload. It is group-level aggregate data with
> no customer names, but it is still your revenue. `data/raw/` and
> `data/clean/` are excluded by `.gitignore` — `data/clean/customer_rfm.csv`
> carries 1,483 customer names.

---

## 1. Supabase — database

1. Create a project. Choose the **Singapore** region (lowest latency to
   Thailand).
2. Open the **SQL editor**, paste the whole of `backend/schema.sql`, run it.
   It is idempotent — safe to run again.

   > **If your database was created before 2026-09-20**, also run
   > `backend/migrations/001_flag_columns_to_text.sql`. `schema.sql` is
   > `create table if not exists`, so it does **nothing** to tables that
   > already exist — re-pasting it will not fix them. Without the migration
   > the first upload fails with
   > `invalid input syntax for type double precision: "Y"`.
3. Collect from **Settings → Database → Connection string → URI**:
   `postgresql://postgres:<password>@db.<ref>.supabase.co:5432/postgres`
4. Collect from **Settings → API**: Project URL, `anon` key, `service_role`
   key, and the **JWT secret** (under JWT Settings).

### Create the login
**Authentication → Users → Add user**, with "Auto Confirm User" ticked.
Email signup with confirmation is on by default; without auto-confirm the
account cannot sign in until the email is clicked, which is an unpleasant
surprise during a demo.

---

## 2. Railway — backend

New project → Deploy from GitHub repo → pick this repo → set **root
directory** to `backend`. Railway reads `backend/railway.json` (Nixpacks,
healthcheck on `/health`).

Set these variables (Variables tab):

```
DATABASE_URL         = postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres
SUPABASE_URL         = https://<ref>.supabase.co
SUPABASE_JWT_SECRET  = <JWT secret>
CORS_ORIGINS         = https://<your-vercel-domain>.vercel.app
```

Do **not** set `ALLOW_ANONYMOUS`. It exists only for local development; if it
is set, every endpoint is open to the world. `/health` reports its value so an
accidentally open deployment is visible rather than silent.

Generate a domain (Settings → Networking → Generate Domain), then check:

```
curl https://<service>.up.railway.app/health
# {"status":"ok","source":"postgres","anonymous_access":false,...}
```

If `source` says `csv`, `DATABASE_URL` did not reach the process.

---

## 3. Vercel — frontend

```bash
set VERCEL_TOKEN=...
set API_URL=https://<service>.up.railway.app
set SUPABASE_URL=https://<ref>.supabase.co
set SUPABASE_ANON_KEY=...

python scripts/deploy.py frontend
```

The script writes those values into `frontend/config.js` and uploads the four
static files. There is no build step, so the deploy is a few seconds.

Then go back to Railway and set `CORS_ORIGINS` to the Vercel domain it printed.

---

## 4. First upload

Sign in at the Vercel URL, open **อัปโหลดข้อมูล**, drop in all three CSVs from
`data/raw/` at once. Order and filenames do not matter — the type of each file
is detected from the report title and the document-number prefixes inside it.

Takes about 20 seconds. The response must show all three checks green:

| check | expected |
|---|---|
| revenue_ex_vat | 44,493,479.89 |
| cash_documents | 6,088 |
| credit_documents | 168 |

These are asserted on the server on every upload, so a regression shows up in
the upload response rather than silently populating the dashboard with wrong
numbers.

---

## Verified locally

Run on this machine before committing, against the real exports:

- Upload of all three files through `POST /api/upload`: **ok**, 17.3s,
  revenue 44,493,479.89, 6,088 cash, 168 credit, 0 unparsed rows.
- All seven page endpoints return data (`/api/overview` … `/api/customers`).
- All seven return **401** with a Thai message when no token is supplied;
  `/health` stays public for the Railway healthcheck.
- All eight pages render with no console errors.
- Partial upload (one file) returns 400 naming the missing reports in Thai.

## Verified against live Supabase

Run against the real project (session pooler, port 5432), 2026-09-20:

- **The Postgres write path works.** This was the largest known risk and it
  did fail the first time — see the note on migration 001 above. After the
  fix, four consecutive uploads: 43,459 rows loaded, and the row count of
  every data table was **identical** after uploads 2, 3 and 4.
- **Dedupe by `doc_no` holds.** Re-uploading the same export reports
  `0 inserted / 6,256 updated` on `sales_header` (and 0/31,578 on
  `sales_lines`). Nothing doubles.
- Reference checks green through the API on every run: 44,493,479.89 /
  6,088 cash / 168 credit. End-to-end 20–22s against a remote database.
- `upload_batches` records one row per upload.
- Thai filenames survive the browser's multipart encoding.

## Not yet verified (needs the live platforms)

- Supabase JWT verification against real tokens. The local runs used
  `ALLOW_ANONYMOUS=1`; only the signature path is unexercised, as the 401
  behaviour is covered.
- Railway's Nixpacks build (pandas/numpy wheels).
- Vercel deploy — no token yet.
