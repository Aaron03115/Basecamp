-- Basecamp schema (PostgreSQL). Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objectives (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    region       TEXT NOT NULL,
    difficulty   TEXT NOT NULL,
    elevation_ft INTEGER,
    season_notes TEXT,
    lat          DOUBLE PRECISION,  -- used by the map stretch goal
    lng          DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS gear_items (
    id        SERIAL PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    category  TEXT NOT NULL,
    weight_oz DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS loadouts (
    id        SERIAL PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    trip_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loadout_items (
    loadout_id   INTEGER NOT NULL REFERENCES loadouts(id) ON DELETE CASCADE,
    gear_item_id INTEGER NOT NULL REFERENCES gear_items(id) ON DELETE CASCADE,
    qty          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (loadout_id, gear_item_id)
);

CREATE TABLE IF NOT EXISTS trips (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    objective_id  INTEGER NOT NULL REFERENCES objectives(id),
    loadout_id    INTEGER REFERENCES loadouts(id) ON DELETE SET NULL,
    trip_date     TEXT,
    status        TEXT NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned', 'attempted', 'completed')),
    notes         TEXT,
    route_geojson TEXT  -- stretch goal only
);

CREATE TABLE IF NOT EXISTS trip_reports (
    id              SERIAL PRIMARY KEY,
    trip_id         INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    conditions_text TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
