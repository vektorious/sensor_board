"""Rate and quota enforcement, backed by SQLite (plan §§9–11, §24).

State lives in the database rather than in process memory because the app runs
under gunicorn with more than one worker: in-process counters would each see
roughly half the traffic, so every configured limit would silently become
double what it says. At the planned write rate (10 rows/sec platform-wide) the
extra database traffic is negligible next to the measurement writes themselves.

Two shapes of limit:

*Fixed windows* (`rate_counters`) answer "how many events in the current
minute/hour/day?". Cheap, and the worst case — a client aligning its burst with
a window boundary and getting 2x for one instant — is irrelevant at these
volumes.

*Token buckets* (`rate_buckets`) answer "may this batch of N rows through right
now?" for the write budgets, where a burst allowance is the whole point.

Every check returns a `Decision`; the caller decides the HTTP status. Nothing in
here raises, and nothing in here logs a key — buckets are keyed on hashes and
truncated identifiers.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, func, select

from app.config import settings
from app.database import db_size_bytes, engine, get_session
from app.models import Device, Reading

log = logging.getLogger("sensor_board.limits")

# Shortest interval that yields a meaningful growth rate (see observe_db_size).
MIN_GROWTH_SAMPLE_HOURS = 0.1  # 6 minutes


@dataclass(frozen=True)
class Decision:
    """Outcome of one limit check."""

    allowed: bool
    code: str = ""
    message: str = ""
    # Seconds the client should wait, for the Retry-After header.
    retry_after: int = 0

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Decision(allowed=True)


def _denied(code: str, message: str, retry_after: int) -> Decision:
    return Decision(allowed=False, code=code, message=message, retry_after=max(1, retry_after))


# --- schema -----------------------------------------------------------------
# Plain SQL rather than SQLModel tables: these are ephemeral bookkeeping rows
# with a composite primary key and upsert-heavy access, and keeping them out of
# the ORM metadata makes it obvious they are not application data.

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS rate_counters (
        scope       TEXT NOT NULL,
        subject     TEXT NOT NULL,
        window_start INTEGER NOT NULL,
        count       INTEGER NOT NULL,
        PRIMARY KEY (scope, subject, window_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_buckets (
        scope       TEXT NOT NULL,
        subject     TEXT NOT NULL,
        tokens      REAL NOT NULL,
        updated_at  REAL NOT NULL,
        PRIMARY KEY (scope, subject)
    )
    """,
    # Sweeping old windows is a range scan over window_start.
    "CREATE INDEX IF NOT EXISTS ix_rate_counters_window ON rate_counters (window_start)",
)


def init_limits() -> None:
    with engine.connect() as conn:
        for statement in _SCHEMA:
            conn.exec_driver_sql(statement)
        conn.commit()


# --- fixed-window counters --------------------------------------------------


def _window_start(seconds: int, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now // seconds) * seconds


def check_window(
    scope: str,
    subject: str,
    limit: int,
    window_seconds: int,
    *,
    cost: int = 1,
    record: bool = True,
) -> Decision:
    """Allow `limit` events per `window_seconds` for (scope, subject).

    With `record=False` the current count is tested but not incremented — used
    where the caller must know the answer before deciding whether the event
    actually happened.
    """
    start = _window_start(window_seconds)
    with get_session() as s:
        current = s.execute(
            text(
                "SELECT count FROM rate_counters "
                "WHERE scope = :scope AND subject = :subject AND window_start = :start"
            ),
            {"scope": scope, "subject": subject, "start": start},
        ).scalar()
        current = current or 0
        if current + cost > limit:
            return _denied(
                f"{scope}_rate_limited",
                f"Rate limit exceeded: {limit} per {_humanize(window_seconds)}.",
                retry_after=int(start + window_seconds - time.time()),
            )
        if record:
            _increment(s, scope, subject, start, cost)
            s.commit()
    return ALLOWED


def record_window(scope: str, subject: str, window_seconds: int, cost: int = 1) -> None:
    """Count an event without testing a limit — the deferred half of a
    `check_window(..., record=False)` pair."""
    with get_session() as s:
        _increment(s, scope, subject, _window_start(window_seconds), cost)
        s.commit()


def _increment(session: Session, scope: str, subject: str, start: int, cost: int) -> None:
    session.execute(
        text(
            "INSERT INTO rate_counters (scope, subject, window_start, count) "
            "VALUES (:scope, :subject, :start, :cost) "
            "ON CONFLICT(scope, subject, window_start) DO UPDATE SET "
            "count = rate_counters.count + excluded.count"
        ),
        {"scope": scope, "subject": subject, "start": start, "cost": cost},
    )


def _humanize(seconds: int) -> str:
    return {60: "minute", 3600: "hour", 86400: "day"}.get(seconds, f"{seconds}s")


# --- token buckets ----------------------------------------------------------


def check_bucket(scope: str, subject: str, rate_per_sec: float, burst: int, cost: int) -> Decision:
    """Spend `cost` tokens from a bucket refilling at `rate_per_sec`.

    Tokens are only deducted when the whole cost fits, so a single oversized
    request cannot half-drain the budget and still be rejected.
    """
    if cost <= 0:
        return ALLOWED
    now = time.time()
    with get_session() as s:
        row = s.execute(
            text(
                "SELECT tokens, updated_at FROM rate_buckets "
                "WHERE scope = :scope AND subject = :subject"
            ),
            {"scope": scope, "subject": subject},
        ).first()

        if row is None:
            tokens = float(burst)
        else:
            tokens, updated_at = row
            tokens = min(float(burst), tokens + (now - updated_at) * rate_per_sec)

        if cost > burst:
            # Nothing will ever admit this batch; say so instead of stalling.
            return _denied(
                f"{scope}_over_budget",
                f"Request exceeds the {burst}-measurement burst allowance.",
                retry_after=1,
            )
        if tokens < cost:
            return _denied(
                f"{scope}_over_budget",
                "Write budget exhausted; the platform is shedding load.",
                retry_after=int((cost - tokens) / rate_per_sec) + 1,
            )

        s.execute(
            text(
                "INSERT INTO rate_buckets (scope, subject, tokens, updated_at) "
                "VALUES (:scope, :subject, :tokens, :now) "
                "ON CONFLICT(scope, subject) DO UPDATE SET "
                "tokens = excluded.tokens, updated_at = excluded.updated_at"
            ),
            {"scope": scope, "subject": subject, "tokens": tokens - cost, "now": now},
        )
        s.commit()
    return ALLOWED


# --- composite checks used by the ingest endpoint ----------------------------


def check_request_rate(policy, client_ip: str) -> Decision:
    """Per-IP request limits (§10). Counted for failed requests too.

    IP is an abuse-control signal, not identity: it is shared behind NAT and
    changes underneath mobile clients, so the per-IP numbers are the loosest
    of the limits and nothing about ownership depends on them.
    """
    minute = check_window(
        "ip_minute", client_ip, policy.requests_per_minute_per_ip, 60
    )
    if not minute:
        return minute
    return check_window("ip_day", client_ip, policy.requests_per_day_per_ip, 86_400)


def check_device_write_rate(policy, device_id: str) -> Decision:
    """Per-device successful-write limit (§10)."""
    return check_window(
        "device_minute", device_id, policy.writes_per_minute_per_device, 60
    )


def check_device_creation(policy, ip_hash: str) -> Decision:
    """Creation-rate and active-device caps for one IP (§11).

    Checked without recording: the caller records the creation only once the
    device row actually lands, so a request that fails validation afterwards
    doesn't consume the IP's hourly allowance.
    """
    hourly = check_window(
        "ip_new_devices", ip_hash, policy.new_devices_per_hour_per_ip, 3600, record=False
    )
    if not hourly:
        return _denied(
            "device_creation_limited",
            f"Too many new devices: {policy.new_devices_per_hour_per_ip} per hour per IP.",
            hourly.retry_after,
        )

    with get_session() as session:
        active = session.exec(
            select(func.count(Device.id)).where(Device.created_from_ip_hash == ip_hash)
        ).one()
    if (active or 0) >= policy.active_devices_per_ip:
        return _denied(
            "active_device_limit",
            f"Too many active devices for this IP ({policy.active_devices_per_ip}); "
            "wait for one to expire.",
            3600,
        )
    return ALLOWED


def record_device_creation(ip_hash: str) -> None:
    """Consume one slot from the IP's hourly device-creation allowance."""
    record_window("ip_new_devices", ip_hash, 3600)


def check_sensor_cardinality(policy, device_id: str, sensor_names: set[str]) -> Decision:
    """Cap the number of *distinct* sensors one device may accumulate (§12).

    Without this, a single accepted request can invent a new sensor name every
    time and grow the device's series set without bound.
    """
    with get_session() as session:
        existing = set(
            session.exec(
                select(Reading.sensor_type)
                .where(Reading.device_id == device_id)
                .distinct()
            ).all()
        )
    total = len(existing | sensor_names)
    if total > policy.max_sensors_per_device:
        return _denied(
            "too_many_sensors",
            f"This device would exceed {policy.max_sensors_per_device} distinct "
            f"sensors (it already has {len(existing)}).",
            60,
        )
    return ALLOWED


def check_write_budget(policy, subject: str, rows: int) -> Decision:
    """Platform-wide budget, then this credential's slice of it (§24).

    Order matters: the global bucket is checked first so that when the platform
    is saturated everyone is shed evenly, rather than whoever happens to have
    per-key tokens left winning the race.
    """
    global_check = check_bucket(
        "global_writes",
        "-",
        settings.global_writes_per_second,
        settings.global_write_burst,
        rows,
    )
    if not global_check:
        return global_check
    return check_bucket(
        "key_writes", subject, policy.writes_per_second, policy.write_burst, rows
    )


def check_db_capacity() -> Decision:
    """Hard storage ceiling (§24, §27).

    A stock limit behind the flow limit: if the write budget is misconfigured or
    the per-row size estimate is wrong, this still stops the database before it
    fills the host's quota.
    """
    size = db_size_bytes()
    if size >= settings.db_size_max_bytes:
        log.error(
            "database at %.2f GB — at or above the %.2f GB ceiling; rejecting writes",
            size / 1024**3,
            settings.db_size_max_bytes / 1024**3,
        )
        return _denied(
            "storage_full",
            "The platform is at its storage ceiling and is not accepting new "
            "measurements. Try again later.",
            300,
        )
    return ALLOWED


# --- housekeeping -----------------------------------------------------------


def sweep_expired_windows(older_than_seconds: int = 2 * 86_400) -> int:
    """Delete counter rows whose window closed long ago.

    Without this the table grows one row per (IP, window) forever. Buckets are
    left alone: there is at most one row per subject and it gets reused.
    """
    cutoff = int(time.time()) - older_than_seconds
    with get_session() as s:
        result = s.execute(
            text("DELETE FROM rate_counters WHERE window_start < :cutoff"),
            {"cutoff": cutoff},
        )
        s.commit()
    return result.rowcount or 0


def observe_db_size() -> dict:
    """Sample the database size and warn on absolute size or growth rate (§27).

    Early warning, not enforcement — `check_db_capacity` does the enforcing.
    The previous sample is persisted in `platform_metrics` so the growth rate
    survives a restart.
    """
    from app import metrics

    size = db_size_bytes()
    now = datetime.now(UTC)
    previous = metrics.get_with_time("db_size_bytes")

    report = {"size_bytes": size, "mb_per_hour": None, "warnings": []}

    if previous is not None:
        last_size, last_at = previous
        elapsed_hours = (now - last_at).total_seconds() / 3600
        # Below a few minutes the rate is meaningless: a restart samples twice
        # seconds apart, and dividing an ordinary WAL checkpoint by that gives
        # thousands of MB/h. Skipping the estimate is better than crying wolf
        # every time the service restarts.
        if elapsed_hours >= MIN_GROWTH_SAMPLE_HOURS:
            rate = (size - last_size) / 1024**2 / elapsed_hours
            report["mb_per_hour"] = round(rate, 2)
            if rate > settings.db_growth_warn_mb_per_hour:
                message = (
                    f"database growing at {rate:.1f} MB/h, above the "
                    f"{settings.db_growth_warn_mb_per_hour:.0f} MB/h warning threshold"
                )
                report["warnings"].append(message)
                log.warning(message)

    if size >= settings.db_size_warn_bytes:
        message = (
            f"database at {size / 1024**3:.2f} GB, above the "
            f"{settings.db_size_warn_bytes / 1024**3:.2f} GB warning size"
        )
        report["warnings"].append(message)
        log.warning(message)

    with get_session() as s:
        metrics.set_gauge(s, "db_size_bytes", size)
        s.commit()
    return report
