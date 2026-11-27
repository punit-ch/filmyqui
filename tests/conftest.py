"""Shared test setup. Runs before tests/test_app.py is imported."""
import os

# app.py refuses to start without a long SECRET_KEY, so give the tests one before it is imported.
os.environ["SECRET_KEY"] = "test-only-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz"
os.environ.pop("TMDB_READ_TOKEN", None)
os.environ.pop("FLASK_DEBUG", None)
os.environ.pop("TRUST_PROXY", None)
os.environ.pop("ALLOWED_HOSTS", None)

import pytest  # noqa: E402

import app as appmod  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """The TMDB cache and the rate limiter are module-level, so reset them between tests."""
    appmod._cache.clear()
    appmod.limiter.clear()
    yield
    appmod._cache.clear()
    appmod.limiter.clear()
