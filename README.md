# Sensor Board

A small, self-hostable dashboard for time-series sensor data. Devices POST
measurements to one endpoint; the dashboard **auto-populates** — every sensor a
device reports becomes a panel, with no per-sensor code. Data is grouped into
**projects** and **devices**, each with its own bookmarkable URL:

- `GET {ROOT_PATH}/` — overview of all projects and devices
- `GET {ROOT_PATH}/device/{device_uuid}` — one device: latest values + line charts
- `GET {ROOT_PATH}/project/{slug}` — one project: per-sensor charts overlaying all
  its devices, plus the device list

`ROOT_PATH` is empty by default (UI at the domain root, e.g. `/device/{uuid}`);
set it to `/dashboard` to serve the whole UI under that prefix (e.g.
`/dashboard/device/{uuid}`). The ingestion endpoint is independent of the prefix.

It's generic: nothing is plant-specific. Point any device that can send JSON at
it and it just works.

## How it works

Each measurement is stored long-format — one row per
`(device, sensor, timestamp)` — in SQLite (WAL mode). Because the schema is
generic, the dashboard discovers what to render by querying the distinct
`sensor_type`s in the data. New sensors appear automatically. A device is
identified by its **`device_uuid`** (stable, used in URLs); its **name** is
display-only and always reflects the most recently reported value, so renaming a
device updates the label everywhere without changing its URL.

Presentation (labels, units, chart type, sort order) is an *optional* override
layer in [`app/sensors.py`](app/sensors.py) — unknown sensors fall back to a
humanized label + line chart.

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/fetch_vendor.sh          # vendor ECharts into app/static/js/

API_KEY=my-key .venv/bin/uvicorn app.main:app --reload --port 8020
```

Open http://127.0.0.1:8020/. Send a test measurement:

```bash
curl -X POST http://127.0.0.1:8020/sensor/measurement \
  -H 'x-api-key: my-key' -H 'content-type: application/json' \
  -d '{
        "project": "demo",
        "name": "Basil #3",
        "device_uuid": "a1b2c3d4",
        "sensors": {
          "temperature": {"value": 21.4, "unit": "C"},
          "moisture_pct": {"value": 62.0, "unit": "%"}
        }
      }'
```

Then visit http://127.0.0.1:8020/dashboard/device/a1b2c3d4.

## Ingestion contract

`POST {INGEST_PATH}` (default `/sensor/measurement`), header `x-api-key: {API_KEY}`:

```jsonc
{
  "project": "workshop-2026",   // optional — groups devices; omit for ungrouped
  "name": "Basil #3",           // optional — display name (latest wins)
  "device_uuid": "a1b2c3d4",    // required — stable identity
  "sensors": {
    "temperature":  {"value": 21.4, "unit": "C"},   // unit optional
    "battery_voltage": 3.97                          // bare number also accepted
  }
}
```

Each sensor entry becomes one row. An optional per-sensor `"plot"` field
(`"line"`/`"gauge"`) is stored for future use and otherwise ignored.

The endpoint validates the request and returns descriptive JSON errors:
`401` (missing/invalid `X-API-Key`), `413` (body over `MAX_PAYLOAD_BYTES`, default
50 KB), and `400` (malformed JSON, missing `device_uuid`, missing/empty `sensors`,
or a non-numeric sensor value).

## Beta testing

You'll be given an **API key**. Send it in the `X-API-Key` header. Your
`device_uuid` is **user-defined** — pick any stable string per device (e.g.
`workbench-sensor-01`); it's what your dashboard URL uses.

```bash
curl -X POST https://diy-sensor.org/sensor/measurement \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "device_uuid": "workbench-sensor-01",
    "sensors": {
      "temperature": 22.4,
      "humidity": 51
    }
  }'
```

Then view your device at `…/dashboard/device/workbench-sensor-01`.

**Please don't share your API key** — it identifies your submissions. Keys are
never stored in plaintext; only a SHA-256 hash is kept with each measurement.

*Data deletion (beta):* because each row is tagged with the key's hash, an
operator can remove all of a tester's data with a single query — a temporary
measure for the beta (see [`docs/future-auth-model.md`](docs/future-auth-model.md)
for the intended long-term design):

```sql
DELETE FROM readings WHERE api_key_hash = '<sha256-of-key>';
```

## Retention (automatic cleanup)

To keep beta clutter from accumulating, the app auto-deletes stale data. A
background sweep runs every `RETENTION_SWEEP_INTERVAL_HOURS` (and once at
startup): any device whose most recent reading is older than `RETENTION_HOURS`
(default **48h**) is purged. Because a project is just the set of readings that
carry its name, a project disappears automatically once its last device is
removed — but a project keeps living as long as **any** of its devices is still
active. Set `RETENTION_HOURS=0` to turn the whole thing off.

**Exceptions.** Some devices/projects should never be reaped (a permanent demo,
a reference station). List them in `RETENTION_EXEMPT_DEVICES` (by `device_uuid`)
and/or `RETENTION_EXEMPT_PROJECTS` (by project name), comma-separated. A device
is spared if its UUID is exempt *or* its latest project is exempt. These are
config today; the deletion logic takes the exempt sets as parameters, so the
source can later move to a table without changing the rule.

## Configuration (environment variables)

All optional; sensible defaults apply. See [`.env.example`](.env.example).

| Var | Default | Purpose |
|-----|---------|---------|
| `APP_TITLE` | `Sensor Board` | Page title |
| `BRAND` | = `APP_TITLE` | Header brand text |
| `BASE_URL` | *(empty)* | Public URL, for building shareable links |
| `ROOT_PATH` | *(empty)* | URL prefix the UI mounts under (e.g. `/dashboard`) |
| `API_KEY` | `change-me` | Shared secret devices send as `x-api-key` |
| `INGEST_PATH` | `/sensor/measurement` | Endpoint devices POST to |
| `MAX_PAYLOAD_BYTES` | `51200` | Max ingest body size (bytes); larger → 413 |
| `DB_PATH` | `app/data/sensors.db` | SQLite file location |
| `RETENTION_HOURS` | `48` | Auto-delete devices idle longer than this; `0` disables |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `1` | How often the cleanup sweep runs |
| `RETENTION_EXEMPT_DEVICES` | *(empty)* | Comma-separated device UUIDs never auto-deleted |
| `RETENTION_EXEMPT_PROJECTS` | *(empty)* | Comma-separated project names never auto-deleted |
| `ECHARTS_SRC` | `/static/js/echarts.min.js` | Where the chart lib is served from |
| `DEFAULT_RANGE_HOURS` | `168` | Default chart lookback (7 days) |

## Deploying on Uberspace

1. Copy the project to `~/sensor_board` and install deps into a venv there.
2. Run `./scripts/fetch_vendor.sh` to vendor ECharts.
3. [`conf.py`](conf.py) is a Gunicorn config binding `:8020` with Uvicorn
   workers. Expose it:
   ```bash
   uberspace web backend set / --http --port 8020
   ```
4. Start it (e.g. via a supervisord service running `gunicorn -c conf.py`).
5. Set your env vars (`API_KEY`, `BASE_URL`, `APP_TITLE`, …) in the service
   environment.

Point devices at `https://<your-domain>{INGEST_PATH}` with the matching
`API_KEY`.

## Project layout

```
app/
  main.py         # FastAPI app assembly
  config.py       # env-driven settings
  database.py     # SQLite engine, WAL, indexes
  models.py       # Reading (long-format table)
  sensors.py      # optional presentation registry + sort order
  queries.py      # read-side queries (device + project)
  routes/
    ingest.py     # POST measurement endpoint
    api.py        # JSON API for the front-end
    web.py        # HTML pages
  templates/      # Jinja2: index, project, device
  static/         # CSS, ECharts (vendored), dashboard.js, project.js
conf.py           # Gunicorn/Uvicorn config for Uberspace
scripts/
  fetch_vendor.sh # download ECharts into static/
```

## Notes

- Access is by URL — anyone with a device/project link can view it. There is no
  per-page auth; keep `device_uuid`s unguessable if the data is sensitive.
- The project overlay charts use an 8-hue categorical palette; beyond ~8 devices
  per project, colors repeat and the chart gets busy. For large projects,
  small-multiples would read better.
