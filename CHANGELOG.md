# Changelog

All notable changes to Sensor Board are documented here. The project follows
[Semantic Versioning](https://semver.org/). While the major version is `0`, the
ingestion contract may change between minor releases — breaking changes are
called out explicitly.

## [0.2.0] — unreleased

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
  write-key and device-ID generators.
- Security headers (CSP, `X-Content-Type-Options`, `Referrer-Policy`,
  frame-ancestors) on every response.

### Changed

- Retention now expires *devices* by `last_seen_at` rather than inferring
  staleness from measurement timestamps, and skips persistent devices.
- The dashboard moved under `/dashboard`; `/` is the main page.

## [0.1.0] — 2026-07-15

Initial beta: API-key ingestion, auto-populating project and device dashboards,
and time-based retention.
