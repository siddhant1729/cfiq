"""Small helpers shared by the mining modules (report folders, figures, markdown tables)."""

from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # render to files; no display needed
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


def report_dir(cfg: dict, name: str) -> Path:
    d = cfg["paths"]["reports"] / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_fig(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    """DataFrame -> GitHub markdown table (no extra dependency)."""

    def fmt(v):
        if isinstance(v, float):
            return format(v, floatfmt)
        return str(v)

    header = "| " + " | ".join(str(c) for c in df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = [
        "| " + " | ".join(fmt(v) for v in row) + " |"
        for row in df.itertuples(index=False, name=None)
    ]
    return "\n".join([header, sep, *rows])


def meta_row(run_id: str, name: str, started: datetime, rows: int, message=None) -> dict:
    return {
        "run_id": run_id,
        "phase": "mine",
        "endpoint": name,
        "started_at": started,
        "finished_at": datetime.now(UTC),
        "row_count": rows,
        "api_calls": 0,
        "cache_hits": 0,
        "status": "success",
        "message": message,
    }
