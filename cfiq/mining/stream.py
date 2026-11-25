"""Stream mining: verdict statistics for one contest, updated incrementally.

DMDW topic: mining data streams - each element is seen once and summaries are
updated in O(1) per element instead of recomputing over all data:
  * running counters per verdict and language family, running acceptance rate
  * a sliding window (last W submissions) acceptance rate, kept with a deque
  * duplicate suppression by submission id (polls overlap)

Two sources feed the same IncrementalVerdictStats object:
  * replay (default) - a finished contest's submissions (cached once) are fed in
    chronological batches, as if they were arriving live;
  * live (--live)    - contest.status is polled every few seconds; only unseen
    submission ids are processed.
"""

import logging
import time
from collections import Counter, deque

import matplotlib.pyplot as plt
import pandas as pd

from cfiq.config import PROJECT_ROOT
from cfiq.extract.client import CodeforcesClient
from cfiq.mining.common import md_table, report_dir, save_fig
from cfiq.transform.discretize import language_family

log = logging.getLogger(__name__)


class IncrementalVerdictStats:
    """One-pass summary of a submission stream."""

    def __init__(self, window: int, tcfg: dict):
        self.tcfg = tcfg
        self.seen: set[int] = set()
        self.total = 0
        self.accepted = 0
        self.verdicts: Counter = Counter()
        self.languages: Counter = Counter()
        self.window: deque = deque(maxlen=window)  # 1 = accepted, 0 = not
        self.window_accepted = 0

    def update(self, submission: dict) -> bool:
        """Process one submission; returns False if it was already seen or still testing."""
        sid = submission["id"]
        verdict = submission.get("verdict")
        if sid in self.seen or verdict in (None, "TESTING"):
            return False
        self.seen.add(sid)
        ok = int(verdict == "OK")
        self.total += 1
        self.accepted += ok
        self.verdicts[verdict] += 1
        self.languages[language_family(submission.get("programmingLanguage"), self.tcfg)] += 1
        if len(self.window) == self.window.maxlen:
            self.window_accepted -= self.window[0]  # element about to fall out
        self.window.append(ok)
        self.window_accepted += ok
        return True

    def snapshot(self) -> dict:
        return {
            "processed": self.total,
            "acceptance_rate": self.accepted / self.total if self.total else 0.0,
            "window_acceptance_rate": self.window_accepted / len(self.window)
            if self.window
            else 0.0,
            **{f"v_{k}": v for k, v in self.verdicts.most_common(5)},
        }


def default_contest_id(cfg: dict, client: CodeforcesClient) -> int:
    contests = client.get("contest.list", {"gym": "false"})
    div = cfg["mining"]["stream"]["default_division"].replace(".", r"\.")
    import re

    finished = [c for c in contests if c["phase"] == "FINISHED" and re.search(div, c["name"])]
    return max(finished, key=lambda c: c["startTimeSeconds"])["id"]


def run_stream(cfg: dict, contest_id: int | None = None, live: bool = False, polls: int = 5):
    scfg = cfg["mining"]["stream"]
    client = CodeforcesClient.from_config(cfg)
    contest_id = contest_id or default_contest_id(cfg, client)
    stats = IncrementalVerdictStats(window=scfg["replay_batch_size"], tcfg=cfg["transform"])
    snapshots = []
    log.info("stream: contest %d (%s mode)", contest_id, "live" if live else "replay")

    if live:
        for i in range(polls):
            batch = client.get(
                "contest.status",
                {"contestId": contest_id, "from": 1, "count": scfg["live_poll_count"]},
                use_cache=False,
            )
            new = sum(stats.update(s) for s in reversed(batch))  # API returns newest first
            snapshots.append({"step": i + 1, "new": new, **stats.snapshot()})
            log.info(
                "poll %d: %d new, %d total, acceptance %.3f",
                i + 1,
                new,
                stats.total,
                snapshots[-1]["acceptance_rate"],
            )
            if i < polls - 1:
                time.sleep(scfg["live_interval_seconds"])
    else:
        subs = client.get(
            "contest.status",
            {"contestId": contest_id, "from": 1, "count": scfg["replay_max_submissions"]},
        )
        subs = sorted(subs, key=lambda s: (s["creationTimeSeconds"], s["id"]))  # oldest first
        size = scfg["replay_batch_size"]
        for i in range(0, len(subs), size):
            new = sum(stats.update(s) for s in subs[i : i + size])
            last = subs[min(i + size, len(subs)) - 1]
            snapshots.append(
                {
                    "step": i // size + 1,
                    "new": new,
                    "minute": last.get("relativeTimeSeconds", 0) // 60,
                    **stats.snapshot(),
                }
            )

    snaps = pd.DataFrame(snapshots).fillna(0)
    out = report_dir(cfg, "stream")
    snaps.to_csv(out / f"contest_{contest_id}_snapshots.csv", index=False)

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(snaps["processed"], snaps["acceptance_rate"], label="running (all so far)")
    ax.plot(
        snaps["processed"],
        snaps["window_acceptance_rate"],
        label=f"sliding window (last {scfg['replay_batch_size']})",
    )
    ax.set(
        title=f"Contest {contest_id}: acceptance rate as the stream arrives",
        xlabel="submissions processed",
        ylabel="acceptance rate",
    )
    ax.legend()
    ax.grid(alpha=0.3)
    save_fig(fig, out / f"contest_{contest_id}_acceptance.png")

    verdicts = pd.DataFrame(stats.verdicts.most_common(), columns=["verdict", "count"])
    langs = pd.DataFrame(stats.languages.most_common(), columns=["language_family", "count"])
    (out / "summary.md").write_text(
        "\n".join(
            [
                f"# Stream mining: contest {contest_id} ({'live' if live else 'replay'})",
                "",
                f"- Submissions processed: **{stats.total:,}** in {len(snaps)} batches/polls "
                f"(each submission seen once; duplicates skipped by id)",
                f"- Final acceptance rate: **{stats.accepted / max(stats.total, 1):.3f}**",
                "",
                md_table(verdicts),
                "",
                md_table(langs),
                "",
                f"![acceptance](contest_{contest_id}_acceptance.png)",
            ]
        )
        + "\n"
    )
    log.info(
        "stream: %d submissions, acceptance %.3f -> %s",
        stats.total,
        stats.accepted / max(stats.total, 1),
        out.relative_to(PROJECT_ROOT),
    )
    return stats, snaps
