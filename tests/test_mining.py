"""Tests for mining helpers that are easy to get subtly wrong (leakage, streams, OLAP parsing)."""

import pandas as pd

from cfiq.mining.association import mine_rules, one_hot
from cfiq.mining.classification import past_solve_rates, time_split, user_rating_before
from cfiq.mining.outliers import iqr_fences, zscore_within_group
from cfiq.mining.stream import IncrementalVerdictStats
from cfiq.warehouse.olap import parse_named_queries

T = pd.Timestamp


def test_past_solve_rates_use_only_earlier_contests():
    pairs = pd.DataFrame(
        {
            "user_key": [1, 1, 1],
            "contest_start": [
                T("2024-01-01", tz="UTC"),
                T("2024-02-01", tz="UTC"),
                T("2024-03-01", tz="UTC"),
            ],
            "problem_key": [10, 11, 12],
            "solved": [1, 0, 1],
        }
    )
    cats = pd.DataFrame({"problem_key": [10, 11, 12], "category": ["math"] * 3})
    r = past_solve_rates(pairs, cats, ["math"]).set_index("contest_start")["past_rate_math"]
    assert r.iloc[0] == 0.5  # nothing before -> prior (0+1)/(0+2)
    assert r.iloc[1] == (1 + 1) / (1 + 2)  # only contest 1 counted, NOT contest 2 itself
    assert r.iloc[2] == (1 + 1) / (2 + 2)


def test_user_rating_is_taken_before_the_contest():
    pairs = pd.DataFrame(
        {
            "user_key": [1, 1],
            "contest_start": [T("2024-01-01", tz="UTC"), T("2024-03-01", tz="UTC")],
        }
    )
    ratings = pd.DataFrame(
        {"user_key": [1], "rating_updated_at": [T("2024-01-01", tz="UTC")], "new_rating": [1700]}
    )
    r = user_rating_before(pairs, ratings, default=1400)
    assert r.tolist() == [1400, 1700]  # the same-moment update is not visible yet


def test_time_split_never_mixes_a_contest():
    starts = [T(f"2024-01-{d:02d}", tz="UTC") for d in range(1, 11)]
    df = pd.DataFrame({"contest_start": starts * 3, "contest_key": list(range(10)) * 3})
    train, test = time_split(df, 0.2)
    assert train["contest_start"].max() < test["contest_start"].min()
    assert not set(train["contest_key"]) & set(test["contest_key"])


def test_rules_have_expected_metrics():
    tx = [["dp", "greedy"], ["dp", "greedy"], ["dp", "math"], ["math"]]
    _, rules = mine_rules(one_hot(tx), min_support=0.5, min_confidence=0.6, min_lift=1.0, max_len=2)
    r = rules[
        (rules["antecedents"] == frozenset({"greedy"}))
        & (rules["consequents"] == frozenset({"dp"}))
    ].iloc[0]
    assert r["support"] == 0.5 and r["confidence"] == 1.0
    assert abs(r["lift"] - 1 / 0.75) < 1e-9


def test_outlier_helpers():
    lo, hi = iqr_fences(pd.Series([1, 2, 3, 4, 100]), 1.5)
    assert hi < 100
    z = zscore_within_group(pd.Series([1.0, 3.0, 10.0, 10.0]), pd.Series(["a", "a", "b", "b"]))
    assert z.iloc[0] < 0 < z.iloc[1] and z.iloc[2:].isna().all()  # std 0 -> undefined


def test_stream_stats_are_incremental_and_skip_duplicates(tcfg):
    s = IncrementalVerdictStats(window=2, tcfg=tcfg)
    batch = [
        {"id": 1, "verdict": "OK", "programmingLanguage": "Python 3"},
        {"id": 2, "verdict": "WRONG_ANSWER", "programmingLanguage": "GNU C++17"},
        {"id": 3, "verdict": "TESTING"},
    ]
    assert sum(s.update(x) for x in batch) == 2
    assert sum(s.update(x) for x in batch) == 0  # same poll again: nothing new
    s.update({"id": 4, "verdict": "OK", "programmingLanguage": "Java 21"})
    snap = s.snapshot()
    assert snap["processed"] == 3 and abs(snap["acceptance_rate"] - 2 / 3) < 1e-9
    assert snap["window_acceptance_rate"] == 0.5  # window = last 2: WA, OK


def test_olap_file_parses_into_named_queries():
    from cfiq.warehouse.olap import OLAP_FILE

    q = parse_named_queries(OLAP_FILE.read_text())
    for name in (
        "rollup_tag_to_category",
        "drilldown_category_to_tag",
        "slice_python",
        "dice_experts_cpp_python_recent",
        "pivot_category_by_band",
        "rollup_operator",
        "cube_operator",
        "grouping_sets_operator",
    ):
        code = [ln for ln in q[name].splitlines() if not ln.lstrip().startswith("--")]
        assert name in q and code[0].upper().startswith("SELECT")
