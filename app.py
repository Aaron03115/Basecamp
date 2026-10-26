import json
import os
from urllib.parse import urlencode

import psycopg
from flask import Flask, abort, g, jsonify, render_template, request
from psycopg.rows import dict_row

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
    {"path": "/gear", "label": "Gear Library", "checkpoint": 3},
    {"path": "/loadouts", "label": "Loadout Builder", "checkpoint": 4},
    {"path": "/trips/plan", "label": "Plan a Trip", "checkpoint": 5},
    {"path": "/trips", "label": "My Trips", "checkpoint": 5},
    {"path": "/summit-log", "label": "Summit Log", "checkpoint": 7},
]

app = Flask(__name__)
app.config["DATABASE_URL"] = DATABASE_URL


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


@app.route("/api/health")
def health():
    return jsonify(status="ok")


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
