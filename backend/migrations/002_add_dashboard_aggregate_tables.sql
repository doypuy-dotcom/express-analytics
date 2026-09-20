-- Migration 002: the three tables pages 1 and 2 read
--
-- /api/overview reads kpi_monthly and sales_by_group_month; /api/sales also
-- reads sales_by_person_month. None of the three was ever in schema.sql.
--
-- It went unnoticed because every local test ran in CSV-fallback mode, where
-- the API reads data/app/*.csv and never touches Postgres. The failure only
-- appears once DATABASE_URL is set -- production -- as:
--
--   503 อ่านฐานข้อมูลไม่สำเร็จ: relation "kpi_monthly" does not exist
--
-- Copied verbatim from the regenerated schema.sql. Safe to re-run.

create table if not exists kpi_monthly (
    "month"                            text not null,
    "revenue_ex_vat"                   double precision,
    "n_documents"                      bigint,
    "selling_days"                     bigint,
    "n_customers"                      bigint,
    "revenue_per_selling_day"          double precision,
    "avg_document_value"               double precision,
    "pct_change_per_selling_day"       double precision,
    "is_partial_month"                 boolean,
    primary key ("month")
);
alter table kpi_monthly enable row level security;
drop policy if exists kpi_monthly_read on kpi_monthly;
create policy kpi_monthly_read on kpi_monthly for select to authenticated using (true);

create table if not exists sales_by_group_month (
    "month"                            text not null,
    "category"                         text not null,
    "group_name"                       text not null,
    "revenue_ex_vat"                   double precision,
    "n_invoices"                       bigint,
    "n_lines"                          bigint,
    primary key ("month", "category", "group_name")
);
create index if not exists idx_sales_by_group_month_month on sales_by_group_month ("month");
alter table sales_by_group_month enable row level security;
drop policy if exists sales_by_group_month_read on sales_by_group_month;
create policy sales_by_group_month_read on sales_by_group_month for select to authenticated using (true);

create table if not exists sales_by_person_month (
    "month"                            text not null,
    "salesperson_code"                 text not null,
    "revenue_ex_vat"                   double precision,
    "n_invoices"                       bigint,
    primary key ("month", "salesperson_code")
);
alter table sales_by_person_month enable row level security;
drop policy if exists sales_by_person_month_read on sales_by_person_month;
create policy sales_by_person_month_read on sales_by_person_month for select to authenticated using (true);
