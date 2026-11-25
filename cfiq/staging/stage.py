"""Staging layer: load raw JSON as-is into stg_* jsonb tables.

DMDW topic: ETL vs ELT. This is the E-L part of ELT: data is Loaded into the
database untouched (one jsonb document per row) BEFORE any Transformation.
The T can then be done in SQL inside the database (see cfiq/staging/elt.py and
sql/elt_transform.sql), in contrast with the ETL path (cfiq/transform) where
pandas cleans the data first and only clean rows are loaded.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from cfiq.config import PROJECT_ROOT
from cfiq.db import copy_rows, run_sql_file
from cfiq.extract import raw_reader

log = logging.getLogger(__name__)
STAGING_DDL = PROJECT_ROOT / "sql" / "staging_schema.sql"


def _doc(obj) -> str:
    # Postgres jsonb rejects the NUL character; strip it if the API ever sends one.
    return json.dumps(obj).replace("\\u0000", "")


def _meta(run_id, table, started, rows) -> dict:
    return {
        "run_id": run_id,
        "phase": "stage",
        "endpoint": table,
        "started_at": started,
        "finished_at": datetime.now(UTC),
        "row_count": rows,
        "api_calls": 0,
        "cache_hits": 0,
        "status": "success",
        "message": None,
    }


def run_stage(cfg: dict, engine) -> list[dict]:
    """Full refresh of the staging tables from the raw cache. Returns etl_metadata rows."""
    run_id = str(uuid.uuid4())
    run_sql_file(engine, STAGING_DDL)
    meta = []
    rel = lambda p: str(p.relative_to(PROJECT_ROOT))  # noqa: E731

    started = datetime.now(UTC)
    f = raw_reader.contests_file(cfg)
    rows = ((rel(f), _doc(c)) for c in raw_reader.read_cached(f))
    meta.append(
        _meta(
            run_id,
            "stg_contest",
            started,
            copy_rows(engine, "stg_contest", ["source_file", "payload"], rows),
        )
    )

    started = datetime.now(UTC)
    f = raw_reader.problems_file(cfg)
    rows = ((rel(f), _doc(p)) for p in raw_reader.read_cached(f)["problems"])
    meta.append(
        _meta(
            run_id,
            "stg_problem",
            started,
            copy_rows(engine, "stg_problem", ["source_file", "payload"], rows),
        )
    )

    started = datetime.now(UTC)
    rows = (("user.info", _doc(u)) for u in raw_reader.user_info_records(cfg))
    meta.append(
        _meta(
            run_id,
            "stg_user",
            started,
            copy_rows(engine, "stg_user", ["source_file", "payload"], rows),
        )
    )

    for endpoint, table in (
        ("user.rating", "stg_rating_change"),
        ("user.status", "stg_submission"),
    ):
        started = datetime.now(UTC)

        def gen(endpoint=endpoint):
            for handle, path in raw_reader.per_handle_files(cfg, endpoint):
                for item in raw_reader.read_cached(path):
                    yield rel(path), handle, _doc(item)

        n = copy_rows(engine, table, ["source_file", "handle", "payload"], gen())
        meta.append(_meta(run_id, table, started, n))

    for m in meta:
        log.info("%-20s %8d rows", m["endpoint"], m["row_count"])
    return meta
