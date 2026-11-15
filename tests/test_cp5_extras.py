"""Checkpoint 5 extras: trip filters/sorting, quick links, packing checklist, calendar file,
duplicate-trip warning."""
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


def trips_in_db():
    with basecamp_app.get_db_connection() as conn:
        return conn.execute(
            "SELECT t.* FROM trips t JOIN users u ON u.id = t.user_id "
            "WHERE u.email LIKE 'test-%@example.com' ORDER BY t.id"
        ).fetchall()


def set_status(trip_id, status):
    with basecamp_app.get_db_connection() as conn:
        conn.execute("UPDATE trips SET status = %s WHERE id = %s", (status, trip_id))
        conn.commit()


def table_text(client, url):
    return page(client, url).split("<tbody>")[1].split("</tbody>")[0]  # skip flash messages


# ------------------------------------------------------- filter and sort
def test_filter_my_trips_by_status(client):
    signup(client)
    plan(client, objective_id=str(objective_id("Mount Elbert")), trip_date=future(10))
    plan(client, objective_id=str(objective_id("Mount Massive")), trip_date=future(20))
    elbert = trips_in_db()[0]["id"]
    set_status(elbert, "completed")
    done = table_text(client, "/trips?status=completed")
    assert "Mount Elbert" in done and "Mount Massive" not in done
    planned = table_text(client, "/trips?status=planned")
    assert "Mount Massive" in planned and "Mount Elbert" not in planned
    assert "1 of 2 trips" in page(client, "/trips?status=planned")
    assert "No trips match those filters" in page(client, "/trips?status=attempted")


def test_sort_by_date_both_directions(client):
    signup(client)
    plan(client, objective_id=str(objective_id("Mount Elbert")), trip_date=future(10))
    plan(client, objective_id=str(objective_id("Mount Massive")), trip_date=future(90))
    newest = table_text(client, "/trips")
    assert newest.index("Mount Massive") < newest.index("Mount Elbert")
    oldest = table_text(client, "/trips?sort=oldest")
    assert oldest.index("Mount Elbert") < oldest.index("Mount Massive")


@pytest.mark.parametrize("query", [
    "status=bogus", "sort=bogus", "status=planned%27%3B+DROP+TABLE+trips%3B--",
    "sort=oldest%3B+DROP+TABLE+trips", "status=&sort=",
])
def test_bad_filter_values_are_ignored_safely(client, query):
    signup(client)
    plan(client)
    assert "Mount Elbert" in table_text(client, "/trips?" + query)
    assert len(trips_in_db()) == 1


def test_filters_only_show_my_own_trips(client, other_client):
    signup(client)
    plan(client, notes="Alice private note")
    signup(other_client, name="Bob")
    assert "No trips yet" in page(other_client, "/trips")
    assert "Alice private note" not in page(other_client, "/trips?status=planned")


# ------------------------------------------------------- quick links
def test_loadout_page_links_to_plan_a_trip_with_it_selected(client):
    signup(client)
    lid = make_loadout(client, name="Elbert pack")
    body = page(client, f"/loadouts/{lid}")
    assert f'href="/trips/plan?loadout={lid}"' in body
    form = page(client, f"/trips/plan?loadout={lid}")
    assert re.search(rf'<option value="{lid}" selected>', form)


def test_objective_page_lists_my_trips_there(client, other_client):
    signup(client)
    elbert = objective_id("Mount Elbert")
    assert "You have not planned a trip here yet" in page(client, f"/objectives/{elbert}")
    plan(client, trip_date=future(10))
    plan(client, trip_date=future(50))
    plan(client, objective_id=str(objective_id("Mount Massive")), trip_date=future(5))
    body = page(client, f"/objectives/{elbert}")
    assert "You have 2 trips to Mount Elbert" in body
    assert "Planned" in body
    signup(other_client, name="Bob")
    assert "You have not planned a trip here yet" in page(other_client, f"/objectives/{elbert}")


def test_objective_page_hides_your_trips_when_logged_out(client):
    body = page(client, f"/objectives/{objective_id()}")
    assert "Your trips here" not in body


# ------------------------------------------------------- packing checklist
def test_checklist_lists_loadout_items_and_total(client):
    signup(client)
    tent = add_gear(client, "Tent", "Shelter", "48", "oz")
    quilt = add_gear(client, "Quilt", "Sleep", "16", "oz")
    lid = make_loadout(client, name="Elbert pack")
    post(client, f"/loadouts/{lid}/items", gear_item_id=str(tent), qty="1")
    post(client, f"/loadouts/{lid}/items", gear_item_id=str(quilt), qty="2")
    plan(client, loadout_id=str(lid), notes="Start at 4am")
    trip_id = trips_in_db()[0]["id"]
    body = page(client, f"/trips/{trip_id}/checklist")
    assert "Mount Elbert" in body and "Elbert pack" in body
    assert "Tent" in body and "Quilt" in body and "&times; 2" in body
    assert body.count('type="checkbox"') == 2
    assert "5 lb 0 oz" in body and "3 items" in body
    assert "Start at 4am" in body
    assert "window.print()" in body


def test_checklist_without_a_loadout_explains_itself(client):
    signup(client)
    plan(client)
    body = page(client, f"/trips/{trips_in_db()[0]['id']}/checklist")
    assert "no loadout attached" in body


def test_checklist_for_empty_loadout(client):
    signup(client)
    lid = make_loadout(client, name="Empty pack")
    plan(client, loadout_id=str(lid))
    assert "has no gear in it yet" in page(client, f"/trips/{trips_in_db()[0]['id']}/checklist")


def test_checklist_and_calendar_are_private(client, other_client):
    signup(client)
    plan(client)
    trip_id = trips_in_db()[0]["id"]
    signup(other_client, name="Bob")
    assert other_client.get(f"/trips/{trip_id}/checklist").status_code == 404
    assert other_client.get(f"/trips/{trip_id}/calendar.ics").status_code == 404
    assert client.get("/trips/99999999/checklist").status_code == 404


def test_checklist_and_calendar_require_login(client):
    for url in ("/trips/1/checklist", "/trips/1/calendar.ics"):
        assert client.get(url).status_code == 302


def test_checklist_escapes_names(client):
    signup(client)
    gear = add_gear(client, "<script>alert(1)</script>", "Other", "1", "oz")
    lid = make_loadout(client)
    post(client, f"/loadouts/{lid}/items", gear_item_id=str(gear), qty="1")
    plan(client, loadout_id=str(lid), notes="<b>bold</b>")
    body = page(client, f"/trips/{trips_in_db()[0]['id']}/checklist")
    assert "<script>alert(1)</script>" not in body and "<b>bold</b>" not in body


# ------------------------------------------------------- calendar file
def test_calendar_download_is_a_valid_all_day_event(client):
    signup(client)
    d = datetime.date.today() + datetime.timedelta(days=40)
    lid = make_loadout(client, name="Elbert pack")
    plan(client, trip_date=d.isoformat(), loadout_id=str(lid), notes="Alpine start, 4am; bring layers")
    trip_id = trips_in_db()[0]["id"]
    resp = client.get(f"/trips/{trip_id}/calendar.ics")
    assert resp.status_code == 200
    assert resp.mimetype == "text/calendar"
    assert 'attachment; filename="basecamp-mount-elbert-' + d.isoformat() + '.ics"' in resp.headers["Content-Disposition"]
    text = resp.get_data(as_text=True)
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")  # CRLF everywhere
    unfolded = text.replace("\r\n ", "")
    assert f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}" in unfolded
    assert f"DTEND;VALUE=DATE:{(d + datetime.timedelta(days=1)).strftime('%Y%m%d')}" in unfolded
    assert "SUMMARY:Basecamp: Mount Elbert" in unfolded
    assert f"UID:basecamp-trip-{trip_id}@basecamp" in unfolded
    assert re.search(r"GEO:39\.\d{5};-106\.\d{5}", unfolded)
    assert r"Loadout: Elbert pack\nAlpine start\, 4am\; bring layers" in unfolded


def test_ics_helpers_escape_and_fold():
    assert basecamp_app.ics_escape("a,b;c\\d\ne") == r"a\,b\;c\\d\ne"
    long_line = "DESCRIPTION:" + "x" * 200 + "é" * 50
    folded = basecamp_app.ics_fold(long_line)
    parts = folded.split("\r\n ")
    assert all(len(p.encode("utf-8")) <= 75 for p in parts[:1]) and all(len(p.encode("utf-8")) <= 74 for p in parts[1:])
    assert "".join(parts) == long_line  # nothing lost, no character split


# ------------------------------------------------------- duplicate warning
def test_duplicate_trip_asks_before_saving(client):
    signup(client)
    d = future(25)
    assert plan(client, trip_date=d).status_code == 302
    resp = plan(client, trip_date=d, notes="Second try")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "already have a trip to Mount Elbert on this date" in body
    assert 'name="confirm_duplicate"' in body and "Save anyway" in body
    assert "Second try" in body  # what you typed is kept
    assert len(trips_in_db()) == 1  # nothing saved yet
    again = plan(client, trip_date=d, notes="Second try", confirm_duplicate="1")
    assert again.status_code == 302
    assert len(trips_in_db()) == 2


def test_no_duplicate_warning_for_different_day_or_objective_or_user(client, other_client):
    signup(client)
    plan(client, trip_date=future(25))
    assert plan(client, trip_date=future(26)).status_code == 302
    assert plan(client, trip_date=future(25), objective_id=str(objective_id("Mount Massive"))).status_code == 302
    signup(other_client, name="Bob")
    assert plan(other_client, trip_date=future(25)).status_code == 302  # Bob's own trip, no warning
    assert len(trips_in_db()) == 4
