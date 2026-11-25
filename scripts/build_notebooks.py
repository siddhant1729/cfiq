"""Generate the demo notebooks in notebooks/ (run: uv run python scripts/build_notebooks.py).

Each notebook walks through one module step by step using the module's own
functions, so what is shown in the demo is exactly the code that the pipeline runs.
Execute them with:
    uv run jupyter nbconvert --to notebook --execute --inplace notebooks/*.ipynb
"""

from pathlib import Path

import nbformat as nbf

NB_DIR = Path(__file__).resolve().parent.parent / "notebooks"

SETUP = """\
import pandas as pd
from IPython.display import Image, Markdown, display

from cfiq.config import load_config
from cfiq.db import get_engine, read_sql

cfg = load_config()
engine = get_engine()
REPORTS = cfg["paths"]["reports"]
pd.set_option("display.max_columns", 30)
pd.set_option("display.width", 200)"""


def notebook(cells: list[tuple[str, str]]) -> nbf.NotebookNode:
    nb = nbf.v4.new_notebook()
    nb.metadata["kernelspec"] = {
        "name": "python3",
        "display_name": "Python 3",
        "language": "python",
    }
    for kind, src in cells:
        nb.cells.append(
            nbf.v4.new_markdown_cell(src) if kind == "md" else nbf.v4.new_code_cell(src)
        )
    return nb


NOTEBOOKS = {
    "00_warehouse_olap.ipynb": [
        (
            "md",
            "# 00 · Warehouse, ETL vs ELT, and OLAP\n\n"
            "**DMDW topics:** ETL vs ELT, star / snowflake schema, fact constellation, "
            "surrogate keys, OLAP operations (roll-up, drill-down, slice, dice, pivot), "
            "ROLLUP / CUBE / GROUPING SETS.",
        ),
        ("code", SETUP),
        (
            "md",
            "## ETL audit trail (`etl_metadata`)\nEvery phase appends one row per "
            "endpoint / table, so a run can be traced end to end.",
        ),
        (
            "code",
            "read_sql(engine, '''\nSELECT phase, endpoint, row_count, api_calls, cache_hits, "
            "status, finished_at\nFROM etl_metadata\nWHERE id IN (SELECT max(id) FROM "
            "etl_metadata GROUP BY phase, endpoint)\nORDER BY CASE phase WHEN 'extract' "
            "THEN 1 WHEN 'stage' THEN 2 WHEN 'transform' THEN 3 WHEN 'load' THEN 4 ELSE 5 "
            "END, id''')",
        ),
        ("md", "## ELT: raw JSON lives in the database as jsonb"),
        (
            "code",
            "read_sql(engine, \"SELECT handle, payload->>'verdict' AS verdict, "
            "payload->'problem'->>'name' AS problem, payload->'author'->>'participantType'"
            ' AS participant FROM stg_submission LIMIT 5")',
        ),
        (
            "code",
            "display(Markdown((REPORTS / 'etl_vs_elt.md').read_text()))\n"
            "display(Markdown((REPORTS / 'preprocessing.md').read_text()))",
        ),
        ("md", "## Star schema row counts"),
        (
            "code",
            "tables = ['dim_rating_band','dim_user','dim_contest','dim_problem',"
            "'dim_tag_category','dim_tag','bridge_problem_tag','dim_language','dim_time',"
            "'fact_submission','fact_rating_change']\n"
            "pd.DataFrame({'table': tables, 'rows': [read_sql(engine, f'SELECT count(*) AS n "
            "FROM {t}')['n'][0] for t in tables]})",
        ),
        (
            "md",
            "## OLAP queries (`sql/olap_queries.sql`)\nEach query is run exactly as "
            "written in the SQL file.",
        ),
        (
            "code",
            "from cfiq.warehouse.olap import OLAP_FILE, parse_named_queries\n"
            "queries = parse_named_queries(OLAP_FILE.read_text())\nlist(queries)",
        ),
        ("md", "### Roll-up: tag → tag category"),
        ("code", "read_sql(engine, queries['rollup_tag_to_category'])"),
        ("md", "### Drill-down: category 'graph theory' → its tags, by year/month"),
        ("code", "read_sql(engine, queries['drilldown_category_to_tag']).head(15)"),
        ("md", "### Slice: language family = Python"),
        ("code", "read_sql(engine, queries['slice_python']).head(15)"),
        ("md", "### Dice: bands {Expert, CM} × languages {C++, Python} × year ≥ 2024"),
        ("code", "read_sql(engine, queries['dice_experts_cpp_python_recent'])"),
        ("md", "### Pivot: category × band cross-tab"),
        ("code", "read_sql(engine, queries['pivot_category_by_band'])"),
        ("md", "### ROLLUP(category, tag): subtotals along one hierarchy"),
        ("code", "read_sql(engine, queries['rollup_operator']).head(20)"),
        ("md", "### CUBE(band, language family): all 4 grouping combinations"),
        ("code", "read_sql(engine, queries['cube_operator'])"),
        ("md", "### GROUPING SETS: per band, per language, per month, and total"),
        (
            "code",
            "gs = read_sql(engine, queries['grouping_sets_operator'])\n"
            "gs.groupby('grouped_by').head(5)",
        ),
    ],
    "01_association.ipynb": [
        (
            "md",
            "# 01 · Association rules: tag pattern miner\n\n"
            "**DMDW topics:** frequent itemsets, Apriori vs FP-Growth, support / "
            "confidence / lift, constraint-based mining.\n\nTransaction = the set of tags "
            "of the problems one user solved.",
        ),
        (
            "code",
            SETUP + "\nfrom cfiq.mining import association as A\n"
            "acfg = cfg['mining']['association']; acfg",
        ),
        (
            "code",
            "solved = read_sql(engine, A.SOLVED_TAGS_SQL)\n"
            "tx = A.to_transactions(solved)\nonehot = A.one_hot(tx)\n"
            "print(len(tx), 'transactions; first one:', tx[0][:10])\nonehot.head()",
        ),
        ("md", "## Apriori vs FP-Growth at the same min_support"),
        (
            "code",
            "rt = A.compare_runtimes(onehot, acfg['runtime_supports'], "
            "acfg['runtime_repeats'], acfg['max_len'])\nrt",
        ),
        ("md", "## Rules (FP-Growth itemsets → confidence/lift filter)"),
        (
            "code",
            "itemsets, rules = A.mine_rules(onehot, acfg['min_support'], "
            "acfg['min_confidence'], acfg['min_lift'], acfg['max_len'])\n"
            "print(len(itemsets), 'frequent itemsets,', len(rules), 'rules')\n"
            "A.format_rules(rules, 'all').head(15)",
        ),
        (
            "md",
            "## Constraint-based mining\nRun the full module (also writes the "
            "constrained rule sets, plots and the `mine_assoc_rules` table).",
        ),
        (
            "code",
            "A.run_association(cfg, engine)\n"
            "text = (REPORTS / 'association' / 'summary.md').read_text()\n"
            "display(Markdown(text[text.index('## Constraint'):]))",
        ),
        ("code", "Image(REPORTS / 'association' / 'runtime_comparison.png')"),
    ],
    "02_classification.ipynb": [
        (
            "md",
            "# 02 · Classification: solve predictor\n\n"
            "**DMDW topics:** decision trees (entropy ≈ ID3, gini = CART), Naive Bayes, "
            "SVM, MLP, Random Forest; accuracy / precision / recall / F1 / confusion matrix; "
            "time-based split; preprocessing effects (imputation, scaling).",
        ),
        ("code", SETUP + "\nfrom cfiq.mining import classification as C"),
        ("md", "## Build the (user, problem) dataset from the warehouse"),
        (
            "code",
            "pairs = read_sql(engine, C.PAIRS_SQL)\n"
            "pairs['contest_start'] = pd.to_datetime(pairs['contest_start'], utc=True)\n"
            "ratings = read_sql(engine, C.RATINGS_SQL)\n"
            "ratings['rating_updated_at'] = pd.to_datetime(ratings['rating_updated_at'], "
            "utc=True)\nproblem_cats = read_sql(engine, C.CATEGORIES_SQL)\n"
            "data, continuous, binary = C.build_features(pairs, problem_cats, ratings, "
            "cfg['transform']['default_initial_rating'])\n"
            "print(len(data), 'pairs, solved share =', round(data['solved'].mean(), 3))\n"
            "data[['user_key','problem_key','contest_start','solved'] + continuous[:5]]"
            ".head()",
        ),
        ("md", "## Time-based split (no contest on both sides)"),
        (
            "code",
            "train, test = C.time_split(data, cfg['mining']['classification']"
            "['test_fraction'])\nprint('train', train['contest_start'].min().date(), '..', "
            "train['contest_start'].max().date(), len(train))\nprint('test ', "
            "test['contest_start'].min().date(), '..', test['contest_start'].max().date(), "
            "len(test))",
        ),
        ("md", "## Train and compare all models (3 preprocessing experiments)"),
        (
            "code",
            "C.run_classification(cfg, engine)\n"
            "pd.read_csv(REPORTS / 'classification' / 'model_comparison.csv')",
        ),
        ("code", "Image(REPORTS / 'classification' / 'confusion_matrices.png')"),
        ("md", "## Missing-value strategy and scaling"),
        (
            "code",
            "m = pd.read_csv(REPORTS / 'classification' / 'all_metrics.csv')\n"
            "m.pivot(index='model', columns='experiment', values='f1').round(3)",
        ),
        ("code", "Image(REPORTS / 'classification' / 'decision_tree_entropy.png')"),
        ("code", "Image(REPORTS / 'classification' / 'rf_feature_importance.png')"),
    ],
    "03_clustering.ipynb": [
        (
            "md",
            "# 03 · Clustering: user profiler\n\n"
            "**DMDW topics:** K-means (elbow + silhouette), agglomerative clustering "
            "(dendrogram), DBSCAN (k-distance graph), silhouette comparison.",
        ),
        ("code", SETUP + "\nfrom cfiq.mining import clustering as K"),
        (
            "code",
            "vectors = K.build_user_vectors(read_sql(engine, K.SOLVES_SQL), "
            "cfg['mining']['clustering']['min_solved'])\nvectors.round(3).head()",
        ),
        (
            "code",
            "K.run_clustering(cfg, engine)\n"
            "pd.read_csv(REPORTS / 'clustering' / 'kmeans_k_selection.csv')",
        ),
        ("code", "Image(REPORTS / 'clustering' / 'kmeans_elbow_silhouette.png')"),
        ("code", "Image(REPORTS / 'clustering' / 'dendrogram.png')"),
        ("code", "Image(REPORTS / 'clustering' / 'dbscan_k_distance.png')"),
        ("code", "pd.read_csv(REPORTS / 'clustering' / 'silhouette_comparison.csv')"),
        ("code", "Image(REPORTS / 'clustering' / 'clusters_pca.png')"),
        ("md", "## Clusters in plain words"),
        (
            "code",
            "text = (REPORTS / 'clustering' / 'summary.md').read_text()\n"
            "display(Markdown(text[text.index('## kmeans'):text.index('## K-means "
            "centroids')]))",
        ),
    ],
    "04_outliers.ipynb": [
        (
            "md",
            "# 04 · Outliers: anomaly detector\n\n"
            "**DMDW topics:** statistical outlier detection - z-score (relative to each "
            "user's own history) and IQR / box-plot fences.",
        ),
        ("code", SETUP + "\nfrom cfiq.mining import outliers as O"),
        (
            "code",
            "O.run_outliers(cfg, engine)\nranked = pd.read_csv(REPORTS / 'outliers' / "
            "'ranked_anomalies.csv')\nranked[ranked.anomaly_type == 'rating_jump'].head(10)",
        ),
        ("code", "Image(REPORTS / 'outliers' / 'rating_delta_hist.png')"),
        ("code", "ranked[ranked.anomaly_type == 'submission_burst'].head(10)"),
        ("code", "Image(REPORTS / 'outliers' / 'submission_burst_hist.png')"),
    ],
    "05_stream.ipynb": [
        (
            "md",
            "# 05 · Stream mining (optional)\n\n"
            "**DMDW topic:** mining data streams - one pass, incremental counters, sliding "
            "window, duplicate suppression. Replay mode feeds a finished contest's "
            "submissions in time order; `cfiq stream --live` polls the API instead.",
        ),
        ("code", SETUP + "\nfrom cfiq.mining.stream import run_stream"),
        ("code", "stats, snaps = run_stream(cfg)\nsnaps.head(10)"),
        ("code", "display(Markdown((REPORTS / 'stream' / 'summary.md').read_text()))"),
        (
            "code",
            "img = sorted((REPORTS / 'stream').glob('contest_*_acceptance.png'))[-1]\nImage(img)",
        ),
    ],
}


def main() -> None:
    NB_DIR.mkdir(exist_ok=True)
    for name, cells in NOTEBOOKS.items():
        nbf.write(notebook(cells), NB_DIR / name)
        print("wrote", NB_DIR / name)


if __name__ == "__main__":
    main()
