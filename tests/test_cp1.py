"""Checkpoint 1 tests: the app boots, the schema exists, and core pages render.

These need a reachable PostgreSQL test database (see tests/conftest.py).
Feature-specific tests are added alongside each later checkpoint.
"""
from concurrent.futures import ThreadPoolExecutor

import app as basecamp_app

TABLES = {
    "users", "objectives", "gear_items", "loadouts",
    "loadout_items", "trips", "trip_reports",
}


def _count_objectives():
    with basecamp_app.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM objectives")
            return cur.fetchone()["n"]


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_landing_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Basecamp" in body
    assert "Who it" in body  # "Who it's for" section
    assert "Summit Log" in body  # explore link to a planned page


def test_header_navigation_links_every_page(client):
    body = client.get("/").get_data(as_text=True)
    for page in basecamp_app.PAGES:
        assert f'href="{page["path"]}"' in body


def test_planned_pages_do_not_404(client):
    for page in basecamp_app.PAGES:
        if "checkpoint" in page:
            resp = client.get(page["path"])
            assert resp.status_code == 200, page["path"]
            assert page["label"] in resp.get_data(as_text=True)


def test_schema_has_all_tables():
    with basecamp_app.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            found = {row["table_name"] for row in cur.fetchall()}
    assert TABLES <= found


def test_schema_has_stretch_goal_columns():
    with basecamp_app.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
            cols = {(r["table_name"], r["column_name"]) for r in cur.fetchall()}
    assert ("objectives", "lat") in cols
    assert ("objectives", "lng") in cols
    assert ("trips", "route_geojson") in cols


def test_schema_seeded_objectives():
    assert _count_objectives() >= 5


def test_init_db_is_idempotent():
    before = _count_objectives()
    basecamp_app.init_db()
    basecamp_app.init_db()
    assert _count_objectives() == before


def test_concurrent_init_does_not_duplicate_seed_data():
    """Several workers starting at once (gunicorn) must seed exactly once."""
    with basecamp_app.get_db_connection() as conn:
        conn.execute("TRUNCATE objectives RESTART IDENTITY CASCADE")
        conn.commit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(basecamp_app.init_db) for _ in range(4)]
        for f in futures:
            f.result()  # re-raises any error from a worker
    assert _count_objectives() == len(basecamp_app.SEED_OBJECTIVES)
