"""Checkpoint 4 tests: loadout builder (named packing lists, quantities, automatic weight totals)."""
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


def qty_in_db(loadout_id, gear_id):
    with basecamp_app.get_db_connection() as conn:
        row = conn.execute(
            "SELECT qty FROM loadout_items WHERE loadout_id = %s AND gear_item_id = %s",
            (loadout_id, gear_id),
        ).fetchone()
    return row["qty"] if row else None


def page(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def add_item(client, loadout_id, gear_id, qty="1"):
    return post(client, f"/loadouts/{loadout_id}/items", gear_item_id=str(gear_id), qty=qty)


# ---------------------------------------------------------------- access
def test_loadouts_require_login(client):
    resp = client.get("/loadouts")
    assert resp.status_code == 302 and "/login?next=/loadouts" in resp.headers["Location"]
    assert client.post("/loadouts/new", data={"name": "x", "trip_type": "Other"}).status_code == 400  # no CSRF


def test_loadout_builder_is_a_real_page(client):
    signup(client)
    body = page(client, "/loadouts")
    assert "planned for Checkpoint" not in body and "Create loadout" in body


# ---------------------------------------------------------------- create / edit / delete
def test_create_loadout_and_see_it_listed(client):
    signup(client)
    lid = make_loadout(client, name="Elbert day pack", trip_type="Day hike")
    body = page(client, "/loadouts")
    assert "Elbert day pack" in body and "Day hike" in body and f"/loadouts/{lid}" in body
    assert "0 oz" in body  # empty loadout weighs nothing


@pytest.mark.parametrize("name,trip_type,message", [
    ("", "Overnight", "loadout name"),
    ("   ", "Overnight", "loadout name"),
    ("x" * 101, "Overnight", "loadout name"),
    ("Pack", "Moon landing", "trip type"),
    ("Pack", "", "trip type"),
])
def test_create_loadout_validation(client, name, trip_type, message):
    signup(client)
    resp = post(client, "/loadouts/new", name=name, trip_type=trip_type)
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)


def test_rename_loadout(client):
    signup(client)
    lid = make_loadout(client, name="Old name")
    assert post(client, f"/loadouts/{lid}/edit", name="New name", trip_type="Winter / snow").status_code == 302
    body = page(client, f"/loadouts/{lid}")
    assert "New name" in body and "Winter / snow" in body
    assert post(client, f"/loadouts/{lid}/edit", name="", trip_type="Other").status_code == 400


def test_delete_loadout(client):
    signup(client)
    lid = make_loadout(client)
    assert post(client, f"/loadouts/{lid}/delete").status_code == 302
    assert client.get(f"/loadouts/{lid}").status_code == 404


def test_loadout_names_are_escaped(client):
    signup(client)
    lid = make_loadout(client, name="<script>alert(1)</script>")
    for url in ("/loadouts", f"/loadouts/{lid}"):
        body = page(client, url)
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


# ---------------------------------------------------------------- items and weight totals
def test_weight_totals_are_computed_automatically(client):
    signup(client)
    tent = add_gear(client, "Tent", "Shelter", "48", "oz")
    quilt = add_gear(client, "Quilt", "Sleep", "16", "oz")
    lid = make_loadout(client)
    assert add_item(client, lid, tent, "1").status_code == 302
    assert add_item(client, lid, quilt, "2").status_code == 302
    body = page(client, f"/loadouts/{lid}")
    assert "5 lb 0 oz" in body          # 48 + 2 x 16 = 80 oz
    assert "3 items packed" in body     # quantities count
    assert "Shelter" in body and "3 lb 0 oz" in body
    assert "Sleep" in body and "2 lb 0 oz" in body  # 2 x 16 oz
    listing = page(client, "/loadouts")
    assert "5 lb 0 oz" in listing


def test_adding_same_gear_again_increases_quantity(client):
    signup(client)
    gear = add_gear(client, "Water bottle", "Water", "2", "oz")
    lid = make_loadout(client)
    add_item(client, lid, gear, "1")
    add_item(client, lid, gear, "2")
    assert qty_in_db(lid, gear) == 3
    add_item(client, lid, gear, "99")
    assert qty_in_db(lid, gear) == 99  # capped, not an error


def test_set_quantity_and_remove_item(client):
    signup(client)
    gear = add_gear(client, "Headlamp", "Electronics", "3", "oz")
    lid = make_loadout(client)
    add_item(client, lid, gear)
    assert post(client, f"/loadouts/{lid}/items/{gear}/qty", qty="4").status_code == 302
    assert qty_in_db(lid, gear) == 4
    assert "12 oz" in page(client, f"/loadouts/{lid}")
    assert post(client, f"/loadouts/{lid}/items/{gear}/remove").status_code == 302
    assert qty_in_db(lid, gear) is None
    assert "0 items packed" in page(client, f"/loadouts/{lid}")


@pytest.mark.parametrize("qty", ["0", "-1", "100", "abc", "1.5", "", " "])
def test_quantity_validation(client, qty):
    signup(client)
    gear = add_gear(client, "Gloves", "Clothing", "4", "oz")
    lid = make_loadout(client)
    add_item(client, lid, gear, "2")
    add_item(client, lid, gear, qty)  # rejected: nothing changes
    assert qty_in_db(lid, gear) == 2
    post(client, f"/loadouts/{lid}/items/{gear}/qty", qty=qty)
    assert qty_in_db(lid, gear) == 2


def test_changing_gear_weight_updates_loadout_totals(client):
    signup(client)
    gear = add_gear(client, "Tent", "Shelter", "48", "oz")
    lid = make_loadout(client)
    add_item(client, lid, gear)
    assert "3 lb 0 oz" in page(client, f"/loadouts/{lid}")
    resp = post(client, f"/gear/{gear}/edit", name="Tent", category="Shelter", weight="2", unit="lb")
    assert resp.status_code == 302
    assert "2 lb 0 oz" in page(client, f"/loadouts/{lid}")


def test_deleting_gear_removes_it_from_loadouts(client):
    signup(client)
    gear = add_gear(client, "Old stove", "Cooking", "10", "oz")
    lid = make_loadout(client)
    add_item(client, lid, gear)
    post(client, f"/gear/{gear}/delete")
    assert qty_in_db(lid, gear) is None
    assert client.get(f"/loadouts/{lid}").status_code == 200


def test_copy_loadout_is_independent(client):
    signup(client)
    gear = add_gear(client, "Tent", "Shelter", "48", "oz")
    lid = make_loadout(client, name="Original")
    add_item(client, lid, gear, "1")
    resp = post(client, f"/loadouts/{lid}/copy")
    assert resp.status_code == 302
    copy_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    assert copy_id != lid and qty_in_db(copy_id, gear) == 1
    assert "Original (copy)" in page(client, f"/loadouts/{copy_id}")
    post(client, f"/loadouts/{copy_id}/items/{gear}/qty", qty="5")
    assert qty_in_db(lid, gear) == 1  # original untouched


def test_loadout_with_no_gear_library_shows_hint(client):
    signup(client)
    lid = make_loadout(client)
    assert "Your gear library is empty" in page(client, f"/loadouts/{lid}")


# ---------------------------------------------------------------- ownership
def test_users_cannot_touch_each_others_loadouts(client, other_client):
    signup(client)
    gear = add_gear(client, "Alice tent", "Shelter", "48", "oz")
    lid = make_loadout(client, name="Alice private pack")
    add_item(client, lid, gear)
    signup(other_client, name="Bob")
    assert "Alice private pack" not in page(other_client, "/loadouts")
    assert other_client.get(f"/loadouts/{lid}").status_code == 404
    for action, data in [
        ("edit", {"name": "Hacked", "trip_type": "Other"}),
        ("delete", {}),
        ("copy", {}),
        ("items", {"gear_item_id": str(gear), "qty": "1"}),
        (f"items/{gear}/qty", {"qty": "9"}),
        (f"items/{gear}/remove", {}),
    ]:
        assert post(other_client, f"/loadouts/{lid}/{action}", **data).status_code == 404, action
    assert qty_in_db(lid, gear) == 1
    assert "Alice private pack" in page(client, "/loadouts")


def test_cannot_pack_someone_elses_gear(client, other_client):
    signup(client)
    alice_gear = add_gear(client, "Alice stove", "Cooking", "10", "oz")
    signup(other_client, name="Bob")
    bob_loadout = make_loadout(other_client, name="Bob pack")
    add_item(other_client, bob_loadout, alice_gear)
    assert qty_in_db(bob_loadout, alice_gear) is None
    body = page(other_client, f"/loadouts/{bob_loadout}")
    assert "Alice stove" not in body


def test_add_item_with_garbage_gear_id_is_rejected(client):
    signup(client)
    lid = make_loadout(client)
    for bad in ("abc", "", "-5", "99999999"):
        assert add_item(client, lid, bad).status_code == 302
    with basecamp_app.get_db_connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM loadout_items WHERE loadout_id = %s", (lid,)).fetchone()["n"]
    assert n == 0
