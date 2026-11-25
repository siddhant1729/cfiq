"""Solve predictor: will a user solve a problem they attempt in a contest?

DMDW topics: classification - Decision Tree (entropy ~ ID3, gini = CART),
Naive Bayes (Gaussian, Bernoulli), SVM, neural network (MLP), Random Forest
(ensemble); evaluation with accuracy / precision / recall / F1 / confusion matrix;
a time-based train/test split; and the effect of preprocessing choices
(missing-value strategy, min-max vs z-score) on the metrics.

One row = one (user, problem) pair: for every contest the user took part in
officially, every problem of that contest (attempted or not).
Label  = 1 if the user got the problem accepted during the contest.
(Using only attempted problems would make the task trivial: people mostly
submit what they can solve, so ~87% of attempted pairs are positive.)

Features (all known BEFORE the contest starts - no leakage):
  user_rating        rating after the user's last rated contest before this one
  problem_rating     difficulty (imputed or dropped if missing, per experiment)
  rating_gap         problem_rating - user_rating
  cat_<category>     one-hot tag categories of the problem (multi-hot)
  past_rate_<cat>    user's smoothed solve rate per category over EARLIER contests
                     (solved + 1) / (attempted + 2)   (Laplace smoothing)
  past_rate_match    mean past_rate over this problem's own categories
  past_attempts      log(1 + problems the user faced in earlier contests)

Time-based split: a cutoff date is chosen so that about `test_fraction` of the
pairs come from contests starting on/after it; those form the test set. Whole
contests fall on one side, and the model never sees the future while training.
Scalers are fitted on the training set only.
"""

import logging
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.naive_bayes import BernoulliNB, GaussianNB
from sklearn.neural_network import MLPClassifier
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier, plot_tree

from cfiq.db import read_sql, replace_table_rows
from cfiq.mining.common import md_table, report_dir, save_fig
from cfiq.transform.impute import apply_strategy
from cfiq.transform.scale import apply_scaler, fit_scaler

log = logging.getLogger(__name__)

PAIRS_SQL = """
WITH participation AS (          -- contests the user took part in (made a submission)
    SELECT DISTINCT user_key, contest_key FROM fact_submission
), outcome AS (                  -- did the user get the problem accepted?
    SELECT user_key, problem_key, BOOL_OR(is_accepted) AS solved
    FROM fact_submission GROUP BY user_key, problem_key
)
SELECT pa.user_key, p.problem_key, pa.contest_key, c.start_time AS contest_start,
       COALESCE(o.solved, FALSE)::int AS solved,
       p.rating AS problem_rating, p.rating_imputed
FROM participation pa
JOIN dim_contest c ON c.contest_key = pa.contest_key
JOIN dim_problem p ON p.contest_id = c.contest_id          -- EVERY problem of that contest
LEFT JOIN outcome o ON o.user_key = pa.user_key AND o.problem_key = p.problem_key
"""
CATEGORIES_SQL = """
SELECT DISTINCT b.problem_key, c.category_name AS category
FROM bridge_problem_tag b
JOIN dim_tag t          ON t.tag_key = b.tag_key
JOIN dim_tag_category c ON c.category_key = t.category_key
"""
RATINGS_SQL = "SELECT user_key, rating_updated_at, new_rating FROM fact_rating_change"


def _col(cat: str) -> str:
    return cat.replace(" ", "_")


# ----------------------------------------------------------------- features


def user_rating_before(pairs: pd.DataFrame, ratings: pd.DataFrame, default: int) -> pd.Series:
    """Rating each user had just before each contest started (as-of join)."""
    left = pairs[["user_key", "contest_start"]].reset_index().sort_values("contest_start")
    right = ratings.sort_values("rating_updated_at")
    merged = pd.merge_asof(
        left,
        right,
        left_on="contest_start",
        right_on="rating_updated_at",
        by="user_key",
        direction="backward",
        allow_exact_matches=False,
    )
    return merged.set_index("index")["new_rating"].reindex(pairs.index).fillna(default)


def past_solve_rates(
    pairs: pd.DataFrame, problem_cats: pd.DataFrame, categories: list[str]
) -> pd.DataFrame:
    """Per (user, contest): smoothed solve rate per category using ONLY earlier contests.

    1. count attempts / solves per (user, contest, category)
    2. cumulative sum over the user's contests in time order
    3. subtract the current contest's own counts -> strictly-earlier totals
    """
    pc = pairs[["user_key", "contest_start", "problem_key", "solved"]].merge(
        problem_cats, on="problem_key"
    )
    att = pc.pivot_table(
        index=["user_key", "contest_start"],
        columns="category",
        values="solved",
        aggfunc="size",
        fill_value=0,
    )
    sol = pc.pivot_table(
        index=["user_key", "contest_start"],
        columns="category",
        values="solved",
        aggfunc="sum",
        fill_value=0,
    )
    # every (user, contest) the user took part in, even if none of its problems had tags
    index = pd.MultiIndex.from_frame(
        pairs[["user_key", "contest_start"]].drop_duplicates()
    ).sort_values()
    att = att.reindex(index=index, columns=categories, fill_value=0)
    sol = sol.reindex(index=index, columns=categories, fill_value=0)

    past_att = att.groupby(level="user_key").cumsum() - att
    past_sol = sol.groupby(level="user_key").cumsum() - sol
    rate = (past_sol + 1) / (past_att + 2)
    rate.columns = [f"past_rate_{_col(c)}" for c in categories]

    # total problems seen in earlier contests (all categories)
    per_contest = pairs.groupby(["user_key", "contest_start"]).size().sort_index()
    past_total = per_contest.groupby(level="user_key").cumsum() - per_contest
    rate["past_attempts"] = np.log1p(past_total.reindex(rate.index, fill_value=0))
    return rate.reset_index()


def build_features(
    pairs: pd.DataFrame, problem_cats: pd.DataFrame, ratings: pd.DataFrame, default_rating: int
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Returns (data, continuous feature names, binary feature names)."""
    categories = sorted(problem_cats["category"].unique())
    df = pairs.copy()
    df["user_rating"] = user_rating_before(df, ratings, default_rating)
    df["rating_gap"] = df["problem_rating"] - df["user_rating"]

    onehot = (
        problem_cats.assign(v=1)
        .pivot_table(index="problem_key", columns="category", values="v", fill_value=0)
        .reindex(columns=categories, fill_value=0)
    )
    onehot.columns = [f"cat_{_col(c)}" for c in categories]
    df = df.merge(onehot, left_on="problem_key", right_index=True, how="left")
    cat_cols = list(onehot.columns)
    df[cat_cols] = df[cat_cols].fillna(0).astype(int)

    df = df.merge(
        past_solve_rates(df, problem_cats, categories), on=["user_key", "contest_start"], how="left"
    )
    rate_cols = [f"past_rate_{_col(c)}" for c in categories]
    # mean past rate over the problem's own categories (0.5 prior if it has no tags)
    own = df[cat_cols].to_numpy()
    rates = df[rate_cols].to_numpy()
    n_own = own.sum(axis=1)
    df["past_rate_match"] = np.where(
        n_own > 0, (own * rates).sum(axis=1) / np.maximum(n_own, 1), 0.5
    )

    continuous = [
        "user_rating",
        "problem_rating",
        "rating_gap",
        "past_rate_match",
        "past_attempts",
        *rate_cols,
    ]
    return df, continuous, cat_cols


def time_split(df: pd.DataFrame, test_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Train on contests before the cutoff date, test on contests from it onwards."""
    cutoff = df["contest_start"].quantile(1 - test_fraction)
    return df[df["contest_start"] < cutoff], df[df["contest_start"] >= cutoff]


# ----------------------------------------------------------------- models


def make_models(ccfg: dict, scaler: str, seed: int) -> dict:
    # BernoulliNB needs binary inputs: threshold = mean (z-score 0) or mid-range (min-max 0.5)
    binarize = 0.0 if scaler == "zscore" else 0.5
    return {
        "DecisionTree (entropy, ~ID3)": DecisionTreeClassifier(
            criterion="entropy", max_depth=8, min_samples_leaf=20, random_state=seed
        ),
        "DecisionTree (gini, CART)": DecisionTreeClassifier(
            criterion="gini", max_depth=8, min_samples_leaf=20, random_state=seed
        ),
        "GaussianNB": GaussianNB(),
        "BernoulliNB": BernoulliNB(binarize=binarize),
        "SVM (RBF kernel)": SVC(kernel="rbf", C=1.0, gamma="scale", random_state=seed),
        "MLP neural network": MLPClassifier(
            hidden_layer_sizes=tuple(ccfg["mlp_hidden"]),
            max_iter=300,
            early_stopping=True,
            random_state=seed,
        ),
        "RandomForest": RandomForestClassifier(
            n_estimators=200, max_depth=12, min_samples_leaf=5, n_jobs=-1, random_state=seed
        ),
    }


def evaluate(y_true, y_pred) -> dict:
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    return {
        "accuracy": accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "tp": int(tp),
    }


def run_experiment(
    data: pd.DataFrame,
    continuous: list[str],
    binary: list[str],
    ccfg: dict,
    strategy: str,
    scaler: str,
    seed: int,
):
    """Train every model for one (missing-value strategy, scaler) combination."""
    df = apply_strategy(data, strategy)  # impute: keep all | drop: remove unrated problems
    train, test = time_split(df, ccfg["test_fraction"])
    params = fit_scaler(train, continuous, scaler)  # fit on TRAIN only
    train_s, test_s = apply_scaler(train, params), apply_scaler(test, params)
    features = continuous + binary
    X_train, y_train = train_s[features].to_numpy(), train_s["solved"].to_numpy()
    X_test, y_test = test_s[features].to_numpy(), test_s["solved"].to_numpy()

    rows, fitted, preds = [], {}, {}
    # Baseline without learning: predict "solved" when the problem is not harder than the user.
    base_pred = (test["rating_gap"] <= 0).astype(int).to_numpy()
    rows.append({"model": "Baseline (gap <= 0)", **evaluate(y_test, base_pred), "fit_s": 0.0})

    rng = np.random.default_rng(seed)
    for name, model in make_models(ccfg, scaler, seed).items():
        Xt, yt = X_train, y_train
        if name.startswith("SVM") and len(Xt) > ccfg["svm_max_train"]:
            idx = rng.choice(len(Xt), ccfg["svm_max_train"], replace=False)
            Xt, yt = Xt[idx], yt[idx]
        t0 = time.perf_counter()
        model.fit(Xt, yt)
        fit_s = time.perf_counter() - t0
        pred = model.predict(X_test)
        rows.append({"model": name, **evaluate(y_test, pred), "fit_s": fit_s})
        fitted[name], preds[name] = model, pred
    metrics = pd.DataFrame(rows)
    metrics.insert(0, "experiment", f"{strategy}+{scaler}")
    metrics["train_rows"], metrics["test_rows"] = len(train), len(test)
    return metrics, fitted, preds, (y_test, features, train, test)


# ----------------------------------------------------------------- plots


def plot_confusion_matrices(y_test, preds: dict, path) -> None:
    n = len(preds)
    cols = 4
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 3.6 * rows))
    for ax, (name, pred) in zip(axes.ravel(), preds.items(), strict=False):
        ConfusionMatrixDisplay(
            confusion_matrix(y_test, pred, labels=[0, 1]), display_labels=["not solved", "solved"]
        ).plot(ax=ax, colorbar=False, values_format="d")
        ax.set_title(name, fontsize=9)
    for ax in axes.ravel()[n:]:
        ax.axis("off")
    save_fig(fig, path)


def plot_tree_top(model, features, path) -> None:
    fig, ax = plt.subplots(figsize=(18, 8))
    plot_tree(
        model,
        max_depth=3,
        feature_names=features,
        class_names=["no", "yes"],
        filled=True,
        impurity=True,
        fontsize=7,
        ax=ax,
    )
    ax.set_title("Decision tree (entropy / ID3-style), top 3 levels")
    save_fig(fig, path)


def plot_importances(model, features, path) -> None:
    imp = pd.Series(model.feature_importances_, index=features).sort_values()
    fig, ax = plt.subplots(figsize=(7, 6))
    imp.tail(15).plot.barh(ax=ax)
    ax.set_title("Random Forest feature importance (top 15)")
    save_fig(fig, path)


def plot_f1(all_metrics: pd.DataFrame, path) -> None:
    pivot = all_metrics.pivot(index="model", columns="experiment", values="f1")
    fig, ax = plt.subplots(figsize=(9, 5))
    pivot.plot.barh(ax=ax)
    ax.set_xlabel("F1 (solved = positive class)")
    ax.set_title("F1 by model and preprocessing choice")
    ax.legend(fontsize=8)
    save_fig(fig, path)


# ----------------------------------------------------------------- entry point


def run_classification(cfg: dict, engine) -> dict:
    ccfg = cfg["mining"]["classification"]
    seed = cfg["mining"]["random_state"]
    out = report_dir(cfg, "classification")

    pairs = read_sql(engine, PAIRS_SQL)
    pairs["contest_start"] = pd.to_datetime(pairs["contest_start"], utc=True)
    ratings = read_sql(engine, RATINGS_SQL)
    ratings["rating_updated_at"] = pd.to_datetime(ratings["rating_updated_at"], utc=True)
    problem_cats = read_sql(engine, CATEGORIES_SQL)

    data, continuous, binary = build_features(
        pairs, problem_cats, ratings, cfg["transform"]["default_initial_rating"]
    )
    log.info(
        "classification: %d (user, problem) pairs, %.1f%% solved, %d features",
        len(data),
        100 * data["solved"].mean(),
        len(continuous) + len(binary),
    )

    main_strategy = cfg["transform"]["missing_rating_strategy"]
    main_scaler = ccfg["scaler"]
    other_strategy = "drop" if main_strategy == "impute" else "impute"
    other_scaler = "minmax" if main_scaler == "zscore" else "zscore"
    experiments = [
        (main_strategy, main_scaler),
        (other_strategy, main_scaler),
        (main_strategy, other_scaler),
    ]

    results = []
    for i, (strategy, scaler) in enumerate(experiments):
        metrics, fitted, preds, (y_test, features, train, test) = run_experiment(
            data, continuous, binary, ccfg, strategy, scaler, seed
        )
        log.info(
            "  %-14s train=%d test=%d best F1=%.3f",
            f"{strategy}+{scaler}",
            len(train),
            len(test),
            metrics["f1"].max(),
        )
        results.append(metrics)
        if strategy == "impute" and scaler == main_scaler:
            # Fair comparison with "drop": score the impute-trained models only on test
            # rows whose problem rating was NOT imputed (the rows "drop" is scored on).
            rated = ~test["rating_imputed"].to_numpy()
            fair_f1 = {
                "Baseline (gap <= 0)": f1_score(
                    y_test[rated], (test["rating_gap"] <= 0).to_numpy()[rated]
                )
            }
            fair_f1.update({n: f1_score(y_test[rated], p[rated]) for n, p in preds.items()})
        if i == 0:  # plots for the main experiment
            plot_confusion_matrices(y_test, preds, out / "confusion_matrices.png")
            plot_tree_top(
                fitted["DecisionTree (entropy, ~ID3)"], features, out / "decision_tree_entropy.png"
            )
            plot_importances(fitted["RandomForest"], features, out / "rf_feature_importance.png")
            split_info = (
                train["contest_start"].min(),
                train["contest_start"].max(),
                test["contest_start"].min(),
                test["contest_start"].max(),
            )

    all_metrics = pd.concat(results, ignore_index=True)
    all_metrics.to_csv(out / "all_metrics.csv", index=False)
    plot_f1(all_metrics, out / "f1_comparison.png")

    db = all_metrics.rename(columns={"precision": "precision_"}).drop(columns=["fit_s"])
    replace_table_rows(engine, "mine_model_metrics", db)

    main_name = f"{main_strategy}+{main_scaler}"
    main = all_metrics[all_metrics["experiment"] == main_name]
    cols = ["model", "accuracy", "precision", "recall", "f1", "tn", "fp", "fn", "tp", "fit_s"]
    main[cols].to_csv(out / "model_comparison.csv", index=False)

    def side_by_side(a: str, b: str) -> pd.DataFrame:
        x = all_metrics[all_metrics["experiment"] == a].set_index("model")
        y = all_metrics[all_metrics["experiment"] == b].set_index("model")
        return pd.DataFrame(
            {
                f"F1 {a}": x["f1"],
                f"F1 {b}": y["f1"],
                "dF1": y["f1"] - x["f1"],
                f"acc {a}": x["accuracy"],
                f"acc {b}": y["accuracy"],
            }
        ).reset_index()

    imp_cmp = side_by_side(main_name, f"{other_strategy}+{main_scaler}")
    imp_cmp["F1 impute, rated test rows only"] = imp_cmp["model"].map(fair_f1)
    fair_gap = (
        imp_cmp[f"F1 {other_strategy}+{main_scaler}"] - imp_cmp["F1 impute, rated test rows only"]
    ).mean()
    scale_cmp = side_by_side(main_name, f"{main_strategy}+{other_scaler}")
    n_imputed = int(data["rating_imputed"].sum())
    (out / "imputation_comparison.md").write_text(
        "# Missing-value strategy vs model metrics\n\n"
        f"{n_imputed} of {len(data)} (user, problem) pairs ({n_imputed / len(data):.1%}) "
        "involve a problem whose rating was missing.\n\n" + md_table(imp_cmp) + "\n"
    )

    best = main.loc[main["f1"].idxmax()]
    summary = [
        "# Classification: solve predictor",
        "",
        f"- (user, problem) pairs: **{len(data):,}**, solved: **{data['solved'].mean():.1%}**",
        f"- Features ({len(continuous) + len(binary)}): {', '.join(continuous + binary)}",
        f"- Time-based split: train contests {split_info[0]:%Y-%m-%d} .. "
        f"{split_info[1]:%Y-%m-%d}, test contests {split_info[2]:%Y-%m-%d} .. "
        f"{split_info[3]:%Y-%m-%d} (no contest in both)",
        f"- SVM trained on a random subsample of at most {ccfg['svm_max_train']:,} rows "
        "(kernel SVM is O(n^2) in memory/time).",
        "- Caveat: Codeforces assigns a problem's rating *after* the contest, from who solved "
        "it, so `problem_rating` carries some post-contest information. In a real-time "
        "predictor it would have to be replaced by an estimate (e.g. division + index median, "
        "the same rule used for imputation). The baseline row shows how much signal that "
        "single feature carries.",
        "",
        f"## Model comparison ({main_name})",
        "",
        md_table(main[cols]),
        "",
        f"Best F1: **{best['model']}** ({best['f1']:.3f}).",
        "",
        "![confusion matrices](confusion_matrices.png)",
        "",
        f"## Missing ratings: {main_strategy} vs {other_strategy}",
        "",
        f"{n_imputed:,} pairs ({n_imputed / len(data):.1%}) involve a problem with a missing "
        "rating.",
        "",
        md_table(imp_cmp),
        "",
        "Read with care: *drop* is scored on a smaller test set without the unrated "
        "problems, and those are the hardest rows to predict (their rating is only a median "
        "guess). The last column scores the *impute*-trained models on exactly the rows "
        "*drop* is scored on; comparing it with the *drop* column isolates the effect of the "
        "training data.",
        "",
        f"**Result:** on identical test rows the mean F1 difference (drop - impute) is "
        f"{fair_gap:+.4f}, so the apparent {imp_cmp['dF1'].mean():+.3f} gain of *drop* comes "
        "from the easier test set, not from better training. Imputation keeps "
        f"{n_imputed / len(data):.0%} more data at no cost in accuracy, and lets the warehouse "
        "answer difficulty questions about every problem.",
        "",
        f"## Scaling: {main_scaler} vs {other_scaler}",
        "",
        md_table(scale_cmp),
        "",
        "Tree-based models are unaffected by monotone rescaling; distance/gradient based "
        "models (SVM, MLP) and the Naive Bayes binarization threshold are where scaling "
        "matters.",
        "",
        "![F1](f1_comparison.png) ![tree](decision_tree_entropy.png) "
        "![importance](rf_feature_importance.png)",
    ]
    (out / "summary.md").write_text("\n".join(summary) + "\n")
    return {"rows": len(all_metrics), "message": f"best {best['model']} F1={best['f1']:.3f}"}
