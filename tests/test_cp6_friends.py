"""Checkpoint 6 extra: friends, trip partners, and opt-in gear sharing."""
import datetime

import pytest

import app as basecamp_app
from helpers import add_gear, delete_test_users, post, signup


@pytest.fixture(autouse=True)
def cleanup():
    yield
    delete_test_users()


def new_client():
    return basecamp_app.app.test_client()


@pytest.fixture()
def alice():
    c = new_client()
    c.email = signup(c, name="Alice Owner")
    return c


@pytest.fixture()
def bob():
    c = new_client()
    c.email = signup(c, name="Bob Partner")
    return c


@pytest.fixture()
def carol():
    c = new_client()
    c.email = signup(c, name="Carol Third")
    return c


def user_id(client):
    with basecamp_app.get_db_connection() as conn:
        return conn.execute("SELECT id FROM users WHERE email = %s", (client.email,)).fetchone()["id"]


def page(client, url, status=200):
    resp = client.get(url)
    assert resp.status_code == status, (url, resp.status_code)
    return resp.get_data(as_text=True)


def friendship_rows():
    with basecamp_app.get_db_connection() as conn:
        return conn.execute(
            "SELECT f.* FROM friendships f JOIN users u ON u.id = f.requester_id "
            "WHERE u.email LIKE 'test-%@example.com' ORDER BY f.id"
        ).fetchall()


def make_friends(a, b):
    post(a, "/friends/request", email=b.email)
    row = friendship_rows()[-1]
    assert post(b, f"/friends/{row['id']}/accept").status_code == 302
    return row["id"]


def make_trip(client, days_ago=7):
    with basecamp_app.get_db_connection() as conn:
        oid = conn.execute("SELECT id FROM objectives WHERE name = 'Mount Elbert'").fetchone()["id"]
    d = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    assert post(client, "/trips/plan", objective_id=str(oid), loadout_id="", trip_date=d, notes="Group trip").status_code == 302
    with basecamp_app.get_db_connection() as conn:
        return conn.execute(
            "SELECT t.id FROM trips t WHERE t.user_id = %s ORDER BY t.id DESC LIMIT 1", (user_id(client),)
        ).fetchone()["id"]


def participant_status(trip_id, client):
    with basecamp_app.get_db_connection() as conn:
        row = conn.execute("SELECT status FROM trip_participants WHERE trip_id = %s AND user_id = %s",
                           (trip_id, user_id(client))).fetchone()
    return row["status"] if row else None


def invite(owner, trip_id, friend):
    return post(owner, f"/trips/{trip_id}/invite", friend_id=str(user_id(friend)))


def join_trip(owner, trip_id, friend):
    invite(owner, trip_id, friend)
    assert post(friend, f"/trips/{trip_id}/invite/accept").status_code == 302
    assert participant_status(trip_id, friend) == "joined"


def share_gear(client, on=True):
    data = {"share_gear": "1"} if on else {}
    assert post(client, "/friends/settings", **data).status_code == 302


# ------------------------------------------------------------------ access
def test_friends_pages_require_login():
    c = new_client()
    assert c.get("/friends").status_code == 302
    for url in ("/friends/request", "/friends/1/accept", "/friends/1/remove", "/friends/settings",
                "/trips/1/invite", "/trips/1/invite/accept", "/trips/1/leave"):
        assert c.post(url, data={}).status_code == 400  # no CSRF token
    assert c.get("/trips/1/partners/2/gear").status_code == 302


def test_friends_is_in_the_navigation(alice):
    assert 'href="/friends"' in page(alice, "/")


# ------------------------------------------------------------------ friend requests
def test_send_and_accept_a_friend_request(alice, bob):
    assert post(alice, "/friends/request", email=bob.email).status_code == 302
    assert "Bob Partner" in page(alice, "/friends")          # waiting for a reply
    incoming = page(bob, "/friends")
    assert "Alice Owner" in incoming and "Requests for you" in incoming
    row = friendship_rows()[0]
    assert row["status"] == "pending"
    post(bob, f"/friends/{row['id']}/accept")
    assert friendship_rows()[0]["status"] == "accepted"
    assert 'id="friend-list"' in page(alice, "/friends") and "Bob Partner" in page(alice, "/friends")
    assert 'id="friend-list"' in page(bob, "/friends") and "Alice Owner" in page(bob, "/friends")


def test_unknown_email_gets_the_same_answer_as_a_real_one(alice, bob):
    def message(email):
        post(alice, "/friends/request", email=email)
        body = page(alice, "/friends")
        return "If that person has a Basecamp account" in body
    assert message(bob.email) is True
    assert message("test-nobody-here@example.com") is True
    assert len(friendship_rows()) == 1  # nothing created for the unknown address


def test_email_matching_ignores_case(alice, bob):
    post(alice, "/friends/request", email=bob.email.upper())
    assert len(friendship_rows()) == 1


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "x" * 300 + "@example.com"])
def test_bad_emails_are_rejected(alice, email):
    post(alice, "/friends/request", email=email)
    assert friendship_rows() == []


def test_cannot_friend_yourself(alice):
    post(alice, "/friends/request", email=alice.email)
    assert friendship_rows() == []
    assert "cannot add yourself" in page(alice, "/friends")


def test_duplicate_requests_do_not_create_duplicates(alice, bob):
    post(alice, "/friends/request", email=bob.email)
    post(alice, "/friends/request", email=bob.email)
    assert len(friendship_rows()) == 1


def test_asking_someone_who_already_asked_you_accepts_it(alice, bob):
    post(alice, "/friends/request", email=bob.email)
    post(bob, "/friends/request", email=alice.email)
    rows = friendship_rows()
    assert len(rows) == 1 and rows[0]["status"] == "accepted"


def test_requesting_an_existing_friend_says_so(alice, bob):
    make_friends(alice, bob)
    post(alice, "/friends/request", email=bob.email)
    assert "already friends" in page(alice, "/friends")
    assert len(friendship_rows()) == 1


def test_only_the_addressee_can_accept(alice, bob, carol):
    post(alice, "/friends/request", email=bob.email)
    fid = friendship_rows()[0]["id"]
    assert post(alice, f"/friends/{fid}/accept").status_code == 404   # the requester cannot
    assert post(carol, f"/friends/{fid}/accept").status_code == 404   # a stranger cannot
    assert friendship_rows()[0]["status"] == "pending"


def test_decline_cancel_and_unfriend(alice, bob, carol):
    post(alice, "/friends/request", email=bob.email)
    fid = friendship_rows()[0]["id"]
    assert post(carol, f"/friends/{fid}/remove").status_code == 404   # strangers cannot touch it
    assert post(bob, f"/friends/{fid}/remove").status_code == 302     # decline
    assert friendship_rows() == []
    post(alice, "/friends/request", email=bob.email)
    fid = friendship_rows()[0]["id"]
    assert post(alice, f"/friends/{fid}/remove").status_code == 302   # cancel own request
    assert friendship_rows() == []
    fid = make_friends(alice, bob)
    assert post(bob, f"/friends/{fid}/remove").status_code == 302     # unfriend
    assert friendship_rows() == []


def test_names_are_escaped_on_the_friends_page():
    a, b = new_client(), new_client()
    a.email = signup(a, name="<script>alert(1)</script>")
    b.email = signup(b, name="Bob")
    post(a, "/friends/request", email=b.email)
    body = page(b, "/friends")
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


# ------------------------------------------------------------------ gear privacy setting
def test_gear_sharing_is_off_by_default_and_can_be_toggled(alice):
    assert 'id="share_gear"' in page(alice, "/friends")
    with basecamp_app.get_db_connection() as conn:
        assert conn.execute("SELECT share_gear FROM users WHERE id = %s", (user_id(alice),)).fetchone()["share_gear"] is False
    share_gear(alice, True)
    with basecamp_app.get_db_connection() as conn:
        assert conn.execute("SELECT share_gear FROM users WHERE id = %s", (user_id(alice),)).fetchone()["share_gear"] is True
    share_gear(alice, False)
    with basecamp_app.get_db_connection() as conn:
        assert conn.execute("SELECT share_gear FROM users WHERE id = %s", (user_id(alice),)).fetchone()["share_gear"] is False


# ------------------------------------------------------------------ trip invitations
def test_owner_can_invite_a_friend_and_friend_can_join(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    assert invite(alice, tid, bob).status_code == 302
    assert participant_status(tid, bob) == "invited"
    assert "Bob Partner" in page(alice, f"/trips/{tid}") and "Invited" in page(alice, f"/trips/{tid}")
    inbox = page(bob, "/trips")
    assert "Trip invitations" in inbox and "Alice Owner" in inbox and "Mount Elbert" in inbox
    assert post(bob, f"/trips/{tid}/invite/accept").status_code == 302
    assert participant_status(tid, bob) == "joined"
    shared = page(bob, "/trips")
    assert 'id="shared-trips"' in shared and "Alice Owner" in shared
    assert "Trip invitations" not in shared


def test_only_friends_can_be_invited(alice, bob):
    tid = make_trip(alice)
    assert invite(alice, tid, bob).status_code == 302
    assert participant_status(tid, bob) is None
    post(alice, "/friends/request", email=bob.email)  # pending is not enough
    invite(alice, tid, bob)
    assert participant_status(tid, bob) is None


@pytest.mark.parametrize("bad", ["", "abc", "-1", "99999999"])
def test_invite_with_garbage_ids_is_rejected(alice, bad):
    tid = make_trip(alice)
    assert post(alice, f"/trips/{tid}/invite", friend_id=bad).status_code == 302


def test_inviting_twice_does_not_duplicate_or_reset(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    invite(alice, tid, bob)  # already joined: must stay joined
    assert participant_status(tid, bob) == "joined"


def test_only_the_owner_can_invite_or_remove(alice, bob, carol):
    make_friends(alice, bob)
    make_friends(bob, carol)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    assert post(bob, f"/trips/{tid}/invite", friend_id=str(user_id(carol))).status_code == 404
    assert post(carol, f"/trips/{tid}/invite", friend_id=str(user_id(carol))).status_code == 404
    assert post(bob, f"/trips/{tid}/partners/{user_id(bob)}/remove").status_code == 404
    assert participant_status(tid, carol) is None and participant_status(tid, bob) == "joined"


def test_decline_and_reinvite(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    invite(alice, tid, bob)
    assert post(bob, f"/trips/{tid}/invite/decline").status_code == 302
    assert participant_status(tid, bob) is None
    invite(alice, tid, bob)
    assert participant_status(tid, bob) == "invited"


def test_only_the_invited_person_can_answer(alice, bob, carol):
    make_friends(alice, bob)
    tid = make_trip(alice)
    invite(alice, tid, bob)
    assert post(carol, f"/trips/{tid}/invite/accept").status_code == 404
    assert post(alice, f"/trips/{tid}/invite/accept").status_code == 404
    assert participant_status(tid, bob) == "invited"


def test_owner_removes_a_partner_and_partner_can_leave(alice, bob, carol):
    make_friends(alice, bob)
    make_friends(alice, carol)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    join_trip(alice, tid, carol)
    post(alice, f"/trips/{tid}/partners/{user_id(bob)}/remove")
    assert participant_status(tid, bob) is None
    assert bob.get(f"/trips/{tid}").status_code == 404
    assert post(carol, f"/trips/{tid}/leave").status_code == 302
    assert participant_status(tid, carol) is None
    assert post(carol, f"/trips/{tid}/leave").status_code == 404


# ------------------------------------------------------------------ what a partner can see and do
def test_partner_sees_a_read_only_trip_page(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    body = page(bob, f"/trips/{tid}")
    assert "Mount Elbert" in body and "Group trip" in body and "Organized by Alice Owner" in body
    assert 'id="edit-trip"' not in body and "/delete" not in body and "/status" not in body
    assert "Leave trip" in body
    for url, data in [(f"/trips/{tid}/edit", {"trip_date": "2030-01-01", "notes": "x"}),
                      (f"/trips/{tid}/status", {"status": "completed"}),
                      (f"/trips/{tid}/reports", {"conditions_text": "x"}),
                      (f"/trips/{tid}/delete", {}),
                      (f"/trips/{tid}/invite", {"friend_id": "1"})]:
        assert post(bob, url, **data).status_code == 404, url
    assert bob.get(f"/trips/{tid}/checklist").status_code == 404
    assert bob.get(f"/trips/{tid}/calendar.ics").status_code == 404


def test_invited_but_not_joined_and_strangers_cannot_open_the_trip(alice, bob, carol):
    make_friends(alice, bob)
    tid = make_trip(alice)
    invite(alice, tid, bob)
    assert bob.get(f"/trips/{tid}").status_code == 404
    assert carol.get(f"/trips/{tid}").status_code == 404
    assert alice.get("/trips/99999999").status_code == 404


# ------------------------------------------------------------------ partner gear
def test_gear_is_private_unless_shared(alice, bob):
    make_friends(alice, bob)
    add_gear(alice, "Alice Secret Tent", "Shelter", "48", "oz")
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    resp = bob.get(f"/trips/{tid}/partners/{user_id(alice)}/gear")
    assert resp.status_code == 403
    body = resp.get_data(as_text=True)
    assert "has not shared" in body and "Alice Secret Tent" not in body
    assert "Gear not shared" in page(bob, f"/trips/{tid}")


def test_partner_can_view_shared_gear_and_revoking_hides_it(alice, bob):
    make_friends(alice, bob)
    add_gear(alice, "Alice Tent", "Shelter", "48", "oz")
    add_gear(alice, "Alice Quilt", "Sleep", "16", "oz")
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    share_gear(alice, True)
    assert f"/trips/{tid}/partners/{user_id(alice)}/gear" in page(bob, f"/trips/{tid}")
    body = page(bob, f"/trips/{tid}/partners/{user_id(alice)}/gear")
    assert "Alice Tent" in body and "Alice Quilt" in body and "4 lb 0 oz" in body and "2 items" in body
    assert "/edit" not in body and "/delete" not in body  # read-only
    share_gear(alice, False)
    assert bob.get(f"/trips/{tid}/partners/{user_id(alice)}/gear").status_code == 403


def test_owner_can_view_a_joined_partners_shared_gear(alice, bob):
    make_friends(alice, bob)
    add_gear(bob, "Bob Stove", "Cooking", "3", "oz")
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    assert alice.get(f"/trips/{tid}/partners/{user_id(bob)}/gear").status_code == 403
    share_gear(bob, True)
    assert "Bob Stove" in page(alice, f"/trips/{tid}/partners/{user_id(bob)}/gear")
    assert f"/trips/{tid}/partners/{user_id(bob)}/gear" in page(alice, f"/trips/{tid}")


def test_partners_can_see_each_others_gear(alice, bob, carol):
    make_friends(alice, bob)
    make_friends(alice, carol)
    add_gear(carol, "Carol Rope", "Climbing", "2", "lb")
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    join_trip(alice, tid, carol)
    share_gear(carol, True)
    assert "Carol Rope" in page(bob, f"/trips/{tid}/partners/{user_id(carol)}/gear")
    assert "Carol Third" in page(bob, f"/trips/{tid}")


def test_gear_view_is_limited_to_people_on_the_trip(alice, bob, carol):
    make_friends(alice, bob)
    add_gear(alice, "Alice Tent", "Shelter", "48", "oz")
    share_gear(alice, True)
    tid = make_trip(alice)
    invite(alice, tid, bob)  # invited, not joined
    url = f"/trips/{tid}/partners/{user_id(alice)}/gear"
    assert bob.get(url).status_code == 404            # invited only
    assert carol.get(url).status_code == 404          # stranger
    assert alice.get(url).status_code == 404          # your own gear has its own page
    join_trip(alice, tid, bob)
    assert bob.get(url).status_code == 200
    assert carol.get(f"/trips/{tid}/partners/{user_id(carol)}/gear").status_code == 404
    assert bob.get(f"/trips/{tid}/partners/{user_id(carol)}/gear").status_code == 404  # carol not on trip
    assert bob.get(f"/trips/{tid}/partners/99999999/gear").status_code == 404


def test_unfriending_ends_shared_trips_and_gear_access(alice, bob):
    fid = make_friends(alice, bob)
    add_gear(alice, "Alice Tent", "Shelter", "48", "oz")
    share_gear(alice, True)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    assert bob.get(f"/trips/{tid}").status_code == 200
    post(bob, f"/friends/{fid}/remove")
    assert participant_status(tid, bob) is None
    assert bob.get(f"/trips/{tid}").status_code == 404
    assert bob.get(f"/trips/{tid}/partners/{user_id(alice)}/gear").status_code == 404


def test_deleting_a_trip_removes_its_partners(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    post(alice, f"/trips/{tid}/delete")
    assert participant_status(tid, bob) is None
    assert bob.get(f"/trips/{tid}").status_code == 404


def test_shared_trip_appears_for_partner_but_owner_trips_stay_private(alice, bob, carol):
    make_friends(alice, bob)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    assert "Alice Owner" in page(bob, "/trips")
    assert "Mount Elbert" not in page(carol, "/trips")
    assert bob.get(f"/trips/{tid}").status_code == 200


def test_partner_names_do_not_leak_onto_objective_reports(alice, bob):
    make_friends(alice, bob)
    tid = make_trip(alice)
    join_trip(alice, tid, bob)
    post(alice, f"/trips/{tid}/status", status="completed")
    post(alice, f"/trips/{tid}/reports", conditions_text="Windy ridge")
    with basecamp_app.get_db_connection() as conn:
        oid = conn.execute("SELECT id FROM objectives WHERE name = 'Mount Elbert'").fetchone()["id"]
    body = page(bob, f"/objectives/{oid}")
    assert "Windy ridge" in body and "Alice Owner" not in body
