-- Row-level data visibility by role.
--
-- Three roles, one filter column. Every sales row carries a salesperson_code,
-- so "what may this user see" reduces to "which salesperson codes may this
-- user see":
--
--     sales          -> exactly one code, their own
--     sales_manager  -> the codes on their team
--     ceo            -> every code, INCLUDING rows with no code at all
--
-- That last clause is not a detail. 30 line items carrying 33,503.50 baht have
-- a null salesperson_code. Only the ceo total includes them, which is why the
-- ceo total is 44,493,479.89 and the four salespeople sum to 44,459,976.39.
-- A filter written as `code = any(allowed)` drops nulls silently; a filter for
-- the ceo must not be written as a filter at all.
--
-- TWO LAYERS, deliberately:
--
--   1. The API filters every query through app/scope.py. This is the layer
--      that actually runs, because the backend connects with the service_role
--      credentials and service_role BYPASSES row level security.
--   2. The policies below. They are what stands between the dataset and
--      anyone holding the anon key who talks to PostgREST directly -- a real
--      exposure, since the anon key ships to the browser.
--
-- Both layers implement the same rule. If they ever disagree, the API is the
-- one that decides what a page shows, so the API is the one the tests assert
-- against; the policies are asserted separately in
-- tests/test_row_level_visibility.py::test_rls_predicate_matches_api_scope.
--
-- Safe to re-run.

-- ---------------------------------------------------------------- tables

create table if not exists user_profiles (
    user_id           uuid primary key,
    email             text,
    role              text not null default 'sales',
    -- Null for a manager or the ceo: neither owns a code of their own.
    salesperson_code  text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- A CHECK, not an enum: a typo'd role like 'Sales' must fail loudly at write
-- time rather than quietly matching nothing at read time and showing an empty
-- dashboard that looks like "no sales this month".
do $$
begin
    alter table user_profiles
        add constraint user_profiles_role_check
        check (role in ('sales', 'sales_manager', 'ceo'));
exception
    when duplicate_object then null;
end $$;

-- Cascade-delete profiles when a Supabase user is deleted, but only if the
-- auth schema is actually present -- this file must also apply to a plain
-- Postgres, which is what the test harness uses.
do $$
begin
    if to_regclass('auth.users') is not null then
        begin
            alter table user_profiles
                add constraint user_profiles_user_fk
                foreign key (user_id) references auth.users(id) on delete cascade;
        exception
            when duplicate_object then null;
        end;
    end if;
end $$;

create table if not exists team_members (
    manager_user_id   uuid not null,
    salesperson_code  text not null,
    created_at        timestamptz not null default now(),
    primary key (manager_user_id, salesperson_code)
);

create index if not exists idx_team_members_manager
    on team_members (manager_user_id);

-- ------------------------------------------------------------- functions
--
-- These are what the POLICIES use. The API does not call them: it reads
-- user_profiles/team_members directly over its own connection, because the
-- service_role connection has no JWT for auth.uid() to read.

-- Deliberately does not call auth.uid(): that function lives in the auth
-- schema, which a non-Supabase Postgres does not have. Reading the claim
-- straight out of the request setting is the same value by a route that
-- always exists. Both spellings are tried -- PostgREST changed from
-- request.jwt.claim.sub to request.jwt.claims (a json blob) in v10.
create or replace function app_current_uid() returns uuid
language sql stable as $$
    select nullif(coalesce(
        nullif(current_setting('request.jwt.claim.sub', true), ''),
        nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'
    ), '')::uuid;
$$;

-- security definer so that a sales user, who is not allowed to read other
-- people's rows in user_profiles, can still have their own role looked up.
-- Without it the policy on user_profiles would recurse into itself.
create or replace function app_role() returns text
language sql stable security definer set search_path = public as $$
    select coalesce(
        (select role from user_profiles where user_id = app_current_uid()),
        'none'
    );
$$;

-- Returns NULL for the ceo, meaning "unrestricted" -- NOT an array of every
-- code. The difference is the null-salesperson rows: an array of every known
-- code still excludes them. Callers must test for null before filtering.
create or replace function app_allowed_codes() returns text[]
language sql stable security definer set search_path = public as $$
    select case app_role()
        when 'ceo' then null::text[]
        when 'sales' then (
            select array_remove(array[salesperson_code], null)
            from   user_profiles where user_id = app_current_uid()
        )
        when 'sales_manager' then (
            select coalesce(array_agg(distinct salesperson_code), '{}')
            from   team_members where manager_user_id = app_current_uid()
        )
        else '{}'::text[]
    end;
$$;

-- The row predicate. One function, used by every policy, so that a change to
-- the rule cannot be applied to eleven tables and forgotten on the twelfth.
create or replace function app_can_see(code text) returns boolean
language sql stable security definer set search_path = public as $$
    select case app_role()
        when 'ceo'  then true
        when 'none' then false
        else code is not null and code = any(app_allowed_codes())
    end;
$$;

create or replace function app_is_ceo() returns boolean
language sql stable security definer set search_path = public as $$
    select app_role() = 'ceo';
$$;

-- ------------------------------------------------------------- policies
--
-- Replaces the `using (true)` policies that make_schema.py used to emit --
-- those granted every signed-in user the whole dataset, which is exactly what
-- this change exists to stop. make_schema.py now emits these instead, so
-- regenerating the schema will not undo this.

alter table user_profiles enable row level security;
drop policy if exists user_profiles_read on user_profiles;
-- Your own row, always. Everyone else's, only if you are the ceo -- the admin
-- page needs the full list.
create policy user_profiles_read on user_profiles for select to authenticated
    using (user_id = app_current_uid() or app_is_ceo());
drop policy if exists user_profiles_write on user_profiles;
create policy user_profiles_write on user_profiles for all to authenticated
    using (app_is_ceo()) with check (app_is_ceo());

alter table team_members enable row level security;
drop policy if exists team_members_read on team_members;
create policy team_members_read on team_members for select to authenticated
    using (manager_user_id = app_current_uid() or app_is_ceo());
drop policy if exists team_members_write on team_members;
create policy team_members_write on team_members for all to authenticated
    using (app_is_ceo()) with check (app_is_ceo());

-- Row-scoped: these tables carry the filter column.
alter table sales_header enable row level security;
drop policy if exists sales_header_read on sales_header;
create policy sales_header_read on sales_header for select to authenticated
    using (app_can_see(salesperson_code));

alter table sales_lines enable row level security;
drop policy if exists sales_lines_read on sales_lines;
create policy sales_lines_read on sales_lines for select to authenticated
    using (app_can_see(salesperson_code));

alter table sales_by_person_month enable row level security;
drop policy if exists sales_by_person_month_read on sales_by_person_month;
create policy sales_by_person_month_read on sales_by_person_month
    for select to authenticated using (app_can_see(salesperson_code));

-- ceo only: every one of these is a COMPANY-WIDE total or a company-wide
-- derived figure. There is no salesperson_code to filter on, so there is no
-- way to serve a correct partial view of them -- a sales user reading
-- kpi_monthly would read the whole company's revenue. The API computes their
-- equivalents from sales_lines instead (app/views.py); nobody but the ceo
-- reads these rows.
do $$
declare t text;
begin
    foreach t in array array[
        'kpi_monthly', 'sales_by_group_month', 'customer_rfm',
        'customer_segments', 'customers', 'monthly_sales',
        'deposits', 'deposit_applications', 'upload_batches'
    ] loop
        if to_regclass('public.' || t) is not null then
            execute format('alter table %I enable row level security', t);
            execute format('drop policy if exists %I on %I', t || '_read', t);
            execute format(
                'create policy %I on %I for select to authenticated using (app_is_ceo())',
                t || '_read', t);
        end if;
    end loop;
end $$;

-- Product-level reference and forecast data. These describe stock and demand,
-- not anybody's sales performance, and they carry no revenue attributable to
-- a person. Readable by any signed-in user with a role; the page-level gate
-- for the sales role lives in the API config flags (SALES_CAN_SEE_FORECAST,
-- SALES_CAN_SEE_STOCK), because it is a policy decision the owner has not
-- made yet and a decision that can be reversed without a migration.
do $$
declare t text;
begin
    foreach t in array array[
        'products', 'weekly_demand', 'dim_product_group',
        'forecast_next_4_weeks', 'trend_alerts', 'reorder_points',
        'stock_check', 'model_accuracy', 'model_accuracy_pooled'
    ] loop
        if to_regclass('public.' || t) is not null then
            execute format('alter table %I enable row level security', t);
            execute format('drop policy if exists %I on %I', t || '_read', t);
            execute format(
                'create policy %I on %I for select to authenticated '
                'using (app_role() <> ''none'')',
                t || '_read', t);
        end if;
    end loop;
end $$;
