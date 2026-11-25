"""User profiler: cluster users by what they solve and how strong they are.

DMDW topics: clustering - partitioning (K-means, with the elbow method and
silhouette analysis to choose k), hierarchical (agglomerative, Ward linkage,
with a dendrogram) and density-based (DBSCAN, eps from the k-distance graph);
comparison by silhouette score.

User vector = share of the user's solved problems in each tag category
(rows sum to 1) + current rating. All features are z-scored so the rating does
not dominate the distance.
"""

import logging

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import dendrogram, linkage
from sklearn.cluster import DBSCAN, AgglomerativeClustering, KMeans
from sklearn.metrics import silhouette_score
from sklearn.neighbors import NearestNeighbors

from cfiq.db import read_sql, replace_table_rows
from cfiq.mining.common import md_table, report_dir, save_fig
from cfiq.transform.discretize import rating_to_band
from cfiq.transform.scale import zscore_scale

log = logging.getLogger(__name__)

SOLVES_SQL = """
SELECT u.handle, u.current_rating, c.category_name AS category,
       COUNT(DISTINCT f.problem_key) AS solved
FROM fact_submission f
JOIN dim_user u           ON u.user_key = f.user_key
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
WHERE f.is_accepted
GROUP BY u.handle, u.current_rating, c.category_name
"""


def build_user_vectors(solves: pd.DataFrame, min_solved: int) -> pd.DataFrame:
    """handle x (share_<category>..., rating). Users with < min_solved tag-solves are dropped.

    The 'other' category (meta tags such as *special) is left out: it says nothing
    about which topics a user is good at."""
    counts = solves.pivot_table(
        index="handle", columns="category", values="solved", aggfunc="sum", fill_value=0
    )
    counts = counts.drop(columns=["other"], errors="ignore")  # meta tags carry no topic
    counts = counts[counts.sum(axis=1) >= min_solved]
    shares = counts.div(counts.sum(axis=1), axis=0)
    shares.columns = [f"share_{c}" for c in shares.columns]
    rating = solves.drop_duplicates("handle").set_index("handle")["current_rating"]
    shares["rating"] = rating.reindex(shares.index).astype(float)
    return shares.dropna()


def elbow_knee(k_table: pd.DataFrame) -> int:
    """Elbow point: the k whose (k, inertia) point lies farthest from the straight line
    joining the first and last points of the curve."""
    x = k_table["k"].to_numpy(float)
    y = k_table["inertia"].to_numpy(float)
    x_n, y_n = (x - x[0]) / (x[-1] - x[0]), (y - y[-1]) / (y[0] - y[-1])
    distance = np.abs(x_n + y_n - 1) / np.sqrt(2)  # distance to the line from (0,1) to (1,0)
    return int(x[np.argmax(distance)])


def choose_k(X: np.ndarray, k_min: int, k_max: int, seed: int) -> pd.DataFrame:
    rows = []
    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(X)
        rows.append({"k": k, "inertia": km.inertia_, "silhouette": silhouette_score(X, km.labels_)})
    return pd.DataFrame(rows)


def k_distances(X: np.ndarray, min_samples: int) -> np.ndarray:
    """Sorted distance from each point to its min_samples-th nearest neighbour."""
    dist, _ = NearestNeighbors(n_neighbors=min_samples).fit(X).kneighbors(X)
    return np.sort(dist[:, -1])


def choose_dbscan_eps(X: np.ndarray, min_samples: int, quantiles: list[float]) -> pd.DataFrame:
    """Try eps = several quantiles of the k-distance curve; report clusters/noise/silhouette."""
    kdist = k_distances(X, min_samples)
    rows = []
    for q in quantiles:
        eps = float(np.quantile(kdist, q))
        labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(X)
        rows.append(
            {
                "quantile": q,
                "eps": eps,
                "clusters": len(set(labels) - {-1}),
                "noise": int((labels == -1).sum()),
                "silhouette": safe_silhouette(X, labels),
            }
        )
    return pd.DataFrame(rows)


def safe_silhouette(X, labels) -> float | None:
    """Silhouette ignoring DBSCAN noise (-1); None if fewer than 2 clusters remain."""
    mask = labels != -1
    if len(set(labels[mask])) < 2:
        return None
    return float(silhouette_score(X[mask], labels[mask]))


def describe_clusters(
    vectors: pd.DataFrame, labels: np.ndarray, bands: list[dict], threshold: float = 0.3
) -> pd.DataFrame:
    """Turn each cluster's centroid into a plain-English sentence.

    A category is called out when the cluster's mean share differs from the population
    mean by at least `threshold` population standard deviations."""
    band_names = {b["code"]: b["name"] for b in bands}
    share_cols = [c for c in vectors.columns if c.startswith("share_")]
    overall = vectors[share_cols].mean()
    spread = vectors[share_cols].std().replace(0, 1)
    rows = []
    for cl in sorted(set(labels)):
        members = vectors[labels == cl]
        if cl == -1:
            rows.append(
                {
                    "cluster": -1,
                    "size": len(members),
                    "mean_rating": members["rating"].mean(),
                    "label": "noise",
                    "description": f"{len(members)} users (mean rating "
                    f"{members['rating'].mean():.0f}) in no dense region: unusual tag mixes.",
                }
            )
            continue
        mean_rating = members["rating"].mean()
        band = band_names.get(rating_to_band(mean_rating, bands), "?")
        # standardised difference of the cluster's mean share from the population mean
        diff = (members[share_cols].mean() - overall) / spread
        strong = [
            c.removeprefix("share_")
            for c, d in diff.sort_values(ascending=False).items()
            if d >= threshold
        ][:2]
        weak = [c.removeprefix("share_") for c, d in diff.sort_values().items() if d <= -threshold][
            :2
        ]
        label = f"{band}" + (f" / {'+'.join(strong)}" if strong else " / balanced")
        text = f"{len(members)} users averaging {mean_rating:.0f} ({band})"
        text += (
            f"; solve relatively more {' and '.join(strong)}"
            if strong
            else "; tag mix close to the population average"
        )
        if weak:
            text += f", less {' and '.join(weak)}"
        rows.append(
            {
                "cluster": int(cl),
                "size": len(members),
                "mean_rating": mean_rating,
                "label": label,
                "description": text + ".",
            }
        )
    return pd.DataFrame(rows)


def pick_dbscan_eps(eps_table: pd.DataFrame, n: int, max_noise: float) -> tuple[pd.Series, str]:
    """Best silhouette among settings with >= 2 clusters and at most `max_noise` noise.

    Silhouette is computed on clustered points only, so without the noise cap a setting
    that throws 90% of users away as noise would look best. If no setting qualifies,
    use the smallest eps whose noise is under the cap: one dense group + outliers."""
    ok = eps_table[eps_table["noise"] <= max_noise * n]
    multi = ok[ok["clusters"] >= 2].dropna(subset=["silhouette"])
    if len(multi):
        return multi.loc[multi["silhouette"].idxmax()], "best silhouette within the noise cap"
    if len(ok):
        return ok.iloc[0], (
            "no setting gives 2+ clusters within the noise cap: the users form one dense "
            "group (a continuum of profiles), and DBSCAN's noise points are the unusual profiles"
        )
    return eps_table.iloc[-1], "every setting exceeds the noise cap; largest eps used"


def run_clustering(cfg: dict, engine) -> dict:
    kcfg = cfg["mining"]["clustering"]
    seed = cfg["mining"]["random_state"]
    out = report_dir(cfg, "clustering")

    vectors = build_user_vectors(read_sql(engine, SOLVES_SQL), kcfg["min_solved"])
    X = zscore_scale(vectors, list(vectors.columns)).to_numpy()
    log.info("clustering: %d users x %d features", *X.shape)

    # ---- K-means: elbow + silhouette
    k_table = choose_k(X, kcfg["k_range"][0], kcfg["k_range"][1], seed)
    k_table.to_csv(out / "kmeans_k_selection.csv", index=False)
    knee_k = elbow_knee(k_table)
    eligible = k_table[k_table["k"] >= kcfg["min_k_choice"]]
    best_k = int(eligible.loc[eligible["silhouette"].idxmax(), "k"])
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    a1.plot(k_table["k"], k_table["inertia"], marker="o")
    a1.axvline(knee_k, ls="--", color="grey")
    a1.set(
        title=f"Elbow method (knee at k = {knee_k})",
        xlabel="k",
        ylabel="inertia (within-cluster SSE)",
    )
    a2.plot(k_table["k"], k_table["silhouette"], marker="o", color="tab:orange")
    a2.axvline(best_k, ls="--", color="grey")
    a2.set(
        title=f"Silhouette (chosen k = {best_k}, k >= {kcfg['min_k_choice']})",
        xlabel="k",
        ylabel="silhouette",
    )
    save_fig(fig, out / "kmeans_elbow_silhouette.png")
    km_labels = KMeans(n_clusters=best_k, n_init=10, random_state=seed).fit_predict(X)

    # ---- Agglomerative (Ward) + dendrogram
    Z = linkage(X, method="ward")
    fig, ax = plt.subplots(figsize=(12, 5))
    dendrogram(Z, truncate_mode="lastp", p=40, ax=ax, color_threshold=Z[-(best_k - 1), 2])
    ax.set(
        title=f"Agglomerative clustering (Ward) dendrogram, cut at k = {best_k}",
        xlabel="users (cluster sizes in brackets)",
        ylabel="merge distance",
    )
    save_fig(fig, out / "dendrogram.png")
    ag_labels = AgglomerativeClustering(n_clusters=best_k, linkage="ward").fit_predict(X)

    # ---- DBSCAN with eps from the k-distance graph
    eps_table = choose_dbscan_eps(X, kcfg["dbscan_min_samples"], kcfg["dbscan_eps_quantiles"])
    eps_table.to_csv(out / "dbscan_eps_selection.csv", index=False)
    best_row, dbscan_note = pick_dbscan_eps(eps_table, len(X), kcfg["dbscan_max_noise_fraction"])
    eps = float(best_row["eps"])
    kdist = k_distances(X, kcfg["dbscan_min_samples"])
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(kdist)
    ax.axhline(eps, ls="--", color="red", label=f"eps = {eps:.2f}")
    ax.set(
        title=f"k-distance graph (k = {kcfg['dbscan_min_samples']})",
        xlabel="users sorted by distance",
        ylabel="distance to k-th neighbour",
    )
    ax.legend()
    save_fig(fig, out / "dbscan_k_distance.png")
    db_labels = DBSCAN(eps=eps, min_samples=kcfg["dbscan_min_samples"]).fit_predict(X)

    # ---- compare
    algos = {"kmeans": km_labels, "agglomerative": ag_labels, "dbscan": db_labels}
    comparison = pd.DataFrame(
        [
            {
                "algorithm": name,
                "clusters": len(set(lbl) - {-1}),
                "noise_points": int((lbl == -1).sum()),
                "silhouette": safe_silhouette(X, lbl),
            }
            for name, lbl in algos.items()
        ]
    )
    comparison.to_csv(out / "silhouette_comparison.csv", index=False)
    comparison["silhouette"] = comparison["silhouette"].map(
        lambda v: "n/a (< 2 clusters)" if pd.isna(v) else round(v, 3)
    )

    # 2-D PCA picture of the three results
    from sklearn.decomposition import PCA

    P = PCA(n_components=2, random_state=seed).fit_transform(X)
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    for ax, (name, lbl) in zip(axes, algos.items(), strict=True):
        for cl in sorted(set(lbl)):
            m = lbl == cl
            if cl == -1:
                ax.scatter(P[m, 0], P[m, 1], c="lightgrey", marker="x", s=14, label="noise")
            else:
                ax.scatter(
                    P[m, 0], P[m, 1], color=plt.cm.tab10(cl % 10), s=14, label=f"cluster {cl}"
                )
        ax.set(title=name, xlabel="PC1", ylabel="PC2")
        ax.legend(fontsize=8)
    fig.suptitle("Clusters projected on the first two principal components")
    save_fig(fig, out / "clusters_pca.png")

    descriptions, db_rows = {}, []
    for name, lbl in algos.items():
        desc = describe_clusters(vectors, lbl, cfg["rating_bands"])
        descriptions[name] = desc
        labels = dict(zip(desc["cluster"], desc["label"], strict=True))
        db_rows.append(
            pd.DataFrame(
                {
                    "handle": vectors.index,
                    "algorithm": name,
                    "cluster": lbl,
                    "cluster_label": [labels[c] for c in lbl],
                }
            )
        )
    assignments = pd.concat(db_rows, ignore_index=True)
    assignments.to_csv(out / "user_clusters.csv", index=False)
    replace_table_rows(engine, "mine_user_clusters", assignments)

    centroids = vectors.assign(cluster=km_labels).groupby("cluster").mean()
    centroids.to_csv(out / "kmeans_centroids.csv")

    summary = [
        "# Clustering: user profiler",
        "",
        f"- Users: **{len(vectors)}** (at least {kcfg['min_solved']} tag-solves), features: "
        f"{', '.join(vectors.columns)} (z-scored)",
        f"- K-means, k = {kcfg['k_range'][0]}..{kcfg['k_range'][1]}: elbow knee at "
        f"k = {knee_k}; best silhouette overall at k = "
        f"{int(k_table.loc[k_table['silhouette'].idxmax(), 'k'])}. Chosen: **k = {best_k}**, "
        f"the best silhouette with k >= {kcfg['min_k_choice']} (k = 2 only separates strong "
        "from weak users, which the rating band already tells us).",
        "",
        md_table(k_table),
        f"- DBSCAN: min_samples = {kcfg['dbscan_min_samples']} (dimensions + 1), eps = "
        f"{eps:.2f} ({best_row['quantile']:.0%} quantile of the k-distance graph). Rule: best "
        f"silhouette among settings with >= 2 clusters and <= "
        f"{kcfg['dbscan_max_noise_fraction']:.0%} noise. Outcome: {dbscan_note}.",
        "",
        "## Silhouette comparison",
        "",
        md_table(comparison),
        "",
        "### DBSCAN eps candidates",
        "",
        md_table(eps_table),
        "",
        "![elbow](kmeans_elbow_silhouette.png)",
        "",
        "![dendrogram](dendrogram.png) ![pca](clusters_pca.png)",
        "",
    ]
    for name, desc in descriptions.items():
        summary += [f"## {name} clusters in plain words", ""]
        summary += [
            f"- **Cluster {r.cluster}** ({r.label}): {r.description}" for r in desc.itertuples()
        ]
        summary.append("")
    summary += ["## K-means centroids (unscaled)", "", md_table(centroids.reset_index(), ".2f")]
    (out / "summary.md").write_text("\n".join(summary) + "\n")
    return {"rows": len(assignments), "message": f"k={best_k}"}
