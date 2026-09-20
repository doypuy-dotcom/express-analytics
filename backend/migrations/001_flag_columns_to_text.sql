-- Migration 001: collected_flag / discount -> text
--
-- Why this exists: schema.sql was generated from a 2000-row sample of each
-- CSV. sales_header is sorted cash-documents-first (6,088 of them), and
-- collected_flag is only ever set on the 168 credit documents -- so the
-- sample saw 2,000 nulls, pandas typed the column float64, and the schema
-- said double precision. The first real upload failed at:
--
--   invalid input syntax for type double precision: "Y"
--   CONTEXT: COPY tmp_sales_header, line 6089, column collected_flag: "Y"
--
-- sales_lines.discount is empty in all 31,578 rows, so its type was a pure
-- guess too; text accepts whatever a future export puts there.
--
-- schema.sql is "create table if not exists", which does nothing to a table
-- that already exists. Applying the regenerated schema.sql is therefore NOT
-- enough -- this migration is required for any database created before it.
--
-- Safe to re-run. Safe on populated tables: both columns widen to text, and
-- widening never loses data.

alter table sales_header alter column collected_flag type text
    using collected_flag::text;

alter table sales_lines  alter column discount       type text
    using discount::text;
