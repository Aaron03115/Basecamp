import os

import psycopg
from flask import Flask, g, jsonify, render_template
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

SEED_OBJECTIVES = [
    ("Mount Elbert", "Sawatch Range", "Class 1", 14439, "Late June to September is typically snow-free.", 39.1178, -106.4454),
    ("Longs Peak (Keyhole Route)", "Front Range", "Class 3", 14259, "Start before dawn; afternoon storms are common.", 40.2549, -105.6162),
    ("Quandary Peak", "Tenmile Range", "Class 1", 14265, "Popular beginner 14er; snow lingers into June.", 39.3972, -106.1064),
    ("Mount Bierstadt", "Front Range", "Class 2", 14060, "Willow thickets near the start can be wet in early summer.", 39.5827, -105.6686),
    ("Grays Peak", "Front Range", "Class 1", 14278, "Often paired with neighboring Torreys Peak.", 39.6339, -105.8176),
]

# Pages from the project spec. Pages that are not built yet render a "coming soon"
# page (below) so navigation never hits a 404. When you build a real page, add its
# route and DELETE its "checkpoint" key here so the placeholder is not registered.
PAGES = [
    {"path": "/objectives", "label": "Objectives", "checkpoint": 2},
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
    """Create the tables and seed sample objectives once.

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
            cur.execute("SELECT COUNT(*) FROM objectives")
            count = cur.fetchone()[0]
            if count == 0:
                cur.executemany(
                    "INSERT INTO objectives "
                    "(name, region, difficulty, elevation_ft, season_notes, lat, lng) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    SEED_OBJECTIVES,
                )
        conn.commit()


@app.context_processor
def inject_pages():
    """Make the page list available to every template (navbar + landing page)."""
    return {"pages": PAGES}


@app.route("/")
def index():
    with get_db().cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM objectives")
        count = cur.fetchone()["n"]
    return render_template("index.html", objective_count=count)


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
