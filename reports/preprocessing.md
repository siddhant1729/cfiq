# Preprocessing report

## Cleaning steps (submissions)

| step | rows |
|---|---:|
| raw submissions | 546,417 |
| after dedupe by id | 545,434 |
| after dropping gym / no contest | 477,257 |
| after keeping official participations | 148,643 |
| after dropping missing/in-flight verdicts | 148,643 |
| after joining problems on (contestId, index) | 148,643 |

## Missing values: problem rating

- Problems (problemset + integrated from submissions): **12,999**
- Missing `rating`: **489** (3.8%)
- Clean submissions on an unrated problem: **17,624**
- Strategy (a) impute: median rating of same division + index letter (fallback: index letter, then global), rounded to 100, flagged `rating_imputed`.
- Strategy (b) drop: remove those problems. The effect of each on the models is in `reports/classification/imputation_comparison.md`.

## Clean tables

| table | rows |
|---|---:|
| contests | 2,172 |
| problems | 12,999 |
| problem_tags | 36,759 |
| submissions | 148,643 |
| users | 350 |
| rating_changes | 17,390 |
