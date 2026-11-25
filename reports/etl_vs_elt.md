# ETL vs ELT: cleaning the submissions

| path | where the T runs | rows out | seconds |
|---|---|---:|---:|
| ETL | pandas, before loading (`cfiq/transform/clean.py`) | 148,643 | 12.89 |
| ELT | SQL inside Postgres on staged jsonb (`sql/elt_transform.sql`) | 148,643 | 1.21 |

Same row count from both paths: **yes**.

ETL time includes reading the raw JSON files from disk; ELT time is the SQL only (the raw JSON was already loaded into `stg_submission` by `cfiq stage`).
ETL keeps the warehouse clean and the logic in testable Python; ELT keeps the raw data in the database so new transformations can be written later in SQL without re-extracting.
