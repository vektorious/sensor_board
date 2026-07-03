# Deploying Sensor Board on Uberspace

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

## 2. Configure the service

```bash
mkdir -p ~/etc/services.d
cp deploy/uberspace/sensor_board.ini ~/etc/services.d/sensor_board.ini
nano ~/etc/services.d/sensor_board.ini    # edit the `environment=` line
```

Set in the `environment=` line:
- `API_KEYS` — your comma-separated keys (e.g. `"grp-a,grp-b,admin"`).
- `BASE_URL` — `https://<your-domain>`.
- `APP_TITLE` / `BRAND` — optional labels.

These are the only secrets, and they live only on the server (never committed).

## 3. Start it

```bash
supervisorctl reread
supervisorctl update
supervisorctl status sensor_board      # should show RUNNING
```

Logs: `~/sensor_board/errors.log` and `access.log`. Restart after a config change
with `supervisorctl restart sensor_board`.

## 4. Route your domain

```bash
uberspace web domain add <your-domain>     # then set the DNS records it prints
uberspace web backend set <your-domain> --http --port 8020
uberspace web backend list                 # confirm it points at :8020
```

DNS propagation + Let's Encrypt cert issuance happen automatically once the
records resolve.

## 5. Verify

```bash
curl -s https://<your-domain>/ | head
# send a test measurement with one of your keys:
curl -X POST https://<your-domain>/sensor/measurement \
  -H 'x-api-key: <one-of-your-keys>' -H 'content-type: application/json' \
  -d '{"project":"demo","name":"Test","device_uuid":"testdev","sensors":{"temperature":{"value":21.4,"unit":"C"}}}'
```

Then open `https://<your-domain>/dashboard/device/testdev`.

## Updating later

```bash
cd ~/sensor_board
git pull
.venv/bin/pip install -r requirements.txt   # only if deps changed
supervisorctl restart sensor_board
```

## Pointing devices at it

In each device's WiFiManager setup portal set:
- **API URL** → `https://<your-domain>/sensor/measurement`
- **API Key** → one of your `API_KEYS`

Existing firmware defaults (`DEFAULT_API_URL`) can also be updated to the new
domain so freshly-flashed devices use it out of the box.
