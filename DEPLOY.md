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

## Pointing devices at it

In each device's WiFiManager setup portal set:
- **API URL** → `https://diy-sensor.org/sensor/measurement` (ingest is at the
  root `/sensor` path, not under `/dashboard`)
- **API Key** → one of your `API_KEYS`

Existing firmware defaults (`DEFAULT_API_URL`) can also be updated to the new
domain so freshly-flashed devices use it out of the box.
