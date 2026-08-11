"""Shared test setup.

`app.config` and `app.policies` read the environment at import time, so every
setting a test depends on has to be in place before anything under `app.` is
imported. Doing it here means individual test modules can just import the app.

The IP-scoped limits are raised well above their production values. That is not
a way of dodging them: every request in the suite appears to come from the same
client, so the real numbers (30 requests/minute, 5 new devices/hour) would
throttle the suite itself rather than the behaviour under test. The limits are
covered directly in `test_limits.py`, against explicit policy objects. Every
other limit — payload size, sensor counts, per-device write rate — is left at
its production default so the tests keep checking the numbers the plan
specifies.
"""
import os
import tempfile

os.environ.setdefault("API_KEYS", "testkey,otherkey")
os.environ.setdefault("ROOT_PATH", "/dashboard")
os.environ.setdefault("DB_PATH", tempfile.mkstemp(suffix=".db")[1])
# Keep the background sweeper out of the test process; retention tests call
# purge_stale() directly with explicit parameters.
os.environ.setdefault("RETENTION_HOURS", "0")

for _policy in ("ANONYMOUS", "TRUSTED"):
    os.environ.setdefault(f"POLICY_{_policy}_REQUESTS_PER_MINUTE_PER_IP", "100000")
    os.environ.setdefault(f"POLICY_{_policy}_REQUESTS_PER_DAY_PER_IP", "100000")
    os.environ.setdefault(f"POLICY_{_policy}_NEW_DEVICES_PER_HOUR_PER_IP", "100000")
    os.environ.setdefault(f"POLICY_{_policy}_ACTIVE_DEVICES_PER_IP", "100000")
