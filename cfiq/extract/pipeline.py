"""Extract pipeline: pull everything CFIQ needs from the Codeforces API into the raw cache.

DMDW topic: extraction + ETL metadata (an audit row per endpoint per run).

Order of steps:
  1. contest.list?gym=false       - all non-gym contests
  2. problemset.problems          - all problems (+ solve counts)
  3. user.ratedList?activeOnly    - the population we sample from
  4. stratified sample            - N users across rating bands (data reduction)
  5. user.info                    - profile for each sampled user (batched)
  6. user.rating?handle=          - rating history per sampled user
  7. user.status?handle=          - all submissions per sampled user

Raw responses go to data/raw/ (via the client cache). The chosen sample is saved
to data/sample_users.csv so later phases know which handles belong to the run.
"""

import logging
import uuid
from datetime import UTC, datetime

from cfiq.extract.client import CodeforcesClient, PermanentAPIError
from cfiq.extract.sampling import stratified_sample

log = logging.getLogger(__name__)


class StepTracker:
    """Collects one etl_metadata row per step (timing, rows, API calls vs cache hits)."""

    def __init__(self, run_id: str, client: CodeforcesClient):
        self.run_id = run_id
        self.client = client
        self.rows: list[dict] = []

    def start(self, endpoint: str) -> dict:
        return {
            "run_id": self.run_id,
            "phase": "extract",
            "endpoint": endpoint,
            "started_at": datetime.now(UTC),
            "_calls0": self.client.api_calls,
            "_hits0": self.client.cache_hits,
        }

    def finish(self, step: dict, row_count: int, status: str = "success", message=None) -> None:
        step["finished_at"] = datetime.now(UTC)
        step["row_count"] = row_count
        step["api_calls"] = self.client.api_calls - step.pop("_calls0")
        step["cache_hits"] = self.client.cache_hits - step.pop("_hits0")
        step["status"] = status
        step["message"] = message
        self.rows.append(step)
        log.info(
            "%-26s rows=%-8d api_calls=%-4d cache_hits=%-4d %s",
            step["endpoint"],
            row_count,
            step["api_calls"],
            step["cache_hits"],
            status,
        )


def fetch_user_info(client: CodeforcesClient, handles: list[str], batch_size: int):
    """user.info in batches. If a batch fails because one handle no longer exists
    (renamed / deleted), retry that batch one handle at a time and skip the bad ones."""
    users, missing = [], []
    for i in range(0, len(handles), batch_size):
        batch = handles[i : i + batch_size]
        try:
            users += client.get("user.info", {"handles": ";".join(batch)})
        except PermanentAPIError:
            for h in batch:
                try:
                    users += client.get("user.info", {"handles": h})
                except PermanentAPIError:
                    missing.append(h)
    return users, missing


def fetch_per_handle(client: CodeforcesClient, endpoint: str, handles: list[str]):
    """Call `endpoint?handle=` for each handle; returns (total rows, handles that failed)."""
    total, missing = 0, []
    for i, h in enumerate(handles, 1):
        try:
            total += len(client.get(endpoint, {"handle": h}))
        except PermanentAPIError:
            missing.append(h)
        if i % 10 == 0 or i == len(handles):
            log.info("  %s: %d/%d users", endpoint, i, len(handles))
    return total, missing


def _status(missing: list[str]) -> tuple[str, str | None]:
    if not missing:
        return "success", None
    return "partial", f"{len(missing)} handle(s) not found: {', '.join(missing[:10])}"


def run_extract(cfg: dict, sample_size: int | None = None, seed: int | None = None):
    """Run all extraction steps. Returns (run_id, list of etl_metadata rows, sample DataFrame)."""
    import pandas as pd

    sample_size = sample_size or cfg["sampling"]["sample_size"]
    seed = seed if seed is not None else cfg["sampling"]["seed"]
    run_id = str(uuid.uuid4())
    client = CodeforcesClient.from_config(cfg)
    track = StepTracker(run_id, client)
    log.info("extract run %s  (N=%d, seed=%d)", run_id, sample_size, seed)

    step = track.start("contest.list")
    contests = client.get("contest.list", {"gym": "false"})
    track.finish(step, len(contests))

    step = track.start("problemset.problems")
    problems = client.get("problemset.problems")
    track.finish(step, len(problems["problems"]))

    step = track.start("user.ratedList")
    rated = client.get("user.ratedList", {"activeOnly": "true"})
    track.finish(step, len(rated))

    step = track.start("sample.stratified")
    sample = stratified_sample(
        pd.DataFrame(rated), sample_size, cfg["rating_bands"], seed, cfg["sampling"]["allocation"]
    )
    sample_file = cfg["paths"]["sample_file"]
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(sample_file, index=False)
    allocation = cfg["sampling"]["allocation"]
    track.finish(step, len(sample), message=f"seed={seed} allocation={allocation}")
    handles = sample["handle"].tolist()

    step = track.start("user.info")
    users, missing = fetch_user_info(client, handles, cfg["api"]["user_info_batch_size"])
    track.finish(step, len(users), *_status(missing))

    step = track.start("user.rating")
    n_rows, missing = fetch_per_handle(client, "user.rating", handles)
    track.finish(step, n_rows, *_status(missing))

    step = track.start("user.status")
    n_rows, missing = fetch_per_handle(client, "user.status", handles)
    track.finish(step, n_rows, *_status(missing))

    return run_id, track.rows, sample
