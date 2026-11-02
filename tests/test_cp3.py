"""Checkpoint 3 tests: accounts (signup/login/logout) and the gear library (CRUD)."""
import re
import uuid

import pytest

import app as basecamp_app

PASSWORD = "correct horse battery"


def _token(client):
    """The CSRF token for this client's session (what the hidden form field would hold)."""
    with client.session_transaction() as sess:
        sess.setdefault("csrf_token", "test-token")
        return sess["csrf_token"]


def post(client, url, **data):
    data["csrf_token"] = _token(client)
    return client.post(url, data=data)


def signup(client, email=None, name="Test Hiker", password=PASSWORD, confirm=None):
    email = email or f"test-{uuid.uuid4().hex[:10]}@example.com"
    resp = post(client, "/signup", name=name, email=email, password=password,
                confirm=password if confirm is None else confirm)
    return email, resp


def add_gear(client, name="Tent", category="Shelter", weight="3", unit="lb"):
    return post(client, "/gear/new", name=name, category=category, weight=weight, unit=unit)


def gear_ids(client):
    return re.findall(r'/gear/(\d+)/edit', client.get("/gear").get_data(as_text=True))


@pytest.fixture(autouse=True)
def cleanup_test_users():
    yield
    with basecamp_app.get_db_connection() as conn:
        conn.execute("DELETE FROM users WHERE email LIKE 'test-%@example.com'")
        conn.commit()


@pytest.fixture()
def other_client():
    """A second, separate browser (own cookies). Not used as a context manager, so it can
    be used alongside the `client` fixture without the two interfering."""
    return basecamp_app.app.test_client()


def fresh_client():
    return basecamp_app.app.test_client()


# ----------------------------- accounts ------------------------------------
def test_signup_creates_account_and_logs_in(client):
    email, resp = signup(client)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/gear")
    with basecamp_app.get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None
    assert row["password_hash"] != PASSWORD and PASSWORD not in row["password_hash"]
    body = client.get("/gear").get_data(as_text=True)
    assert "Test Hiker" in body and "Log out" in body


def test_signup_rejects_duplicate_email(client, other_client):
    email, _ = signup(client)
    _, resp = signup(other_client, email=email.upper())  # emails are case-insensitive
    assert resp.status_code == 400
    assert "already exists" in resp.get_data(as_text=True)


@pytest.mark.parametrize("kwargs,message", [
    ({"password": "short", "confirm": "short"}, "at least 8"),
    ({"confirm": "different password"}, "do not match"),
    ({"email": "not-an-email"}, "valid email"),
    ({"name": "   "}, "Enter your name"),
])
def test_signup_validation(client, kwargs, message):
    _, resp = signup(client, **kwargs)
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)


def test_login_and_logout(client, other_client):
    email, _ = signup(client)
    resp = post(other_client, "/login", email=email, password=PASSWORD)
    assert resp.status_code == 302
    assert "Log out" in other_client.get("/gear").get_data(as_text=True)
    resp = post(other_client, "/logout")
    assert resp.status_code == 302
    assert other_client.get("/gear").status_code == 302  # logged out again


def test_login_fails_with_wrong_password_or_unknown_email(client):
    email, _ = signup(client)
    fresh = fresh_client()
    bad = post(fresh, "/login", email=email, password="wrong password!")
    unknown = post(fresh, "/login", email="test-nobody@example.com", password=PASSWORD)
    assert bad.status_code == unknown.status_code == 401
    assert "Incorrect email or password" in bad.get_data(as_text=True)
    assert "Incorrect email or password" in unknown.get_data(as_text=True)


def test_login_redirect_cannot_leave_the_site(client):
    email, _ = signup(client)
    fresh = fresh_client()
    for evil in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        resp = post(fresh, "/login", email=email, password=PASSWORD, next=evil)
        assert resp.status_code == 302
        assert "evil.example" not in resp.headers["Location"]
        post(fresh, "/logout")


def test_post_without_csrf_token_is_rejected(client):
    resp = client.post("/signup", data={"name": "x", "email": "test-csrf@example.com",
                                        "password": PASSWORD, "confirm": PASSWORD})
    assert resp.status_code == 400
    with basecamp_app.get_db_connection() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE email = 'test-csrf@example.com'").fetchone() is None


def test_gear_requires_login_and_returns_after_login(client):
    resp = client.get("/gear")
    assert resp.status_code == 302 and "/login?next=/gear" in resp.headers["Location"]
    assert client.post("/gear/new", data={}).status_code == 400  # no CSRF token
    body = client.get("/login").get_data(as_text=True)
    assert 'name="csrf_token"' in body


# ------------------------------ gear CRUD ----------------------------------
def test_add_gear_and_list_it(client):
    signup(client)
    assert add_gear(client, name="Big Agnes Tent", weight="3.5", unit="lb").status_code == 302
    body = client.get("/gear").get_data(as_text=True)
    assert "Big Agnes Tent" in body and "Shelter" in body
    assert "3 lb 8 oz" in body  # 3.5 lb stored as 56 oz


def test_weight_units_convert_to_ounces(client):
    signup(client)
    add_gear(client, name="Stove", category="Cooking", weight="100", unit="g")
    add_gear(client, name="Rope", category="Climbing", weight="1", unit="kg")
    with basecamp_app.get_db_connection() as conn:
        rows = {r["name"]: r["weight_oz"] for r in conn.execute("SELECT name, weight_oz FROM gear_items").fetchall()}
    assert rows["Stove"] == pytest.approx(3.53, abs=0.01)
    assert rows["Rope"] == pytest.approx(35.27, abs=0.01)


@pytest.mark.parametrize("kwargs,message", [
    ({"name": ""}, "item name"),
    ({"category": "Bogus"}, "category"),
    ({"weight": "abc"}, "number"),
    ({"weight": "-2"}, "more than zero"),
    ({"weight": "0"}, "more than zero"),
    ({"weight": "nan"}, "number"),
    ({"weight": "inf"}, "number"),
    ({"weight": "99999", "unit": "lb"}, "under 625"),
    ({"unit": "stone"}, "unit"),
])
def test_gear_validation(client, kwargs, message):
    signup(client)
    resp = add_gear(client, **kwargs)
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)
    assert gear_ids(client) == []


def test_gear_totals_and_category_filter(client):
    signup(client)
    add_gear(client, name="Tent", category="Shelter", weight="48", unit="oz")
    add_gear(client, name="Quilt", category="Sleep", weight="16", unit="oz")
    body = client.get("/gear").get_data(as_text=True)
    assert "4 lb" in body and "2 items" in body
    filtered = client.get("/gear?category=Sleep").get_data(as_text=True)
    assert "Quilt" in filtered and ">Tent<" not in filtered.split("<tbody>")[-1]


def test_edit_gear(client):
    signup(client)
    add_gear(client, name="Tent", weight="3", unit="lb")
    item_id = gear_ids(client)[0]
    assert "Tent" in client.get(f"/gear/{item_id}/edit").get_data(as_text=True)
    resp = post(client, f"/gear/{item_id}/edit", name="Better Tent", category="Shelter", weight="2", unit="lb")
    assert resp.status_code == 302
    body = client.get("/gear").get_data(as_text=True)
    assert "Better Tent" in body and "2 lb" in body
    bad = post(client, f"/gear/{item_id}/edit", name="", category="Shelter", weight="2", unit="lb")
    assert bad.status_code == 400


def test_delete_gear(client):
    signup(client)
    add_gear(client, name="Doomed Tent")
    item_id = gear_ids(client)[0]
    assert post(client, f"/gear/{item_id}/delete").status_code == 302
    assert gear_ids(client) == []  # (the "Deleted Doomed Tent" flash message may still show once)
    assert post(client, f"/gear/{item_id}/delete").status_code == 404  # already gone


def test_users_cannot_see_or_change_each_others_gear(client, other_client):
    signup(client)
    add_gear(client, name="Alice Secret Tent")
    item_id = gear_ids(client)[0]
    signup(other_client, name="Bob")
    assert "Alice Secret Tent" not in other_client.get("/gear").get_data(as_text=True)
    assert other_client.get(f"/gear/{item_id}/edit").status_code == 404
    assert post(other_client, f"/gear/{item_id}/edit", name="Hacked", category="Other",
                weight="1", unit="oz").status_code == 404
    assert post(other_client, f"/gear/{item_id}/delete").status_code == 404
    assert "Alice Secret Tent" in client.get("/gear").get_data(as_text=True)


def test_gear_names_are_escaped(client):
    signup(client)
    add_gear(client, name="<script>alert(1)</script>")
    body = client.get("/gear").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_gear_is_a_real_page_and_nav_shows_auth_links(client):
    body = client.get("/").get_data(as_text=True)
    assert "Log in" in body and "Sign up" in body
    assert "planned for Checkpoint 3" not in client.get("/login").get_data(as_text=True)
