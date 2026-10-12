import os
import sqlite3

from flask import Flask, g, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BASECAMP_DB", os.path.join(BASE_DIR, "instance", "basecamp.db"))

SEED_OBJECTIVES = [
    ("Mount Elbert", "Sawatch Range", "Class 1", 14439, "Late June to September is typically snow-free.", 39.1178, -106.4454),
    ("Longs Peak (Keyhole Route)", "Front Range", "Class 3", 14259, "Start before dawn; afternoon storms are common.", 40.2549, -105.6162),
    ("Quandary Peak", "Tenmile Range", "Class 1", 14265, "Popular beginner 14er; snow lingers into June.", 39.3972, -106.1064),
    ("Mount Bierstadt", "Front Range", "Class 2", 14060, "Willow thickets near the start can be wet in early summer.", 39.5827, -105.6686),
    ("Grays Peak", "Front Range", "Class 1", 14278, "Often paired with neighboring Torreys Peak.", 39.6339, -105.8176),
]

app = Flask(__name__)
app.config["DB_PATH"] = DB_PATH


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the database file and tables, and seed sample objectives once.

    Safe to call every time the app starts or start.sh runs.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
        count = conn.execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO objectives (name, region, difficulty, elevation_ft, season_notes, lat, lng) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                SEED_OBJECTIVES,
            )
        conn.commit()
    finally:
        conn.close()


init_db()


@app.route("/")
def index():
    count = get_db().execute("SELECT COUNT(*) FROM objectives").fetchone()[0]
    return render_template("index.html", objective_count=count)


@app.route("/api/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
