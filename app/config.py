"""Runtime configuration, driven entirely by environment variables.

Every deployment-specific value is overridable so the same code can run as a
plant dashboard, a weather dashboard, or anything else. Copy .env.example to
.env (or export the vars in the service) and change what you need.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Load a .env file from the project root if present, so secrets (API keys with
# commas, etc.) live in a file the app parses itself — not in the supervisord
# service file, whose `environment=` line splits on commas. Existing real env
# vars are NOT overridden.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR.parent / ".env")
except ImportError:
    pass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_set(name: str) -> set[str]:
    """Parse a comma-separated env var into a set of trimmed, non-empty values."""
    return {v.strip() for v in os.getenv(name, "").split(",") if v.strip()}


class Settings:
    def __init__(self) -> None:
        # Branding / public identity ------------------------------------
        self.app_title = _env("APP_TITLE", "Sensor Board")
        self.brand = _env("BRAND", self.app_title)
        # Public base URL (no trailing slash), used to build shareable links.
        self.base_url = _env("BASE_URL", "").rstrip("/")

        # Path prefix the dashboard UI is mounted under (no trailing slash).
        # Defaults to /dashboard so that "/" is free for the main page (plan
        # §26). Set it to "" to put the dashboard back at the domain root — the
        # main page is then not served at all, since the two would collide.
        self.root_path = _env("ROOT_PATH", "/dashboard").rstrip("/")

        # Where users should report problems. Shown on the main page; hidden
        # when empty.
        self.feedback_url = _env(
            "FEEDBACK_URL", "https://github.com/vektorious/sensor_board/issues"
        )

        # Ingestion -----------------------------------------------------
        # Accept one or many keys. API_KEYS (comma-separated) takes precedence;
        # otherwise fall back to a single API_KEY. Devices send one of these as
        # the x-api-key header. Multiple keys let you issue a key per workshop /
        # group and revoke one without disturbing the others.
        raw_keys = os.getenv("API_KEYS")
        if raw_keys:
            self.api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        else:
            self.api_keys = [_env("API_KEY", "change-me")]
        # Path devices POST to. Generic by default; override for compat.
        self.ingest_path = _env("INGEST_PATH", "/sensor/measurement")
        # Whether unauthenticated (write-key) ingestion is accepted at all.
        # Turn it off to run a private, API-key-only instance.
        self.allow_anonymous = _env_bool("ALLOW_ANONYMOUS", True)

        # Global write budget (plan §24) ---------------------------------
        # A platform-wide token bucket over accepted measurement rows, on top
        # of the per-policy budgets. This bounds the write *flow* no matter who
        # is writing or how many policies exist. Over budget -> 503.
        self.global_writes_per_second = int(_env("GLOBAL_WRITES_PER_SECOND", "10"))
        self.global_write_burst = int(_env("GLOBAL_WRITE_BURST", "1000"))

        # Database size guards (plan §24, §27) ---------------------------
        # WARN size is early warning; MAX is enforcement — ingestion returns
        # 503 above it. Both assume a 10 GB host quota; scale to the real one.
        self.db_size_warn_bytes = int(_env("DB_SIZE_WARN_BYTES", str(1024**3)))
        self.db_size_max_bytes = int(_env("DB_SIZE_MAX_BYTES", str(2 * 1024**3)))
        # Growth faster than this logs a WARNING on each retention sweep.
        self.db_growth_warn_mb_per_hour = float(
            _env("DB_GROWTH_WARN_MB_PER_HOUR", "50")
        )

        # Storage -------------------------------------------------------
        db_path = _env("DB_PATH", str(BASE_DIR / "data" / "sensors.db"))
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.database_url = f"sqlite:///{db_path}"

        # Retention -----------------------------------------------------
        # Devices with no reading newer than this many hours are purged (and a
        # project disappears once all of its devices are gone). Set to 0 (or
        # negative) to disable automatic deletion entirely.
        self.retention_hours = int(_env("RETENTION_HOURS", "48"))
        # How often the background sweeper runs, in hours.
        self.retention_sweep_interval_hours = float(
            _env("RETENTION_SWEEP_INTERVAL_HOURS", "1")
        )
        # Exceptions to the retention rule: device UUIDs and/or project names
        # that are never auto-deleted, however long they stay silent. Both are
        # comma-separated. A device is spared if its UUID is exempt OR its
        # (latest) project is exempt.
        self.retention_exempt_devices = _env_set("RETENTION_EXEMPT_DEVICES")
        self.retention_exempt_projects = _env_set("RETENTION_EXEMPT_PROJECTS")

        # Frontend ------------------------------------------------------
        # Where the ECharts library is served from. Vendored by default so the
        # app has no external runtime dependency; point at a CDN if you prefer.
        self.echarts_src = _env(
            "ECHARTS_SRC", f"{self.root_path}/static/js/echarts.min.js"
        )
        # Default lookback window for time-series charts, in hours.
        self.default_range_hours = int(_env("DEFAULT_RANGE_HOURS", "168"))


settings = Settings()
