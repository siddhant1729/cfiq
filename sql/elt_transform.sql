-- ELT "T" step: clean submissions INSIDE the database, straight from the raw jsonb
-- in stg_submission. Same rules as the pandas ETL path (cfiq/transform/clean.py):
--   * dedupe by submission id         (DISTINCT ON)
--   * keep official participations    (participantType = :participant_type)
--   * drop gym contests               (contestId < :gym_min)
--   * drop missing / in-flight verdicts
-- Parameters are bound from config/cfiq.yaml by cfiq/staging/elt.py.
CREATE TABLE elt_submission_clean AS
SELECT DISTINCT ON ((payload->>'id')::bigint)
    (payload->>'id')::bigint                                  AS submission_id,
    handle,
    (payload->>'contestId')::int                              AS contest_id,
    payload->'problem'->>'index'                              AS problem_index,
    to_timestamp((payload->>'creationTimeSeconds')::bigint)   AS submitted_at,
    payload->>'programmingLanguage'                           AS language,
    payload->>'verdict'                                       AS verdict,
    (payload->>'verdict') = 'OK'                              AS is_accepted,
    (payload->>'timeConsumedMillis')::int                     AS time_ms,
    (payload->>'memoryConsumedBytes')::bigint                 AS memory_bytes
FROM stg_submission
WHERE payload->'author'->>'participantType' = :participant_type
  AND payload ? 'contestId'
  AND (payload->>'contestId')::int < :gym_min
  AND payload->>'verdict' IS NOT NULL
  AND payload->>'verdict' <> 'TESTING'
ORDER BY (payload->>'id')::bigint
