"""Missing-value handling for problem difficulty ratings.

DMDW topic: data preprocessing - handling missing values.

Two strategies, both available (mining compares their effect on model metrics):
  (a) impute - fill a missing rating with the median rating of problems that share
               the same contest division AND problem index letter (e.g. "Div. 2", "C").
               Fallbacks: median of the same index letter, then the global median.
               Imputed values are rounded to the nearest 100, like real CF ratings,
               and flagged with rating_imputed = True.
  (b) drop   - discard problems without a rating (and their submissions).
"""

import pandas as pd


def impute_problem_ratings(problems: pd.DataFrame) -> pd.DataFrame:
    """Needs columns: rating, division, index_letter. Adds rating_imputed; fills rating."""
    df = problems.copy()
    df["rating_imputed"] = df["rating"].isna()

    by_div_index = df.groupby(["division", "index_letter"])["rating"].transform("median")
    by_index = df.groupby("index_letter")["rating"].transform("median")
    global_median = df["rating"].median()

    fill = by_div_index.fillna(by_index).fillna(global_median)
    fill = (fill / 100).round() * 100
    df["rating"] = df["rating"].fillna(fill)
    return df


def drop_unrated(problems: pd.DataFrame) -> pd.DataFrame:
    """Strategy (b): keep only problems whose rating was present in the source."""
    if "rating_imputed" in problems:
        return problems[~problems["rating_imputed"]].copy()
    return problems[problems["rating"].notna()].copy()


def apply_strategy(problems: pd.DataFrame, strategy: str) -> pd.DataFrame:
    """Return problems after `impute` or `drop` (problems must already be imputed+flagged)."""
    if strategy == "impute":
        return problems
    if strategy == "drop":
        return drop_unrated(problems)
    raise ValueError(f"unknown missing_rating_strategy: {strategy!r}")
