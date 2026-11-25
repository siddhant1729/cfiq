-- =====================================================================
-- Flat, denormalised views for Power BI (Get Data -> PostgreSQL database).
-- Each view is report-ready: no joins needed inside Power BI.
-- Recreated by every `cfiq load` (after the warehouse tables are rebuilt).
-- =====================================================================

-- Acceptance rate by tag x rating band (with the category, for drill-down in visuals)
CREATE OR REPLACE VIEW v_pbi_acceptance_by_tag_band AS
SELECT c.category_name                                  AS tag_category,
       t.tag_name                                       AS tag,
       rb.band_key                                      AS band_order,
       rb.band_name                                     AS rating_band,
       COUNT(DISTINCT f.submission_key)                 AS submissions,
       COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted) AS accepted,
       ROUND(COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)::numeric
             / NULLIF(COUNT(DISTINCT f.submission_key), 0), 4)       AS acceptance_rate
FROM fact_submission f
JOIN dim_user u           ON u.user_key = f.user_key
JOIN dim_rating_band rb   ON rb.band_key = u.band_key
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
GROUP BY c.category_name, t.tag_name, rb.band_key, rb.band_name;

-- Language usage over time (monthly)
CREATE OR REPLACE VIEW v_pbi_language_usage_monthly AS
SELECT d.year, d.month, d.year_month,
       make_date(d.year, d.month, 1)                    AS month_start,
       l.family                                         AS language_family,
       COUNT(*)                                         AS submissions,
       COUNT(DISTINCT f.user_key)                       AS users,
       ROUND(AVG(f.is_accepted::int), 4)                AS acceptance_rate
FROM fact_submission f
JOIN dim_time d     ON d.time_key = f.time_key
JOIN dim_language l ON l.language_key = f.language_key
GROUP BY d.year, d.month, d.year_month, l.family;

-- Rating progression per user (one row per rated contest)
CREATE OR REPLACE VIEW v_pbi_rating_progression AS
SELECT u.handle, rb.band_name AS current_band, u.country,
       r.contest_seq, c.contest_id, c.name AS contest_name, c.division,
       d.full_date AS rating_date,
       r.old_rating, r.new_rating, r.delta, r.rank
FROM fact_rating_change r
JOIN dim_user u         ON u.user_key = r.user_key
LEFT JOIN dim_rating_band rb ON rb.band_key = u.band_key
JOIN dim_contest c      ON c.contest_key = r.contest_key
JOIN dim_time d         ON d.time_key = r.time_key;

-- Verdict distribution by band and language (for a stacked bar)
CREATE OR REPLACE VIEW v_pbi_verdicts AS
SELECT rb.band_key AS band_order, rb.band_name AS rating_band, l.family AS language_family,
       f.verdict, COUNT(*) AS submissions
FROM fact_submission f
JOIN dim_user u         ON u.user_key = f.user_key
JOIN dim_rating_band rb ON rb.band_key = u.band_key
JOIN dim_language l     ON l.language_key = f.language_key
GROUP BY rb.band_key, rb.band_name, l.family, f.verdict;

-- ---------------------------------------------------------------- mining outputs

CREATE OR REPLACE VIEW v_pbi_assoc_rules AS
SELECT rule_set, antecedents || '  =>  ' || consequents AS rule,
       antecedents, consequents, support, confidence, lift
FROM mine_assoc_rules;

CREATE OR REPLACE VIEW v_pbi_model_comparison AS
SELECT experiment, model, accuracy, precision_ AS precision, recall, f1,
       tn, fp, fn, tp, train_rows, test_rows
FROM mine_model_metrics;

CREATE OR REPLACE VIEW v_pbi_user_clusters AS
SELECT m.algorithm, m.cluster, m.cluster_label, m.handle,
       u.current_rating, u.country, rb.band_name AS rating_band
FROM mine_user_clusters m
JOIN dim_user u ON u.handle = m.handle
LEFT JOIN dim_rating_band rb ON rb.band_key = u.band_key;

CREATE OR REPLACE VIEW v_pbi_anomalies AS
SELECT anomaly_type, rank, handle, contest_id, contest_name, value, score, detail
FROM mine_anomalies;
