import pytest

from cfiq.config import load_config


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def tcfg(cfg):
    return cfg["transform"]
