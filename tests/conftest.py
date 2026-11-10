from datetime import date, timedelta

import pytest

from compass.config import get_settings
from compass.db import connect, upsert_series


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_LOCAL_CONFIG", str(tmp_path / "local"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def conn(settings):
    c = connect()
    yield c
    c.close()


def daily(values, start="2020-01-01"):
    """Points on consecutive calendar days starting at `start`."""
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def put(conn, key, points):
    upsert_series(conn, key, points, "test")
