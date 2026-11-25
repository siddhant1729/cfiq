-- =====================================================================
-- CFIQ OLAP queries on fact_submission
-- Measure: acceptance rate = accepted submissions / submissions
-- Dimensions & hierarchies:
--   tag       : tag  -> tag category          (dim_tag -> dim_tag_category)
--   user      : user -> rating band           (dim_user -> dim_rating_band)
--   language  : raw name -> family            (dim_language)
--   time      : day -> month -> quarter -> year (dim_time)
-- Note: problem<->tag is many-to-many (bridge_problem_tag), so counts at tag or
-- category level use COUNT(DISTINCT submission_key) to avoid double counting.
-- Each query starts with "-- name: <id>" so `cfiq olap` can run them one by one.
-- =====================================================================


-- name: rollup_tag_to_category
-- ROLL-UP (climb the concept hierarchy): aggregate from individual tags UP to
-- their parent tag category. Fewer, coarser rows.
SELECT c.category_name,
       COUNT(DISTINCT f.submission_key)                                   AS submissions,
       COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)      AS accepted,
       ROUND(100.0 * COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)
             / NULLIF(COUNT(DISTINCT f.submission_key), 0), 2)            AS acceptance_pct
FROM fact_submission f
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
GROUP BY c.category_name
ORDER BY acceptance_pct DESC;


-- name: drilldown_category_to_tag
-- DRILL-DOWN (descend the hierarchy): open the "graph theory" category to see
-- its individual tags, and the time hierarchy from year down to month.
SELECT c.category_name, t.tag_name, d.year, d.month,
       COUNT(DISTINCT f.submission_key)                                   AS submissions,
       ROUND(100.0 * COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)
             / NULLIF(COUNT(DISTINCT f.submission_key), 0), 2)            AS acceptance_pct
FROM fact_submission f
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
JOIN dim_time d           ON d.time_key = f.time_key
WHERE c.category_name = 'graph theory'
GROUP BY c.category_name, t.tag_name, d.year, d.month
ORDER BY t.tag_name, d.year, d.month;


-- name: slice_python
-- SLICE (fix ONE dimension to a single value): language family = 'Python'.
-- The result is a 2-D sub-cube: tag category x rating band.
SELECT c.category_name, rb.band_name,
       COUNT(DISTINCT f.submission_key)                                   AS submissions,
       ROUND(100.0 * COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)
             / NULLIF(COUNT(DISTINCT f.submission_key), 0), 2)            AS acceptance_pct
FROM fact_submission f
JOIN dim_language l       ON l.language_key = f.language_key
JOIN dim_user u           ON u.user_key = f.user_key
JOIN dim_rating_band rb   ON rb.band_key = u.band_key
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
WHERE l.family = 'Python'
GROUP BY c.category_name, rb.band_key, rb.band_name
ORDER BY c.category_name, rb.band_key;


-- name: dice_experts_cpp_python_recent
-- DICE (select a sub-cube on TWO OR MORE dimensions): bands Expert & Candidate
-- Master, languages C++ & Python, years 2024 onwards.
SELECT rb.band_name, l.family AS language_family, d.year,
       COUNT(*)                                                           AS submissions,
       ROUND(100.0 * AVG(f.is_accepted::int), 2)                          AS acceptance_pct
FROM fact_submission f
JOIN dim_user u         ON u.user_key = f.user_key
JOIN dim_rating_band rb ON rb.band_key = u.band_key
JOIN dim_language l     ON l.language_key = f.language_key
JOIN dim_time d         ON d.time_key = f.time_key
WHERE rb.band_code IN ('EXPERT', 'CM')
  AND l.family IN ('C++', 'Python')
  AND d.year >= 2024
GROUP BY rb.band_key, rb.band_name, l.family, d.year
ORDER BY rb.band_key, l.family, d.year;


-- name: pivot_category_by_band
-- PIVOT (rotate): rating bands move from rows to COLUMNS, giving a
-- category x band cross-tab of acceptance %.
SELECT c.category_name,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'NEWBIE'), 1) AS newbie,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'PUPIL'), 1)  AS pupil,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'SPEC'), 1)   AS specialist,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'EXPERT'), 1) AS expert,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'CM'), 1)     AS cand_master,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'MASTER'), 1) AS master,
       ROUND(100.0 * AVG(f.is_accepted::int) FILTER (WHERE rb.band_code = 'GM'), 1)     AS grandmaster
FROM (  -- one row per (submission, category) so a submission counts once per category
        SELECT DISTINCT f.submission_key, f.is_accepted, f.user_key, t.category_key
        FROM fact_submission f
        JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
        JOIN dim_tag t            ON t.tag_key = b.tag_key
     ) f
JOIN dim_tag_category c ON c.category_key = f.category_key
JOIN dim_user u         ON u.user_key = f.user_key
JOIN dim_rating_band rb ON rb.band_key = u.band_key
GROUP BY c.category_name
ORDER BY c.category_name;


-- name: rollup_operator
-- ROLLUP(category, tag): subtotals along ONE hierarchy.
-- Produces (category, tag) rows, a subtotal per category (tag = NULL), and a
-- grand total (both NULL). GROUPING() = 1 marks a rolled-up column.
SELECT CASE WHEN GROUPING(c.category_name) = 1 THEN '** ALL **' ELSE c.category_name END AS category,
       CASE WHEN GROUPING(t.tag_name) = 1      THEN '** subtotal **' ELSE t.tag_name END AS tag,
       COUNT(DISTINCT f.submission_key)                                   AS submissions,
       ROUND(100.0 * COUNT(DISTINCT f.submission_key) FILTER (WHERE f.is_accepted)
             / NULLIF(COUNT(DISTINCT f.submission_key), 0), 2)            AS acceptance_pct
FROM fact_submission f
JOIN bridge_problem_tag b ON b.problem_key = f.problem_key
JOIN dim_tag t            ON t.tag_key = b.tag_key
JOIN dim_tag_category c   ON c.category_key = t.category_key
GROUP BY ROLLUP (c.category_name, t.tag_name)
ORDER BY GROUPING(c.category_name), c.category_name, GROUPING(t.tag_name), t.tag_name;


-- name: cube_operator
-- CUBE(band, language family): subtotals for EVERY combination of the listed
-- dimensions: (band, family), (band), (family), () -> 2^2 = 4 grouping sets.
SELECT CASE WHEN GROUPING(rb.band_name) = 1 THEN '** ALL **' ELSE rb.band_name END AS band,
       CASE WHEN GROUPING(l.family) = 1     THEN '** ALL **' ELSE l.family END     AS language_family,
       COUNT(*)                                                           AS submissions,
       ROUND(100.0 * AVG(f.is_accepted::int), 2)                          AS acceptance_pct
FROM fact_submission f
JOIN dim_user u         ON u.user_key = f.user_key
JOIN dim_rating_band rb ON rb.band_key = u.band_key
JOIN dim_language l     ON l.language_key = f.language_key
GROUP BY CUBE (rb.band_name, l.family)
HAVING COUNT(*) >= 20
ORDER BY GROUPING(rb.band_name), MIN(rb.band_key), GROUPING(l.family), submissions DESC;


-- name: grouping_sets_operator
-- GROUPING SETS: exactly the groupings we ask for, in one scan:
-- per band, per language family, per month, and the grand total.
SELECT CASE WHEN GROUPING(rb.band_name) = 0 THEN 'band'
            WHEN GROUPING(l.family) = 0     THEN 'language'
            WHEN GROUPING(d.year_month) = 0 THEN 'month'
            ELSE 'total' END                                              AS grouped_by,
       COALESCE(rb.band_name, l.family, d.year_month, 'ALL')              AS member,
       COUNT(*)                                                           AS submissions,
       ROUND(100.0 * AVG(f.is_accepted::int), 2)                          AS acceptance_pct
FROM fact_submission f
JOIN dim_user u         ON u.user_key = f.user_key
JOIN dim_rating_band rb ON rb.band_key = u.band_key
JOIN dim_language l     ON l.language_key = f.language_key
JOIN dim_time d         ON d.time_key = f.time_key
GROUP BY GROUPING SETS ((rb.band_name), (l.family), (d.year_month), ())
ORDER BY grouped_by, member;
