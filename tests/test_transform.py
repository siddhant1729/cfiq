"""Tests for cleaning (dedupe / filters), imputation, scaling and concept hierarchies."""

import numpy as np
import pandas as pd
import pytest

from cfiq.config import PROJECT_ROOT
from cfiq.transform.clean import clean_submissions, enrich_submissions, flatten_submissions
from cfiq.transform.discretize import (
    contest_division,
    language_family,
    load_tag_hierarchy,
    problem_index_letter,
    tag_to_category,
)
from cfiq.transform.impute import apply_strategy, drop_unrated, impute_problem_ratings
from cfiq.transform.scale import apply_scaler, fit_scaler, minmax_scale, zscore_scale


def sub(sid, ptype="CONTESTANT", contest=1500, verdict="OK", index="A", t=1000):
    return {
        "id": sid,
        "contestId": contest,
        "creationTimeSeconds": t,
        "relativeTimeSeconds": 60,
        "problem": {"contestId": contest, "index": index},
        "author": {"participantType": ptype},
        "programmingLanguage": "C++20 (GCC 13-64)",
        "verdict": verdict,
        "timeConsumedMillis": 15,
        "memoryConsumedBytes": 100,
    }


# ----------------------------------------------------------------- dedupe / filters


def test_dedupe_by_submission_id(tcfg):
    raw = [("alice", sub(1)), ("alice", sub(1)), ("bob", sub(1)), ("alice", sub(2))]
    df = clean_submissions(flatten_submissions(raw), tcfg)
    assert sorted(df["submission_id"]) == [1, 2]


@pytest.mark.parametrize("ptype", ["PRACTICE", "VIRTUAL", "OUT_OF_COMPETITION"])
def test_unofficial_participations_dropped(tcfg, ptype):
    raw = [("a", sub(1)), ("a", sub(2, ptype=ptype))]
    df = clean_submissions(flatten_submissions(raw), tcfg)
    assert df["submission_id"].tolist() == [1]


def test_gym_and_missing_contest_dropped(tcfg):
    no_contest = sub(3)
    del no_contest["contestId"]
    raw = [("a", sub(1)), ("a", sub(2, contest=100500)), ("a", no_contest)]
    df = clean_submissions(flatten_submissions(raw), tcfg)
    assert df["submission_id"].tolist() == [1]


def test_in_flight_and_missing_verdicts_dropped(tcfg):
    raw = [("a", sub(1)), ("a", sub(2, verdict="TESTING")), ("a", sub(3, verdict=None))]
    df = clean_submissions(flatten_submissions(raw), tcfg)
    assert df["submission_id"].tolist() == [1]


def test_enrich_joins_problems_and_numbers_attempts(tcfg):
    raw = [
        ("a", sub(1, verdict="WRONG_ANSWER", t=10)),
        ("a", sub(2, t=20)),
        ("a", sub(3, index="B", t=30)),
        ("a", sub(4, index="Z", t=40)),
    ]
    problems = pd.DataFrame(
        {
            "contest_id": [1500, 1500],
            "problem_index": ["A", "B"],
            "rating": [800.0, 1200.0],
            "rating_imputed": [False, True],
        }
    )
    df = enrich_submissions(clean_submissions(flatten_submissions(raw), tcfg), problems, tcfg)
    assert set(df["submission_id"]) == {1, 2, 3}  # problem Z unknown -> not joined
    a = df[df["problem_index"] == "A"].sort_values("submission_id")
    assert a["attempt_no"].tolist() == [1, 2]
    assert a["is_accepted"].tolist() == [False, True]
    assert (df["language_family"] == "C++").all()


# ----------------------------------------------------------------- imputation


def _problems():
    return pd.DataFrame(
        {
            "division": ["Div. 2"] * 4 + ["Div. 1"] * 2 + ["Div. 3"],
            "index_letter": ["A", "A", "A", "B", "A", "A", "Q"],
            "rating": [800, 900, np.nan, np.nan, 1500, np.nan, np.nan],
        }
    )


def test_impute_uses_division_and_index_median():
    out = impute_problem_ratings(_problems())
    assert out.loc[2, "rating"] == 800 or out.loc[2, "rating"] == 900  # median 850 -> rounded
    assert out.loc[2, "rating"] % 100 == 0
    assert out.loc[5, "rating"] == 1500  # Div. 1 A median
    assert out["rating"].notna().all()
    assert out["rating_imputed"].tolist() == [False, False, True, True, False, True, True]


def test_impute_falls_back_to_global_median():
    out = impute_problem_ratings(_problems())
    # Div. 3 / Q has no peers at all -> global median of [800, 900, 1500] = 900
    assert out.loc[6, "rating"] == 900


def test_drop_strategy_removes_unrated():
    imputed = impute_problem_ratings(_problems())
    assert len(drop_unrated(imputed)) == 3
    assert len(apply_strategy(imputed, "impute")) == 7
    with pytest.raises(ValueError):
        apply_strategy(imputed, "mean")


# ----------------------------------------------------------------- scaling


def test_minmax_and_zscore():
    df = pd.DataFrame({"x": [0.0, 5.0, 10.0], "y": [1.0, 1.0, 1.0]})
    mm = minmax_scale(df, ["x", "y"])
    assert mm["x"].tolist() == [0.0, 0.5, 1.0]
    assert mm["y"].tolist() == [0.0, 0.0, 0.0]  # constant column does not divide by zero
    z = zscore_scale(df, ["x"])
    assert z["x"].mean() == pytest.approx(0)
    assert z["x"].std(ddof=0) == pytest.approx(1)


def test_scaler_fitted_on_train_is_reused_on_test():
    train = pd.DataFrame({"x": [0.0, 10.0]})
    test = pd.DataFrame({"x": [20.0]})
    params = fit_scaler(train, ["x"], "minmax")
    assert apply_scaler(test, params)["x"].iloc[0] == 2.0  # not re-fitted on test


# ----------------------------------------------------------------- concept hierarchies


def test_tag_hierarchy_examples():
    h = load_tag_hierarchy(PROJECT_ROOT / "config" / "tag_hierarchy.yaml")
    assert tag_to_category("dp", h) == "algorithms"
    assert tag_to_category("bitmasks", h) == "algorithms"
    assert tag_to_category("dfs and similar", h) == "graph theory"
    assert tag_to_category("number theory", h) == "math"
    assert tag_to_category("hashing", h) == "strings"
    assert tag_to_category("dsu", h) == "data structures"
    assert tag_to_category("brute force", h) == "implementation"
    assert tag_to_category("some new tag", h) == "other"


@pytest.mark.parametrize(
    "name,family",
    [
        ("C++20 (GCC 13-64)", "C++"),
        ("GNU C++11", "C++"),
        ("Clang++20 Diagnostics", "C++"),
        ("GNU C11", "C"),
        ("PyPy 3-64", "Python"),
        ("Python 3", "Python"),
        ("Java 21", "Java"),
        ("Kotlin 1.7", "Kotlin"),
        ("MS C#", "C#"),
        ("Rust 2021", "Rust"),
        ("Go", "Go"),
        ("Befunge", "Other"),
    ],
)
def test_language_family(tcfg, name, family):
    assert language_family(name, tcfg) == family


@pytest.mark.parametrize(
    "name,div",
    [
        ("Codeforces Round 900 (Div. 2)", "Div. 2"),
        ("Codeforces Round 901 (Div. 1)", "Div. 1"),
        ("Codeforces Round 902 (Div. 1 + Div. 2)", "Div. 1+2"),
        ("Educational Codeforces Round 150 (Rated for Div. 2)", "Div. 2"),
        ("Codeforces Global Round 25", "Global"),
        ("April Fools Day Contest 2024", "Other"),
    ],
)
def test_contest_division(tcfg, name, div):
    assert contest_division(name, tcfg) == div


def test_index_letter():
    assert problem_index_letter("C1") == "C"
    assert problem_index_letter("F") == "F"
