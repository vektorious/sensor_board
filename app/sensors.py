"""Sensor presentation registry.

This is an *optional override* layer, not a required list. The dashboard renders
whatever sensor_types exist in the data; this table just supplies nicer labels,
units, chart types, and gauge ranges for the ones you care about. Anything not
listed falls back to sensible defaults (a humanized label + a line chart), so
new/unknown sensors — and non-plant data — still render.

To style a sensor: add or edit an entry. That's the whole extensibility story.
"""
import re

# key -> {label, unit, chart, min, max}. All fields optional.
#   chart: "line" (time series) or "gauge" (single latest value dial)
#   min/max: gauge bounds (ignored for line charts)
# NOTE: gauges are currently disabled everywhere — every sensor renders as a
# line chart, with the latest value shown in the quick-overview strip instead.
# The gauge min/max bounds are kept here for when gauges are re-enabled.
SENSORS: dict[str, dict] = {
    "moisture_pct":    {"label": "Moisture",     "unit": "%",   "chart": "line", "min": 0,    "max": 100},
    "moisture_voltage":{"label": "Moisture (V)",  "unit": "V",   "chart": "line"},
    "battery_voltage": {"label": "Battery",       "unit": "V",   "chart": "line", "min": 3.0,  "max": 4.2},
    "wifi_rssi":       {"label": "Wi-Fi Signal",  "unit": "dBm", "chart": "line", "min": -100, "max": -30},
    "temperature":     {"label": "Temperature",   "unit": "°C",  "chart": "line"},
    "humidity":        {"label": "Humidity",      "unit": "%",   "chart": "line"},
    "pressure":        {"label": "Pressure",      "unit": "hPa", "chart": "line"},
    "lux":             {"label": "Illuminance",   "unit": "lx",  "chart": "line"},
    "ir":              {"label": "IR Light",      "unit": "",    "chart": "line"},
    "full":            {"label": "Full Spectrum", "unit": "",    "chart": "line"},
    "pump_duration":   {"label": "Pump Runtime",  "unit": "s",   "chart": "line"},
}

# Sensors listed here sort to the front (in this order); everything else follows
# alphabetically. Drives panel/overview ordering on device and project pages.
SENSOR_ORDER: list[str] = ["temperature", "humidity", "moisture_pct"]


def sort_key(sensor_type: str) -> tuple:
    try:
        return (0, SENSOR_ORDER.index(sensor_type))
    except ValueError:
        return (1, 0, sensor_type)


def humanize(key: str) -> str:
    """battery_voltage -> 'Battery Voltage'."""
    return re.sub(r"[_\-]+", " ", key).strip().title()


def meta_for(sensor_type: str, unit: str | None = None, plot: str | None = None) -> dict:
    """Resolve presentation metadata for a sensor_type.

    Precedence: registry entry > value stored on the reading (unit) > defaults.
    A non-null `plot` (future per-measurement flag) overrides the chart type.
    """
    meta = {
        "key": sensor_type,
        "label": humanize(sensor_type),
        "unit": "",
        "chart": "line",
        "min": None,
        "max": None,
    }
    meta.update(SENSORS.get(sensor_type, {}))
    # Fall back to the unit stored on the reading if the registry didn't set one.
    if not meta.get("unit") and unit:
        meta["unit"] = unit
    # A per-measurement plot flag wins over the registry default.
    if plot:
        meta["chart"] = plot
    return meta
