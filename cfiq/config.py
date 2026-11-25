"""Configuration loader.

Structure comes from config/cfiq.yaml; secrets and run knobs come from .env
(environment variables win over the YAML file). Nothing else in the code base
hard-codes paths, URLs, credentials, sample sizes or seeds.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "cfiq.yaml"


def load_config(path: str | Path | None = None) -> dict:
    """Read the YAML config, apply .env overrides and resolve paths."""
    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(path or os.getenv("CFIQ_CONFIG", DEFAULT_CONFIG))
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sampling = cfg["sampling"]
    sampling["sample_size"] = int(os.getenv("SAMPLE_SIZE", sampling["sample_size"]))
    sampling["seed"] = int(os.getenv("RANDOM_SEED", sampling["seed"]))

    cfg["paths"] = {name: PROJECT_ROOT / p for name, p in cfg["paths"].items()}
    return cfg


def database_url() -> str:
    """SQLAlchemy URL for Postgres, built from .env values."""
    load_dotenv(PROJECT_ROOT / ".env")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"
