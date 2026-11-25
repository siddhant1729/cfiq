"""Referential integrity of the warehouse after a load (integration test).

Runs against the real Postgres database; skipped if it is not reachable or the
warehouse has not been loaded yet (`cfiq load`).
"""

import pytest
from sqlalchemy import text

from cfiq.db import db_available, get_engine

# (child table, child column, parent table, parent column)
FOREIGN_KEYS = [
    ("fact_submission", "user_key", "dim_user", "user_key"),
    ("fact_submission", "problem_key", "dim_problem", "problem_key"),
    ("fact_submission", "contest_key", "dim_contest", "contest_key"),
    ("fact_submission", "time_key", "dim_time", "time_key"),
    ("fact_submission", "language_key", "dim_language", "language_key"),
    ("fact_rating_change", "user_key", "dim_user", "user_key"),
    ("fact_rating_change", "contest_key", "dim_contest", "contest_key"),
    ("fact_rating_change", "time_key", "dim_time", "time_key"),
    ("bridge_problem_tag", "problem_key", "dim_problem", "problem_key"),
    ("bridge_problem_tag", "tag_key", "dim_tag", "tag_key"),
    ("dim_tag", "category_key", "dim_tag_category", "category_key"),
    ("dim_user", "band_key", "dim_rating_band", "band_key"),
]


@pytest.fixture(scope="module")
def conn():
    engine = get_engine()
    if not db_available(engine):
        pytest.skip("Postgres not reachable")
    with engine.connect() as c:
        if not c.execute(text("SELECT to_regclass('fact_submission')")).scalar():
            pytest.skip("warehouse not loaded")
        yield c


@pytest.mark.parametrize("child,col,parent,pcol", FOREIGN_KEYS)
def test_no_orphan_foreign_keys(conn, child, col, parent, pcol):
    orphans = conn.execute(
        text(
            f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON p.{pcol} = c.{col} "
            f"WHERE c.{col} IS NOT NULL AND p.{pcol} IS NULL"
        )
    ).scalar()
    assert orphans == 0


@pytest.mark.parametrize("child,col,parent,pcol", FOREIGN_KEYS)
def test_foreign_key_constraints_declared(conn, child, col, parent, pcol):
    n = conn.execute(
        text("""
        SELECT count(*) FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage k
          ON k.constraint_name = tc.constraint_name AND k.table_name = tc.table_name
        JOIN information_schema.constraint_column_usage cu
          ON cu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = :child
          AND k.column_name = :col AND cu.table_name = :parent"""),
        {"child": child, "col": col, "parent": parent},
    ).scalar()
    assert n == 1


def test_facts_are_not_empty_and_keys_unique(conn):
    assert conn.execute(text("SELECT count(*) FROM fact_submission")).scalar() > 0
    dupes = conn.execute(
        text("SELECT count(*) - count(DISTINCT submission_id) FROM fact_submission")
    ).scalar()
    assert dupes == 0


def test_every_user_has_a_band(conn):
    assert conn.execute(text("SELECT count(*) FROM dim_user WHERE band_key IS NULL")).scalar() == 0
