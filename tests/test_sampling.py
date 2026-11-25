"""Tests for rating-band discretization and stratified sampling."""

import pandas as pd
import pytest

from cfiq.config import load_config
from cfiq.extract.sampling import allocate_quotas, stratified_sample
from cfiq.transform.discretize import rating_to_band

BANDS = load_config()["rating_bands"]


@pytest.mark.parametrize(
    "rating,band",
    [
        (0, "NEWBIE"),
        (1199, "NEWBIE"),
        (1200, "PUPIL"),
        (1399, "PUPIL"),
        (1400, "SPEC"),
        (1599, "SPEC"),
        (1600, "EXPERT"),
        (1899, "EXPERT"),
        (1900, "CM"),
        (2099, "CM"),
        (2100, "MASTER"),
        (2399, "MASTER"),
        (2400, "GM"),
        (3800, "GM"),
        (None, None),
    ],
)
def test_rating_to_band_boundaries(rating, band):
    assert rating_to_band(rating, BANDS) == band


def test_equal_allocation():
    q = allocate_quotas({"A": 100, "B": 100, "C": 100}, 30, "equal")
    assert q == {"A": 10, "B": 10, "C": 10}


def test_remainder_goes_round_robin():
    q = allocate_quotas({"A": 100, "B": 100, "C": 100}, 32, "equal")
    assert q == {"A": 11, "B": 11, "C": 10}


def test_small_band_shortfall_is_redistributed():
    q = allocate_quotas({"A": 100, "B": 2, "C": 100}, 30, "equal")
    assert q["B"] == 2 and sum(q.values()) == 30


def test_proportional_allocation():
    q = allocate_quotas({"A": 800, "B": 200}, 10, "proportional")
    assert q == {"A": 8, "B": 2}


def test_n_larger_than_population():
    q = allocate_quotas({"A": 3, "B": 2}, 100, "equal")
    assert q == {"A": 3, "B": 2}


def _population():
    ratings = list(range(800, 3600, 7))  # ~400 users spread across all bands
    return pd.DataFrame({"handle": [f"u{i}" for i in range(len(ratings))], "rating": ratings})


def test_stratified_sample_is_deterministic_and_balanced():
    pop = _population()
    s1 = stratified_sample(pop, 70, BANDS, seed=42)
    s2 = stratified_sample(pop.sample(frac=1, random_state=1), 70, BANDS, seed=42)  # shuffled input
    assert s1["handle"].tolist() == s2["handle"].tolist()
    assert len(s1) == 70
    assert set(s1["band"].value_counts()) == {10}  # 70 / 7 bands
    assert s1["handle"].is_unique


def test_different_seed_gives_different_sample():
    pop = _population()
    a = stratified_sample(pop, 70, BANDS, seed=1)["handle"]
    b = stratified_sample(pop, 70, BANDS, seed=2)["handle"]
    assert set(a) != set(b)
