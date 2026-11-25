"""Run the mining modules and record one etl_metadata row per module."""

import logging
import uuid
from datetime import UTC, datetime

from cfiq.mining.common import meta_row

log = logging.getLogger(__name__)


def run_mining(cfg: dict, engine, only: list[str] | None = None) -> list[dict]:
    from cfiq.mining.association import run_association
    from cfiq.mining.classification import run_classification
    from cfiq.mining.clustering import run_clustering
    from cfiq.mining.outliers import run_outliers

    modules = {
        "association": run_association,
        "classification": run_classification,
        "clustering": run_clustering,
        "outliers": run_outliers,
    }
    run_id = str(uuid.uuid4())
    meta = []
    for name, func in modules.items():
        if only and name not in only:
            continue
        started = datetime.now(UTC)
        log.info("----- mining: %s", name)
        result = func(cfg, engine)
        log.info("      %s -> %s", name, result["message"])
        meta.append(meta_row(run_id, name, started, result["rows"], result["message"]))
    return meta
