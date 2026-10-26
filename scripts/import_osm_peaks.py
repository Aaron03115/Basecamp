#!/usr/bin/env python3
"""Add peaks from OpenStreetMap to data/seed_objectives.json.

Run this on a machine with internet access, review the result, and commit the file:

    python3 scripts/import_osm_peaks.py              # Colorado peaks of 13,000 ft or more
    python3 scripts/import_osm_peaks.py --min-ft 12000 --dry-run

It uses only the Python standard library. Peaks that are already in the seed file
(matched by name) are left exactly as they are, so the details you wrote by hand
(region, difficulty, best season, notes) are never overwritten. Peaks that are new
get placeholder details ("Other Colorado", "Unrated", "Summer") that you can edit.

Data: (c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright).
Please be kind to the free Overpass servers: run this occasionally, not in a loop.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "BasecampClassProject/1.0 (student project; seed data import)"
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "seed_objectives.json")
FEET_PER_METER = 3.28084


def build_query(min_ft):
    min_m = int(min_ft / FEET_PER_METER)
    return (
        "[out:json][timeout:90];\n"
        'area["ISO3166-2"="US-CO"]["admin_level"="4"]->.co;\n'
        f'node["natural"="peak"]["name"]["ele"](area.co)(if: number(t["ele"]) >= {min_m});\n'
        "out;"
    )


def fetch_peaks(min_ft):
    data = urllib.parse.urlencode({"data": build_query(min_ft)}).encode("utf-8")
    req = urllib.request.Request(OVERPASS_URL, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def parse_elevation_ft(value):
    """OSM 'ele' is meters by default. Accepts '4401', '4401.2', '4,401 m', '14439 ft'."""
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "")
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(m|ft|')?\s*$", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    feet = number if unit in ("ft", "'") else number * FEET_PER_METER
    return int(round(feet))


def normalize_name(name):
    """Lowercase and drop 'Mount'/'Mt.' so 'Mt. Elbert' matches 'Mount Elbert'."""
    text = re.sub(r"[^\w\s]", " ", name.lower())
    words = [w for w in text.split() if w not in ("mount", "mt")]
    return " ".join(words)


def osm_to_objectives(osm_json, min_ft):
    found = {}
    for element in osm_json.get("elements", []):
        tags = element.get("tags", {})
        name = (tags.get("name") or "").strip()
        feet = parse_elevation_ft(tags.get("ele"))
        if not name or feet is None or feet < min_ft:
            continue
        if "lat" not in element or "lon" not in element:
            continue
        record = {
            "name": name,
            "region": "Other Colorado",
            "difficulty": "Unrated",
            "elevation_ft": feet,
            "best_season": "Summer",
            "season_notes": "Imported from OpenStreetMap. Add route and season details.",
            "lat": round(element["lat"], 4),
            "lng": round(element["lon"], 4),
        }
        key = normalize_name(name)
        # If two OSM points share a name, keep the higher one.
        if key not in found or feet > found[key]["elevation_ft"]:
            found[key] = record
    return list(found.values())


def merge(existing, imported):
    """Return (merged list, number added). Existing entries always win."""
    known = {normalize_name(o["name"]) for o in existing}
    added = [o for o in imported if normalize_name(o["name"]) not in known]
    added.sort(key=lambda o: (-o["elevation_ft"], o["name"]))
    return existing + added, len(added)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-ft", type=int, default=13000, help="minimum elevation in feet (default 13000)")
    parser.add_argument("--out", default=SEED_FILE, help="seed file to update")
    parser.add_argument("--dry-run", action="store_true", help="show what would be added without writing")
    args = parser.parse_args(argv)

    with open(args.out, encoding="utf-8") as f:
        existing = json.load(f)

    print(f"Asking Overpass for Colorado peaks of {args.min_ft:,} ft or more...")
    osm_json = fetch_peaks(args.min_ft)
    imported = osm_to_objectives(osm_json, args.min_ft)
    merged, added = merge(existing, imported)
    print(f"OpenStreetMap returned {len(imported)} peaks; {added} are new (the rest were already in the seed file).")

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(merged)} objectives to {os.path.normpath(args.out)}.")
    print("Review the new entries, edit region/difficulty/season by hand, then commit the file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
