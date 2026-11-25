"""Transform pipeline (the T of ETL): raw cache -> clean DataFrames in data/clean/.

DMDW topics: cleaning, integration, missing values, discretization, concept hierarchy.
Also produces reports/preprocessing.md (what each cleaning step removed and how
many ratings were missing) and reports/etl_vs_elt.md (pandas ETL vs SQL ELT).
"""

import logging
import time
import uuid
from datetime import UTC, datetime

import pandas as pd

from cfiq.config import PROJECT_ROOT
from cfiq.extract import raw_reader
from cfiq.transform import clean
from cfiq.transform.discretize import load_tag_hierarchy
from cfiq.transform.impute import impute_problem_ratings

log = logging.getLogger(__name__)

CLEAN_TABLES = ["contests", "problems", "problem_tags", "submissions", "users", "rating_changes"]


def load_clean(cfg: dict) -> dict[str, pd.DataFrame]:
    """Read the clean DataFrames written by run_transform()."""
    d = cfg["paths"]["clean_dir"]
    return {name: pd.read_pickle(d / f"{name}.pkl") for name in CLEAN_TABLES}


def run_transform(cfg: dict) -> tuple[list[dict], dict]:
    """Returns (etl_metadata rows, stats dict used for the reports)."""
    tcfg = cfg["transform"]
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    stats: dict = {}
    t0 = time.perf_counter()

    hierarchy = load_tag_hierarchy(PROJECT_ROOT / tcfg["tag_hierarchy_file"])

    # ---- read raw cache
    raw_contests = raw_reader.read_cached(raw_reader.contests_file(cfg))
    raw_problems = raw_reader.read_cached(raw_reader.problems_file(cfg))["problems"]
    raw_users = raw_reader.user_info_records(cfg)
    raw_subs, raw_ratings = [], []
    for handle, path in raw_reader.per_handle_files(cfg, "user.status"):
        raw_subs += [(handle, s) for s in raw_reader.read_cached(path)]
    for _, path in raw_reader.per_handle_files(cfg, "user.rating"):
        raw_ratings += raw_reader.read_cached(path)
    t_read = time.perf_counter()

    # ---- contests, problems (integration + imputation), tags
    contests = clean.build_contests(raw_contests, tcfg)
    problems = clean.build_problems(raw_problems, [s for _, s in raw_subs], contests)
    stats["problems_total"] = len(problems)
    stats["problems_missing_rating"] = int(problems["rating"].isna().sum())
    problems = impute_problem_ratings(problems)
    problem_tags = clean.explode_problem_tags(problems, hierarchy)

    # ---- submissions: flatten -> clean -> enrich (step-by-step counts for the report)
    t_clean0 = time.perf_counter()
    subs = clean.flatten_submissions(raw_subs)
    steps = [("raw submissions", len(subs))]
    subs = subs.drop_duplicates("submission_id")
    steps.append(("after dedupe by id", len(subs)))
    subs = subs[subs["contest_id"].notna() & (subs["contest_id"] < tcfg["gym_contest_id_min"])]
    steps.append(("after dropping gym / no contest", len(subs)))
    subs = subs[subs["participant_type"].isin(tcfg["keep_participant_types"])]
    steps.append(("after keeping official participations", len(subs)))
    subs = clean.clean_submissions(subs, tcfg)  # (idempotent) applies verdict filter too
    steps.append(("after dropping missing/in-flight verdicts", len(subs)))
    t_clean1 = time.perf_counter()
    subs = clean.enrich_submissions(subs, problems, tcfg)
    steps.append(("after joining problems on (contestId, index)", len(subs)))
    stats["cleaning_steps"] = steps
    stats["pandas_clean_seconds"] = (t_read - t0) + (t_clean1 - t_clean0)
    stats["pandas_clean_rows"] = steps[-2][1]
    stats["subs_on_unrated_problems"] = int(subs["rating_imputed"].sum())

    users = clean.build_users(raw_users, cfg["rating_bands"])
    rating_changes = clean.build_rating_changes(raw_ratings, contests)

    # ---- persist
    out = cfg["paths"]["clean_dir"]
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "contests": contests,
        "problems": problems,
        "problem_tags": problem_tags,
        "submissions": subs,
        "users": users,
        "rating_changes": rating_changes,
    }
    meta = []
    for name, df in tables.items():
        df.to_pickle(out / f"{name}.pkl")
        log.info("clean/%-16s %8d rows", name, len(df))
        meta.append(
            {
                "run_id": run_id,
                "phase": "transform",
                "endpoint": f"clean/{name}",
                "started_at": started,
                "finished_at": datetime.now(UTC),
                "row_count": len(df),
                "api_calls": 0,
                "cache_hits": 0,
                "status": "success",
                "message": None,
            }
        )
    stats["table_rows"] = {k: len(v) for k, v in tables.items()}
    return meta, stats


def write_preprocessing_report(cfg: dict, stats: dict, elt: dict | None) -> None:
    reports = cfg["paths"]["reports"]
    reports.mkdir(parents=True, exist_ok=True)
    p_total, p_missing = stats["problems_total"], stats["problems_missing_rating"]
    lines = [
        "# Preprocessing report",
        "",
        "## Cleaning steps (submissions)",
        "",
        "| step | rows |",
        "|---|---:|",
        *[f"| {s} | {n:,} |" for s, n in stats["cleaning_steps"]],
        "",
        "## Missing values: problem rating",
        "",
        f"- Problems (problemset + integrated from submissions): **{p_total:,}**",
        f"- Missing `rating`: **{p_missing:,}** ({p_missing / p_total:.1%})",
        f"- Clean submissions on an unrated problem: **{stats['subs_on_unrated_problems']:,}**",
        "- Strategy (a) impute: median rating of same division + index letter "
        "(fallback: index letter, then global), rounded to 100, flagged `rating_imputed`.",
        "- Strategy (b) drop: remove those problems. The effect of each on the models is in "
        "`reports/classification/imputation_comparison.md`.",
        "",
        "## Clean tables",
        "",
        "| table | rows |",
        "|---|---:|",
        *[f"| {k} | {v:,} |" for k, v in stats["table_rows"].items()],
    ]
    (reports / "preprocessing.md").write_text("\n".join(lines) + "\n")

    if elt is not None:
        etl_rows, etl_s = stats["pandas_clean_rows"], stats["pandas_clean_seconds"]
        same = "yes" if etl_rows == elt["rows"] else "NO"
        md = [
            "# ETL vs ELT: cleaning the submissions",
            "",
            "| path | where the T runs | rows out | seconds |",
            "|---|---|---:|---:|",
            f"| ETL | pandas, before loading (`cfiq/transform/clean.py`) | {etl_rows:,} | "
            f"{etl_s:.2f} |",
            f"| ELT | SQL inside Postgres on staged jsonb (`sql/elt_transform.sql`) | "
            f"{elt['rows']:,} | {elt['seconds']:.2f} |",
            "",
            f"Same row count from both paths: **{same}**.",
            "",
            "ETL time includes reading the raw JSON files from disk; ELT time is the SQL "
            "only (the raw JSON was already loaded into `stg_submission` by `cfiq stage`).",
            "ETL keeps the warehouse clean and the logic in testable Python; ELT keeps the raw "
            "data in the database so new transformations can be written later in SQL without "
            "re-extracting.",
        ]
        (reports / "etl_vs_elt.md").write_text("\n".join(md) + "\n")
