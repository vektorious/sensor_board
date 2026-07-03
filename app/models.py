"""Database models.

The core table is deliberately generic and long-format: one row per
(device, sensor, timestamp) measurement. The dashboard discovers what to show
by querying the distinct sensor_types that exist for a device or project, so
new sensors — or entirely new kinds of data — appear automatically with no code
change.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Reading(SQLModel, table=True):
    __tablename__ = "readings"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Grouping keys. `project` is optional so devices that don't send one still
    # work (they simply aren't grouped into a project dashboard).
    project: Optional[str] = Field(default=None, index=True)
    device_uuid: str = Field(index=True)
    device_name: Optional[str] = Field(default=None)

    timestamp: datetime = Field(index=True)

    sensor_type: str = Field(index=True)
    value: Optional[float] = Field(default=None)
    unit: Optional[str] = Field(default=None)

    # Reserved for the future per-measurement plot-style flag. When the payload
    # carries sensors[x].plot = "gauge" | "line" | ..., it lands here and
    # overrides the default chart type. Unused today; harmless when null.
    plot: Optional[str] = Field(default=None)
