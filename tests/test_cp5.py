"""Checkpoint 5 tests: the Plan a Trip form and the My Trips list."""
import datetime
import re
import urllib.parse

import pytest

import app as basecamp_app
from helpers import add_gear, delete_test_users, make_loadout, post, signup


@pytest.fixture(autouse=True)
def cleanup():
    yield
    delete_test_users()


@pytest.fixture()
def other_client():
    return basecamp_app.app.test_client()


def future(days=30):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def objective_id(name="Mount Elbert"):
    with basecamp_app.get_db_connection() as conn:
        return conn.execute("SELECT id FROM objectives WHERE name = %s", (name,)).fetchone()["id"]


def page(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def plan(client, **overrides):
    data = {"objective_id": str(objective_id()), "loadout_id": "", "trip_date": future(), "notes": ""}
    data.update(overrides)
    return post(client, "/trips/plan", **data)


def trip_rows():
    with basecamp_app.get_db_connection() as conn:
        return conn.execute(
            "SELECT t.* FROM trips t JOIN users u ON u.id = t.user_id "
            "WHERE u.email LIKE 'test-%@example.com' ORDER BY t.id"
        ).fetchall()


# ------------------------------------------------------------------ access
def test_trip_pages_require_login(client):
    for url in ("/trips", "/trips/plan"):
        resp = client.get(url)
        assert resp.status_code == 302 and f"/login?next={url}" in resp.headers["Location"]
    assert client.post("/trips/plan", data={}).status_code == 400  # no CSRF token


def test_trip_pages_are_real_pages(client):
    signup(client)
    assert "planned for Checkpoint" not in page(client, "/trips")
    assert "planned for Checkpoint" not in page(client, "/trips/plan")


def test_objective_page_link_prefills_the_form(client):
    signup(client)
    oid = objective_id("Mount Elbert")
    body = page(client, f"/trips/plan?objective={oid}")
    assert re.search(rf'<option value="{oid}" selected>', body)
    # A garbage value is ignored rather than crashing
    assert page(client, "/trips/plan?objective=abc&loadout=zzz")


def test_login_redirect_keeps_the_prefill(client):
    oid = objective_id()
    resp = client.get(f"/trips/plan?objective={oid}")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["Location"]).query)
    assert query["next"] == [f"/trips/plan?objective={oid}"]


# ------------------------------------------------------------------ creating trips
def test_plan_a_trip_and_see_it_in_my_trips(client):
    signup(client)
    d = future(40)
    resp = plan(client, trip_date=d, notes="Alpine start at 4am")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/trips")
    rows = trip_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "planned" and rows[0]["trip_date"] == d
    assert rows[0]["notes"] == "Alpine start at 4am" and rows[0]["loadout_id"] is None
    body = page(client, "/trips")
    assert "Mount Elbert" in body and "Planned" in body and "Alpine start at 4am" in body
    assert "None" in body  # no loadout attached


def test_trip_with_loadout_shows_its_weight(client):
    signup(client)
    tent = add_gear(client, "Tent", "Shelter", "48", "oz")
    lid = make_loadout(client, name="Elbert pack")
    post(client, f"/loadouts/{lid}/items", gear_item_id=str(tent), qty="2")
    assert plan(client, loadout_id=str(lid)).status_code == 302
    body = page(client, "/trips")
    assert "Elbert pack" in body and "6 lb 0 oz" in body and f"/loadouts/{lid}" in body


def test_empty_notes_are_stored_as_null_and_past_dates_are_allowed(client):
    signup(client)
    past = (datetime.date.today() - datetime.timedelta(days=100)).isoformat()
    assert plan(client, trip_date=past, notes="   ").status_code == 302
    row = trip_rows()[0]
    assert row["notes"] is None and row["trip_date"] == past


def test_trips_are_sorted_newest_first(client):
    signup(client)
    plan(client, trip_date=future(10), objective_id=str(objective_id("Mount Elbert")))
    plan(client, trip_date=future(90), objective_id=str(objective_id("Mount Massive")))
    table = page(client, "/trips").split("<tbody>")[1]  # skip the flash messages above the table
    assert table.index("Mount Massive") < table.index("Mount Elbert")


@pytest.mark.parametrize("overrides,message", [
    ({"objective_id": ""}, "Choose an objective"),
    ({"objective_id": "abc"}, "Choose an objective"),
    ({"objective_id": "99999999"}, "does not exist"),
    ({"trip_date": ""}, "Pick a trip date"),
    ({"trip_date": "tomorrow"}, "year-month-day"),
    ({"trip_date": "2026-02-31"}, "year-month-day"),
    ({"trip_date": "1999-12-31"}, "between 2000"),
    ({"trip_date": "2999-01-01"}, "between 2000"),
    ({"loadout_id": "abc"}, "your loadouts"),
    ({"loadout_id": "99999999"}, "your loadouts"),
    ({"notes": "x" * 2001}, "Notes can be up to"),
])
def test_trip_form_validation(client, overrides, message):
    signup(client)
    resp = plan(client, **overrides)
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)
    assert trip_rows() == []


def test_failed_submit_keeps_what_you_typed(client):
    signup(client)
    body = plan(client, trip_date="", notes="Remember this note").get_data(as_text=True)
    assert "Remember this note" in body


def test_notes_are_escaped(client):
    signup(client)
    plan(client, notes="<script>alert(1)</script>")
    body = page(client, "/trips")
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


# ------------------------------------------------------------------ ownership and cleanup
def test_cannot_attach_someone_elses_loadout(client, other_client):
    signup(client)
    alice_loadout = make_loadout(client, name="Alice secret loadout")
    signup(other_client, name="Bob")
    resp = plan(other_client, loadout_id=str(alice_loadout))
    assert resp.status_code == 400
    assert "Alice secret loadout" not in page(other_client, "/trips/plan")  # not even offered


def test_trips_are_private_and_only_owner_can_remove(client, other_client):
    signup(client)
    plan(client, notes="Alice private note")
    trip_id = trip_rows()[0]["id"]
    signup(other_client, name="Bob")
    assert "Alice private note" not in page(other_client, "/trips")
    assert post(other_client, f"/trips/{trip_id}/delete").status_code == 404
    assert len(trip_rows()) == 1
    assert post(client, f"/trips/{trip_id}/delete").status_code == 302
    assert trip_rows() == []
    assert post(client, f"/trips/{trip_id}/delete").status_code == 404


def test_deleting_a_loadout_keeps_the_trip(client):
    signup(client)
    lid = make_loadout(client, name="Temp loadout")
    plan(client, loadout_id=str(lid))
    post(client, f"/loadouts/{lid}/delete")
    rows = trip_rows()
    assert len(rows) == 1 and rows[0]["loadout_id"] is None
    assert "Mount Elbert" in page(client, "/trips")


def test_empty_state(client):
    signup(client)
    assert "No trips yet" in page(client, "/trips")
