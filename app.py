import functools
import hmac
import json
import os
import re
import secrets
from urllib.parse import urlencode

import psycopg
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
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
    {"path": "/loadouts", "label": "Loadout Builder", "checkpoint": 4},
    {"path": "/trips/plan", "label": "Plan a Trip", "checkpoint": 5},
    {"path": "/trips", "label": "My Trips", "checkpoint": 5},
    {"path": "/summit-log", "label": "Summit Log", "checkpoint": 7},
]

GEAR_CATEGORIES = [
    "Shelter", "Sleep", "Packs", "Clothing", "Footwear", "Cooking", "Water",
    "Navigation", "Safety", "Climbing", "Electronics", "Other",
]
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
    return render_template("objective_detail.html", o=objective, reports=reports)


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
