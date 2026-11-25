"""Read the on-disk raw cache written by the extractor (no API calls).

Both the staging (ELT) and transform (ETL) phases start from these files.
"""

import json
from pathlib import Path

import pandas as pd

from cfiq.extract.client import CodeforcesClient


def _client(cfg: dict) -> CodeforcesClient:
    return CodeforcesClient.from_config(cfg)


def read_cached(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["response"]["result"]


def sample_handles(cfg: dict) -> list[str]:
    return pd.read_csv(cfg["paths"]["sample_file"])["handle"].tolist()


def contests_file(cfg: dict) -> Path:
    return _client(cfg).cache_path("contest.list", {"gym": "false"})


def problems_file(cfg: dict) -> Path:
    return _client(cfg).cache_path("problemset.problems")


def user_info_records(cfg: dict) -> list[dict]:
    """All cached user.info records for handles in the current sample (deduplicated)."""
    wanted = set(sample_handles(cfg))
    found = {}
    for path in sorted((cfg["paths"]["raw_cache"] / "user.info").glob("*.json")):
        for user in read_cached(path):
            if user["handle"] in wanted:
                found[user["handle"]] = user
    return list(found.values())


def per_handle_files(cfg: dict, endpoint: str):
    """Yield (handle, cache file) for each sampled handle that has a cached response."""
    client = _client(cfg)
    for h in sample_handles(cfg):
        path = client.cache_path(endpoint, {"handle": h})
        if path.exists():
            yield h, path
