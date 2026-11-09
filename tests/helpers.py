"""Shared helpers for tests that need a logged-in user (used by test_cp4 onward)."""
import re
import uuid

import app as basecamp_app

PASSWORD = "correct horse battery"


def csrf(client):
    with client.session_transaction() as sess:
        sess.setdefault("csrf_token", "test-token")
        return sess["csrf_token"]


def post(client, url, **data):
    data["csrf_token"] = csrf(client)
    return client.post(url, data=data)


def signup(client, name="Test Hiker"):
    email = f"test-{uuid.uuid4().hex[:10]}@example.com"
    resp = post(client, "/signup", name=name, email=email, password=PASSWORD, confirm=PASSWORD)
    assert resp.status_code == 302
    return email


def add_gear(client, name, category="Shelter", weight="1", unit="lb"):
    resp = post(client, "/gear/new", name=name, category=category, weight=weight, unit=unit)
    assert resp.status_code == 302
    with basecamp_app.get_db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM gear_items WHERE name = %s ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
    return row["id"]


def make_loadout(client, name="Weekend", trip_type="Overnight"):
    resp = post(client, "/loadouts/new", name=name, trip_type=trip_type)
    assert resp.status_code == 302
    return int(re.search(r"/loadouts/(\d+)", resp.headers["Location"]).group(1))


def delete_test_users():
    with basecamp_app.get_db_connection() as conn:
        conn.execute("DELETE FROM users WHERE email LIKE 'test-%@example.com'")
        conn.commit()
