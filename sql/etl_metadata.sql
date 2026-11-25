-- ETL control table: one row per (run, phase, endpoint/table).
-- Every phase (extract, stage, transform, load, mine) appends here, so a run
-- can be audited end to end: what was touched, how many rows, how long, status.
CREATE TABLE IF NOT EXISTS etl_metadata (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID        NOT NULL,
    phase        TEXT        NOT NULL,             -- extract | stage | transform | load | mine
    endpoint     TEXT        NOT NULL,             -- API endpoint or target table
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    row_count    INTEGER,
    api_calls    INTEGER     NOT NULL DEFAULT 0,   -- real HTTP requests made
    cache_hits   INTEGER     NOT NULL DEFAULT 0,   -- responses served from disk cache
    status       TEXT        NOT NULL,             -- success | partial | failed
    message      TEXT
);

CREATE INDEX IF NOT EXISTS ix_etl_metadata_run_id ON etl_metadata (run_id);
