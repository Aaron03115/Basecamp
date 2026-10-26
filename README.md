# Basecamp

Trip planning and summit log for backcountry hikers, backpackers, and mountaineers.
Plan a trip around an objective, attach a gear loadout, and log completed summits/trips.

**Stack:** Python 3, Flask, PostgreSQL, Jinja2, Bootstrap 5, gunicorn + nginx on AWS EC2 (Ubuntu).

## Run it locally

1. Install this first (Ubuntu/Debian; on Windows use WSL):
   ```bash
   sudo apt install git python3 python3-venv
   ```
2. Clone and start:
   ```bash
   git clone https://github.com/Aaron03115/Basecamp.git
   cd Basecamp
   ./start.sh
   ```
3. Open http://localhost:5000.

`./start.sh` does everything else for you. It checks Python, installs PostgreSQL with `apt` if
it is missing (it uses `sudo`, so it may ask for your password), starts the server, creates the
`basecamp` database role, the `basecamp` database and a `basecamp_test` database, creates a
virtual environment, installs `requirements.txt`, creates the tables and loads the objectives from `data/seed_objectives.json`,
and starts the app. It is safe to run again.

Options:
- Different database: `DATABASE_URL=postgresql://user:pass@host:5432/dbname ./start.sh`
- Different port: `PORT=8080 ./start.sh`

## Deploy to AWS EC2

You need the server's public IP, your `.pem` key (never commit it), and the login user.

1. Copy the deploy folder to a fresh server and run the one-time setup. This installs
   PostgreSQL, creates the `basecamp` database and role, installs the systemd service, and
   configures nginx:
   ```bash
   scp -i ~/keys/mykey.pem -r deploy ubuntu@<PUBLIC-IP>:~
   ssh -i ~/keys/mykey.pem ubuntu@<PUBLIC-IP>
   sudo bash deploy/setup-ec2.sh
   ```
   Override the DB credentials by exporting `DB_NAME`, `DB_USER`, `DB_PASSWORD` before
   running the script.
2. Commit your work (and tag it, e.g. `git tag -a cp1 -m "Checkpoint 1"`), then from your
   laptop deploy it (needs `rsync`):
   ```bash
   ./deploy/deploy.sh -h <PUBLIC-IP> -i ~/keys/mykey.pem
   ```
   The script refuses to deploy uncommitted changes (use `-f` to override), runs the tests if
   the local test database exists, copies the code, installs dependencies, restarts the
   service and checks `/api/health`.
3. Open `http://<PUBLIC-IP>/`. The service is enabled in systemd, so it also comes back
   after `sudo reboot`.

Make sure the instance's security group allows inbound HTTP (port 80) and SSH (port 22).

## Tests

Tests use `pytest` and always run against the `basecamp_test` database (never your dev data).
`./start.sh` creates it, so after running `./start.sh` once:

```bash
.venv/bin/python -m pytest -q
```

Use a different test database with `TEST_DATABASE_URL=postgresql://... ` (its name must end in
`_test`, because some tests delete data).

## Pages

| Page | What it does |
|---|---|
| `/objectives` | Browse the objectives library. Search by name or region and filter by region, difficulty and season. Results are paginated. |
| `/objectives/<id>` | Detail page: elevation, difficulty, best season, coordinates, conditions notes, recent trip reports, and a "Plan a trip here" link. |

The navbar links to every planned page. Pages not built yet show a "planned for Checkpoint N"
placeholder instead of a 404. When you build a real page, add its route and remove its
`checkpoint` entry from `PAGES` in `app.py`.

## Objective data

Objectives live in `data/seed_objectives.json` (hand-curated: region, difficulty, best season and notes
are written by hand). Every time the app starts, `init_db()` upserts that file into the `objectives`
table by name, so editing the file and redeploying updates the live site without losing anything else.

To add more peaks from OpenStreetMap (run on a machine with internet access, then review and commit):

```bash
python3 scripts/import_osm_peaks.py --dry-run   # see what would be added
python3 scripts/import_osm_peaks.py             # add Colorado peaks of 13,000 ft or more
```

Existing entries are never overwritten. Peak coordinates and elevations from OpenStreetMap are
(c) OpenStreetMap contributors, ODbL.

## Files

| File | Purpose |
|---|---|
| `start.sh` | Installs dependencies, sets up PostgreSQL, and starts the app on any Linux machine |
| `deploy/setup-ec2.sh` | One-time server setup incl. PostgreSQL (runs on the server with sudo) |
| `deploy/basecamp.service` | systemd unit: starts on boot, restarts on crash |
| `deploy/nginx-basecamp.conf` | nginx forwards port 80 to the app |
| `deploy/deploy.sh` | Copies code to the server and restarts it (runs on your laptop) |
| `app.py`, `schema.sql`, `templates/` | The Flask app, database schema, and pages |
| `data/seed_objectives.json` | The objectives library (loaded into PostgreSQL on startup) |
| `scripts/import_osm_peaks.py` | Optional helper that adds peaks from OpenStreetMap to the seed file |
| `tests/` | pytest suite (grows with each checkpoint) |
