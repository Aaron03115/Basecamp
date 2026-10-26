"""Checkpoint 2 tests: seed data, objectives search/filter page, objective detail page."""
import json
import re

import app as basecamp_app


def _ensure_seeded():
    basecamp_app.init_db()


def _total(body):
    return int(re.search(r"(\d+) objectives? found", body).group(1))


def test_seed_file_is_valid_and_unique():
    seed = basecamp_app.load_seed_objectives()
    assert len(seed) >= 20
    names = [o["name"] for o in seed]
    assert len(names) == len(set(names))
    for o in seed:
        assert o["name"] and o["region"] and o["difficulty"] and o["best_season"]
        assert 0 < o["elevation_ft"] < 15000
        assert 36 < o["lat"] < 42 and -110 < o["lng"] < -101  # inside Colorado


def test_seed_loads_into_database_without_duplicates():
    _ensure_seeded()
    basecamp_app.init_db()  # second run must not add rows
    with basecamp_app.get_db_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT name) AS d FROM objectives")
        row = cur.fetchone()
    assert row["n"] == row["d"] >= len(basecamp_app.SEED_OBJECTIVES)


def test_objectives_page_lists_objectives(client):
    _ensure_seeded()
    resp = client.get("/objectives")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Mount Elbert" in body
    assert _total(body) >= len(basecamp_app.SEED_OBJECTIVES)


def test_search_by_name_is_case_insensitive(client):
    _ensure_seeded()
    body = client.get("/objectives?q=eLbErT").get_data(as_text=True)
    assert "Mount Elbert" in body
    assert "Grays Peak" not in body


def test_filter_by_region_difficulty_and_season(client):
    _ensure_seeded()
    body = client.get("/objectives?region=Sawatch+Range&difficulty=Class+2&season=Summer").get_data(as_text=True)
    assert "Mount Massive" in body
    assert "Mount Elbert" not in body  # Class 1
    assert "Quandary" not in body  # different region


def test_filters_combine_with_search(client):
    _ensure_seeded()
    body = client.get("/objectives?q=peak&difficulty=Class+1").get_data(as_text=True)
    assert "Grays Peak" in body
    assert "Capitol Peak" not in body  # Class 4


def test_no_results_message(client):
    _ensure_seeded()
    body = client.get("/objectives?q=zzzznotapeak").get_data(as_text=True)
    assert "No objectives match" in body
    assert _total(body) == 0


def test_search_is_safe_against_sql_injection_and_wildcards(client):
    _ensure_seeded()
    resp = client.get("/objectives", query_string={"q": "x'; DROP TABLE objectives; --"})
    assert resp.status_code == 200
    assert "No objectives match" in resp.get_data(as_text=True)
    # The table is still there, and % is treated as a literal character, not a wildcard.
    assert _total(client.get("/objectives").get_data(as_text=True)) >= 20
    assert "No objectives match" in client.get("/objectives", query_string={"q": "%"}).get_data(as_text=True)


def test_pagination(client):
    _ensure_seeded()
    first = client.get("/objectives").get_data(as_text=True)
    second = client.get("/objectives?page=2").get_data(as_text=True)
    assert "Page 1 of" in first and "Page 2 of" in second
    ids1 = set(re.findall(r'href="/objectives/(\d+)"', first))
    ids2 = set(re.findall(r'href="/objectives/(\d+)"', second))
    assert ids1 and ids2 and not (ids1 & ids2)
    assert client.get("/objectives?page=abc").status_code == 200
    assert client.get("/objectives?page=9999").status_code == 200


def test_detail_page_shows_objective(client):
    _ensure_seeded()
    listing = client.get("/objectives?q=Elbert").get_data(as_text=True)
    oid = re.search(r'href="/objectives/(\d+)"', listing).group(1)
    resp = client.get(f"/objectives/{oid}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Mount Elbert" in body and "14,439 ft" in body and "Sawatch Range" in body
    assert "Plan a trip here" in body
    assert 'active" href="/objectives"' in body  # navbar highlights Objectives


def test_detail_page_missing_objective_is_404(client):
    resp = client.get("/objectives/99999999")
    assert resp.status_code == 404
    assert "Page not found" in resp.get_data(as_text=True)
    assert client.get("/objectives/not-a-number").status_code == 404


def test_objectives_is_a_real_page_not_a_placeholder(client):
    body = client.get("/objectives").get_data(as_text=True)
    assert "planned for Checkpoint" not in body
