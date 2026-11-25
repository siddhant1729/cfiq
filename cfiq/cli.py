"""Command-line entry point.

cfiq extract     Codeforces API -> raw JSON cache (data/raw)          [Phase 1]
cfiq stage       raw JSON -> stg_* jsonb tables (ELT path)            [Phase 2]
cfiq transform   raw JSON -> clean DataFrames (ETL path) + ELT SQL    [Phase 3]
cfiq load        clean DataFrames -> star schema + Power BI views     [Phase 4]
cfiq olap        run sql/olap_queries.sql -> reports/olap/*.csv       [Phase 4]
cfiq mine        association, classification, clustering, outliers   [Phase 5]
cfiq stream      incremental verdict statistics for one contest      [Phase 5, optional]
cfiq all         everything above except stream, end to end (from cache)
"""

import argparse
import logging
import sys

from cfiq.config import load_config

log = logging.getLogger("cfiq")


def _engine():
    from cfiq.db import db_available, get_engine

    engine = get_engine()
    if not db_available(engine):
        sys.exit("Postgres is not reachable. Start it with `docker compose up -d`.")
    return engine


def _record(engine, rows) -> None:
    from cfiq.db import record_runs

    if rows:
        record_runs(engine, rows)


def cmd_extract(args, cfg) -> int:
    from cfiq.db import db_available, get_engine
    from cfiq.extract.pipeline import run_extract

    run_id, rows, sample = run_extract(cfg, sample_size=args.sample_size, seed=args.seed)

    print(f"\nRun {run_id}")
    print(f"{'endpoint':<22}{'rows':>10}{'api_calls':>11}{'cache_hits':>12}  status")
    for r in rows:
        print(
            f"{r['endpoint']:<22}{r['row_count']:>10}{r['api_calls']:>11}"
            f"{r['cache_hits']:>12}  {r['status']}"
        )
    print("\nSample by band:")
    print(sample["band"].value_counts(sort=False).to_string())

    engine = get_engine()
    if db_available(engine):
        _record(engine, rows)
        print(f"\nRecorded {len(rows)} rows in etl_metadata.")
    else:
        print(
            "\nWARNING: Postgres is not reachable, so etl_metadata was not written. "
            "Start it (`docker compose up -d`) and rerun `cfiq extract`; "
            "the rerun is served from the disk cache in seconds."
        )
    return 0


def cmd_stage(args, cfg) -> int:
    from cfiq.staging.stage import run_stage

    engine = _engine()
    _record(engine, run_stage(cfg, engine))
    return 0


def cmd_transform(args, cfg) -> int:
    from cfiq.staging.elt import run_elt_transform
    from cfiq.transform.pipeline import run_transform, write_preprocessing_report

    engine = _engine()
    meta, stats = run_transform(cfg)
    elt = run_elt_transform(cfg, engine)
    log.info(
        "ELT (SQL) cleaned %d rows in %.2fs; ETL (pandas) %d rows in %.2fs",
        elt["rows"],
        elt["seconds"],
        stats["pandas_clean_rows"],
        stats["pandas_clean_seconds"],
    )
    write_preprocessing_report(cfg, stats, elt)
    _record(engine, meta)
    return 0


def cmd_load(args, cfg) -> int:
    from cfiq.warehouse.load import run_load

    engine = _engine()
    _record(engine, run_load(cfg, engine))
    return 0


def cmd_olap(args, cfg) -> int:
    from cfiq.warehouse.olap import run_olap

    for name, df in run_olap(cfg, _engine()).items():
        log.info("olap %-32s %5d rows -> reports/olap/%s.csv", name, len(df), name)
    return 0


def cmd_mine(args, cfg) -> int:
    from cfiq.mining.run import run_mining

    engine = _engine()
    _record(engine, run_mining(cfg, engine, only=args.only))
    return 0


def cmd_stream(args, cfg) -> int:
    from cfiq.mining.stream import run_stream

    run_stream(cfg, contest_id=args.contest_id, live=args.live, polls=args.polls)
    return 0


def cmd_all(args, cfg) -> int:
    for step in (cmd_extract, cmd_stage, cmd_transform, cmd_load, cmd_olap, cmd_mine):
        log.info("===== %s =====", step.__name__.removeprefix("cmd_"))
        step(args, cfg)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfiq", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)
        return p

    for name, func in (("extract", cmd_extract), ("all", cmd_all)):
        p = add(name, func, f"run {name}")
        p.add_argument("--sample-size", type=int, help="override SAMPLE_SIZE")
        p.add_argument("--seed", type=int, help="override RANDOM_SEED")
        p.set_defaults(only=None)
    add("stage", cmd_stage, "raw JSON -> staging tables (ELT)")
    add("transform", cmd_transform, "clean / impute / discretize (ETL) + ELT comparison")
    add("load", cmd_load, "load the star schema")
    add("olap", cmd_olap, "run OLAP queries")
    p = add("mine", cmd_mine, "run the mining modules")
    p.add_argument(
        "--only", nargs="+", choices=["association", "classification", "clustering", "outliers"]
    )
    p = add("stream", cmd_stream, "incremental verdict stats for one contest")
    p.add_argument("--contest-id", type=int, help="default: most recent finished Div. 2")
    p.add_argument("--live", action="store_true", help="poll the live API instead of replay")
    p.add_argument("--polls", type=int, default=5, help="number of polls in --live mode")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("matplotlib", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
