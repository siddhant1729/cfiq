"""Run the OLAP queries in sql/olap_queries.sql and save each result to reports/olap/.

DMDW topic: OLAP operations (roll-up, drill-down, slice, dice, pivot) and the
SQL grouping extensions ROLLUP, CUBE and GROUPING SETS.
"""

import re

from cfiq.config import PROJECT_ROOT
from cfiq.db import read_sql

OLAP_FILE = PROJECT_ROOT / "sql" / "olap_queries.sql"


def parse_named_queries(sql_text: str) -> dict[str, str]:
    """Split a .sql file into {name: query} on lines like '-- name: rollup_tag_to_category'."""
    parts = re.split(r"^--\s*name:\s*(\S+)\s*$", sql_text, flags=re.MULTILINE)
    # parts = [preamble, name1, body1, name2, body2, ...]
    return {parts[i]: parts[i + 1].strip().rstrip(";") for i in range(1, len(parts), 2)}


def run_olap(cfg: dict, engine) -> dict:
    out = cfg["paths"]["reports"] / "olap"
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, query in parse_named_queries(OLAP_FILE.read_text()).items():
        df = read_sql(engine, query)
        df.to_csv(out / f"{name}.csv", index=False)
        results[name] = df
    return results
