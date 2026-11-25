"""Stratified sampling of rated users across rating bands.

DMDW topic: data reduction by sampling (stratified random sampling).

The population (all active rated users) is split into strata = rating bands.
Each band gets a quota, then a simple random sample of that size is drawn
inside the band with a fixed seed, so the same seed always gives the same users.

Allocation:
  equal        - every band gets n / k users (good for mining: rare bands like
                 GM are not drowned out by Newbies).
  proportional - each band gets a share matching its share of the population.
If a band has fewer users than its quota, the leftover is handed to the other
bands one user at a time (in band order) until n is reached.
"""

import pandas as pd

from cfiq.transform.discretize import rating_to_band


def allocate_quotas(available: dict[str, int], n: int, allocation: str = "equal") -> dict[str, int]:
    """How many users to draw from each band. `available` = users per band, in band order."""
    bands = list(available)
    total = sum(available.values())
    n = min(n, total)

    if allocation == "equal":
        quotas = {b: n // len(bands) for b in bands}
    elif allocation == "proportional":
        quotas = {b: n * available[b] // total for b in bands}
    else:
        raise ValueError(f"unknown allocation: {allocation!r}")

    # A band cannot give more users than it has.
    quotas = {b: min(q, available[b]) for b, q in quotas.items()}

    # Hand out the remainder (rounding + shortfalls) round-robin to bands with spare users.
    while sum(quotas.values()) < n:
        for b in bands:
            if sum(quotas.values()) == n:
                break
            if quotas[b] < available[b]:
                quotas[b] += 1
    return quotas


def stratified_sample(
    users: pd.DataFrame, n: int, bands: list[dict], seed: int, allocation: str = "equal"
) -> pd.DataFrame:
    """Draw a stratified sample of `n` users. `users` needs `handle` and `rating` columns.

    Returns the sampled rows with an added `band` column, sorted by band then handle.
    """
    df = users[["handle", "rating"]].dropna().copy()
    # Sort first so the result does not depend on the order the API returned rows in.
    df = df.sort_values("handle").reset_index(drop=True)
    df["band"] = df["rating"].apply(lambda r: rating_to_band(r, bands))

    band_order = [b["code"] for b in bands]
    available = {b: int((df["band"] == b).sum()) for b in band_order}
    quotas = allocate_quotas(available, n, allocation)

    parts = []
    for band, quota in quotas.items():
        if quota > 0:
            parts.append(df[df["band"] == band].sample(n=quota, random_state=seed))
    sample = pd.concat(parts)

    sample["band"] = pd.Categorical(sample["band"], categories=band_order, ordered=True)
    return sample.sort_values(["band", "handle"]).reset_index(drop=True)
