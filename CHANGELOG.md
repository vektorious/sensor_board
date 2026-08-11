# Changelog

All notable changes to Sensor Board are documented here. The project follows
[Semantic Versioning](https://semver.org/). While the major version is `0`, the
ingestion contract may change between minor releases — breaking changes are
called out explicitly.

## [0.2.1] — 2026-08-11

### Fixed

- **Schema setup crashed when gunicorn started more than one worker.** Each
  worker ran the migration concurrently; the one that lost the race died with
  `index ix_readings_device_id already exists`, and the service crash-looped
  until the schema happened to be complete. Schema changes now run under a
  cross-process file lock.
- **A racing worker could delete the parked pre-0.2 table**, which holds the
  only copy of the old rows. A second legacy table is now parked beside the
  first rather than over it.
- Growth-rate warnings are no longer computed from samples taken moments apart,
  which made every restart log an implausible figure (`2061 MB/h` on the first
  real deployment).

## [0.2.0] — 2026-08-11

The temporary-device release: anyone can publish sensor data without an account
or API key, claiming a device with a write key they choose themselves.

### Breaking

- **`device_uuid` is now `device_id`** in the ingestion payload, the JSON API,
  and dashboard URLs (`/device/{device_id}`). Devices sending `device_uuid`
  receive `400 invalid_field`. Update your firmware before upgrading.

### Added

- Anonymous ingestion: a device is claimed on its first write with a
  client-supplied `write_key`, and every later write must present that key.
- A `devices` table recording ownership, persistence, and activity, with
  measurements cascading on delete.
- API keys now grant *persistence* (exemption from the 48-hour expiry) and a
  named limit policy; they never override a device's write key.
- A full limit system — payload size, request rates per IP/device/key, device
  creation and active-device caps, sensor cardinality, a platform-wide write
  budget, and a hard database-size ceiling.
- Lifetime platform metrics that survive expiry (`devices_total`,
  `measurements_total`, `projects_total`).
- A public main page at `/` with documentation, live totals, and in-browser
  write-key and device-ID generators. Generated values are substituted straight
  into the curl, Python, and ESP32 examples, each of which has its own copy
  button.
- Security headers (CSP, `X-Content-Type-Options`, `Referrer-Policy`,
  frame-ancestors) on every response, and sanitising of attacker-controlled
  fields before they reach the log.
- Boolean, short-string, and null measurements alongside numbers. Booleans are
  stored as 1/0 so they still chart.
- `python -m app.admin` for status, forced sweeps, device inspection, and
  deleting one tester's data.

### Changed

- Retention now expires *devices* by `last_seen_at` rather than inferring
  staleness from measurement timestamps, and skips persistent devices. Only
  successful writes advance the clock, and expiry is also checked when a device
  ID is claimed rather than only on the hourly sweep.
- The dashboard moved under `/dashboard`; `/` is the main page. Set
  `ROOT_PATH=""` for the old layout, which drops the main page.
- `APP_TITLE`, `BRAND`, and `BASE_URL` now default to this project's own
  deployment (DIY Sensor, https://diy-sensor.org); override them for a
  differently branded instance.
- Clients can no longer send their own `timestamp`; the server's receipt time is
  authoritative, so data cannot be backdated past the retention window.

### Removed

- `MAX_PAYLOAD_BYTES` — payload size is now a per-policy limit
  (`POLICY_<NAME>_MAX_PAYLOAD_BYTES`), 16 KB for anonymous requests.
- Pre-0.2 `readings` rows are not migrated. The old table is renamed to
  `readings_pre_v0_2` on first start so it can be exported, then dropped by
  hand; the app starts with an empty `readings` table.

## [0.1.0] — 2026-07-15

Initial beta: API-key ingestion, auto-populating project and device dashboards,
and time-based retention.
