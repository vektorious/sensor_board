"""Limit policies (plan §§7–12, with the values from §24).

Every request is evaluated against exactly one policy. Anonymous requests get
the ``anonymous`` policy; a request carrying a valid API key gets whichever
policy that key is mapped to (``trusted`` unless configured otherwise).

The defaults here are the single source of truth for limit *values* in code —
they mirror plan §24, which explains the reasoning. All of them are deliberately
tight: the point is to start small and loosen once real beta traffic shows what
normal looks like.

Two rules constrain what a policy may do, both from §7:

* A policy **raises** limits, it never removes them. A non-positive override is
  rejected, so no policy can end up meaning "unlimited".
* A policy may not drop below the anonymous baseline — that would be a
  restriction dressed up as an exemption, and it makes the mental model
  ("API keys get more room") unreliable.

Overrides come from the environment as ``POLICY_<NAME>_<FIELD>``, e.g.
``POLICY_TRUSTED_WRITES_PER_MINUTE_PER_DEVICE=60``. Keys are mapped to policies
with ``API_KEY_POLICIES="<key>:<policy>,..."``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields, replace

log = logging.getLogger("sensor_board.policies")

# Field names a policy override may not raise above the baseline, because they
# are booleans or a retention window rather than a "how much" limit.
_NON_NUMERIC = {"name", "persistent_devices"}


@dataclass(frozen=True)
class Policy:
    name: str

    # --- retention (§4) ---
    # Idle hours before a *temporary* device is deleted. Persistent devices
    # ignore this entirely.
    retention_hours: int
    # Whether devices created under this policy are exempt from expiry.
    persistent_devices: bool

    # --- payload (§9, §12) ---
    max_payload_bytes: int
    max_sensor_fields_per_request: int
    max_sensors_per_device: int
    max_device_id_length: int
    max_sensor_name_length: int
    max_string_value_length: int
    max_nesting_depth: int

    # --- request rates (§10) ---
    requests_per_minute_per_ip: int
    requests_per_day_per_ip: int
    writes_per_minute_per_device: int

    # --- device creation (§11) ---
    new_devices_per_hour_per_ip: int
    active_devices_per_ip: int

    # --- write budget sub-allocation (§24) ---
    # Measurement rows per second this credential may sustain, and how large a
    # burst it may bank. Sits underneath the platform-wide budget in limits.py.
    writes_per_second: int
    write_burst: int


def _default_retention_hours() -> int:
    """RETENTION_HOURS is the pre-existing name for the anonymous window.

    Kept as the default so deployments that already set it don't have to learn
    the POLICY_ANONYMOUS_RETENTION_HOURS spelling. A non-positive value there
    means "disable retention", which is a `settings` concern, not a policy one,
    so it falls back to the plan's 48h here.
    """
    try:
        configured = int(os.getenv("RETENTION_HOURS", "48"))
    except ValueError:
        return 48
    return configured if configured > 0 else 48


# Baseline for unauthenticated traffic — the §24 table verbatim.
ANONYMOUS = Policy(
    name="anonymous",
    retention_hours=_default_retention_hours(),
    persistent_devices=False,
    max_payload_bytes=16 * 1024,
    max_sensor_fields_per_request=16,
    max_sensors_per_device=16,
    max_device_id_length=64,
    max_sensor_name_length=64,
    max_string_value_length=64,
    max_nesting_depth=2,
    requests_per_minute_per_ip=30,
    requests_per_day_per_ip=1_000,
    writes_per_minute_per_device=12,
    new_devices_per_hour_per_ip=5,
    active_devices_per_ip=10,
    writes_per_second=3,
    write_burst=300,
)

# Default for API-key traffic: more room everywhere, and devices never expire.
# Still finite — §7 is explicit that trusted does not mean unlimited.
TRUSTED = replace(
    ANONYMOUS,
    name="trusted",
    persistent_devices=True,
    max_payload_bytes=64 * 1024,
    max_sensor_fields_per_request=64,
    max_sensors_per_device=64,
    requests_per_minute_per_ip=300,
    requests_per_day_per_ip=50_000,
    writes_per_minute_per_device=60,
    new_devices_per_hour_per_ip=100,
    active_devices_per_ip=500,
    writes_per_second=3,
    write_burst=300,
)

_BUILTINS = {p.name: p for p in (ANONYMOUS, TRUSTED)}


def _apply_env_overrides(policy: Policy) -> Policy:
    """Read POLICY_<NAME>_<FIELD> for every field and validate the result."""
    prefix = f"POLICY_{policy.name.upper()}_"
    changes: dict[str, object] = {}

    for f in fields(Policy):
        if f.name == "name":
            continue
        raw = os.getenv(prefix + f.name.upper())
        if raw is None:
            continue
        raw = raw.strip()
        if f.type == "bool" or f.name in _NON_NUMERIC:
            changes[f.name] = raw.lower() in ("1", "true", "yes", "on")
            continue
        try:
            value = int(raw)
        except ValueError:
            log.warning("ignoring %s%s: %r is not an integer", prefix, f.name.upper(), raw)
            continue
        if value <= 0:
            # "Unlimited" is not an option a policy may express (§7).
            log.warning(
                "ignoring %s%s=%s: limits must stay positive", prefix, f.name.upper(), value
            )
            continue
        changes[f.name] = value

    updated = replace(policy, **changes) if changes else policy
    return _not_below_baseline(updated)


def _not_below_baseline(policy: Policy) -> Policy:
    """Raise any numeric limit that config pushed below the anonymous baseline."""
    if policy.name == ANONYMOUS.name:
        return policy
    lifted = {}
    for f in fields(Policy):
        if f.name in _NON_NUMERIC:
            continue
        mine = getattr(policy, f.name)
        floor = getattr(ANONYMOUS, f.name)
        if isinstance(mine, int) and mine < floor:
            log.warning(
                "policy %s: %s=%s is below the anonymous baseline; using %s",
                policy.name, f.name, mine, floor,
            )
            lifted[f.name] = floor
    return replace(policy, **lifted) if lifted else policy


def _parse_key_map() -> dict[str, str]:
    """Parse API_KEY_POLICIES="<key>:<policy>,<key>:<policy>" into a mapping."""
    raw = os.getenv("API_KEY_POLICIES", "")
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, policy_name = pair.partition(":")
        if not key.strip() or not policy_name.strip():
            log.warning("ignoring malformed API_KEY_POLICIES entry")
            continue
        mapping[key.strip()] = policy_name.strip()
    return mapping


class PolicyRegistry:
    """All configured policies plus the API-key -> policy mapping."""

    def __init__(self) -> None:
        self._key_map = _parse_key_map()
        # Any policy named by the key map that isn't built in starts life as a
        # copy of `trusted`, then takes its own POLICY_<NAME>_* overrides. That
        # is what makes "a policy per workshop" possible without a table.
        names = set(_BUILTINS) | set(self._key_map.values())
        self._policies = {
            name: _apply_env_overrides(
                _BUILTINS.get(name) or replace(TRUSTED, name=name)
            )
            for name in names
        }

    @property
    def anonymous(self) -> Policy:
        return self._policies[ANONYMOUS.name]

    def by_name(self, name: str) -> Policy:
        return self._policies.get(name) or self._policies[TRUSTED.name]

    def for_api_key(self, api_key: str) -> Policy:
        """Policy for a *already validated* API key."""
        return self.by_name(self._key_map.get(api_key, TRUSTED.name))

    def names(self) -> list[str]:
        return sorted(self._policies)


registry = PolicyRegistry()
