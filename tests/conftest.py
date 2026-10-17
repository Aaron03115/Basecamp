import os
import urllib.parse

import psycopg
import pytest

# Tests ALWAYS run against a dedicated test database, never the dev database, even if
# DATABASE_URL happens to be exported in your shell. ./start.sh creates "basecamp_test".
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://basecamp:basecamp@localhost:5432/basecamp_test",
)

_db_name = urllib.parse.urlparse(TEST_DATABASE_URL).path.lstrip("/")
if not _db_name.endswith("_test"):
    pytest.exit(
        f"Refusing to run tests against database '{_db_name}': the test database name "
        "must end in '_test' because some tests delete data.",
        returncode=2,
    )

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

try:
    psycopg.connect(TEST_DATABASE_URL, connect_timeout=5).close()
except psycopg.OperationalError as exc:
    pytest.exit(
        "Cannot reach the test database. Run ./start.sh once (it creates it), or set "
        f"TEST_DATABASE_URL to an empty PostgreSQL database whose name ends in _test.\n{exc}",
        returncode=2,
    )

import app as basecamp_app  # noqa: E402  (import after DATABASE_URL is set)


@pytest.fixture()
def client():
    basecamp_app.app.config.update(TESTING=True)
    with basecamp_app.app.test_client() as c:
        yield c
