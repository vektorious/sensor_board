# Deploying DIY Sensor on Uberspace

The app runs as a **supervisord** daemon on a local port (Gunicorn + Uvicorn
worker, see [`conf.py`](conf.py), port `8020`), and a **web backend** routes your
domain to it.

Run these over SSH on the Uberspace host
(`ssh <USER>@<your-host>.uber.space`), or paste them here prefixed with `!`.

## 1. Clone and install

```bash
cd ~
git clone https://github.com/vektorious/sensor_board.git
cd sensor_board

python3.12 -m venv .venv          # any Python 3.11+ available on the host
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

ECharts is vendored in the repo, so there's nothing else to fetch. (If it's ever
missing: `./scripts/fetch_vendor.sh`.)

## 2. Configure

Configuration lives in a `.env` file that the app reads itself (keeping
comma-containing secrets out of the supervisord file, whose `environment=` line
splits on commas):

```bash
cp .env.example .env
nano .env
```

Set at least:
- `API_KEYS` — comma-separated operator keys (e.g. `grp-a,grp-b,admin`). No
  quotes needed. Anonymous publishing needs no key at all; these only raise
  limits and make devices permanent.
- `BASE_URL` — `https://diy-sensor.org`, the domain alone. The main page uses it
  to print copy-paste-able examples, so it must not include `ROOT_PATH`.
- `ROOT_PATH` — dashboard prefix, `/dashboard` by default. Leave it, or the main
  page has nowhere to live.
- `APP_TITLE` / `BRAND` — optional labels; both default to `DIY Sensor`.
- `DB_SIZE_WARN_BYTES` / `DB_SIZE_MAX_BYTES` — the defaults (1 GB / 2 GB) suit a
  10 GB Uberspace quota. Scale them if yours differs.

`.env` is git-ignored, so secrets never get committed. Then install the service
file (it carries no secrets — it just runs the app, which loads `.env`):

```bash
mkdir -p ~/etc/services.d
cp deploy/uberspace/sensor_board.ini ~/etc/services.d/sensor_board.ini
```

## 3. Start it

```bash
supervisorctl reread
supervisorctl update
supervisorctl status sensor_board      # should show RUNNING
```

Logs: `~/sensor_board/errors.log` and `access.log`. Restart after a config change
with `supervisorctl restart sensor_board`.

## 4. Route your domain

Three paths belong to the app: the main page at `/`, the dashboard under
`/dashboard`, and ingestion under `/sensor`. Routing the root covers all three,
since Uberspace passes the full path through:

```bash
uberspace web domain add diy-sensor.org       # then set the DNS records it prints
uberspace web backend set / --http --port 8020
uberspace web backend list                    # confirm it points at :8020
```

If you would rather keep the root for something else, route the two subpaths
individually instead — the main page is then unreachable, which is fine:

```bash
uberspace web backend set /dashboard --http --port 8020
uberspace web backend set /sensor    --http --port 8020
```

DNS propagation + Let's Encrypt cert issuance happen automatically once the
records resolve. `https://diy-sensor.org/` then serves the public main page —
the documentation, live totals, and the device-ID/write-key generators.

## 5. Verify

```bash
curl -s https://diy-sensor.org/ | head            # main page
curl -s https://diy-sensor.org/dashboard/ | head  # dashboard

# Anonymous publishing — no key needed. The write key is yours to choose;
# whoever writes first with it owns the device ID from then on.
curl -X POST https://diy-sensor.org/sensor/measurement \
  -H 'content-type: application/json' \
  -d '{"project":"demo","name":"Test","device_id":"testdev","write_key":"pick-a-strong-one","sensors":{"temperature":{"value":21.4,"unit":"C"}}}'

# With an operator key, the device becomes permanent instead of expiring.
curl -X POST https://diy-sensor.org/sensor/measurement \
  -H 'x-api-key: <one-of-your-keys>' -H 'content-type: application/json' \
  -d '{"device_id":"reference-station","sensors":{"temperature":21.4}}'
```

Then open `https://diy-sensor.org/dashboard/device/testdev`. A first write
returns `201` (device claimed); later writes return `200`.

Check the platform's own view of itself with:

```bash
cd ~/sensor_board && .venv/bin/python -m app.admin status
```

## Updating later

```bash
cd ~/sensor_board
git pull
.venv/bin/pip install -r requirements.txt   # only if deps changed
supervisorctl restart sensor_board
```

## One-time upgrade to 0.2

0.2 changes the ingestion contract and the URL layout, so it needs more than a
`git pull`. Nothing here is reversible by simply checking out the old commit —
take the backup.

**1. Back up the database.** The app renames the old `readings` table aside on
first start rather than migrating it; a copy means you can still export from it
if something surprises you.

```bash
cd ~/sensor_board
supervisorctl stop sensor_board
cp app/data/sensors.db ~/sensors-pre-0.2.db
```

**2. Pull the new code.** No new dependencies, so `pip install` is optional.

```bash
git pull
```

**3. Update `.env`.** Three values changed meaning:

- `BASE_URL` must now be the **domain alone** — `https://diy-sensor.org`, *not*
  `https://diy-sensor.org/dashboard`. The main page appends `INGEST_PATH` to it
  when printing examples, so a prefix here produces a broken curl command.
- `ROOT_PATH` should stay `/dashboard`. That is now the default, and it is what
  leaves `/` free for the main page.
- `APP_TITLE` / `BRAND`: delete them to take the `DIY Sensor` default, or set
  them to whatever you want the page to say. A stale value here silently wins
  over the new default.

Nothing else needs adding — every new setting (policy limits, database
ceilings, global budgets) has a working default. See `.env.example` for what is
available.

**4. Route the domain root.** Three paths now belong to the app — `/` for the
main page, `/dashboard`, and `/sensor` — so one backend at the root replaces
the two subpath ones:

```bash
uberspace web backend set / --http --port 8020
uberspace web backend del /dashboard        # now covered by /
uberspace web backend del /sensor
uberspace web backend list                  # confirm one entry, pointing at :8020
```

**5. Start it and check.**

```bash
supervisorctl start sensor_board
supervisorctl status sensor_board
.venv/bin/python -m app.admin status
```

The log will carry a one-line `WARNING` that the pre-0.2 `readings` table was
renamed to `readings_pre_v0_2`. The app starts with an empty `readings` table:
old rows have no owner under the new model, so they are set aside rather than
migrated. Export anything you want from that table, then drop it:

```bash
sqlite3 app/data/sensors.db "DROP TABLE readings_pre_v0_2;"
```

**6. Update your devices.** This is the part that will not fix itself:

- `device_uuid` is now **`device_id`** — an old payload gets a `400`.
- Anonymous devices must send a `write_key`. Generate one at
  `https://diy-sensor.org/`, save it, and flash it alongside the device ID.
- If a device keeps using its API key and no write key, it carries on working
  unchanged apart from the field rename, and stays permanent.
- Remove any client-supplied `timestamp` field; it is now rejected.

## Pointing devices at it

In each device's WiFiManager setup portal set:
- **API URL** → `https://diy-sensor.org/sensor/measurement` (ingest is at the
  root `/sensor` path, not under `/dashboard`)
- **API Key** → one of your `API_KEYS`

Existing firmware defaults (`DEFAULT_API_URL`) can also be updated to the new
domain so freshly-flashed devices use it out of the box.
