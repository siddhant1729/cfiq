"""Warehouse loader: clean DataFrames -> star/snowflake schema in Postgres.

DMDW topics: dimensional modelling (star schema, snowflaked dimensions, fact
constellation), surrogate keys, and the L of ETL.

Every dimension gets a surrogate integer key (1..n) assigned here. Facts carry
only surrogate keys, which are looked up from the natural keys (handle,
contest_id, (contest_id, index), raw language name, date).
"""

import logging
import uuid
from datetime import UTC, datetime

import pandas as pd

from cfiq.config import PROJECT_ROOT
from cfiq.db import copy_df, run_sql_file
from cfiq.transform.pipeline import load_clean

log = logging.getLogger(__name__)
SQL = PROJECT_ROOT / "sql"


def with_surrogate_key(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Prepend a surrogate key column numbered 1..n."""
    df = df.reset_index(drop=True).copy()
    df.insert(0, key, range(1, len(df) + 1))
    return df


# ------------------------------------------------------------------ dimensions


def build_dim_rating_band(bands: list[dict]) -> pd.DataFrame:
    lowers = [b["lower"] for b in bands]
    uppers = lowers[1:] + [None]
    df = pd.DataFrame(
        {
            "band_code": [b["code"] for b in bands],
            "band_name": [b["name"] for b in bands],
            "rating_lower": pd.array(lowers, dtype="Int64"),
            "rating_upper": pd.array(uppers, dtype="Int64"),
        }
    )
    return with_surrogate_key(df, "band_key")


def build_dim_time(dates: pd.Series) -> pd.DataFrame:
    d = pd.Series(sorted(dates.dropna().unique()))
    d = pd.to_datetime(d)
    return pd.DataFrame(
        {
            "time_key": (d.dt.year * 10000 + d.dt.month * 100 + d.dt.day).astype(int),
            "full_date": d.dt.date,
            "day": d.dt.day,
            "day_of_week": d.dt.dayofweek + 1,
            "day_name": d.dt.day_name(),
            "week": d.dt.isocalendar().week.astype(int).values,
            "month": d.dt.month,
            "month_name": d.dt.month_name(),
            "quarter": d.dt.quarter,
            "year": d.dt.year,
            "year_month": d.dt.strftime("%Y-%m"),
            "is_weekend": d.dt.dayofweek >= 5,
        }
    )


def time_key_of(ts: pd.Series) -> pd.Series:
    """UTC timestamp -> YYYYMMDD integer key."""
    return (ts.dt.year * 10000 + ts.dt.month * 100 + ts.dt.day).astype(int)


def build_warehouse_frames(clean: dict[str, pd.DataFrame], bands: list[dict]) -> dict:
    """Pure function: clean tables -> {table_name: DataFrame} ready to COPY."""
    subs, rc = clean["submissions"], clean["rating_changes"]

    dim_band = build_dim_rating_band(bands)
    band_key = dict(zip(dim_band["band_code"], dim_band["band_key"], strict=True))

    users = clean["users"].sort_values("handle")
    dim_user = with_surrogate_key(users, "user_key")
    dim_user["band_key"] = dim_user["band"].map(band_key).astype("Int64")
    dim_user = dim_user.drop(columns=["band"])
    user_key = dict(zip(dim_user["handle"], dim_user["user_key"], strict=True))

    contests = clean["contests"].sort_values("contest_id")
    dim_contest = with_surrogate_key(
        contests[
            ["contest_id", "name", "division", "contest_type", "start_time", "duration_seconds"]
        ],
        "contest_key",
    )
    contest_key = dict(zip(dim_contest["contest_id"], dim_contest["contest_key"], strict=True))

    problems = clean["problems"].sort_values(["contest_id", "problem_index"])
    dim_problem = with_surrogate_key(
        problems[
            [
                "contest_id",
                "problem_index",
                "index_letter",
                "name",
                "rating",
                "rating_imputed",
                "points",
            ]
        ],
        "problem_key",
    )
    dim_problem["rating"] = dim_problem["rating"].round().astype("Int64")
    problem_key = {
        (c, i): k
        for c, i, k in dim_problem[["contest_id", "problem_index", "problem_key"]].itertuples(
            index=False, name=None
        )
    }

    pt = clean["problem_tags"]
    dim_cat = with_surrogate_key(
        pd.DataFrame({"category_name": sorted(pt["category"].unique())}), "category_key"
    )
    cat_key = dict(zip(dim_cat["category_name"], dim_cat["category_key"], strict=True))
    tags = pt[["tag", "category"]].drop_duplicates("tag").sort_values("tag")
    dim_tag = with_surrogate_key(
        pd.DataFrame({"tag_name": tags["tag"], "category_key": tags["category"].map(cat_key)}),
        "tag_key",
    )
    tag_key = dict(zip(dim_tag["tag_name"], dim_tag["tag_key"], strict=True))
    bridge = pd.DataFrame(
        {
            "problem_key": [
                problem_key[(c, i)]
                for c, i in zip(pt["contest_id"], pt["problem_index"], strict=True)
            ],
            "tag_key": pt["tag"].map(tag_key),
        }
    ).drop_duplicates()

    langs = (
        subs[["language", "language_family"]].drop_duplicates("language").sort_values("language")
    )
    dim_language = with_surrogate_key(
        langs.rename(columns={"language": "raw_name", "language_family": "family"}), "language_key"
    )
    language_key = dict(zip(dim_language["raw_name"], dim_language["language_key"], strict=True))

    all_dates = pd.concat([subs["submitted_at"].dt.date, rc["rating_updated_at"].dt.date])
    dim_time = build_dim_time(all_dates)

    # ---- fact_submission (only rows whose user is in dim_user)
    s = subs[subs["handle"].isin(user_key)].sort_values("submission_id")
    fact_sub = with_surrogate_key(
        pd.DataFrame(
            {
                "submission_id": s["submission_id"].values,
                "user_key": s["handle"].map(user_key).values,
                "problem_key": [
                    problem_key[(c, i)]
                    for c, i in zip(s["contest_id"], s["problem_index"], strict=True)
                ],
                "contest_key": s["contest_id"].map(contest_key).values,
                "time_key": time_key_of(s["submitted_at"]).values,
                "language_key": s["language"].map(language_key).values,
                "verdict": s["verdict"].values,
                "is_accepted": s["is_accepted"].values,
                "attempt_no": s["attempt_no"].values,
                "time_ms": s["time_ms"].values,
                "memory_bytes": s["memory_bytes"].values,
                "submitted_at": s["submitted_at"].values,
                "relative_time_seconds": s["relative_time_seconds"]
                .where(s["relative_time_seconds"] < 2**31 - 1)
                .astype("Int64")
                .values,
            }
        ),
        "submission_key",
    )

    # ---- fact_rating_change
    r = rc[rc["handle"].isin(user_key)]
    fact_rc = with_surrogate_key(
        pd.DataFrame(
            {
                "user_key": r["handle"].map(user_key).values,
                "contest_key": r["contest_id"].map(contest_key).values,
                "time_key": time_key_of(r["rating_updated_at"]).values,
                "old_rating": r["old_rating"].values,
                "new_rating": r["new_rating"].values,
                "delta": r["delta"].values,
                "rank": r["rank"].values,
                "contest_seq": r["contest_seq"].values,
                "rating_updated_at": r["rating_updated_at"].values,
            }
        ),
        "rating_change_key",
    )

    # Order matters: parents before children (foreign keys).
    return {
        "dim_rating_band": dim_band,
        "dim_user": dim_user,
        "dim_contest": dim_contest,
        "dim_problem": dim_problem,
        "dim_tag_category": dim_cat,
        "dim_tag": dim_tag,
        "bridge_problem_tag": bridge,
        "dim_language": dim_language,
        "dim_time": dim_time,
        "fact_submission": fact_sub,
        "fact_rating_change": fact_rc,
    }


def run_load(cfg: dict, engine) -> list[dict]:
    """Drop + recreate the warehouse, load every table, create Power BI views."""
    run_id = str(uuid.uuid4())
    frames = build_warehouse_frames(load_clean(cfg), cfg["rating_bands"])

    run_sql_file(engine, SQL / "schema.sql")
    meta = []
    for table, df in frames.items():
        started = datetime.now(UTC)
        n = copy_df(engine, table, df)
        log.info("%-20s %8d rows", table, n)
        meta.append(
            {
                "run_id": run_id,
                "phase": "load",
                "endpoint": table,
                "started_at": started,
                "finished_at": datetime.now(UTC),
                "row_count": n,
                "api_calls": 0,
                "cache_hits": 0,
                "status": "success",
                "message": None,
            }
        )
    with engine.begin() as conn:
        conn.exec_driver_sql("ANALYZE")
    run_sql_file(engine, SQL / "mining_schema.sql")
    run_sql_file(engine, SQL / "powerbi_views.sql")
    return meta
