"""ELT transform: clean submissions with SQL inside Postgres, from the staged jsonb.

DMDW topic: ETL vs ELT. `run_elt_transform` is the T of ELT; its row count and
runtime are compared with the pandas (ETL) cleaning in cfiq/transform/pipeline.py.
"""

import time

from sqlalchemy import text

from cfiq.config import PROJECT_ROOT

ELT_SQL = PROJECT_ROOT / "sql" / "elt_transform.sql"


def run_elt_transform(cfg: dict, engine) -> dict:
    """Build elt_submission_clean from stg_submission. Returns {rows, seconds}."""
    tcfg = cfg["transform"]
    if len(tcfg["keep_participant_types"]) != 1:
        raise ValueError("elt_transform.sql supports exactly one participant type")
    sql = "\n".join(
        line for line in ELT_SQL.read_text().splitlines() if not line.lstrip().startswith("--")
    )
    t0 = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS elt_submission_clean"))
        conn.execute(
            text(sql),
            {
                "participant_type": tcfg["keep_participant_types"][0],
                "gym_min": tcfg["gym_contest_id_min"],
            },
        )
        rows = conn.execute(text("SELECT count(*) FROM elt_submission_clean")).scalar_one()
    return {"rows": rows, "seconds": time.perf_counter() - t0}
