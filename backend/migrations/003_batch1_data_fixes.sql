-- Migration 003: the schema half of the Batch 1 data fixes
--
-- schema.sql is `create table if not exists` throughout, so it does NOTHING to
-- a table that already exists. Every column change below therefore has to be
-- stated explicitly or the live database keeps the old shape while the code
-- expects the new one.
--
-- That failure mode is quiet and specific. db.replace() copies only the columns
-- present in BOTH the dataframe and the table, so a column the pipeline now
-- produces but the table lacks is dropped without an error -- the upload
-- succeeds, reports its row counts, and the dashboard cell stays "–". That is
-- exactly how the เอกสาร column would have behaved if this migration were
-- skipped: n_documents written by the pipeline, n_invoices sitting in the
-- table, nothing joining them, no error anywhere.
--
-- Safe to re-run.


-- 1. sales_by_person_month: n_invoices -> n_documents
--
-- Not a rename. n_invoices came from monthly_sales, which is keyed by product
-- group as well as salesperson, so an invoice touching three groups counted
-- three times: December reported 1,444 documents against 611 real ones. The
-- new column is a distinct doc_no count taken from sales_header, where each
-- invoice exists exactly once. The old column is dropped rather than left
-- behind, because a wholesale-replaced derived table would carry it forever as
-- a permanently NULL column that still looks like an answer.
alter table sales_by_person_month add column if not exists n_documents bigint;
alter table sales_by_person_month drop column if exists n_invoices;


-- 2. The stopped flag, on both tables anyone orders from
--
-- stopped is computed once in forecast_plan and lived only in trend_alerts, so
-- the forecast and reorder tables had no way to know that group 05 had sold
-- nothing since mid-July. They went on presenting a 251 m forecast and a 323 m
-- reorder point -- correct arithmetic on a series that had ended.
alter table forecast_next_4_weeks add column if not exists stopped boolean;
alter table reorder_points       add column if not exists stopped boolean;


-- 3. Pooled model accuracy
--
-- The accuracy page was averaging the per-group WAPEs to get a headline. WAPE
-- is a ratio of sums, so an average across groups weights a group selling 68
-- units the same as one selling 90,000: it reported ma8 at 50.9% when the
-- pooled figure is 14.2%. The correct number cannot be recovered from the
-- per-group table at all -- it needs the raw backtest totals -- so it is
-- computed in the pipeline and stored here.
--
-- Copied verbatim from the regenerated schema.sql.
create table if not exists model_accuracy_pooled (
    "scenario"                         text not null,
    "model"                            text not null,
    "wape_weekly"                      double precision,
    "wape_4wk_total"                   double precision,
    "holdout_qty"                      double precision,
    "n_groups"                         bigint,
    primary key ("scenario", "model")
);
alter table model_accuracy_pooled enable row level security;
drop policy if exists model_accuracy_pooled_read on model_accuracy_pooled;
create policy model_accuracy_pooled_read on model_accuracy_pooled for select to authenticated using (true);
