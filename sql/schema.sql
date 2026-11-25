-- =====================================================================
-- CFIQ data warehouse
--   * Star schema around fact_submission
--   * Second fact table fact_rating_change sharing dimensions
--       -> together a FACT CONSTELLATION (galaxy schema)
--   * Snowflaked dimensions: dim_user -> dim_rating_band,
--                            dim_tag -> dim_tag_category,
--                            dim_problem <-> dim_tag via bridge_problem_tag (many-to-many)
--   * Surrogate integer keys everywhere; natural keys kept as attributes.
-- Full refresh on every `cfiq load` (idempotent).
-- =====================================================================

DROP TABLE IF EXISTS fact_submission, fact_rating_change, bridge_problem_tag,
    dim_tag, dim_tag_category, dim_problem, dim_contest, dim_user,
    dim_rating_band, dim_language, dim_time CASCADE;

-- ---------------------------------------------------------------- dimensions

CREATE TABLE dim_rating_band (
    band_key      SMALLINT PRIMARY KEY,
    band_code     TEXT NOT NULL UNIQUE,      -- NEWBIE, PUPIL, ...
    band_name     TEXT NOT NULL,
    rating_lower  INTEGER,                   -- inclusive, NULL = unbounded
    rating_upper  INTEGER                    -- exclusive, NULL = unbounded
);

CREATE TABLE dim_user (
    user_key        INTEGER PRIMARY KEY,
    handle          TEXT NOT NULL UNIQUE,    -- natural key
    country         TEXT,
    current_rating  INTEGER,
    max_rating      INTEGER,
    cf_rank         TEXT,
    registered_at   TIMESTAMPTZ,
    band_key        SMALLINT REFERENCES dim_rating_band (band_key)
);

CREATE TABLE dim_contest (
    contest_key       INTEGER PRIMARY KEY,
    contest_id        INTEGER NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    division          TEXT NOT NULL,         -- Div. 1 / Div. 2 / ... / Educational / Other
    contest_type      TEXT,                  -- CF / ICPC / IOI
    start_time        TIMESTAMPTZ,
    duration_seconds  INTEGER
);

CREATE TABLE dim_problem (
    problem_key     INTEGER PRIMARY KEY,
    contest_id      INTEGER NOT NULL,
    problem_index   TEXT NOT NULL,
    index_letter    TEXT NOT NULL,
    name            TEXT,
    rating          INTEGER,                 -- after imputation
    rating_imputed  BOOLEAN NOT NULL,        -- TRUE = rating was missing and imputed
    points          NUMERIC,
    UNIQUE (contest_id, problem_index)
);

CREATE TABLE dim_tag_category (
    category_key   SMALLINT PRIMARY KEY,
    category_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE dim_tag (
    tag_key       SMALLINT PRIMARY KEY,
    tag_name      TEXT NOT NULL UNIQUE,
    category_key  SMALLINT NOT NULL REFERENCES dim_tag_category (category_key)
);

CREATE TABLE bridge_problem_tag (
    problem_key  INTEGER  NOT NULL REFERENCES dim_problem (problem_key),
    tag_key      SMALLINT NOT NULL REFERENCES dim_tag (tag_key),
    PRIMARY KEY (problem_key, tag_key)
);

CREATE TABLE dim_language (
    language_key  SMALLINT PRIMARY KEY,
    raw_name      TEXT NOT NULL UNIQUE,      -- 'C++20 (GCC 13-64)'
    family        TEXT NOT NULL              -- 'C++'
);

CREATE TABLE dim_time (
    time_key     INTEGER PRIMARY KEY,        -- YYYYMMDD (date grain)
    full_date    DATE NOT NULL UNIQUE,
    day          SMALLINT NOT NULL,
    day_of_week  SMALLINT NOT NULL,          -- 1 = Monday ... 7 = Sunday
    day_name     TEXT NOT NULL,
    week         SMALLINT NOT NULL,          -- ISO week
    month        SMALLINT NOT NULL,
    month_name   TEXT NOT NULL,
    quarter      SMALLINT NOT NULL,
    year         SMALLINT NOT NULL,
    year_month   TEXT NOT NULL,              -- '2025-03', handy for charts
    is_weekend   BOOLEAN NOT NULL
);

-- ---------------------------------------------------------------- facts

CREATE TABLE fact_submission (
    submission_key         BIGINT PRIMARY KEY,
    submission_id          BIGINT NOT NULL UNIQUE,  -- degenerate dimension (CF id)
    user_key               INTEGER  NOT NULL REFERENCES dim_user (user_key),
    problem_key            INTEGER  NOT NULL REFERENCES dim_problem (problem_key),
    contest_key            INTEGER  NOT NULL REFERENCES dim_contest (contest_key),
    time_key               INTEGER  NOT NULL REFERENCES dim_time (time_key),
    language_key           SMALLINT NOT NULL REFERENCES dim_language (language_key),
    verdict                TEXT     NOT NULL,
    is_accepted            BOOLEAN  NOT NULL,
    attempt_no             SMALLINT NOT NULL,       -- 1st, 2nd... try on this problem
    time_ms                INTEGER,
    memory_bytes           BIGINT,
    submitted_at           TIMESTAMPTZ NOT NULL,
    relative_time_seconds  INTEGER                  -- seconds since the contest started
);

CREATE TABLE fact_rating_change (
    rating_change_key  INTEGER PRIMARY KEY,
    user_key           INTEGER NOT NULL REFERENCES dim_user (user_key),
    contest_key        INTEGER NOT NULL REFERENCES dim_contest (contest_key),
    time_key           INTEGER NOT NULL REFERENCES dim_time (time_key),
    old_rating         INTEGER NOT NULL,
    new_rating         INTEGER NOT NULL,
    delta              INTEGER NOT NULL,
    rank               INTEGER,
    contest_seq        SMALLINT NOT NULL,       -- user's 1st, 2nd, ... rated contest
    rating_updated_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (user_key, contest_key)
);

-- ---------------------------------------------------------------- indexes on fact FKs

CREATE INDEX ix_fs_user     ON fact_submission (user_key);
CREATE INDEX ix_fs_problem  ON fact_submission (problem_key);
CREATE INDEX ix_fs_contest  ON fact_submission (contest_key);
CREATE INDEX ix_fs_time     ON fact_submission (time_key);
CREATE INDEX ix_fs_language ON fact_submission (language_key);
CREATE INDEX ix_frc_user    ON fact_rating_change (user_key);
CREATE INDEX ix_frc_contest ON fact_rating_change (contest_key);
CREATE INDEX ix_frc_time    ON fact_rating_change (time_key);
CREATE INDEX ix_bpt_tag     ON bridge_problem_tag (tag_key);
