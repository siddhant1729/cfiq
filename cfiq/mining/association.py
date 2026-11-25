"""Tag pattern miner: frequent itemsets and association rules over solved-problem tags.

DMDW topics: association rule mining - Apriori vs FP-Growth, support / confidence /
lift, and constraint-based mining.

Transactions: one per user = the set of tags of the problems that user solved
(accepted in an official contest participation), read from the warehouse.

  support(X)        = fraction of users whose tag set contains X
  confidence(X=>Y)  = support(X u Y) / support(X)
  lift(X=>Y)        = confidence(X=>Y) / support(Y)   (> 1 means positive correlation)

Preprocessing: per-user tag sets are very dense (an active user has solved most
common tags). Items present in almost every transaction (support >=
drop_items_above_support) are removed: any rule involving them has lift ~ 1,
so they only inflate the search space.

Apriori vs FP-Growth is timed on two datasets with opposite shapes:
  * users          - few (hundreds), dense transactions
  * contest baskets- many (tens of thousands), sparse: tags one user solved in one
                     contest (the classic market-basket shape)

Constraint-based mining (two kinds):
  * data constraint  - mine only the transactions of users in one rating band
  * item constraint  - "every item belongs to tag category C". This constraint is
    anti-monotone (if an itemset breaks it, every superset does too), so it is
    PUSHED into mining by projecting the transactions onto C's tags first, instead
    of mining everything and filtering afterwards.
"""

import logging
import time

import matplotlib.pyplot as plt
import pandas as pd
from mlxtend.frequent_patterns import apriori, association_rules, fpgrowth
from mlxtend.preprocessing import TransactionEncoder

from cfiq.db import read_sql, replace_table_rows
from cfiq.mining.common import md_table, report_dir, save_fig

log = logging.getLogger(__name__)

SOLVED_TAGS_SQL = """
SELECT DISTINCT u.handle, rb.band_code, t.tag_name, c.category_name
FROM fact_submission f
JOIN dim_user u           ON u.user_key = f.user_key
JOIN dim_rating_band rb   ON rb.band_key = u.band_key
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
WHERE f.is_accepted
"""
CONTEST_BASKETS_SQL = """
SELECT DISTINCT f.user_key, f.contest_key, t.tag_name
FROM fact_submission f
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
WHERE f.is_accepted
"""
RULE_COLUMNS = ["antecedents", "consequents", "support", "confidence", "lift"]


def to_transactions(solved: pd.DataFrame, by: str | list[str] = "handle") -> list[list[str]]:
    return solved.groupby(by)["tag_name"].apply(lambda s: sorted(set(s))).tolist()


def one_hot(transactions: list[list[str]]) -> pd.DataFrame:
    """Transactions -> boolean transaction x tag matrix (the input format mlxtend expects)."""
    te = TransactionEncoder()
    return pd.DataFrame(te.fit(transactions).transform(transactions), columns=te.columns_)


def drop_ubiquitous(onehot: pd.DataFrame, max_support: float) -> tuple[pd.DataFrame, list[str]]:
    """Remove items that appear in >= max_support of transactions. Returns (matrix, dropped)."""
    support = onehot.mean()
    dropped = sorted(support[support >= max_support].index)
    return onehot.drop(columns=dropped), dropped


def compare_runtimes(
    onehot: pd.DataFrame, supports: list[float], repeats: int, max_len: int | None
) -> pd.DataFrame:
    """Time Apriori and FP-Growth at the same min_support (best of `repeats` runs)."""
    rows = []
    for s in supports:
        times, counts = {}, {}
        for name, algo in (("apriori", apriori), ("fpgrowth", fpgrowth)):
            best = float("inf")
            for _ in range(repeats):
                t0 = time.perf_counter()
                result = algo(onehot, min_support=s, use_colnames=True, max_len=max_len)
                best = min(best, time.perf_counter() - t0)
            times[name], counts[name] = best, len(result)
        assert counts["apriori"] == counts["fpgrowth"], "both algorithms must agree"
        rows.append(
            {
                "min_support": s,
                "itemsets": counts["apriori"],
                "apriori_s": times["apriori"],
                "fpgrowth_s": times["fpgrowth"],
                "apriori_over_fpgrowth": times["apriori"] / max(times["fpgrowth"], 1e-9),
            }
        )
    return pd.DataFrame(rows)


def mine_rules(
    onehot: pd.DataFrame, min_support: float, min_confidence: float, min_lift: float, max_len: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """FP-Growth itemsets -> rules filtered by confidence and lift. Returns (itemsets, rules)."""
    itemsets = fpgrowth(onehot, min_support=min_support, use_colnames=True, max_len=max_len)
    if itemsets.empty or itemsets["itemsets"].map(len).max() < 2:
        return itemsets, pd.DataFrame(columns=RULE_COLUMNS)
    rules = association_rules(
        itemsets, num_itemsets=len(onehot), metric="confidence", min_threshold=min_confidence
    )
    rules = rules[rules["lift"] >= min_lift]
    return itemsets, rules.sort_values(["lift", "confidence"], ascending=False)


def format_rules(rules: pd.DataFrame, rule_set: str) -> pd.DataFrame:
    def join(s):
        return ", ".join(sorted(s))

    return pd.DataFrame(
        {
            "rule_set": rule_set,
            "antecedents": rules["antecedents"].map(join),
            "consequents": rules["consequents"].map(join),
            "support": rules["support"].astype(float).round(4),
            "confidence": rules["confidence"].astype(float).round(4),
            "lift": rules["lift"].astype(float).round(4),
        }
    ).reset_index(drop=True)


def plot_runtimes(runtimes: dict[str, pd.DataFrame], path) -> None:
    fig, axes = plt.subplots(1, len(runtimes), figsize=(6 * len(runtimes), 4))
    for ax, (title, rt) in zip(axes, runtimes.items(), strict=True):
        ax.plot(rt["min_support"], rt["apriori_s"], marker="o", label="Apriori")
        ax.plot(rt["min_support"], rt["fpgrowth_s"], marker="s", label="FP-Growth")
        ax.set(xlabel="min_support", ylabel="runtime (s, best of repeats)", title=title)
        ax.invert_xaxis()  # lower support = harder, to the right
        ax.grid(alpha=0.3)
        ax.legend()
    fig.suptitle("Apriori vs FP-Growth (identical frequent itemsets)")
    save_fig(fig, path)


def run_association(cfg: dict, engine) -> dict:
    acfg = cfg["mining"]["association"]
    out = report_dir(cfg, "association")
    args = (acfg["min_support"], acfg["min_confidence"], acfg["min_lift"], acfg["max_len"])
    solved = read_sql(engine, SOLVED_TAGS_SQL)

    transactions = to_transactions(solved)
    onehot, dropped = drop_ubiquitous(one_hot(transactions), acfg["drop_items_above_support"])
    avg_len = sum(map(len, transactions)) / max(len(transactions), 1)
    log.info(
        "association: %d users, %.1f tags/user; dropped ubiquitous %s",
        len(onehot),
        avg_len,
        dropped,
    )

    # 1) Apriori vs FP-Growth at identical min_support values, on two data shapes
    baskets = one_hot(
        to_transactions(read_sql(engine, CONTEST_BASKETS_SQL), ["user_key", "contest_key"])
    )
    runtimes = {
        f"users: {len(onehot)} dense transactions": compare_runtimes(
            onehot, acfg["runtime_supports"], acfg["runtime_repeats"], acfg["max_len"]
        ),
        f"contest baskets: {len(baskets):,} sparse transactions": compare_runtimes(
            baskets, acfg["basket_runtime_supports"], acfg["runtime_repeats"], None
        ),
    }
    pd.concat([rt.assign(dataset=name) for name, rt in runtimes.items()]).to_csv(
        out / "runtime_comparison.csv", index=False
    )
    plot_runtimes(runtimes, out / "runtime_comparison.png")

    # 2) Rules on all users
    itemsets, rules = mine_rules(onehot, *args)
    itemsets.assign(itemsets=itemsets["itemsets"].map(lambda s: ", ".join(sorted(s)))).sort_values(
        "support", ascending=False
    ).to_csv(out / "frequent_itemsets.csv", index=False)
    all_rules = format_rules(rules, "all")

    # 3a) Data constraint: one rating band only
    band = acfg["constraint_band"]
    band_tx = to_transactions(solved[solved["band_code"] == band])
    band_rules = pd.DataFrame(columns=RULE_COLUMNS)
    if band_tx:
        band_onehot, _ = drop_ubiquitous(one_hot(band_tx), acfg["drop_items_above_support"])
        _, band_rules = mine_rules(band_onehot, *args)
    band_rules = format_rules(band_rules, f"band:{band}")

    # 3b) Item constraint pushed into mining: project transactions onto one category's tags
    cat = acfg["constraint_category"]
    cat_tx = to_transactions(solved[solved["category_name"] == cat])
    cat_tx += [[] for _ in range(len(transactions) - len(cat_tx))]  # users with none of them
    cat_onehot, _ = drop_ubiquitous(one_hot(cat_tx), acfg["drop_items_above_support"])
    _, cat_rules = mine_rules(cat_onehot, *args)
    cat_rules = format_rules(cat_rules, f"category:{cat}")

    for df, fname in (
        (all_rules, "rules_all.csv"),
        (band_rules, f"rules_band_{band}.csv"),
        (cat_rules, "rules_category.csv"),
    ):
        df.to_csv(out / fname, index=False)

    top = 200  # keep the Power BI table readable
    replace_table_rows(
        engine,
        "mine_assoc_rules",
        pd.concat(
            [all_rules.head(top), band_rules.head(top), cat_rules.head(top)], ignore_index=True
        ),
    )

    rt_md = []
    for name, rt in runtimes.items():
        rt_md += [f"### {name}", "", md_table(rt, ".4f"), ""]
    summary = [
        "# Association rules: tag pattern miner",
        "",
        f"- Transactions (users): **{len(onehot)}**, mean tags per user: **{avg_len:.1f}** "
        f"of {onehot.shape[1] + len(dropped)}",
        f"- Dropped ubiquitous items (support >= {acfg['drop_items_above_support']}): "
        f"{', '.join(dropped) or 'none'}",
        f"- min_support = {acfg['min_support']}, min_confidence = {acfg['min_confidence']}, "
        f"min_lift = {acfg['min_lift']}, max_len = {acfg['max_len']}",
        f"- Frequent itemsets: **{len(itemsets)}**; rules (all users): **{len(all_rules)}**",
        "",
        "## Apriori vs FP-Growth (same min_support, same itemsets)",
        "",
        *rt_md,
        "![runtime](runtime_comparison.png)",
        "",
        "Apriori generates candidates level by level and scans the data once per level; "
        "mlxtend's implementation does this with vectorised NumPy, which is very fast when "
        "there are few transactions (the dense per-user data). FP-Growth builds a compressed "
        "FP-tree and grows patterns without candidate generation; it wins on the large, sparse "
        "contest baskets, and its lead grows as min_support drops and the number of candidate "
        "itemsets explodes.",
        "",
        "## Top rules, all users (by lift)",
        "",
        md_table(all_rules[RULE_COLUMNS].head(15), ".3f") if len(all_rules) else "_no rules_",
        "",
        "Reading the rules: a rare tag on the left (e.g. *divide and conquer*) mostly signals "
        "an experienced user, who has therefore solved many other tags too, so many rules "
        "encode breadth of experience rather than a topic link. Restricting to one band "
        "(below) holds experience roughly constant; the category constraint keeps only "
        "within-topic links.",
        "",
        f"## Constraint: band = {band} only ({len(band_tx)} users, {len(band_rules)} rules)",
        "",
        md_table(band_rules[RULE_COLUMNS].head(10), ".3f") if len(band_rules) else "_no rules_",
        "",
        f"## Constraint: all items in category '{cat}' (pushed into mining, "
        f"{len(cat_rules)} rules)",
        "",
        md_table(cat_rules[RULE_COLUMNS].head(10), ".3f") if len(cat_rules) else "_no rules_",
    ]
    (out / "summary.md").write_text("\n".join(summary) + "\n")
    return {"rows": len(all_rules), "message": f"{len(itemsets)} itemsets, {len(all_rules)} rules"}
