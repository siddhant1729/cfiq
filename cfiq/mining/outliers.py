"""Anomaly detector: abnormal rating jumps and submission bursts.

DMDW topic: outlier analysis - statistical (parametric z-score and
non-parametric IQR / box-plot fences) detection.

1. Rating jumps. For every rating change, compare its delta with that user's
   OWN history:  z = (delta - user_mean) / user_std.  Independently, flag deltas
   outside the global IQR fences  [Q1 - m*IQR, Q3 + m*IQR].  The first few rated
   contests are skipped, because Codeforces deliberately boosts newcomers there
   (those jumps are expected, not anomalous).
2. Submission bursts. For each (user, contest), the maximum number of submissions
   in any one-minute bucket of contest time. Rank (user, contest) pairs by the
   z-score of that maximum across all pairs. A burst must exceed the z threshold AND
   the IQR fence (the fence alone collapses here, because most values equal 1).
Output: a ranked list (reports/outliers/*.csv and the mine_anomalies table).
"""

import logging

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from cfiq.db import read_sql, replace_table_rows
from cfiq.mining.common import md_table, report_dir, save_fig

log = logging.getLogger(__name__)

RATING_SQL = """
SELECT u.handle, c.contest_id, c.name AS contest_name, r.contest_seq,
       r.old_rating, r.new_rating, r.delta, r.rank
FROM fact_rating_change r
JOIN dim_user u    ON u.user_key = r.user_key
JOIN dim_contest c ON c.contest_key = r.contest_key
"""
SUBMISSION_TIMES_SQL = """
SELECT u.handle, c.contest_id, c.name AS contest_name, f.relative_time_seconds
FROM fact_submission f
JOIN dim_user u    ON u.user_key = f.user_key
JOIN dim_contest c ON c.contest_key = f.contest_key
WHERE f.relative_time_seconds IS NOT NULL
  AND f.relative_time_seconds <= c.duration_seconds      -- during the contest only
"""


def iqr_fences(values: pd.Series, k: float) -> tuple[float, float]:
    q1, q3 = values.quantile(0.25), values.quantile(0.75)
    return q1 - k * (q3 - q1), q3 + k * (q3 - q1)


def zscore_within_group(values: pd.Series, groups: pd.Series) -> pd.Series:
    """z-score of each value relative to the mean/std of its own group."""
    g = values.groupby(groups)
    std = g.transform("std").replace(0, np.nan)
    return (values - g.transform("mean")) / std


def rating_jumps(rc: pd.DataFrame, ocfg: dict) -> tuple[pd.DataFrame, tuple[float, float]]:
    df = rc[rc["contest_seq"] > ocfg["skip_first_contests"]].copy()
    # need some history for a per-user std
    df = df[df.groupby("handle")["delta"].transform("size") >= 5]
    df["z_user"] = zscore_within_group(df["delta"], df["handle"])
    lo, hi = iqr_fences(df["delta"], ocfg["iqr_multiplier"])
    df["outside_iqr"] = (df["delta"] < lo) | (df["delta"] > hi)
    df["is_outlier"] = (df["z_user"].abs() > ocfg["z_threshold"]) | df["outside_iqr"]
    df = df.sort_values("z_user", key=lambda s: s.abs(), ascending=False)
    return df, (lo, hi)


def submission_bursts(times: pd.DataFrame, ocfg: dict) -> tuple[pd.DataFrame, tuple[float, float]]:
    w = ocfg["burst_window_seconds"]
    t = times.assign(bucket=times["relative_time_seconds"] // w)
    per_bucket = t.groupby(["handle", "contest_id", "contest_name", "bucket"]).size()
    per_contest = (
        per_bucket.groupby(["handle", "contest_id", "contest_name"])
        .agg(max_per_window="max", total="sum")
        .reset_index()
    )
    per_contest["z"] = (
        per_contest["max_per_window"] - per_contest["max_per_window"].mean()
    ) / per_contest["max_per_window"].std()
    lo, hi = iqr_fences(per_contest["max_per_window"], ocfg["iqr_multiplier"])
    per_contest["outside_iqr"] = per_contest["max_per_window"] > hi
    # Most users peak at 1 submission per minute, so Q1 = Q3 and the IQR fence collapses
    # onto the median; on this discrete, skewed data a burst must pass BOTH rules.
    per_contest["is_outlier"] = (per_contest["z"] > ocfg["z_threshold"]) & per_contest[
        "outside_iqr"
    ]
    return per_contest.sort_values("z", ascending=False), (lo, hi)


def run_outliers(cfg: dict, engine) -> dict:
    ocfg = cfg["mining"]["outliers"]
    out = report_dir(cfg, "outliers")
    top = ocfg["top_n"]

    jumps, (jlo, jhi) = rating_jumps(read_sql(engine, RATING_SQL), ocfg)
    bursts, (_, bhi) = submission_bursts(read_sql(engine, SUBMISSION_TIMES_SQL), ocfg)
    jumps.to_csv(out / "rating_jumps_all.csv", index=False)
    bursts.to_csv(out / "submission_bursts_all.csv", index=False)
    top_jumps = jumps[jumps["is_outlier"]].head(top)
    top_bursts = bursts[bursts["is_outlier"]].head(top)

    # ---- plots
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(jumps["delta"], bins=80, color="tab:blue", alpha=0.8)
    for x in (jlo, jhi):
        ax.axvline(x, color="red", ls="--")
    ax.set(
        title=f"Rating deltas (after first {ocfg['skip_first_contests']} contests) with IQR fences",
        xlabel="delta",
        ylabel="rating changes",
    )
    save_fig(fig, out / "rating_delta_hist.png")

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(
        bursts["max_per_window"],
        bins=range(1, int(bursts["max_per_window"].max()) + 2),
        color="tab:green",
        alpha=0.8,
    )
    ax.axvline(bhi, color="red", ls="--", label="IQR upper fence")
    ax.set_yscale("log")
    ax.set(
        title=f"Max submissions per {ocfg['burst_window_seconds']}s window, per (user, contest)",
        xlabel="submissions in busiest window",
        ylabel="count (log)",
    )
    ax.legend()
    save_fig(fig, out / "submission_burst_hist.png")

    # ---- ranked list -> DB
    ranked = pd.concat(
        [
            pd.DataFrame(
                {
                    "anomaly_type": "rating_jump",
                    "rank": range(1, len(top_jumps) + 1),
                    "handle": top_jumps["handle"].values,
                    "contest_id": top_jumps["contest_id"].values,
                    "contest_name": top_jumps["contest_name"].values,
                    "value": top_jumps["delta"].values,
                    "score": top_jumps["z_user"].round(3).values,
                    "detail": [
                        f"{o} -> {n} (contest #{s} for the user)"
                        for o, n, s in zip(
                            top_jumps["old_rating"],
                            top_jumps["new_rating"],
                            top_jumps["contest_seq"],
                            strict=True,
                        )
                    ],
                }
            ),
            pd.DataFrame(
                {
                    "anomaly_type": "submission_burst",
                    "rank": range(1, len(top_bursts) + 1),
                    "handle": top_bursts["handle"].values,
                    "contest_id": top_bursts["contest_id"].values,
                    "contest_name": top_bursts["contest_name"].values,
                    "value": top_bursts["max_per_window"].values,
                    "score": top_bursts["z"].round(3).values,
                    "detail": [
                        f"{m} submissions in one {ocfg['burst_window_seconds']}s window, "
                        f"{t} in the contest"
                        for m, t in zip(
                            top_bursts["max_per_window"], top_bursts["total"], strict=True
                        )
                    ],
                }
            ),
        ],
        ignore_index=True,
    )
    ranked.to_csv(out / "ranked_anomalies.csv", index=False)
    replace_table_rows(engine, "mine_anomalies", ranked)

    jcols = ["handle", "contest_name", "contest_seq", "old_rating", "new_rating", "delta", "z_user"]
    bcols = ["handle", "contest_name", "max_per_window", "total", "z"]
    summary = [
        "# Outliers: anomaly detector",
        "",
        "## Abnormal rating jumps",
        "",
        f"- Rating changes considered: **{len(jumps):,}** (skipping each user's first "
        f"{ocfg['skip_first_contests']} contests; users with >= 5 remaining changes)",
        f"- Global IQR fences on delta: [{jlo:.0f}, {jhi:.0f}]; per-user |z| > "
        f"{ocfg['z_threshold']}",
        f"- Flagged: **{int(jumps['is_outlier'].sum()):,}** "
        f"(z-rule: {int((jumps['z_user'].abs() > ocfg['z_threshold']).sum())}, "
        f"IQR rule: {int(jumps['outside_iqr'].sum())})",
        "",
        md_table(top_jumps[jcols].head(15), ".2f"),
        "",
        "![deltas](rating_delta_hist.png)",
        "",
        "## Submission bursts",
        "",
        f"- (user, contest) pairs: **{len(bursts):,}**; IQR upper fence: {bhi:.1f} "
        f"submissions per {ocfg['burst_window_seconds']} s",
        f"- Flagged: **{int(bursts['is_outlier'].sum()):,}** (z > {ocfg['z_threshold']} AND "
        f"above the fence; the IQR fence alone would flag {int(bursts['outside_iqr'].sum()):,}, "
        "because most users never exceed 1 submission per minute, so Q1 = Q3 and IQR = 0)",
        "",
        md_table(top_bursts[bcols].head(15), ".2f"),
        "",
        "![bursts](submission_burst_hist.png)",
        "",
        "A flagged item is a candidate for review, not proof of wrongdoing: a big jump can be "
        "a genuinely great contest, and bursts often come from resubmitting after a "
        "wrong-answer fix or from interactive / output-only problems.",
    ]
    (out / "summary.md").write_text("\n".join(summary) + "\n")
    return {"rows": len(ranked), "message": f"{len(top_jumps)} jumps, {len(top_bursts)} bursts"}
