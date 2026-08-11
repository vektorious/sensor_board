"""Database models.

Two tables carry the whole app:

``devices`` records *ownership* — who may write to a device ID, whether the
device is temporary or persistent, and when it was last written to. It is the
single source of truth for expiry (§4 of the plan).

``readings`` records the data itself, deliberately generic and long-format: one
row per (device, sensor, timestamp) measurement. The dashboard discovers what to
show by querying the distinct sensor_types that exist for a device or project,
so new sensors — or entirely new kinds of data — appear automatically with no
code change.

A third table, ``platform_metrics``, holds lifetime counters that must survive
the deletion of the rows they counted (§19).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, ForeignKey, String
from sqlmodel import Field, SQLModel


class Device(SQLModel, table=True):
    """One claimed device ID and the credential that owns it.

    A device is either *temporary* (claimed anonymously with a client-supplied
    write key, deleted after ``RETENTION_HOURS`` of silence) or *persistent*
    (created by a request carrying a valid API key, never auto-deleted).

    ``write_key_hash`` and ``persistent`` are independent: a persistent device
    may also carry a write key, in which case that key is still required on
    every write. An API key grants persistence and a policy — it never
    substitutes for a write key.
    """

    __tablename__ = "devices"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Public identifier, chosen by the client. Unique: claiming it is what
    # "owning a device" means, and the unique constraint is what makes
    # concurrent creation race-safe (§5).
    device_id: str = Field(sa_column=Column(String, unique=True, nullable=False, index=True))

    # SHA-256 of the write key. Null for keyless persistent devices (the
    # pre-0.2 API-key model). Never the plaintext key, never logged.
    write_key_hash: Optional[str] = Field(default=None)

    # True = exempt from the idle-expiry sweep.
    persistent: bool = Field(default=False)

    # Name of the limit policy applied to this device (see app/policies.py).
    policy: str = Field(default="anonymous")

    # SHA-256 of the API key that created the device, when there was one. Used
    # for attribution and bulk deletion; the plan's `created_by_api_key_id`
    # becomes a hash here because API keys live in the environment, not in a
    # table (see plan §27, "API-key auth settings left untouched for the beta").
    created_by_key_hash: Optional[str] = Field(default=None)

    created_at: datetime = Field(index=True)
    # Advanced only by *successful* writes — this is what expiry is measured
    # against, so a rejected request can never keep a device alive (§3).
    last_seen_at: datetime = Field(index=True)


class Reading(SQLModel, table=True):
    __tablename__ = "readings"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Grouping keys. `project` is optional so devices that don't send one still
    # work (they simply aren't grouped into a project dashboard).
    project: Optional[str] = Field(default=None, index=True)

    # References devices.device_id. ON DELETE CASCADE means deleting a device
    # takes its measurements with it (§6); retention still deletes both
    # explicitly, so the cascade is a safety net, not the mechanism.
    device_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("devices.device_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    device_name: Optional[str] = Field(default=None)

    timestamp: datetime = Field(index=True)

    sensor_type: str = Field(index=True)

    # Numeric channel — the only one the charts read. Booleans land here as
    # 1.0/0.0 so they plot; strings and nulls leave it empty.
    value: Optional[float] = Field(default=None)
    # Short string measurements (§12). Null unless value_type == "text".
    value_text: Optional[str] = Field(default=None)
    # "number" | "bool" | "text" | "null" — what the client actually sent, so
    # the UI can render a boolean as on/off rather than 1/0.
    value_type: str = Field(default="number")

    unit: Optional[str] = Field(default=None)

    # Per-measurement chart-style override. When the payload carries
    # sensors[x].plot = "gauge" | "line" | ..., it lands here and overrides the
    # default chart type for that sensor.
    plot: Optional[str] = Field(default=None)

    # SHA-256 hash of the API key that submitted this measurement, when one was
    # used. Never the plaintext key. Used for usage attribution and bulk
    # deletion (DELETE FROM readings WHERE api_key_hash = ?).
    api_key_hash: Optional[str] = Field(default=None, index=True)


class PlatformMetric(SQLModel, table=True):
    """Lifetime counters (§19).

    Deliberately a key/value table rather than columns, so a new counter needs
    no migration. Values only ever increase; cleanup jobs must never touch
    them, which is why active counts are computed from the live tables instead
    of being stored here.
    """

    __tablename__ = "platform_metrics"

    metric_name: str = Field(primary_key=True)
    metric_value: int = Field(default=0)
    updated_at: datetime
