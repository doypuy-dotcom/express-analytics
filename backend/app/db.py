"""Postgres (Supabase) access.

Loading strategy, and why:

    COPY into a TEMP table, then INSERT ... ON CONFLICT DO UPDATE.

A row-by-row executemany over 31,578 sales lines across the public internet to
a Supabase instance takes minutes and times out the request. COPY streams the
whole table in one round trip. The temp-table hop is what gives us dedupe:
COPY cannot express ON CONFLICT, so we land the rows first and then merge them
by primary key. Re-uploading the same Express export therefore updates rows in
place instead of duplicating them, which is the required behaviour.
"""

from __future__ import annotations

import io
import os
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import psycopg
from psycopg import sql

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "schema.sql"


def dsn() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Put the Supabase connection string "
            "(Project settings -> Database -> Connection string -> URI) in the "
            "environment before starting the API."
        )
    # Supabase hands out postgres:// ; psycopg wants postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@contextmanager
def connect():
    with psycopg.connect(dsn(), autocommit=False) as conn:
        yield conn


def ensure_schema() -> None:
    """Apply schema.sql. Idempotent -- every statement is IF NOT EXISTS."""
    ddl = SCHEMA_SQL.read_text(encoding="utf-8")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def _sanitize(df: pd.DataFrame) -> pd.DataFrame:
    """Make a frame safe to COPY as CSV.

    pandas NaN/NaT/pd.NA all have to become a real SQL NULL, and numpy scalars
    have to become text. Writing CSV with an explicit empty null marker and
    letting Postgres parse it is simpler and faster than per-value conversion.
    """
    out = df.copy()
    for c in out.columns:
        s = out[c]
        if pd.api.types.is_bool_dtype(s):
            out[c] = s.map({True: "true", False: "false"})
        elif s.dtype == object:
            # Object columns can hold real bools mixed with strings.
            out[c] = s.map(
                lambda v: "true" if v is True else "false" if v is False else v
            )
    return out


def copy_frame(cur, table: str, df: pd.DataFrame, columns: list[str]) -> None:
    buf = io.StringIO()
    _sanitize(df)[columns].to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )
    with cur.copy(stmt) as cp:
        while chunk := buf.read(1 << 16):
            cp.write(chunk)


def table_columns(cur, table: str) -> list[str]:
    cur.execute(
        "select column_name from information_schema.columns "
        "where table_schema = 'public' and table_name = %s order by ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def primary_key(cur, table: str) -> list[str]:
    cur.execute(
        """
        select a.attname
        from   pg_index i
        join   pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
        where  i.indrelid = %s::regclass and i.indisprimary
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def upsert(conn, table: str, df: pd.DataFrame) -> dict:
    """Merge a frame into `table` by primary key. Returns row counts.

    Columns present in the database but absent from the frame are left alone;
    columns in the frame but not in the database are dropped with no error, so
    an added analysis column cannot break an upload before the schema catches
    up.
    """
    if df is None or df.empty:
        return {"table": table, "received": 0, "inserted": 0, "updated": 0}

    with conn.cursor() as cur:
        db_cols = table_columns(cur, table)
        if not db_cols:
            raise RuntimeError(
                f"Table '{table}' does not exist. Run backend/schema.sql in "
                f"Supabase first (POST /admin/init-db does this for you)."
            )
        cols = [c for c in df.columns if c in db_cols]
        pk = primary_key(cur, table)

        cur.execute("select count(*) from " + sql.Identifier(table).as_string(conn))
        before = cur.fetchone()[0]

        tmp = f"tmp_{table}"
        cur.execute(
            sql.SQL("create temp table {} (like {} including defaults) on commit drop")
            .format(sql.Identifier(tmp), sql.Identifier(table))
        )
        copy_frame(cur, tmp, df, cols)

        target = sql.Identifier(table)
        collist = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
        if pk:
            updatable = [c for c in cols if c not in pk]
            action = (
                sql.SQL("do update set ") + sql.SQL(", ").join(
                    sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c))
                    for c in updatable
                )
            ) if updatable else sql.SQL("do nothing")
            cur.execute(
                sql.SQL(
                    "insert into {t} ({cols}) select {cols} from {tmp} "
                    "on conflict ({pk}) {action}"
                ).format(
                    t=target, cols=collist, tmp=sql.Identifier(tmp),
                    pk=sql.SQL(", ").join(sql.Identifier(c) for c in pk),
                    action=action,
                )
            )
        else:
            cur.execute(sql.SQL("truncate {}").format(target))
            cur.execute(
                sql.SQL("insert into {t} ({cols}) select {cols} from {tmp}").format(
                    t=target, cols=collist, tmp=sql.Identifier(tmp)
                )
            )

        cur.execute("select count(*) from " + target.as_string(conn))
        after = cur.fetchone()[0]

    received = len(df)
    inserted = after - before
    return {
        "table": table,
        "received": received,
        "inserted": inserted,
        "updated": received - inserted,
        "total_rows": after,
    }


def replace(conn, table: str, df: pd.DataFrame) -> dict:
    """Wholesale replace -- for derived tables that are recomputed every run."""
    with conn.cursor() as cur:
        db_cols = table_columns(cur, table)
        cols = [c for c in df.columns if c in db_cols]
        cur.execute(sql.SQL("truncate {}").format(sql.Identifier(table)))
        if cols and not df.empty:
            copy_frame(cur, table, df, cols)
    return {"table": table, "received": len(df), "inserted": len(df), "updated": 0}


def fetch(query: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query and return a list of dicts (JSON-ready)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def healthy() -> tuple[bool, str]:
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
        return True, "ok"
    except Exception as exc:
        return False, str(exc)
