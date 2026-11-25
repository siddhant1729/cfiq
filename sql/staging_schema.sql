-- ELT staging area: raw API JSON loaded as-is, one jsonb document per row.
-- Full refresh on every `cfiq stage` run (idempotent).
DROP TABLE IF EXISTS stg_contest, stg_problem, stg_user, stg_rating_change, stg_submission CASCADE;

CREATE TABLE stg_contest (
    stg_id      BIGSERIAL PRIMARY KEY,
    source_file TEXT        NOT NULL,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB       NOT NULL
);
CREATE TABLE stg_problem       (LIKE stg_contest INCLUDING ALL);
CREATE TABLE stg_user          (LIKE stg_contest INCLUDING ALL);
CREATE TABLE stg_rating_change (LIKE stg_contest INCLUDING ALL, handle TEXT NOT NULL);
CREATE TABLE stg_submission    (LIKE stg_contest INCLUDING ALL, handle TEXT NOT NULL);
