import datetime
import functools
import hmac
import json
import os
import re
import secrets
from urllib.parse import urlencode

import psycopg
from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
    url_for,
)
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PostgreSQL connection string. Override with DATABASE_URL in the environment.
# Example: postgresql://basecamp:basecamp@localhost:5432/basecamp
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://basecamp:basecamp@localhost:5432/basecamp",
)

# Arbitrary constant used as a PostgreSQL advisory-lock key so that several
# processes starting at once (gunicorn workers, tests) set up the schema one at a time.
INIT_LOCK_ID = 727274

SEED_FILE = os.path.join(BASE_DIR, "data", "seed_objectives.json")
PER_PAGE = 12


def load_seed_objectives():
    """Read the curated objectives from data/seed_objectives.json."""
    with open(SEED_FILE, encoding="utf-8") as f:
        return json.load(f)


SEED_OBJECTIVES = load_seed_objectives()

# Pages from the project spec. Pages that are not built yet render a "coming soon"
# page (below) so navigation never hits a 404. When you build a real page, add its
# route and DELETE its "checkpoint" key here so the placeholder is not registered.
PAGES = [
    {"path": "/objectives", "label": "Objectives"},
    {"path": "/gear", "label": "Gear Library"},
    {"path": "/loadouts", "label": "Loadout Builder"},
    {"path": "/trips/plan", "label": "Plan a Trip"},
    {"path": "/trips", "label": "My Trips"},
    {"path": "/friends", "label": "Friends"},
    {"path": "/summit-log", "label": "Summit Log", "checkpoint": 7},
]

GEAR_CATEGORIES = [
    "Shelter", "Sleep", "Packs", "Clothing", "Footwear", "Cooking", "Water",
    "Navigation", "Safety", "Climbing", "Electronics", "Other",
]
TRIP_TYPES = [
    "Day hike", "Overnight", "Weekend backpacking", "Multi-day trek",
    "Peak climb", "Winter / snow", "Other",
]
MAX_QTY = 99
MAX_NOTES = 2000
TRIP_STATUSES = ['planned', 'attempted', 'completed']
TRIP_SORTS = {'newest': 'DESC', 'oldest': 'ASC'}
# Status workflow: which statuses a trip may move to from each status.
TRIP_TRANSITIONS = {
    'planned': ['attempted', 'completed'],
    'attempted': ['completed', 'planned'],
    'completed': ['attempted'],
}
MAX_REPORT = 2000
EARLIEST_TRIP_DATE = datetime.date(2000, 1, 1)
WEIGHT_UNITS = {"oz": 1.0, "lb": 16.0, "g": 0.03527396195, "kg": 35.27396195}
MAX_WEIGHT_OZ = 10000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load_secret_key():
    """Key that signs login cookies. Use SECRET_KEY if set, else create one on first run
    and keep it in instance/secret_key (not committed, not overwritten by deploys)."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE_DIR, "instance", "secret_key")
    for _ in range(2):
        try:
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue  # another process created it first; read theirs
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_hex(32))
    raise RuntimeError("could not create instance/secret_key")


app = Flask(__name__)
app.config["DATABASE_URL"] = DATABASE_URL
app.config["SECRET_KEY"] = _load_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def get_db_connection():
    """Open a new PostgreSQL connection (no Flask request context needed)."""
    return psycopg.connect(app.config["DATABASE_URL"], row_factory=dict_row)


def get_db():
    if "db" not in g:
        g.db = get_db_connection()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create/upgrade the tables and load the objectives from data/seed_objectives.json.

    Safe to call every time the app starts or start.sh runs, and safe when several
    processes call it at the same time: an advisory lock makes them take turns, so
    the schema is created and the sample data inserted exactly once.
    """
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        schema_sql = f.read()
    with psycopg.connect(app.config["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            # Held until this transaction commits.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (INIT_LOCK_ID,))
            cur.execute(schema_sql)
            # Upsert by name: new peaks are added, existing ones are refreshed, and
            # anything a user created elsewhere (trips, reports) is left alone.
            cur.executemany(
                "INSERT INTO objectives "
                "(name, region, difficulty, elevation_ft, best_season, season_notes, lat, lng) "
                "VALUES (%(name)s, %(region)s, %(difficulty)s, %(elevation_ft)s, "
                "%(best_season)s, %(season_notes)s, %(lat)s, %(lng)s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "region = EXCLUDED.region, difficulty = EXCLUDED.difficulty, "
                "elevation_ft = EXCLUDED.elevation_ft, best_season = EXCLUDED.best_season, "
                "season_notes = EXCLUDED.season_notes, lat = EXCLUDED.lat, lng = EXCLUDED.lng",
                SEED_OBJECTIVES,
            )
        conn.commit()


@app.context_processor
def inject_pages():
    """Make the page list available to every template (navbar + landing page)."""
    return {"pages": PAGES}


def page_url(n):
    """URL of /objectives page n that keeps the current search and filters."""
    args = {k: v for k, v in request.args.items() if k in ("q", "region", "difficulty", "season") and v}
    args["page"] = n
    return "/objectives?" + urlencode(args)


app.jinja_env.globals["page_url"] = page_url


@app.route("/")
def index():
    with get_db().cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM objectives")
        count = cur.fetchone()["n"]
    return render_template("index.html", objective_count=count)


def _filter_options(cur):
    cur.execute("SELECT DISTINCT region FROM objectives ORDER BY region")
    regions = [r["region"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT difficulty FROM objectives ORDER BY difficulty")
    difficulties = [r["difficulty"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT best_season FROM objectives WHERE best_season IS NOT NULL ORDER BY best_season")
    seasons = [r["best_season"] for r in cur.fetchall()]
    return regions, difficulties, seasons


@app.route("/objectives")
def objectives():
    """Browse, search and filter objectives. All values reach SQL as parameters."""
    q = request.args.get("q", "").strip()
    region = request.args.get("region", "").strip()
    difficulty = request.args.get("difficulty", "").strip()
    season = request.args.get("season", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    where, params = [], []
    if q:
        where.append("(name ILIKE %s OR region ILIKE %s)")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [like, like]
    if region:
        where.append("region = %s")
        params.append(region)
    if difficulty:
        where.append("difficulty = %s")
        params.append(difficulty)
    if season:
        where.append("best_season = %s")
        params.append(season)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with get_db().cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM objectives" + clause, params)
        total = cur.fetchone()["n"]
        total_pages = max(1, -(-total // PER_PAGE))
        page = min(page, total_pages)
        cur.execute(
            "SELECT id, name, region, difficulty, elevation_ft, best_season FROM objectives"
            + clause + " ORDER BY elevation_ft DESC NULLS LAST, name LIMIT %s OFFSET %s",
            params + [PER_PAGE, (page - 1) * PER_PAGE],
        )
        rows = cur.fetchall()
        regions, difficulties, seasons = _filter_options(cur)

    filters = {"q": q, "region": region, "difficulty": difficulty, "season": season}
    return render_template(
        "objectives.html", objectives=rows, total=total, page=page, total_pages=total_pages,
        filters=filters, regions=regions, difficulties=difficulties, seasons=seasons,
        active_filters={k: v for k, v in filters.items() if v},
    )


@app.route("/objectives/<int:objective_id>")
def objective_detail(objective_id):
    with get_db().cursor() as cur:
        cur.execute("SELECT * FROM objectives WHERE id = %s", (objective_id,))
        objective = cur.fetchone()
        if objective is None:
            abort(404)
        cur.execute(
            "SELECT r.conditions_text, r.created_at, t.trip_date, t.status "
            "FROM trip_reports r JOIN trips t ON t.id = r.trip_id "
            "WHERE t.objective_id = %s ORDER BY r.created_at DESC LIMIT 10",
            (objective_id,),
        )
        reports = cur.fetchall()
        my_trips = []
        if g.user:
            cur.execute(
                "SELECT id, trip_date, status FROM trips WHERE user_id = %s AND objective_id = %s "
                "ORDER BY trip_date DESC NULLS LAST, id DESC",
                (g.user["id"], objective_id),
            )
            my_trips = cur.fetchall()
    return render_template("objective_detail.html", o=objective, reports=reports, my_trips=my_trips)


@app.errorhandler(404)
def not_found(_exc):
    return render_template("404.html"), 404


@app.errorhandler(400)
def bad_request(exc):
    message = getattr(exc, "description", None) or "That request could not be processed."
    return render_template("error.html", title="Something went wrong", message=message), 400


@app.route("/api/health")
def health():
    return jsonify(status="ok")

# ---------------------------------------------------------------------------
# Accounts: signup, login, logout
# ---------------------------------------------------------------------------
def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_user_and_check_csrf():
    """Load the logged-in user, and reject POSTs that lack the form's CSRF token."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        with get_db().cursor() as cur:
            cur.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,))
            g.user = cur.fetchone()
        if g.user is None:
            session.pop("user_id", None)
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, description="Your form expired. Go back, refresh the page, and try again.")


@app.context_processor
def inject_user():
    return {"current_user": g.get("user")}


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    """Only allow redirects to a path on this site (blocks //evil.com and http://evil.com)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("gear")


def start_session(user_id):
    csrf = secrets.token_hex(16)
    session.clear()  # drop anything left over from before logging in
    session["user_id"] = user_id
    session["csrf_token"] = csrf


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("gear"))
    form = {"name": "", "email": ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        errors = []
        if not form["name"] or len(form["name"]) > 80:
            errors.append("Enter your name (up to 80 characters).")
        if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 200:
            errors.append("Enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        elif len(password) > 200:
            errors.append("Password is too long (200 characters max).")
        if password != confirm:
            errors.append("The two passwords do not match.")
        if not errors:
            db = get_db()
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                        (form["name"], form["email"], generate_password_hash(password)),
                    )
                    user_id = cur.fetchone()["id"]
                db.commit()
            except pg_errors.UniqueViolation:
                db.rollback()
                errors.append("An account with that email already exists. Try logging in.")
            else:
                start_session(user_id)
                flash("Welcome to Basecamp! Your account is ready.", "success")
                return redirect(url_for("gear"))
        for e in errors:
            flash(e, "danger")
        return render_template("signup.html", form=form), 400
    return render_template("signup.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("gear"))
    email = ""
    next_url = request.values.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db().cursor() as cur:
            cur.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
        if user and check_password_hash(user["password_hash"], password):
            start_session(user["id"])
            flash("Logged in.", "success")
            return redirect(safe_next(next_url))
        flash("Incorrect email or password.", "danger")  # same message for both cases
        return render_template("login.html", email=email, next_url=next_url), 401
    return render_template("login.html", email=email, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Gear library: each user's own gear items (create, read, update, delete)
# ---------------------------------------------------------------------------
def format_weight(oz):
    pounds, ounces = divmod(round(oz * 10) / 10, 16)
    if pounds:
        return f"{int(pounds)} lb {ounces:g} oz"
    return f"{ounces:g} oz"


app.jinja_env.filters["weight"] = format_weight


def parse_gear_form(form):
    """Return (clean values, list of error messages) for the add/edit gear form."""
    name = form.get("name", "").strip()
    category = form.get("category", "")
    unit = form.get("unit", "oz")
    errors = []
    if not name or len(name) > 100:
        errors.append("Enter an item name (up to 100 characters).")
    if category not in GEAR_CATEGORIES:
        errors.append("Choose a category from the list.")
    weight_oz = None
    if unit not in WEIGHT_UNITS:
        errors.append("Choose a weight unit.")
    else:
        try:
            amount = float(form.get("weight", "").strip())
            if amount != amount or amount in (float("inf"), float("-inf")):
                raise ValueError
            weight_oz = round(amount * WEIGHT_UNITS[unit], 2)
            if not 0 < weight_oz <= MAX_WEIGHT_OZ:
                errors.append("Weight must be more than zero and under 625 lb.")
        except ValueError:
            errors.append("Enter the weight as a number, like 3.5.")
    return {"name": name, "category": category, "weight_oz": weight_oz, "unit": unit,
            "weight": form.get("weight", "")}, errors


def get_own_gear_or_404(item_id):
    with get_db().cursor() as cur:
        cur.execute("SELECT * FROM gear_items WHERE id = %s AND user_id = %s", (item_id, g.user["id"]))
        item = cur.fetchone()
    if item is None:
        abort(404)
    return item


def gear_page_context(category=""):
    """Everything the gear page template needs for the logged-in user."""
    where, params = "WHERE user_id = %s", [g.user["id"]]
    if category in GEAR_CATEGORIES:
        where += " AND category = %s"
        params.append(category)
    else:
        category = ""
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT id, name, category, weight_oz FROM gear_items " + where + " ORDER BY category, name",
            params,
        )
        items = cur.fetchall()
        cur.execute(
            "SELECT category, SUM(weight_oz) AS oz, COUNT(*) AS n FROM gear_items "
            "WHERE user_id = %s GROUP BY category ORDER BY category",
            (g.user["id"],),
        )
        totals = cur.fetchall()
    return {
        "items": items, "categories": GEAR_CATEGORIES, "selected": category, "totals": totals,
        "total_oz": sum(t["oz"] for t in totals), "total_count": sum(t["n"] for t in totals),
        "form": {"name": "", "category": "", "weight": "", "unit": "oz"},
    }


@app.route("/gear")
@login_required
def gear():
    return render_template("gear.html", **gear_page_context(request.args.get("category", "").strip()))


@app.route("/gear/new", methods=["POST"])
@login_required
def gear_create():
    values, errors = parse_gear_form(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        context = gear_page_context()
        context["form"] = values
        return render_template("gear.html", **context), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO gear_items (user_id, name, category, weight_oz) VALUES (%s, %s, %s, %s)",
            (g.user["id"], values["name"], values["category"], values["weight_oz"]),
        )
    db.commit()
    flash(f"Added {values['name']}.", "success")
    return redirect(url_for("gear"))


@app.route("/gear/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def gear_edit(item_id):
    item = get_own_gear_or_404(item_id)
    if request.method == "POST":
        values, errors = parse_gear_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("gear_edit.html", item=item, categories=GEAR_CATEGORIES, form=values), 400
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "UPDATE gear_items SET name = %s, category = %s, weight_oz = %s "
                "WHERE id = %s AND user_id = %s",
                (values["name"], values["category"], values["weight_oz"], item_id, g.user["id"]),
            )
        db.commit()
        flash(f"Saved changes to {values['name']}.", "success")
        return redirect(url_for("gear"))
    form = {"name": item["name"], "category": item["category"],
            "weight": f"{item['weight_oz']:g}", "unit": "oz"}
    return render_template("gear_edit.html", item=item, categories=GEAR_CATEGORIES, form=form)


@app.route("/gear/<int:item_id>/delete", methods=["POST"])
@login_required
def gear_delete(item_id):
    item = get_own_gear_or_404(item_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM gear_items WHERE id = %s AND user_id = %s", (item_id, g.user["id"]))
    db.commit()
    flash(f"Deleted {item['name']}.", "info")
    return redirect(url_for("gear"))


# ---------------------------------------------------------------------------
# Loadout builder: named, reusable packing lists made from your gear library
# ---------------------------------------------------------------------------
def parse_loadout_form(form):
    name = form.get("name", "").strip()
    trip_type = form.get("trip_type", "")
    errors = []
    if not name or len(name) > 100:
        errors.append("Enter a loadout name (up to 100 characters).")
    if trip_type not in TRIP_TYPES:
        errors.append("Choose a trip type from the list.")
    return {"name": name, "trip_type": trip_type}, errors


def parse_qty(raw):
    """Quantity must be a whole number from 1 to MAX_QTY, else None."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    qty = int(raw)
    return qty if 1 <= qty <= MAX_QTY else None


def get_own_loadout_or_404(loadout_id):
    with get_db().cursor() as cur:
        cur.execute("SELECT * FROM loadouts WHERE id = %s AND user_id = %s", (loadout_id, g.user["id"]))
        loadout = cur.fetchone()
    if loadout is None:
        abort(404)
    return loadout


def loadouts_list_context():
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT l.id, l.name, l.trip_type, "
            "COALESCE(SUM(li.qty), 0) AS item_count, "
            "COALESCE(SUM(gi.weight_oz * li.qty), 0) AS total_oz "
            "FROM loadouts l "
            "LEFT JOIN loadout_items li ON li.loadout_id = l.id "
            "LEFT JOIN gear_items gi ON gi.id = li.gear_item_id "
            "WHERE l.user_id = %s GROUP BY l.id ORDER BY l.name, l.id",
            (g.user["id"],),
        )
        loadouts = cur.fetchall()
    return {"loadouts": loadouts, "trip_types": TRIP_TYPES, "form": {"name": "", "trip_type": ""}}


@app.route("/loadouts")
@login_required
def loadouts():
    return render_template("loadouts.html", **loadouts_list_context())


@app.route("/loadouts/new", methods=["POST"])
@login_required
def loadout_create():
    values, errors = parse_loadout_form(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        context = loadouts_list_context()
        context["form"] = values
        return render_template("loadouts.html", **context), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO loadouts (user_id, name, trip_type) VALUES (%s, %s, %s) RETURNING id",
            (g.user["id"], values["name"], values["trip_type"]),
        )
        new_id = cur.fetchone()["id"]
    db.commit()
    flash(f"Created {values['name']}. Now add some gear.", "success")
    return redirect(url_for("loadout_detail", loadout_id=new_id))


def loadout_detail_context(loadout):
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT li.gear_item_id, li.qty, gi.name, gi.category, gi.weight_oz, "
            "gi.weight_oz * li.qty AS line_oz "
            "FROM loadout_items li JOIN gear_items gi ON gi.id = li.gear_item_id "
            "WHERE li.loadout_id = %s ORDER BY gi.category, gi.name",
            (loadout["id"],),
        )
        items = cur.fetchall()
        cur.execute(
            "SELECT gi.category, SUM(gi.weight_oz * li.qty) AS oz, SUM(li.qty) AS n "
            "FROM loadout_items li JOIN gear_items gi ON gi.id = li.gear_item_id "
            "WHERE li.loadout_id = %s GROUP BY gi.category ORDER BY gi.category",
            (loadout["id"],),
        )
        totals = cur.fetchall()
        cur.execute(
            "SELECT id, name, category, weight_oz FROM gear_items WHERE user_id = %s "
            "ORDER BY category, name",
            (g.user["id"],),
        )
        gear_choices = cur.fetchall()
    return {
        "loadout": loadout, "items": items, "totals": totals, "gear_choices": gear_choices,
        "total_oz": sum(t["oz"] for t in totals), "total_count": sum(t["n"] for t in totals),
        "trip_types": TRIP_TYPES, "max_qty": MAX_QTY,
        "form": {"name": loadout["name"], "trip_type": loadout["trip_type"]},
    }


@app.route("/loadouts/<int:loadout_id>")
@login_required
def loadout_detail(loadout_id):
    loadout = get_own_loadout_or_404(loadout_id)
    return render_template("loadout_detail.html", **loadout_detail_context(loadout))


@app.route("/loadouts/<int:loadout_id>/edit", methods=["POST"])
@login_required
def loadout_edit(loadout_id):
    loadout = get_own_loadout_or_404(loadout_id)
    values, errors = parse_loadout_form(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        context = loadout_detail_context(loadout)
        context["form"] = values
        return render_template("loadout_detail.html", **context), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE loadouts SET name = %s, trip_type = %s WHERE id = %s AND user_id = %s",
            (values["name"], values["trip_type"], loadout_id, g.user["id"]),
        )
    db.commit()
    flash("Loadout updated.", "success")
    return redirect(url_for("loadout_detail", loadout_id=loadout_id))


@app.route("/loadouts/<int:loadout_id>/delete", methods=["POST"])
@login_required
def loadout_delete(loadout_id):
    loadout = get_own_loadout_or_404(loadout_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM loadouts WHERE id = %s AND user_id = %s", (loadout_id, g.user["id"]))
    db.commit()
    flash(f"Deleted {loadout['name']}.", "info")
    return redirect(url_for("loadouts"))


@app.route("/loadouts/<int:loadout_id>/copy", methods=["POST"])
@login_required
def loadout_copy(loadout_id):
    loadout = get_own_loadout_or_404(loadout_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO loadouts (user_id, name, trip_type) VALUES (%s, %s, %s) RETURNING id",
            (g.user["id"], (loadout["name"] + " (copy)")[:100], loadout["trip_type"]),
        )
        new_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO loadout_items (loadout_id, gear_item_id, qty) "
            "SELECT %s, gear_item_id, qty FROM loadout_items WHERE loadout_id = %s",
            (new_id, loadout_id),
        )
    db.commit()
    flash("Copied. Edit the copy without changing the original.", "success")
    return redirect(url_for("loadout_detail", loadout_id=new_id))


@app.route("/loadouts/<int:loadout_id>/items", methods=["POST"])
@login_required
def loadout_add_item(loadout_id):
    get_own_loadout_or_404(loadout_id)
    qty = parse_qty(request.form.get("qty", "1"))
    gear_id = request.form.get("gear_item_id", "")
    if qty is None:
        flash(f"Quantity must be a whole number from 1 to {MAX_QTY}.", "danger")
        return redirect(url_for("loadout_detail", loadout_id=loadout_id))
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT id, name FROM gear_items WHERE id = %s AND user_id = %s",
            (gear_id if gear_id.isdigit() else -1, g.user["id"]),
        )
        item = cur.fetchone()
    if item is None:
        flash("Choose one of your gear items to add.", "danger")
        return redirect(url_for("loadout_detail", loadout_id=loadout_id))
    db = get_db()
    with db.cursor() as cur:
        # Adding gear that is already packed increases its quantity instead of failing.
        cur.execute(
            "INSERT INTO loadout_items (loadout_id, gear_item_id, qty) VALUES (%s, %s, %s) "
            "ON CONFLICT (loadout_id, gear_item_id) DO UPDATE "
            "SET qty = LEAST(loadout_items.qty + EXCLUDED.qty, %s)",
            (loadout_id, item["id"], qty, MAX_QTY),
        )
    db.commit()
    flash(f"Added {item['name']}.", "success")
    return redirect(url_for("loadout_detail", loadout_id=loadout_id))


@app.route("/loadouts/<int:loadout_id>/items/<int:gear_id>/qty", methods=["POST"])
@login_required
def loadout_set_qty(loadout_id, gear_id):
    get_own_loadout_or_404(loadout_id)
    qty = parse_qty(request.form.get("qty", ""))
    if qty is None:
        flash(f"Quantity must be a whole number from 1 to {MAX_QTY}.", "danger")
        return redirect(url_for("loadout_detail", loadout_id=loadout_id))
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE loadout_items SET qty = %s WHERE loadout_id = %s AND gear_item_id = %s",
            (qty, loadout_id, gear_id),
        )
    db.commit()
    return redirect(url_for("loadout_detail", loadout_id=loadout_id))


@app.route("/loadouts/<int:loadout_id>/items/<int:gear_id>/remove", methods=["POST"])
@login_required
def loadout_remove_item(loadout_id, gear_id):
    get_own_loadout_or_404(loadout_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM loadout_items WHERE loadout_id = %s AND gear_item_id = %s",
            (loadout_id, gear_id),
        )
    db.commit()
    flash("Removed from loadout.", "info")
    return redirect(url_for("loadout_detail", loadout_id=loadout_id))


# ---------------------------------------------------------------------------
# Trips: plan a trip (objective + loadout + date + notes) and list your trips
# ---------------------------------------------------------------------------
def pretty_date(value):
    """'2026-07-12' -> 'Sun, Jul 12, 2026'. Anything unparseable is shown as stored."""
    try:
        d = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "No date"
    return d.strftime("%a, %b ") + str(d.day) + d.strftime(", %Y")


app.jinja_env.filters["pretty_date"] = pretty_date


def parse_trip_form(form):
    """Validate the plan-a-trip form. Returns (clean values, errors).

    Values keep the raw strings for re-showing the form; the ids are checked against
    the database by the caller."""
    errors = []
    objective_raw = form.get("objective_id", "").strip()
    loadout_raw = form.get("loadout_id", "").strip()
    date_raw = form.get("trip_date", "").strip()
    notes = form.get("notes", "").strip()

    objective_id = int(objective_raw) if objective_raw.isdigit() and len(objective_raw) < 10 else None
    if objective_id is None:
        errors.append("Choose an objective.")

    loadout_id = None
    if loadout_raw:
        if loadout_raw.isdigit() and len(loadout_raw) < 10:
            loadout_id = int(loadout_raw)
        else:
            errors.append("Choose one of your loadouts, or leave it blank.")

    trip_date = None
    if not date_raw:
        errors.append("Pick a trip date.")
    else:
        try:
            trip_date = datetime.date.fromisoformat(date_raw)
        except ValueError:
            errors.append("Enter the date as year-month-day, like 2026-07-12.")
        else:
            latest = datetime.date.today() + datetime.timedelta(days=365 * 5)
            if not EARLIEST_TRIP_DATE <= trip_date <= latest:
                errors.append("Pick a date between 2000 and five years from now.")

    if len(notes) > MAX_NOTES:
        errors.append(f"Notes can be up to {MAX_NOTES} characters.")

    values = {"objective_id": objective_id, "loadout_id": loadout_id,
              "trip_date": date_raw, "notes": notes}
    return values, errors, trip_date


def trip_form_context(values):
    with get_db().cursor() as cur:
        cur.execute("SELECT id, name, region, elevation_ft FROM objectives ORDER BY region, name")
        objectives = cur.fetchall()
        cur.execute("SELECT id, name, trip_type FROM loadouts WHERE user_id = %s ORDER BY name, id",
                    (g.user["id"],))
        loadouts = cur.fetchall()
    return {"objectives": objectives, "loadouts": loadouts, "form": values,
            "today": datetime.date.today().isoformat(), "max_notes": MAX_NOTES}


@app.route("/trips/plan", methods=["GET", "POST"])
@login_required
def trip_plan():
    if request.method == "POST":
        values, errors, trip_date = parse_trip_form(request.form)
        if not errors:
            with get_db().cursor() as cur:
                cur.execute("SELECT id, name FROM objectives WHERE id = %s", (values["objective_id"],))
                objective = cur.fetchone()
                if objective is None:
                    errors.append("That objective does not exist.")
                if values["loadout_id"] is not None:
                    cur.execute("SELECT id FROM loadouts WHERE id = %s AND user_id = %s",
                                (values["loadout_id"], g.user["id"]))
                    if cur.fetchone() is None:
                        errors.append("Choose one of your loadouts, or leave it blank.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("trip_plan.html", **trip_form_context(values)), 400
        if not request.form.get("confirm_duplicate"):
            with get_db().cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM trips WHERE user_id = %s AND objective_id = %s AND trip_date = %s",
                    (g.user["id"], values["objective_id"], trip_date.isoformat()),
                )
                duplicate = cur.fetchone()["n"] > 0
            if duplicate:
                # Ask before saving the same objective on the same day twice.
                context = trip_form_context(values)
                context.update(duplicate=True, duplicate_name=objective["name"])
                return render_template("trip_plan.html", **context)
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO trips (user_id, objective_id, loadout_id, trip_date, status, notes) "
                "VALUES (%s, %s, %s, %s, 'planned', %s)",
                (g.user["id"], values["objective_id"], values["loadout_id"],
                 trip_date.isoformat(), values["notes"] or None),
            )
        db.commit()
        flash(f"Trip to {objective['name']} planned for {pretty_date(trip_date.isoformat())}.", "success")
        return redirect(url_for("trips"))

    # GET: links such as /trips/plan?objective=5 (from an objective page) pre-fill the form.
    pre_objective = request.args.get("objective", "")
    pre_loadout = request.args.get("loadout", "")
    values = {
        "objective_id": int(pre_objective) if pre_objective.isdigit() and len(pre_objective) < 10 else None,
        "loadout_id": int(pre_loadout) if pre_loadout.isdigit() and len(pre_loadout) < 10 else None,
        "trip_date": "", "notes": "",
    }
    return render_template("trip_plan.html", **trip_form_context(values))


@app.route("/trips")
@login_required
def trips():
    status = request.args.get("status", "")
    if status not in TRIP_STATUSES:
        status = ""
    sort = request.args.get("sort", "newest")
    if sort not in TRIP_SORTS:
        sort = "newest"
    where, params = "WHERE t.user_id = %s", [g.user["id"]]
    if status:
        where += " AND t.status = %s"
        params.append(status)
    direction = TRIP_SORTS[sort]  # from a fixed dict, never from user text
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT t.id, t.trip_date, t.status, t.notes, "
            "o.id AS objective_id, o.name AS objective_name, o.region, o.elevation_ft, "
            "l.id AS loadout_id, l.name AS loadout_name, "
            "(SELECT COALESCE(SUM(gi.weight_oz * li.qty), 0) FROM loadout_items li "
            " JOIN gear_items gi ON gi.id = li.gear_item_id WHERE li.loadout_id = l.id) AS pack_oz "
            "FROM trips t JOIN objectives o ON o.id = t.objective_id "
            "LEFT JOIN loadouts l ON l.id = t.loadout_id "
            + where + " ORDER BY t.trip_date " + direction + " NULLS LAST, t.id " + direction,
            params,
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM trips WHERE user_id = %s", (g.user["id"],))
        all_count = cur.fetchone()["n"]
        cur.execute(
            "SELECT p.status AS invite_status, t.id AS trip_id, t.trip_date, t.status, "
            "o.name AS objective_name, o.region, u.name AS owner_name "
            "FROM trip_participants p JOIN trips t ON t.id = p.trip_id "
            "JOIN objectives o ON o.id = t.objective_id JOIN users u ON u.id = t.user_id "
            "WHERE p.user_id = %s ORDER BY t.trip_date, t.id",
            (g.user["id"],),
        )
        partner_rows = cur.fetchall()
    invites = [r for r in partner_rows if r["invite_status"] == "invited"]
    shared = [r for r in partner_rows if r["invite_status"] == "joined"]
    return render_template("trips.html", trips=rows, status=status, sort=sort,
                           statuses=TRIP_STATUSES, all_count=all_count,
                           invites=invites, shared=shared)


def get_own_trip_or_404(trip_id):
    """The trip with its objective and loadout names, or 404 if it is not yours."""
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT t.id, t.trip_date, t.status, t.notes, "
            "o.id AS objective_id, o.name AS objective_name, o.region, o.lat, o.lng, "
            "o.elevation_ft, o.difficulty, "
            "l.id AS loadout_id, l.name AS loadout_name "
            "FROM trips t JOIN objectives o ON o.id = t.objective_id "
            "LEFT JOIN loadouts l ON l.id = t.loadout_id "
            "WHERE t.id = %s AND t.user_id = %s",
            (trip_id, g.user["id"]),
        )
        trip = cur.fetchone()
    if trip is None:
        abort(404)
    return trip


def trip_is_in_future(trip):
    try:
        return datetime.date.fromisoformat(trip["trip_date"]) > datetime.date.today()
    except (TypeError, ValueError):
        return False


def trip_detail_context(trip, form=None):
    with get_db().cursor() as cur:
        pack_oz, pack_count = 0, 0
        if trip["loadout_id"]:
            cur.execute(
                "SELECT COALESCE(SUM(gi.weight_oz * li.qty), 0) AS oz, COALESCE(SUM(li.qty), 0) AS n "
                "FROM loadout_items li JOIN gear_items gi ON gi.id = li.gear_item_id "
                "WHERE li.loadout_id = %s",
                (trip["loadout_id"],),
            )
            row = cur.fetchone()
            pack_oz, pack_count = row["oz"], row["n"]
        cur.execute(
            "SELECT id, conditions_text, created_at FROM trip_reports "
            "WHERE trip_id = %s ORDER BY created_at DESC, id DESC",
            (trip["id"],),
        )
        reports = cur.fetchall()
        cur.execute("SELECT id, name, trip_type FROM loadouts WHERE user_id = %s ORDER BY name, id",
                    (g.user["id"],))
        loadouts = cur.fetchall()
        cur.execute(
            "SELECT u.id, u.name, u.share_gear, p.status FROM trip_participants p "
            "JOIN users u ON u.id = p.user_id WHERE p.trip_id = %s ORDER BY p.status, u.name",
            (trip["id"],),
        )
        partners = cur.fetchall()
        invitable = [f for f in friends_of(cur, g.user["id"]) if f["id"] not in {p["id"] for p in partners}]
    return {
        "partners": partners, "invitable_friends": invitable,
        "trip": trip, "pack_oz": pack_oz, "pack_count": pack_count, "reports": reports,
        "loadouts": loadouts, "next_statuses": TRIP_TRANSITIONS[trip["status"]],
        "can_edit": trip["status"] == "planned", "can_report": trip["status"] != "planned",
        "in_future": trip_is_in_future(trip), "max_notes": MAX_NOTES, "max_report": MAX_REPORT,
        "form": form or {"loadout_id": trip["loadout_id"], "trip_date": trip["trip_date"],
                         "notes": trip["notes"] or ""},
    }


@app.route("/trips/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    with get_db().cursor() as cur:
        cur.execute("SELECT user_id FROM trips WHERE id = %s", (trip_id,))
        row = cur.fetchone()
    if row is not None and row["user_id"] != g.user["id"]:
        return partner_trip_view(trip_id)  # not yours: only joined partners get in (else 404)
    trip = get_own_trip_or_404(trip_id)
    return render_template("trip_detail.html", **trip_detail_context(trip))


@app.route("/trips/<int:trip_id>/edit", methods=["POST"])
@login_required
def trip_edit(trip_id):
    trip = get_own_trip_or_404(trip_id)
    if trip["status"] != "planned":
        flash("Only planned trips can be edited. Move it back to planned first.", "warning")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    data = request.form.to_dict()
    data["objective_id"] = str(trip["objective_id"])  # the objective is fixed once a trip exists
    values, errors, trip_date = parse_trip_form(data)
    if not errors and values["loadout_id"] is not None:
        with get_db().cursor() as cur:
            cur.execute("SELECT id FROM loadouts WHERE id = %s AND user_id = %s",
                        (values["loadout_id"], g.user["id"]))
            if cur.fetchone() is None:
                errors.append("Choose one of your loadouts, or leave it blank.")
    if errors:
        for e in errors:
            flash(e, "danger")
        form = {"loadout_id": values["loadout_id"], "trip_date": values["trip_date"], "notes": values["notes"]}
        return render_template("trip_detail.html", **trip_detail_context(trip, form)), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE trips SET loadout_id = %s, trip_date = %s, notes = %s WHERE id = %s AND user_id = %s",
            (values["loadout_id"], trip_date.isoformat(), values["notes"] or None, trip_id, g.user["id"]),
        )
    db.commit()
    flash("Trip updated.", "success")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/status", methods=["POST"])
@login_required
def trip_set_status(trip_id):
    trip = get_own_trip_or_404(trip_id)
    new_status = request.form.get("status", "")
    if new_status not in TRIP_TRANSITIONS[trip["status"]]:
        flash(f"A {trip['status']} trip cannot be moved to '{new_status or 'nothing'}'.", "danger")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if new_status in ("attempted", "completed") and trip_is_in_future(trip):
        flash("This trip is dated in the future, so it cannot be attempted or completed yet. "
              "Edit the date first if it already happened.", "danger")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE trips SET status = %s WHERE id = %s AND user_id = %s",
                    (new_status, trip_id, g.user["id"]))
    db.commit()
    flash(f"Trip marked {new_status}.", "success")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/reports", methods=["POST"])
@login_required
def trip_add_report(trip_id):
    trip = get_own_trip_or_404(trip_id)
    text = request.form.get("conditions_text", "").strip()
    if trip["status"] == "planned":
        flash("Mark the trip attempted or completed before writing a conditions report.", "warning")
    elif not text:
        flash("Write a few words about the conditions.", "danger")
    elif len(text) > MAX_REPORT:
        flash(f"Reports can be up to {MAX_REPORT} characters.", "danger")
    else:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("INSERT INTO trip_reports (trip_id, conditions_text) VALUES (%s, %s)", (trip_id, text))
        db.commit()
        flash("Conditions report added.", "success")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/reports/<int:report_id>/delete", methods=["POST"])
@login_required
def trip_delete_report(trip_id, report_id):
    get_own_trip_or_404(trip_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM trip_reports WHERE id = %s AND trip_id = %s", (report_id, trip_id))
    db.commit()
    flash("Report removed.", "info")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/checklist")
@login_required
def trip_checklist(trip_id):
    """Printable packing checklist for a trip's loadout."""
    trip = get_own_trip_or_404(trip_id)
    items, total_oz = [], 0
    if trip["loadout_id"]:
        with get_db().cursor() as cur:
            cur.execute(
                "SELECT li.qty, gi.name, gi.category, gi.weight_oz, gi.weight_oz * li.qty AS line_oz "
                "FROM loadout_items li JOIN gear_items gi ON gi.id = li.gear_item_id "
                "WHERE li.loadout_id = %s ORDER BY gi.category, gi.name",
                (trip["loadout_id"],),
            )
            items = cur.fetchall()
        total_oz = sum(i["line_oz"] for i in items)
    return render_template("trip_checklist.html", trip=trip, items=items, total_oz=total_oz,
                           total_count=sum(i["qty"] for i in items))


def ics_escape(text):
    """Escape text for an iCalendar value (RFC 5545 section 3.3.11)."""
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def ics_fold(line):
    """Fold a content line to at most 75 octets per physical line, never splitting a character."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(current)
            current, size = "", 0
        current += ch
        size += n
    out.append(current)
    return "\r\n ".join(out)


def build_trip_ics(trip):
    day = datetime.date.fromisoformat(trip["trip_date"])
    description = []
    if trip["loadout_name"]:
        description.append("Loadout: " + trip["loadout_name"])
    if trip["notes"]:
        description.append(trip["notes"])
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Basecamp//Trip planner//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:basecamp-trip-{trip['id']}@basecamp",
        "DTSTAMP:" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"),
        "DTEND;VALUE=DATE:" + (day + datetime.timedelta(days=1)).strftime("%Y%m%d"),
        "SUMMARY:" + ics_escape("Basecamp: " + trip["objective_name"]),
        "LOCATION:" + ics_escape(f"{trip['objective_name']}, {trip['region']}"),
    ]
    if trip["lat"] is not None and trip["lng"] is not None:
        lines.append(f"GEO:{trip['lat']:.5f};{trip['lng']:.5f}")
    if description:
        lines.append("DESCRIPTION:" + ics_escape("\n".join(description)))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "".join(ics_fold(line) + "\r\n" for line in lines)


@app.route("/trips/<int:trip_id>/calendar.ics")
@login_required
def trip_calendar(trip_id):
    trip = get_own_trip_or_404(trip_id)
    try:
        body = build_trip_ics(trip)
    except (TypeError, ValueError):
        abort(404)  # a trip without a usable date cannot be put on a calendar
    slug = re.sub(r"[^a-z0-9]+", "-", trip["objective_name"].lower()).strip("-") or "trip"
    return Response(
        body, mimetype="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="basecamp-{slug}-{trip["trip_date"]}.ics"'},
    )


@app.route("/trips/<int:trip_id>/delete", methods=["POST"])
@login_required
def trip_delete(trip_id):
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT t.id, o.name FROM trips t JOIN objectives o ON o.id = t.objective_id "
            "WHERE t.id = %s AND t.user_id = %s",
            (trip_id, g.user["id"]),
        )
        trip = cur.fetchone()
    if trip is None:
        abort(404)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM trips WHERE id = %s AND user_id = %s", (trip_id, g.user["id"]))
    db.commit()
    flash(f"Removed your trip to {trip['name']}.", "info")
    return redirect(url_for("trips"))


# ---------------------------------------------------------------------------
# Friends, trip partners, and gear sharing
# ---------------------------------------------------------------------------
def friends_of(cur, user_id):
    """Accepted friends of a user: [{id, name, email, friendship_id}]."""
    cur.execute(
        "SELECT u.id, u.name, u.email, f.id AS friendship_id FROM friendships f "
        "JOIN users u ON u.id = CASE WHEN f.requester_id = %s THEN f.addressee_id ELSE f.requester_id END "
        "WHERE f.status = 'accepted' AND (f.requester_id = %s OR f.addressee_id = %s) ORDER BY u.name, u.id",
        (user_id, user_id, user_id),
    )
    return cur.fetchall()


@app.route("/friends")
@login_required
def friends():
    me = g.user["id"]
    with get_db().cursor() as cur:
        friend_list = friends_of(cur, me)
        cur.execute(
            "SELECT f.id AS friendship_id, u.name, u.email FROM friendships f "
            "JOIN users u ON u.id = f.requester_id WHERE f.addressee_id = %s AND f.status = 'pending' "
            "ORDER BY f.id",
            (me,),
        )
        incoming = cur.fetchall()
        cur.execute(
            "SELECT f.id AS friendship_id, u.name, u.email FROM friendships f "
            "JOIN users u ON u.id = f.addressee_id WHERE f.requester_id = %s AND f.status = 'pending' "
            "ORDER BY f.id",
            (me,),
        )
        outgoing = cur.fetchall()
        cur.execute("SELECT share_gear FROM users WHERE id = %s", (me,))
        share_gear = cur.fetchone()["share_gear"]
    return render_template("friends.html", friends=friend_list, incoming=incoming,
                           outgoing=outgoing, share_gear=share_gear)


@app.route("/friends/request", methods=["POST"])
@login_required
def friend_request():
    email = request.form.get("email", "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        flash("Enter a valid email address.", "danger")
        return redirect(url_for("friends"))
    me = g.user["id"]
    generic = "If that person has a Basecamp account, they will see your friend request."
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
        other = cur.fetchone()
        if other is None:
            flash(generic, "info")  # same message whether or not the account exists
            return redirect(url_for("friends"))
        if other["id"] == me:
            flash("You cannot add yourself.", "warning")
            return redirect(url_for("friends"))
        cur.execute(
            "SELECT id, requester_id, status FROM friendships "
            "WHERE LEAST(requester_id, addressee_id) = LEAST(%s, %s) "
            "AND GREATEST(requester_id, addressee_id) = GREATEST(%s, %s)",
            (me, other["id"], me, other["id"]),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute("INSERT INTO friendships (requester_id, addressee_id) VALUES (%s, %s)",
                        (me, other["id"]))
            flash(generic, "info")
        elif existing["status"] == "accepted":
            flash(f"You and {other['name']} are already friends.", "info")
        elif existing["requester_id"] == me:
            flash(generic, "info")  # already waiting on them
        else:
            # They already asked you: sending a request back just accepts theirs.
            cur.execute("UPDATE friendships SET status = 'accepted' WHERE id = %s", (existing["id"],))
            flash(f"You and {other['name']} are now friends.", "success")
    db.commit()
    return redirect(url_for("friends"))


@app.route("/friends/<int:friendship_id>/accept", methods=["POST"])
@login_required
def friend_accept(friendship_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM friendships WHERE id = %s AND addressee_id = %s AND status = 'pending'",
            (friendship_id, g.user["id"]),
        )
        if cur.fetchone() is None:
            abort(404)
        cur.execute("UPDATE friendships SET status = 'accepted' WHERE id = %s", (friendship_id,))
    db.commit()
    flash("Friend request accepted.", "success")
    return redirect(url_for("friends"))


@app.route("/friends/<int:friendship_id>/remove", methods=["POST"])
@login_required
def friend_remove(friendship_id):
    """Decline a request, cancel your own request, or unfriend. Any person in the pair may do it."""
    me = g.user["id"]
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, requester_id, addressee_id FROM friendships "
            "WHERE id = %s AND (requester_id = %s OR addressee_id = %s)",
            (friendship_id, me, me),
        )
        pair = cur.fetchone()
        if pair is None:
            abort(404)
        a, b = pair["requester_id"], pair["addressee_id"]
        cur.execute("DELETE FROM friendships WHERE id = %s", (friendship_id,))
        # Unfriending also ends any shared trips between the two, so gear access stops at once.
        cur.execute(
            "DELETE FROM trip_participants p USING trips t WHERE p.trip_id = t.id AND "
            "((t.user_id = %s AND p.user_id = %s) OR (t.user_id = %s AND p.user_id = %s))",
            (a, b, b, a),
        )
    db.commit()
    flash("Removed.", "info")
    return redirect(url_for("friends"))


@app.route("/friends/settings", methods=["POST"])
@login_required
def friend_settings():
    share = request.form.get("share_gear") == "1"
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE users SET share_gear = %s WHERE id = %s", (share, g.user["id"]))
    db.commit()
    flash("Your gear is now shared with trip partners." if share else "Your gear is now private.", "success")
    return redirect(url_for("friends"))


@app.route("/trips/<int:trip_id>/invite", methods=["POST"])
@login_required
def trip_invite(trip_id):
    get_own_trip_or_404(trip_id)
    friend_raw = request.form.get("friend_id", "")
    friend_id = int(friend_raw) if friend_raw.isdigit() and len(friend_raw) < 10 else None
    db = get_db()
    with db.cursor() as cur:
        friend = next((f for f in friends_of(cur, g.user["id"]) if f["id"] == friend_id), None)
        if friend is None:
            flash("Choose one of your friends to invite.", "danger")
        else:
            cur.execute(
                "INSERT INTO trip_participants (trip_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT (trip_id, user_id) DO NOTHING",
                (trip_id, friend["id"]),
            )
            flash(f"Invited {friend['name']}.", "success")
    db.commit()
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/partners/<int:user_id>/remove", methods=["POST"])
@login_required
def trip_remove_partner(trip_id, user_id):
    get_own_trip_or_404(trip_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM trip_participants WHERE trip_id = %s AND user_id = %s", (trip_id, user_id))
    db.commit()
    flash("Removed from the trip.", "info")
    return redirect(url_for("trip_detail", trip_id=trip_id))


def _answer_invite(trip_id, accept):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 AS ok FROM trip_participants WHERE trip_id = %s AND user_id = %s AND status = 'invited'",
            (trip_id, g.user["id"]),
        )
        if cur.fetchone() is None:
            abort(404)
        if accept:
            cur.execute("UPDATE trip_participants SET status = 'joined' WHERE trip_id = %s AND user_id = %s",
                        (trip_id, g.user["id"]))
        else:
            cur.execute("DELETE FROM trip_participants WHERE trip_id = %s AND user_id = %s",
                        (trip_id, g.user["id"]))
    db.commit()
    flash("You joined the trip." if accept else "Invitation declined.", "success" if accept else "info")
    return redirect(url_for("trips"))


@app.route("/trips/<int:trip_id>/invite/accept", methods=["POST"])
@login_required
def trip_invite_accept(trip_id):
    return _answer_invite(trip_id, True)


@app.route("/trips/<int:trip_id>/invite/decline", methods=["POST"])
@login_required
def trip_invite_decline(trip_id):
    return _answer_invite(trip_id, False)


@app.route("/trips/<int:trip_id>/leave", methods=["POST"])
@login_required
def trip_leave(trip_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 AS ok FROM trip_participants WHERE trip_id = %s AND user_id = %s AND status = 'joined'",
            (trip_id, g.user["id"]),
        )
        if cur.fetchone() is None:
            abort(404)
        cur.execute("DELETE FROM trip_participants WHERE trip_id = %s AND user_id = %s", (trip_id, g.user["id"]))
    db.commit()
    flash("You left the trip.", "info")
    return redirect(url_for("trips"))


def trip_members(cur, trip_id):
    """The trip's owner plus joined partners: [{id, name, share_gear, is_owner}]."""
    cur.execute(
        "SELECT u.id, u.name, u.share_gear, TRUE AS is_owner FROM trips t JOIN users u ON u.id = t.user_id "
        "WHERE t.id = %s UNION ALL "
        "SELECT u.id, u.name, u.share_gear, FALSE AS is_owner FROM trip_participants p "
        "JOIN users u ON u.id = p.user_id WHERE p.trip_id = %s AND p.status = 'joined'",
        (trip_id, trip_id),
    )
    return cur.fetchall()


def partner_trip_view(trip_id):
    """Read-only trip page for a joined partner. Anyone else gets a 404."""
    with get_db().cursor() as cur:
        members = trip_members(cur, trip_id)
        if g.user["id"] not in {m["id"] for m in members}:
            abort(404)
        cur.execute(
            "SELECT t.id, t.trip_date, t.status, t.notes, o.id AS objective_id, o.name AS objective_name, "
            "o.region, o.elevation_ft, o.difficulty FROM trips t JOIN objectives o ON o.id = t.objective_id "
            "WHERE t.id = %s",
            (trip_id,),
        )
        trip = cur.fetchone()
    owner = next(m for m in members if m["is_owner"])
    others = [m for m in members if m["id"] != g.user["id"]]
    return render_template("trip_partner.html", trip=trip, owner=owner, others=others)


@app.route("/trips/<int:trip_id>/partners/<int:user_id>/gear")
@login_required
def partner_gear(trip_id, user_id):
    """A trip partner's gear library, visible only to people on the same trip and only if shared."""
    with get_db().cursor() as cur:
        members = trip_members(cur, trip_id)
        ids = {m["id"] for m in members}
        if g.user["id"] not in ids or user_id not in ids or user_id == g.user["id"]:
            abort(404)
        target = next(m for m in members if m["id"] == user_id)
        if not target["share_gear"]:
            return render_template(
                "error.html", title="Gear not shared",
                message=f"{target['name']} has not shared their gear with trip partners."), 403
        cur.execute(
            "SELECT name, category, weight_oz FROM gear_items WHERE user_id = %s ORDER BY category, name",
            (user_id,),
        )
        items = cur.fetchall()
        cur.execute(
            "SELECT o.name FROM trips t JOIN objectives o ON o.id = t.objective_id WHERE t.id = %s", (trip_id,)
        )
        objective_name = cur.fetchone()["name"]
    return render_template("partner_gear.html", owner_name=target["name"], items=items, trip_id=trip_id,
                           objective_name=objective_name, total_oz=sum(i["weight_oz"] for i in items))


def _planned_view(label, checkpoint):
    def view():
        return render_template("coming_soon.html", title=label, checkpoint=checkpoint)
    return view


for _page in PAGES:
    if "checkpoint" in _page:
        app.add_url_rule(
            _page["path"],
            endpoint="planned_" + _page["path"].strip("/").replace("/", "_"),
            view_func=_planned_view(_page["label"], _page["checkpoint"]),
        )


# Initialize the schema when the module is imported (gunicorn, start.sh, tests).
init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
