# DIY Sensor

A small, self-hostable dashboard for time-series sensor data. Devices POST
measurements to one endpoint; the dashboard **auto-populates** — every sensor a
device reports becomes a panel, with no per-sensor code.

Publishing needs **no account and no API key**. A device is identified by a
public `device_id` and owned by a `write_key` the client chooses itself: the
first write to an unused ID claims it, and every later write must present the
same key. Anonymous devices are temporary — they and their data are deleted
after 48 hours without a successful write, which keeps a public instance from
filling up with abandoned experiments.

```bash
curl -X POST https://diy-sensor.org/sensor/measurement \
  -H 'content-type: application/json' \
  -d '{
        "device_id": "greenhouse-01",
        "write_key": "a-strong-random-key-you-generated",
        "sensors": {"temperature": 21.4, "humidity": 51}
      }'
```

Your dashboard is then at `/dashboard/device/greenhouse-01`.

- `GET /` — main page: docs, live totals, and in-browser key generators
- `GET /dashboard/` — overview of all projects and devices
- `GET /dashboard/device/{device_id}` — one device: latest values + charts
- `GET /dashboard/project/{slug}` — one project: per-sensor charts overlaying
  all its devices

`ROOT_PATH` controls the dashboard prefix (`/dashboard` by default). Set it to
`""` to serve the dashboard at the domain root; the main page is then not
mounted, since the two would collide. The ingestion endpoint is independent of
the prefix.

## How it works

Two tables carry everything. `devices` records ownership — who may write to a
device ID, whether it is temporary or persistent, and when it was last written
to. `readings` stores the data long-format, one row per
`(device, sensor, timestamp)`, in SQLite (WAL mode). Because the schema is
generic, the dashboard discovers what to render by querying the distinct
`sensor_type`s in the data, so new sensors appear automatically.

A device's **name** is display-only and always reflects the most recently
reported value, so renaming a device updates the label everywhere without
changing its URL. Presentation (labels, units, chart type, sort order) is an
*optional* override layer in [`app/sensors.py`](app/sensors.py) — unknown
sensors fall back to a humanized label and a line chart.

## The two credentials

They answer different questions and neither substitutes for the other.

| | Write key | API key |
|---|---|---|
| Chosen by | the client | the operator |
| Answers | *who owns this device ID* | *which limits apply* |
| Required | on every write to a device that has one | never, unless the device is keyless |
| Effect | ownership | persistence + a raised limit policy |
| Recoverable | **no** | reissued by the operator |

An API key makes a device **persistent** (exempt from the idle expiry) and
raises its limits. It does **not** override a write key: if a device has one,
the correct write key is required on every write, API key or not. A valid API
key can therefore never take over someone else's device.

A device created with an API key and no write key is *keyless*: any valid API
key may write to it. That is a deliberate beta simplification — per-device key
ownership is future work.

Write keys are stored as SHA-256 hashes, never in plaintext, and never logged.
There is no recovery endpoint by design: lose the key and the options are a
different device ID or waiting for the device to expire.

## Ingestion contract

`POST {INGEST_PATH}` (default `/sensor/measurement`):

```jsonc
{
  "device_id": "greenhouse-01",  // required — public identifier, [A-Za-z0-9_-]
  "write_key": "…",              // required for anonymous devices
  "project": "workshop-2026",    // optional — groups devices; omit for ungrouped
  "name": "Basil #3",            // optional — display name (latest wins)
  "sensors": {
    "temperature":  {"value": 21.4, "unit": "C"},   // unit and plot optional
    "battery_voltage": 3.97,                        // bare scalar also accepted
    "pump_running": false
  }
}
```

Send the API key, if you have one, as the `X-API-Key` header.

A sensor value may be a **number, boolean, short string, or null**. Booleans are
stored as 1/0 so they still chart. Lists and nested objects are rejected rather
than flattened. `NaN` and infinity are rejected.

**Timestamps are assigned by the server** on receipt; clients cannot supply
their own, so data cannot be backdated past the retention window. A `timestamp`
field in the payload is an error, not an ignored extra.

Responses: `201` when the request claimed the device, `200` when it appended.
Errors are JSON with a stable `code`:

| Status | Codes | Meaning |
|---|---|---|
| `400` | `missing_device_id`, `invalid_device_id`, `missing_sensors`, `empty_sensors`, `invalid_value`, `unknown_field`, `malformed_json`, … | The payload is not acceptable |
| `401` | `missing_write_key`, `api_key_required`, `invalid_api_key` | No usable credential was offered |
| `403` | `invalid_write_key` | The write key was wrong |
| `413` | `payload_too_large` | Body over the policy's size limit |
| `429` | `*_rate_limited`, `device_creation_limited`, `active_device_limit`, `too_many_sensors` | A rate or quota limit; see `Retry-After` |
| `503` | `storage_full`, `*_over_budget` | Shedding load or out of storage; retry later |

## Limits

Limits are policy-based: `anonymous` for unauthenticated traffic, `trusted` for
API-key traffic, plus any policy you define. A policy may **raise** a limit but
never remove one — a non-positive override is rejected, and no policy may drop
below the anonymous baseline.

Anonymous defaults (start tight, loosen with real traffic):

| Limit | Value |
|---|---|
| Retention | 48 h without a successful write |
| Request body | 16 KB |
| Sensor fields per request | 16 |
| Distinct sensors per device | 16 |
| Device ID / sensor name length | 64 characters |
| Requests per minute / day, per IP | 30 / 1,000 |
| Writes per minute, per device | 12 |
| New devices per hour, per IP | 5 |
| Active devices, per IP | 10 |
| Write budget, per credential | 3 rows/s, burst 300 |

Platform-wide, on top of those: a write budget of 10 rows/s (burst 1,000), a
1 GB database warning, and a hard 2 GB ceiling above which ingestion returns
`503`. The DB thresholds assume a 10 GB host quota — **scale them to yours**.

Counters live in SQLite rather than process memory, because the app runs under
gunicorn with more than one worker; in-process counters would each see a
fraction of the traffic and silently multiply every configured limit.

Override any value with `POLICY_<NAME>_<FIELD>`, and map keys to policies with
`API_KEY_POLICIES`:

```bash
API_KEYS="workshop-a-key,lab-key"
API_KEY_POLICIES="workshop-a-key:workshop"
POLICY_WORKSHOP_MAX_SENSORS_PER_DEVICE=64
POLICY_WORKSHOP_WRITES_PER_MINUTE_PER_DEVICE=60
```

An unmapped valid key gets `trusted`; a named policy that isn't built in starts
as a copy of `trusted` and takes its own overrides.

## Retention

A background sweep runs every `RETENTION_SWEEP_INTERVAL_HOURS` (and once at
startup). A temporary device with no successful write for its policy's
`retention_hours` is deleted, along with all its measurements and its stored key
hash; a project disappears once its last device is gone. Persistent (API-key)
devices are never swept. `RETENTION_HOURS=0` disables expiry entirely.

Only *successful* writes advance `last_seen_at`, so rejected requests cannot
keep a device alive. Deletion re-checks staleness in the `DELETE` itself, so a
device that receives a valid write mid-sweep survives it. Expiry is also checked
on the ingestion path, so claiming a long-silent ID works immediately rather
than waiting for the next sweep.

An expired ID is free to claim again, with a new write key and no trace of the
previous owner's data.

**Exceptions.** List devices in `RETENTION_EXEMPT_DEVICES` (by `device_id`)
and/or projects in `RETENTION_EXEMPT_PROJECTS`, comma-separated. A device is
spared if its ID is exempt *or* its latest project is exempt.

## Platform metrics

Lifetime totals (`devices_total`, `measurements_total`, `projects_total`) are
explicit counters incremented in the same transaction as the write they count,
so a rolled-back request cannot inflate them. They **never** decrease — cleanup
does not touch them. Active counts are queried from the live tables instead, so
they cannot drift. Both are shown on the main page.

## Administration

```bash
python -m app.admin status                      # totals, device counts, DB size
python -m app.admin cleanup --dry-run           # what the sweep would delete
python -m app.admin cleanup                     # force a sweep now
python -m app.admin device greenhouse-01        # inspect one device
python -m app.admin delete-device greenhouse-01 # delete a device and its data
python -m app.admin delete-key-data <sha256>    # delete one API key's rows
```

No command prints a credential: `device` reports *whether* a write key exists,
not its hash, and `delete-key-data` takes the hash recorded on the rows (which
is what appears in the ingest log).

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./scripts/fetch_vendor.sh          # vendor ECharts into app/static/js/

.venv/bin/uvicorn app.main:app --reload --port 8020
```

Open <http://127.0.0.1:8020/> for the main page, generate a device ID and a
write key there, and publish your first measurement. Run the tests with
`.venv/bin/pytest`.

## Configuration (environment variables)

All optional; sensible defaults apply. See [`.env.example`](.env.example).

| Var | Default | Purpose |
|-----|---------|---------|
| `APP_TITLE` | `DIY Sensor` | Page title |
| `BRAND` | = `APP_TITLE` | Header brand text |
| `BASE_URL` | `https://diy-sensor.org` | Public URL, used in the main page's examples |
| `ROOT_PATH` | `/dashboard` | Dashboard prefix; `""` puts it at the root and drops the main page |
| `FEEDBACK_URL` | project issues | Where the main page sends bug reports |
| `API_KEY` / `API_KEYS` | `change-me` | Operator keys accepted as `x-api-key` |
| `API_KEY_POLICIES` | *(empty)* | `key:policy,…` mapping |
| `ALLOW_ANONYMOUS` | `true` | Set false for an API-key-only instance |
| `INGEST_PATH` | `/sensor/measurement` | Endpoint devices POST to |
| `POLICY_<NAME>_<FIELD>` | see above | Raise a policy limit |
| `DB_PATH` | `app/data/sensors.db` | SQLite file location |
| `GLOBAL_WRITES_PER_SECOND` | `10` | Platform-wide sustained write budget |
| `GLOBAL_WRITE_BURST` | `1000` | Platform-wide burst allowance |
| `DB_SIZE_WARN_BYTES` | `1 GB` | Log a warning above this size |
| `DB_SIZE_MAX_BYTES` | `2 GB` | Refuse ingestion above this size |
| `DB_GROWTH_WARN_MB_PER_HOUR` | `50` | Warn when growth exceeds this rate |
| `RETENTION_HOURS` | `48` | Anonymous idle window; `0` disables expiry |
| `RETENTION_SWEEP_INTERVAL_HOURS` | `1` | How often the sweep runs |
| `RETENTION_EXEMPT_DEVICES` | *(empty)* | Device IDs never auto-deleted |
| `RETENTION_EXEMPT_PROJECTS` | *(empty)* | Project names never auto-deleted |
| `ECHARTS_SRC` | `{ROOT_PATH}/static/js/echarts.min.js` | Where the chart lib is served from |
| `DEFAULT_RANGE_HOURS` | `168` | Default chart lookback (7 days) |

## Deploying on Uberspace

1. Copy the project to `~/sensor_board` and install deps into a venv there.
2. Run `./scripts/fetch_vendor.sh` to vendor ECharts.
3. Put your settings in `~/sensor_board/.env` (the app loads it itself —
   supervisord's `environment=` splits on commas, which breaks `API_KEYS`).
4. [`conf.py`](conf.py) is a Gunicorn config binding `:8020` with Uvicorn
   workers. Expose it:
   ```bash
   uberspace web backend set / --http --port 8020
   ```
5. Start it via the supervisord service in
   [`deploy/uberspace/`](deploy/uberspace/sensor_board.ini).

Also cap the request body at the web-server edge, so an oversized upload is
refused before it reaches the application at all.

## Project layout

```
app/
  main.py         # FastAPI app assembly
  config.py       # env-driven settings
  policies.py     # limit policies and the API-key -> policy mapping
  limits.py       # SQLite-backed rate limiting and quotas
  validation.py   # ingestion payload rules
  security.py     # hashing, constant-time compare, log sanitising
  middleware.py   # security response headers
  database.py     # SQLite engine, WAL, indexes
  models.py       # Device, Reading, PlatformMetric
  metrics.py      # lifetime counters and active counts
  retention.py    # expiry sweep
  admin.py        # operator command line
  queries.py      # read-side queries (device + project)
  sensors.py      # optional presentation registry + sort order
  routes/
    ingest.py     # POST measurement endpoint
    home.py       # public main page
    api.py        # JSON API for the front-end
    web.py        # dashboard HTML pages
  templates/      # Jinja2: home, index, project, device
  static/         # CSS, ECharts (vendored), dashboard.js, project.js, generators.js
conf.py           # Gunicorn/Uvicorn config for Uberspace
```

## Notes

- **Dashboards are public.** Anyone with a device or project link can view it;
  there is no per-page auth. Knowing a `device_id` is enough to read its data,
  though not to write to it.
- Read APIs (`/api/projects`, `/api/project/{slug}/devices`) enumerate every
  device on the instance. That is intentional for a shared workshop board —
  consider it before publishing anything sensitive.
- Project overlay charts use an 8-hue categorical palette; beyond ~8 devices per
  project the colors repeat. For large projects, small-multiples would read
  better.
- API keys are currently plaintext in the environment with a `change-me`
  fallback. Accepted for the beta; revisit fail-closed startup before a public
  launch.
