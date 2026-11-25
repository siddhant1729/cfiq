-- Result tables written by the mining modules (cfiq mine) and read by Power BI.
-- Created once (IF NOT EXISTS); each mining run replaces their rows, so the
-- views in powerbi_views.sql that depend on them remain valid.

CREATE TABLE IF NOT EXISTS mine_assoc_rules (
    rule_set     TEXT NOT NULL,     -- all | band:GM | category:graph theory
    antecedents  TEXT NOT NULL,
    consequents  TEXT NOT NULL,
    support      DOUBLE PRECISION,
    confidence   DOUBLE PRECISION,
    lift         DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS mine_model_metrics (
    experiment   TEXT NOT NULL,     -- e.g. impute+zscore
    model        TEXT NOT NULL,
    accuracy     DOUBLE PRECISION,
    precision_   DOUBLE PRECISION,
    recall       DOUBLE PRECISION,
    f1           DOUBLE PRECISION,
    tn INTEGER, fp INTEGER, fn INTEGER, tp INTEGER,
    train_rows   INTEGER,
    test_rows    INTEGER
);

CREATE TABLE IF NOT EXISTS mine_user_clusters (
    handle        TEXT NOT NULL,
    algorithm     TEXT NOT NULL,    -- kmeans | agglomerative | dbscan
    cluster       INTEGER NOT NULL, -- -1 = DBSCAN noise
    cluster_label TEXT
);

CREATE TABLE IF NOT EXISTS mine_anomalies (
    anomaly_type  TEXT NOT NULL,    -- rating_jump | submission_burst
    rank          INTEGER NOT NULL,
    handle        TEXT NOT NULL,
    contest_id    INTEGER,
    contest_name  TEXT,
    value         DOUBLE PRECISION, -- delta, or submissions in the window
    score         DOUBLE PRECISION, -- z-score used for ranking
    detail        TEXT
);
