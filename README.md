# comma-osm-speed

[![CI](https://github.com/shafanator/comma-osm-speed/actions/workflows/ci.yml/badge.svg)](https://github.com/shafanator/comma-osm-speed/actions/workflows/ci.yml)

Compare driving traces from your Comma 3X against OpenStreetMap `maxspeed` tags and flag mismatches above a threshold, so you can review and fix them by hand.

> **Status: early alpha (v0.1).** Developed and tested on **macOS**, but the commands are plain Python and should work anywhere Valhalla and Python 3.9+ are available. The tool finds candidates and helps you review them — **you make any edits yourself, by hand, in the iD editor or JOSM.** It does not upload anything to OpenStreetMap. Expect rough edges; feedback and false-positive reports are very welcome (see [Contributing & feedback](#contributing--feedback)).

## How it works

```
Comma 3X  ──►  Comma Connect cloud
                       │  (comma-osm-speed analyze --start/--end)
                       ▼
              GPS samples per route
                       │
                       ▼
              Valhalla `trace_attributes`
                       │
                       ▼
          per-OSM-way speed samples (mph)
                       │
                       ▼
   85th percentile per way, free-flow filtered (≥5 mph)
                       │
                       ▼
        Overpass: current `maxspeed` tag per way
                       │
                       ▼
    diff > threshold (default 10 mph)  →  candidates
                       │
                       ▼
   review UI  +  JOSM .osc  +  CSV  →  you edit by hand in iD / JOSM
```

The tool pulls your routes from Comma Connect using your account JWT and dongle ID. **No Comma Prime subscription and no SSH access to the device are required.** (If you'd rather work offline, `analyze` can also read a folder of pre-extracted GPS JSON files — see Step 2.)

## A note before you use this

**Speed people drive ≠ posted speed limit.** Drivers routinely exceed limits, and OSM's `maxspeed` tag is supposed to reflect the *legal posted* limit, not observed speeds. This tool surfaces **candidates for human review**, not ground truth. Any edits are made by you, by hand, in the iD editor or JOSM — the tool itself uploads nothing. Before you change anything in OSM:

1. Verify the posted sign on [Mapillary](https://www.mapillary.com), Google Street View, or in person.
2. Read the OSM community guidance on automated edits: https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct
3. For more than a handful of edits, post a discussion thread on the country-specific OSM talk-list first.

## Setup

**Requirements:** Python 3.9+, a Comma account (for Comma Connect), and a Valhalla instance — either the public one (zero setup) or a self-hosted Docker container (see [Valhalla setup](#valhalla-map-matcher) below).

On macOS, use `python3` — Apple doesn't ship a bare `python` symlink. On Linux/Windows, `python` usually works.

```bash
python3 -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
python3 -m pip install -e ".[dev]"

cp .env.example .env
# fill in COMMA_JWT and COMMA_DONGLE_ID
```

After activating the venv, `comma-osm-speed` is on your PATH. Run `deactivate` when done.

### Comma credentials

1. Sign in at https://jwt.comma.ai → copy the JWT into `.env` as `COMMA_JWT`.
2. Your dongle ID is shown in Comma Connect (looks like a 16-hex string) → put it in `.env` as `COMMA_DONGLE_ID`.

That's it — Comma Connect serves your route data with these credentials; no Prime required.

### Valhalla (map matcher)

The tool map-matches your GPS to OSM ways with [Valhalla](https://github.com/valhalla/valhalla). You have two options:

**Option A — use the public instance (zero setup).** `VALHALLA_URL` defaults to the public OSM-hosted server (`https://valhalla1.openstreetmap.de`), which is fine for trying things out. It's a shared, rate-limited resource, so please don't hammer it with large jobs.

**Option B — self-host with Docker (recommended for real use).** Run your own Valhalla built from an OpenStreetMap extract of your region. This needs Docker (Docker Desktop on macOS/Windows). The [docker-valhalla image](https://github.com/nilsnolde/docker-valhalla) makes it close to a one-liner — it downloads the extract and builds the routing graph for you.

**Step 1 — find the extract for the region you drive in.** Browse [download.geofabrik.de](https://download.geofabrik.de) and copy the `.osm.pbf` URL for your state/country. The graph must cover everywhere you drove, or those routes won't map-match. A few examples:

| Region | Extract URL |
|--|--|
| US – South Carolina | `https://download.geofabrik.de/north-america/us/south-carolina-latest.osm.pbf` |
| US – California | `https://download.geofabrik.de/north-america/us/california-latest.osm.pbf` |
| Great Britain | `https://download.geofabrik.de/europe/great-britain-latest.osm.pbf` |

**Step 2 — start the container with that URL** (swap in your region's URL):

```bash
mkdir -p custom_files

docker run -dt --name valhalla -p 8002:8002 \
  -v "$PWD/custom_files:/custom_files" \
  -e tile_urls=https://download.geofabrik.de/north-america/us/south-carolina-latest.osm.pbf \
  ghcr.io/nilsnolde/docker-valhalla/valhalla:latest

# Watch it download the PBF and build tiles. This can take anywhere from a few
# minutes to an hour+ depending on the size of the region.
docker logs -f valhalla

# It's ready when /status responds:
curl -s http://localhost:8002/status
```

Then point the tool at your local instance in `.env`:

```
VALHALLA_URL=http://localhost:8002
```

Notes:
- The graph only covers the region(s) you load. Routes you drove outside it won't map-match — add more space-separated URLs to `tile_urls` and `docker restart valhalla`.
- Built tiles are cached in `custom_files/`, so restarts are fast; the container only rebuilds when the PBF changes.
- The image was formerly published under `gis-ops/docker-valhalla`; that repo is archived and the Docker setup is now maintained upstream at [valhalla/valhalla `docker/`](https://github.com/valhalla/valhalla/blob/master/docker/README.md). The `ghcr.io/nilsnolde/...` command above still works.

## Usage

### Step 1 — analyze trips and write a candidate report

Pull from Comma Connect (default) by giving a date range:

```bash
comma-osm-speed analyze \
  --start 2026-05-01 --end 2026-05-19 \
  --threshold-mph 10 \
  --default-units mph \
  --output ./output
```

Or, to work offline from a folder of pre-extracted GPS `.json` files:

```bash
comma-osm-speed analyze --gps-dir ./gps --threshold-mph 10 --default-units mph
```

This writes:
- `output/candidates.csv` — human review spreadsheet (one row per candidate)
- `output/candidates.json` — machine-readable
- `output/candidates.osc` — load in JOSM to review and edit manually

### Step 2 — review and edit by hand

Use the review UI (below) or open the CSV directly. For each candidate, check:
- The posted sign on Mapillary/Street View.
- Whether the way actually carries through-traffic at the speed you observed (e.g. a service road vs. a residential street with the same way ID).

When a change is warranted, make it yourself in the **iD editor** (the review UI has a one-click "Edit in OSM" link that opens iD with the way selected) or load `candidates.osc` in **JOSM**. Ignore the rest.

## Review GUI

Step through candidates one by one in a local web UI:

```bash
comma-osm-speed review --candidates output/candidates.json
```

A browser tab opens at `http://localhost:8765/`. For each candidate you see:

- The road name, OSM highway type, way ID and version.
- Three stat cards: current OSM tag, your observed 85th-percentile, the proposed value.
- One-click links to **Edit in OSM** (opens the iD editor with that way selected), **Mapillary**, **Street View**, **Google Maps**, and the OSM way page.
- Ignore button that appends to `ignored_ways.txt` (with optional comment).

Keyboard shortcuts: `E` edit, `M` Mapillary, `V` Street View, `I` ignore, `N` / Space / → next, `P` / ← previous.

The ignore state is **persistent** — the next `analyze` or `trends` run won't show you those again. The review UI also filters already-ignored ones immediately, so you only see what still needs decisions.

Common workflow:

1. Run `analyze`.
2. `comma-osm-speed review --candidates output/candidates.json`
3. Click through. For each:
   - Looks like a real missing/wrong tag → click **Edit in OSM**, fix the maxspeed in iD, save.
   - Implicit-default residential, bad map match, or otherwise not worth editing → click **Ignore**.
4. Close the browser when done.

## Ignoring specific way_ids

Some ways will keep showing up that you've decided not to edit — implicit-default residential streets, service roads where the map matcher snaps oddly, etc. Put their IDs (one per line) in `ignored_ways.txt`. Both `analyze` and `trends` honor this list — pass `--ignore-file`:

```bash
comma-osm-speed analyze --start 2026-05-01 --end 2026-05-19 --ignore-file ignored_ways.txt
```

The review UI's Ignore button also appends to this file automatically.

## Trends across runs

If you save each `analyze` run into a dated folder under `history/` (e.g. `--output history/2026-05-19`), you can see which way_ids keep recurring across runs:

```bash
comma-osm-speed trends --history-dir ./history
```

Produces two CSVs (in the latest run's folder by default):

- `recurring_candidates.csv` — way_ids seen in ≥ 2 runs, sorted by weeks_seen + latest sample_count. Includes a `trend_samples` column showing the count per run so you can spot growing evidence. Recurring way_ids are your highest-confidence review targets.
- `trend_summary.csv` — total candidates per run (lets you see if your data quality is improving over time).

## Tuning knobs

In `.env` or via the `Config` object:

- `threshold_mph` — flag threshold (default 10).
- `speed_percentile` — which percentile of observed speed (default 85, the traffic-engineering standard).
- `min_samples_per_way` — require ≥ N samples on a way before drawing conclusions (default 30).
- `min_speed_mph_for_freeflow` — drop samples below this (idling / stop-and-go) to keep the percentile honest (default 5).

## Project layout

```
src/comma_osm_speed/
├── cli.py              # click CLI (analyze / review / trends)
├── config.py           # env-loaded config
├── comma_client.py     # Comma Connect REST client
├── local_source.py     # read pre-extracted GPS JSON files
├── map_matcher.py      # Valhalla trace_attributes wrapper
├── osm_client.py       # Overpass reads + maxspeed parsing
├── osm_submitter.py    # writes JOSM .osc files for manual review
├── analyzer.py         # percentile math + candidate detection
├── trends.py           # recurring-candidate analysis across runs
├── web.py              # local review UI
└── pipeline.py         # glue: fetch → match → analyze → report

tests/                          # pytest unit tests
```

## Tests

```bash
python3 -m pytest
```

## Contributing & feedback

This is an alpha and the most useful thing you can send is **false-positive reports** — cases where a way was flagged but the existing OSM tag is correct. There are issue templates for that, for bugs, and for region/feature requests.

- File an issue: use the templates under **Issues → New issue**.
- Before making OSM edits based on this tool's output, verify the posted sign and read the [Automated Edits Code of Conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct); for anything beyond a handful of edits, post on your country's OSM talk-list first.
- PRs welcome — `ruff check .` and `pytest` should pass (CI runs both on Python 3.9/3.11/3.12).

## License

[MIT](LICENSE) © 2026 Michael Shafran
