"""Shared test setup. Runs before tests/test_app.py is imported."""
import os

# app.py refuses to start without a SECRET_KEY, so give the tests one before it is imported.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.pop("TMDB_READ_TOKEN", None)

import pytest  # noqa: E402

import app as appmod  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """The TMDB cache and the rate limiter are module-level, so reset them between tests."""
    appmod._cache.clear()
    appmod._hits.clear()
    yield
    appmod._cache.clear()
    appmod._hits.clear()
