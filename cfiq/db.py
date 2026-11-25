"""Database helpers: SQLAlchemy engine and the etl_metadata audit table."""

from sqlalchemy import Engine, create_engine, text

from cfiq.config import PROJECT_ROOT, database_url

METADATA_DDL = PROJECT_ROOT / "sql" / "etl_metadata.sql"


def get_engine() -> Engine:
    return create_engine(database_url())


def db_available(engine: Engine) -> bool:
    """True if Postgres accepts a connection."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def run_sql_file(engine: Engine, path) -> None:
    """Execute a .sql file (multiple statements) in one transaction."""
    sql = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)


def record_runs(engine: Engine, rows: list[dict]) -> None:
    """Append ETL run records to etl_metadata (creates the table if needed)."""
    run_sql_file(engine, METADATA_DDL)
    insert = text(
        """
        INSERT INTO etl_metadata
            (run_id, phase, endpoint, started_at, finished_at,
             row_count, api_calls, cache_hits, status, message)
        VALUES
            (:run_id, :phase, :endpoint, :started_at, :finished_at,
             :row_count, :api_calls, :cache_hits, :status, :message)
        """
    )
    with engine.begin() as conn:
        conn.execute(insert, rows)


def copy_rows(engine: Engine, table: str, columns: list[str], rows) -> int:
    """Bulk-load rows with PostgreSQL COPY (much faster than INSERT for 100k+ rows)."""
    n = 0
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
                for row in rows:
                    cp.write_row(row)
                    n += 1
        raw.commit()
    finally:
        raw.close()
    return n


def copy_df(engine: Engine, table: str, df) -> int:
    """COPY a DataFrame into `table` (column names must match). NaN/NaT become NULL."""
    clean = df.astype(object).where(df.notna(), None)
    return copy_rows(engine, table, list(df.columns), clean.itertuples(index=False, name=None))


def replace_table_rows(engine: Engine, table: str, df) -> None:
    """Idempotent write for result tables: delete all rows, then append the new ones.
    (Keeps the table, so the Power BI views that depend on it stay valid.)"""
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))
    if len(df):
        copy_df(engine, table, df)


def read_sql(engine: Engine, sql: str, params: dict | None = None):
    import pandas as pd

    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)
