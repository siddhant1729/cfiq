"""Cleaning and integration of the raw Codeforces data into tidy DataFrames.

DMDW topics: data cleaning (dedupe, filtering noise such as practice/virtual/gym
submissions) and data integration (problems from two sources merged; problems
joined onto submissions by the composite key (contestId, index)).
"""

from datetime import UTC

import pandas as pd

from cfiq.transform.discretize import (
    contest_division,
    language_family,
    problem_index_letter,
    rating_to_band,
)


def _ts(seconds: pd.Series) -> pd.Series:
    return pd.to_datetime(seconds, unit="s", utc=True)


# ----------------------------------------------------------------- contests


def build_contests(raw_contests: list[dict], tcfg: dict) -> pd.DataFrame:
    df = pd.DataFrame(raw_contests)
    df = df[df["id"] < tcfg["gym_contest_id_min"]]
    out = pd.DataFrame(
        {
            "contest_id": df["id"].astype(int),
            "name": df["name"],
            "contest_type": df["type"],
            "phase": df["phase"],
            "start_time": _ts(df["startTimeSeconds"]),
            "duration_seconds": df["durationSeconds"].astype("Int64"),
        }
    )
    out["division"] = out["name"].apply(lambda n: contest_division(n, tcfg))
    return out.drop_duplicates("contest_id").reset_index(drop=True)


# ----------------------------------------------------------------- problems


def build_problems(
    raw_problems: list[dict], raw_submissions: list[dict], contests: pd.DataFrame
) -> pd.DataFrame:
    """Integrate problems from problemset.problems with problems that only appear
    inside submissions (e.g. problems not listed in the public problemset)."""
    cols = ["contestId", "index", "name", "rating", "tags", "points"]
    from_problemset = pd.DataFrame(raw_problems).reindex(columns=cols)
    from_submissions = pd.DataFrame(
        [s["problem"] for s in raw_submissions if "contestId" in s.get("problem", {})]
    ).reindex(columns=cols)
    # Problemset first, so its record wins when the same problem appears in both.
    df = pd.concat([from_problemset, from_submissions], ignore_index=True)
    df = df.dropna(subset=["contestId"]).drop_duplicates(["contestId", "index"])
    df = df.rename(columns={"contestId": "contest_id", "index": "problem_index"})
    df["contest_id"] = df["contest_id"].astype(int)
    df["tags"] = df["tags"].apply(lambda t: t if isinstance(t, list) else [])

    df = df.merge(contests[["contest_id", "division"]], on="contest_id", how="inner")
    df["index_letter"] = df["problem_index"].apply(problem_index_letter)
    return df.reset_index(drop=True)


def explode_problem_tags(problems: pd.DataFrame, hierarchy: dict[str, str]) -> pd.DataFrame:
    """One row per (problem, tag) with the tag's parent category (for the bridge table)."""
    pt = problems[["contest_id", "problem_index", "tags"]].explode("tags").dropna()
    pt = pt.rename(columns={"tags": "tag"})
    pt["category"] = pt["tag"].map(lambda t: hierarchy.get(t, "other"))
    return pt.drop_duplicates().reset_index(drop=True)


# ----------------------------------------------------------------- submissions


def flatten_submissions(raw: list[tuple[str, dict]]) -> pd.DataFrame:
    """raw = [(handle, submission_json), ...] -> one flat row per submission."""
    rows = []
    for handle, s in raw:
        author = s.get("author", {})
        rows.append(
            {
                "submission_id": s["id"],
                "handle": handle,
                "contest_id": s.get("contestId"),
                "problem_index": s.get("problem", {}).get("index"),
                "creation_time": s.get("creationTimeSeconds"),
                "relative_time_seconds": s.get("relativeTimeSeconds"),
                "participant_type": author.get("participantType"),
                "language": s.get("programmingLanguage"),
                "verdict": s.get("verdict"),
                "time_ms": s.get("timeConsumedMillis"),
                "memory_bytes": s.get("memoryConsumedBytes"),
            }
        )
    return pd.DataFrame(rows)


def clean_submissions(subs: pd.DataFrame, tcfg: dict) -> pd.DataFrame:
    """Dedupe by submission id and drop gym / unofficial / practice / virtual rows."""
    df = subs.drop_duplicates("submission_id")
    df = df[df["contest_id"].notna()]
    df = df[df["contest_id"] < tcfg["gym_contest_id_min"]]
    df = df[df["participant_type"].isin(tcfg["keep_participant_types"])]
    drop = [v for v in tcfg["drop_verdicts"] if v is not None]
    df = df[df["verdict"].notna() & ~df["verdict"].isin(drop)]
    df = df.copy()
    df["contest_id"] = df["contest_id"].astype(int)
    return df.reset_index(drop=True)


def enrich_submissions(subs: pd.DataFrame, problems: pd.DataFrame, tcfg: dict) -> pd.DataFrame:
    """Join problem attributes on (contest_id, problem_index); add derived columns."""
    df = subs.merge(
        problems[["contest_id", "problem_index", "rating", "rating_imputed"]],
        on=["contest_id", "problem_index"],
        how="inner",
    ).rename(columns={"rating": "problem_rating"})
    df["submitted_at"] = _ts(df["creation_time"])
    df["is_accepted"] = df["verdict"] == "OK"
    df["language_family"] = df["language"].apply(lambda x: language_family(x, tcfg))
    df = df.sort_values(["handle", "contest_id", "problem_index", "submitted_at", "submission_id"])
    df["attempt_no"] = df.groupby(["handle", "contest_id", "problem_index"]).cumcount() + 1
    return df.drop(columns=["creation_time"]).reset_index(drop=True)


# ----------------------------------------------------------------- users / ratings


def build_users(raw_users: list[dict], bands: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(raw_users).reindex(
        columns=["handle", "country", "rating", "maxRating", "rank", "registrationTimeSeconds"]
    )
    out = pd.DataFrame(
        {
            "handle": df["handle"],
            "country": df["country"],
            "current_rating": df["rating"].astype("Int64"),
            "max_rating": df["maxRating"].astype("Int64"),
            "cf_rank": df["rank"],
            "registered_at": _ts(df["registrationTimeSeconds"]),
        }
    )
    out["band"] = out["current_rating"].apply(
        lambda r: rating_to_band(None if pd.isna(r) else r, bands)
    )
    return out.drop_duplicates("handle").reset_index(drop=True)


def build_rating_changes(raw: list[dict], contests: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    out = pd.DataFrame(
        {
            "handle": df["handle"],
            "contest_id": df["contestId"].astype(int),
            "rank": df["rank"],
            "rating_updated_at": _ts(df["ratingUpdateTimeSeconds"]),
            "old_rating": df["oldRating"],
            "new_rating": df["newRating"],
        }
    )
    out["delta"] = out["new_rating"] - out["old_rating"]
    out = out[out["contest_id"].isin(contests["contest_id"])]
    out = out.drop_duplicates(["handle", "contest_id"]).sort_values(["handle", "rating_updated_at"])
    out["contest_seq"] = out.groupby("handle").cumcount() + 1  # 1st, 2nd... rated contest
    return out.reset_index(drop=True)


_ = UTC  # (kept for readers: all timestamps are UTC)
