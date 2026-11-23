"""Checkpoint 6 tests: trip detail page, editing, status workflow, conditions reports."""
import datetime
import re

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


def day(offset):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def objective_id(name="Mount Elbert"):
    with basecamp_app.get_db_connection() as conn:
        return conn.execute("SELECT id FROM objectives WHERE name = %s", (name,)).fetchone()["id"]


def page(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def make_trip(client, trip_date=None, **overrides):
    """Plan a trip (default: a week ago, so it can be attempted/completed) and return its id."""
    data = {"objective_id": str(objective_id()), "loadout_id": "", "trip_date": trip_date or day(-7), "notes": ""}
    data.update(overrides)
    assert post(client, "/trips/plan", **data).status_code == 302
    with basecamp_app.get_db_connection() as conn:
        return conn.execute(
            "SELECT t.id FROM trips t JOIN users u ON u.id = t.user_id "
            "WHERE u.email LIKE 'test-%@example.com' ORDER BY t.id DESC LIMIT 1"
        ).fetchone()["id"]


def trip_row(trip_id):
    with basecamp_app.get_db_connection() as conn:
        return conn.execute("SELECT * FROM trips WHERE id = %s", (trip_id,)).fetchone()


def reports_in_db(trip_id):
    with basecamp_app.get_db_connection() as conn:
        return conn.execute("SELECT * FROM trip_reports WHERE trip_id = %s ORDER BY id", (trip_id,)).fetchall()


def set_status(client, trip_id, status):
    return post(client, f"/trips/{trip_id}/status", status=status)


# ------------------------------------------------------------------ detail page
def test_trip_pages_require_login(client):
    for url in ("/trips/1",):
        assert client.get(url).status_code == 302
    for url in ("/trips/1/edit", "/trips/1/status", "/trips/1/reports", "/trips/1/reports/1/delete"):
        assert client.post(url, data={}).status_code == 400  # no CSRF token


def test_detail_shows_trip_loadout_and_actions(client):
    signup(client)
    tent = add_gear(client, "Tent", "Shelter", "48", "oz")
    lid = make_loadout(client, name="Elbert pack")
    post(client, f"/loadouts/{lid}/items", gear_item_id=str(tent), qty="2")
    tid = make_trip(client, loadout_id=str(lid), notes="Alpine start")
    body = page(client, f"/trips/{tid}")
    assert "Mount Elbert" in body and "Planned" in body and "14,439 ft" in body
    assert "Elbert pack" in body and "6 lb 0 oz" in body and "2 items" in body
    assert f"/trips/{tid}/checklist" in body and f"/trips/{tid}/calendar.ics" in body
    assert "Alpine start" in body  # shown inside the edit form while planned


def test_detail_404_for_missing_and_other_users_trips(client, other_client):
    signup(client)
    tid = make_trip(client)
    signup(other_client, name="Bob")
    assert other_client.get(f"/trips/{tid}").status_code == 404
    assert client.get("/trips/99999999").status_code == 404
    for url, data in [
        (f"/trips/{tid}/edit", {"trip_date": day(5), "notes": "hacked"}),
        (f"/trips/{tid}/status", {"status": "completed"}),
        (f"/trips/{tid}/reports", {"conditions_text": "hacked"}),
        (f"/trips/{tid}/reports/1/delete", {}),
    ]:
        assert post(other_client, url, **data).status_code == 404, url
    row = trip_row(tid)
    assert row["status"] == "planned" and row["notes"] is None


def test_my_trips_links_to_detail(client):
    signup(client)
    tid = make_trip(client)
    assert f'href="/trips/{tid}"' in page(client, "/trips")


# ------------------------------------------------------------------ editing
def test_edit_planned_trip(client):
    signup(client)
    lid = make_loadout(client, name="New pack")
    tid = make_trip(client, trip_date=day(20), notes="old")
    resp = post(client, f"/trips/{tid}/edit", loadout_id=str(lid), trip_date=day(25), notes="new notes")
    assert resp.status_code == 302
    row = trip_row(tid)
    assert row["loadout_id"] == lid and row["trip_date"] == day(25) and row["notes"] == "new notes"
    assert row["objective_id"] == objective_id()  # the objective cannot be changed by the form


def test_edit_can_clear_loadout_and_notes(client):
    signup(client)
    lid = make_loadout(client)
    tid = make_trip(client, trip_date=day(20), loadout_id=str(lid), notes="something")
    post(client, f"/trips/{tid}/edit", loadout_id="", trip_date=day(20), notes="  ")
    row = trip_row(tid)
    assert row["loadout_id"] is None and row["notes"] is None


@pytest.mark.parametrize("overrides,message", [
    ({"trip_date": ""}, "Pick a trip date"),
    ({"trip_date": "nope"}, "year-month-day"),
    ({"trip_date": "1999-01-01"}, "between 2000"),
    ({"notes": "x" * 2001}, "Notes can be up to"),
    ({"loadout_id": "abc"}, "your loadouts"),
    ({"loadout_id": "99999999"}, "your loadouts"),
])
def test_edit_validation(client, overrides, message):
    signup(client)
    tid = make_trip(client, trip_date=day(20), notes="keep me")
    data = {"loadout_id": "", "trip_date": day(21), "notes": "changed"}
    data.update(overrides)
    resp = post(client, f"/trips/{tid}/edit", **data)
    assert resp.status_code == 400 and message in resp.get_data(as_text=True)
    row = trip_row(tid)
    assert row["trip_date"] == day(20) and row["notes"] == "keep me"


def test_cannot_use_someone_elses_loadout_when_editing(client, other_client):
    signup(client)
    alice = make_loadout(client, name="Alice private")
    signup(other_client, name="Bob")
    tid = make_trip(other_client, trip_date=day(20))
    resp = post(other_client, f"/trips/{tid}/edit", loadout_id=str(alice), trip_date=day(20), notes="")
    assert resp.status_code == 400
    assert trip_row(tid)["loadout_id"] is None


def test_only_planned_trips_can_be_edited(client):
    signup(client)
    tid = make_trip(client, notes="original")
    set_status(client, tid, "completed")
    resp = post(client, f"/trips/{tid}/edit", loadout_id="", trip_date=day(-3), notes="changed")
    assert resp.status_code == 302
    row = trip_row(tid)
    assert row["notes"] == "original" and row["trip_date"] == day(-7)
    assert 'id="edit-trip"' not in page(client, f"/trips/{tid}")


# ------------------------------------------------------------------ status workflow
def test_status_workflow_transitions(client):
    signup(client)
    tid = make_trip(client)
    assert trip_row(tid)["status"] == "planned"
    for new in ("attempted", "completed", "attempted", "planned", "completed"):
        assert set_status(client, tid, new).status_code == 302
        assert trip_row(tid)["status"] == new, new
    assert "Completed" in page(client, f"/trips/{tid}")


@pytest.mark.parametrize("start,bad", [
    ("planned", "planned"), ("planned", "bogus"), ("planned", ""),
    ("attempted", "attempted"), ("completed", "planned"), ("completed", "completed"),
])
def test_invalid_status_changes_are_rejected(client, start, bad):
    signup(client)
    tid = make_trip(client)
    if start != "planned":
        set_status(client, tid, start)
    set_status(client, tid, bad)
    assert trip_row(tid)["status"] == start


def test_future_trips_cannot_be_attempted_or_completed(client):
    signup(client)
    tid = make_trip(client, trip_date=day(10))
    for status in ("attempted", "completed"):
        resp = set_status(client, tid, status)
        assert resp.status_code == 302
        assert trip_row(tid)["status"] == "planned"
    body = page(client, f"/trips/{tid}")
    assert 'id="future-note"' in body
    # Once the date is corrected it works
    post(client, f"/trips/{tid}/edit", loadout_id="", trip_date=day(-1), notes="")
    set_status(client, tid, "completed")
    assert trip_row(tid)["status"] == "completed"


def test_status_shows_on_list_and_filters(client):
    signup(client)
    tid = make_trip(client)
    set_status(client, tid, "completed")
    table = page(client, "/trips?status=completed").split("<tbody>")[1]
    assert "Mount Elbert" in table and "Completed" in table
    assert "No trips match those filters" in page(client, "/trips?status=planned")


# ------------------------------------------------------------------ conditions reports
def test_cannot_report_on_a_planned_trip(client):
    signup(client)
    tid = make_trip(client)
    assert post(client, f"/trips/{tid}/reports", conditions_text="Looks snowy").status_code == 302
    assert reports_in_db(tid) == []
    assert "Mark the trip attempted or completed" in page(client, f"/trips/{tid}")


def test_add_and_delete_report(client):
    signup(client)
    tid = make_trip(client)
    set_status(client, tid, "completed")
    assert post(client, f"/trips/{tid}/reports", conditions_text="Firm snow above 13k, dry trail below").status_code == 302
    assert len(reports_in_db(tid)) == 1
    assert "Firm snow above 13k" in page(client, f"/trips/{tid}")
    report_id = reports_in_db(tid)[0]["id"]
    assert post(client, f"/trips/{tid}/reports/{report_id}/delete").status_code == 302
    assert reports_in_db(tid) == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_report_validation(client, text):
    signup(client)
    tid = make_trip(client)
    set_status(client, tid, "attempted")
    post(client, f"/trips/{tid}/reports", conditions_text=text)
    assert reports_in_db(tid) == []


def test_report_text_is_escaped(client):
    signup(client)
    tid = make_trip(client)
    set_status(client, tid, "completed")
    post(client, f"/trips/{tid}/reports", conditions_text="<script>alert(1)</script>")
    body = page(client, f"/trips/{tid}")
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_reports_appear_on_the_objective_page_without_the_author(client, other_client):
    signup(client, name="Alice Hiker")
    tid = make_trip(client)
    set_status(client, tid, "completed")
    post(client, f"/trips/{tid}/reports", conditions_text="Summit ridge was icy")
    signup(other_client, name="Bob")
    body = page(other_client, f"/objectives/{objective_id()}")
    assert "Summit ridge was icy" in body
    assert "Alice Hiker" not in body and "test-" not in body
    assert "shared anonymously" in body


def test_deleting_the_trip_deletes_its_reports(client):
    signup(client)
    tid = make_trip(client)
    set_status(client, tid, "completed")
    post(client, f"/trips/{tid}/reports", conditions_text="Gone soon")
    post(client, f"/trips/{tid}/delete")
    assert reports_in_db(tid) == []


def test_report_cannot_be_deleted_through_a_different_trip(client):
    signup(client)
    t1 = make_trip(client)
    t2 = make_trip(client, trip_date=day(-9))
    set_status(client, t1, "completed")
    post(client, f"/trips/{t1}/reports", conditions_text="Belongs to trip 1")
    rid = reports_in_db(t1)[0]["id"]
    post(client, f"/trips/{t2}/reports/{rid}/delete")
    assert len(reports_in_db(t1)) == 1


# ------------------------------------------------------------------ stretch: map
def test_objective_page_has_a_map_container_with_coordinates(client):
    signup(client)
    body = page(client, f"/objectives/{objective_id()}")
    assert 'id="objective-map"' in body
    assert re.search(r'data-lat="39\.1178"', body) and 'data-lng="-106.4454"' in body
    assert "leaflet@1.9.4" in body and "openstreetmap.org/copyright" in body


def test_map_is_skipped_when_an_objective_has_no_coordinates(client):
    with basecamp_app.get_db_connection() as conn:
        conn.execute("DELETE FROM objectives WHERE name = 'Test No Coords'")
        oid = conn.execute(
            "INSERT INTO objectives (name, region, difficulty, elevation_ft, best_season) "
            "VALUES ('Test No Coords', 'Nowhere', 'Class 1', 9000, 'Summer') RETURNING id"
        ).fetchone()["id"]
        conn.commit()
    try:
        body = page(client, f"/objectives/{oid}")
        assert 'id="objective-map"' not in body and "leaflet" not in body
    finally:
        with basecamp_app.get_db_connection() as conn:
            conn.execute("DELETE FROM objectives WHERE id = %s", (oid,))
            conn.commit()
