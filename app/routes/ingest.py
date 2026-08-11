"""Device ingestion endpoint.

    POST {INGEST_PATH}          (default /sensor/measurement)
    body:
    {
      "device_id": "greenhouse-01",       # required, public identifier
      "write_key": "…",                   # required for anonymous devices
      "project": "workshop-2026",         # optional, groups devices
      "name": "Basil #3",                 # optional, human label
      "sensors": {
        "temperature": {"value": 21.4, "unit": "C"},
        "battery_voltage": 3.97            # bare scalar also accepted
      }
    }

Two independent credentials, which is the heart of the model (plan §§1–3):

* A **write key** is chosen by the client and decides *who owns a device ID*.
  Whoever first writes to an unused ID with a write key claims it, and every
  later write to that device must present the same key — including writes that
  also carry a valid API key.
* An **API key** is issued by the operator and decides *which limit policy
  applies*. It makes a device persistent (exempt from the idle expiry) and can
  write to devices that have no write key. It never overrides one.

The practical consequence: a valid API key cannot take over someone's
write-key-protected device, and losing a write key is unrecoverable by design —
there is no reset path, only waiting for the device to expire.

Error responses carry a machine-readable `code`: 400 (validation), 401 (no
usable credential), 403 (wrong write key), 413 (payload too large), 429 (rate
limited), 503 (over the write budget or out of storage).
"""
import json
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import limits, metrics
from app.config import settings
from app.database import get_session
from app.models import Device, Reading
from app.policies import registry
from app.security import hash_api_key, hash_ip, hash_write_key, matches, matches_any, safe_log_value
from app.validation import ValidationError, validate

router = APIRouter()
logger = logging.getLogger("sensor_board.ingest")

_EXAMPLE = {
    "device_id": "workbench-sensor-01",
    "write_key": "keep-this-secret",
    "sensors": {"temperature": 22.4, "humidity": 51},
}


class Rejected(Exception):
    """An HTTP-level rejection, raised anywhere in the flow and caught once."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@router.post(settings.ingest_path)
async def ingest(request: Request):
    started = time.monotonic()
    # Filled in as far as the request gets before an exit, so even a rejected
    # request logs whatever was established about it (§20).
    log = {
        "device": "-",
        "sensors": 0,
        "bytes": 0,
        "policy": "-",
        "auth": "none",
        "action": "-",
    }

    def finish(status: int, payload: dict, code: str | None = None):
        # Retry-After is advice the client can act on, so it belongs in the
        # header where an HTTP library will find it, not only in the body.
        headers = {}
        retry_after = payload.get("retry_after")
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        logger.info(
            "ingest ts=%s device=%s sensors=%s bytes=%s policy=%s auth=%s "
            "action=%s status=%s duration_ms=%.1f%s",
            datetime.now(UTC).isoformat(),
            log["device"], log["sensors"], log["bytes"], log["policy"],
            log["auth"], log["action"], status,
            (time.monotonic() - started) * 1000,
            f" code={code}" if code else "",
        )
        return JSONResponse(status_code=status, content=payload, headers=headers)

    try:
        return await _handle(request, log, finish)
    except Rejected as exc:
        return finish(
            exc.status,
            {"error": exc.message, "code": exc.code, **exc.extra},
            code=exc.code,
        )
    except Exception:
        # Never leak a stack trace or a database message to the client (§16).
        logger.exception("ingest failed unexpectedly")
        return finish(
            500,
            {"error": "Internal error storing the measurement.", "code": "internal_error"},
            code="internal_error",
        )


async def _handle(request: Request, log: dict, finish):
    # --- credential: which policy applies? (§7) ---
    api_key = request.headers.get("x-api-key")
    if api_key is None:
        if not settings.allow_anonymous:
            raise Rejected(
                401,
                "anonymous_disabled",
                "This instance requires an API key.",
                hint="Provide your API key using the X-API-Key header.",
            )
        policy = registry.anonymous
        key_hash = None
    elif matches_any(api_key, settings.api_keys):
        policy = registry.for_api_key(api_key)
        key_hash = hash_api_key(api_key)
        log["auth"] = "api_key"
    else:
        raise Rejected(
            401,
            "invalid_api_key",
            "Invalid API key.",
            hint="Omit the X-API-Key header entirely to publish anonymously "
                 "with a write_key.",
        )
    log["policy"] = policy.name

    # --- per-IP request rate (§10) ---
    # Counted before any work is done, and for requests that go on to fail, so
    # a client cannot probe cheaply by sending garbage.
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hash_ip(client_ip)
    rate = limits.check_request_rate(policy, ip_hash)
    if not rate:
        raise _from_decision(429, rate)

    # --- body size (§9) ---
    body = await _read_body(request, policy, log)

    try:
        data = json.loads(body)
    except Exception:
        raise Rejected(
            400,
            "malformed_json",
            "Request body must be valid JSON.",
            example=_EXAMPLE,
        )

    try:
        payload = validate(data, policy)
    except ValidationError as exc:
        extra = {"example": _EXAMPLE}
        if exc.hint:
            extra["hint"] = exc.hint
        raise Rejected(400, exc.code, exc.message, **extra)

    log["device"] = safe_log_value(payload.device_id)
    log["sensors"] = len(payload.sensors)

    # --- storage and write budgets, before touching the database (§24) ---
    capacity = limits.check_db_capacity()
    if not capacity:
        raise _from_decision(503, capacity)
    budget = limits.check_write_budget(
        policy, key_hash or ip_hash, len(payload.sensors)
    )
    if not budget:
        raise _from_decision(503, budget)

    created = _store(payload, policy, key_hash, ip_hash, log)

    return finish(
        201 if created else 200,
        {
            "status": "created" if created else "ok",
            "device_id": payload.device_id,
            "stored": len(payload.sensors),
            "dashboard_url": f"{settings.root_path}/device/{payload.device_id}",
        },
    )


async def _read_body(request: Request, policy, log: dict) -> bytes:
    """Read the body, refusing oversized ones without buffering them (§27).

    Checked in two places on purpose. `Content-Length` is the cheap rejection,
    but it is a claim by the client — a chunked or lying request has none, or a
    false one — so the stream is also capped as it arrives. Neither alone is
    enough: the header check without the stream cap is bypassable, and the
    stream cap alone means reading a hostile body one chunk at a time before
    saying no.
    """
    limit = policy.max_payload_bytes

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                log["bytes"] = int(declared)
                raise _too_large(limit)
        except ValueError:
            raise Rejected(400, "malformed_request", "Invalid Content-Length header.")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            log["bytes"] = total
            raise _too_large(limit)
        chunks.append(chunk)

    log["bytes"] = total
    return b"".join(chunks)


def _too_large(limit: int) -> Rejected:
    return Rejected(
        413,
        "payload_too_large",
        f"Request body exceeds the {limit // 1024} KB limit.",
    )


def _from_decision(status: int, decision) -> Rejected:
    return Rejected(
        status,
        decision.code,
        decision.message,
        retry_after=decision.retry_after,
    )


def _store(payload, policy, key_hash: str | None, ip_hash: str, log: dict) -> bool:
    """Authorize, check quotas, then write — in that order, in three phases.

    The phases are separate on purpose. The limiter keeps its state in the same
    SQLite file as the data, on its own connection, so any limit check made
    *while* a write transaction is open would block on that transaction's write
    lock until `busy_timeout` gave up. Authorization and quota checks therefore
    finish before the writing transaction opens.

    Returns True when this request claimed the device ID. Every rejection path
    raises before the commit, so a refused write leaves no trace: no rows, no
    device, and — importantly for §3 — no advance of `last_seen_at`.
    """
    now = datetime.now(UTC)
    authenticated = key_hash is not None

    # Phase 1 — authorize against the current state (read only).
    with get_session() as session:
        device = session.exec(
            select(Device).where(Device.device_id == payload.device_id)
        ).first()
        creating = device is None
        if creating:
            # Raises if an anonymous claim arrives without a write key.
            _require_claim_credentials(payload, authenticated)
        else:
            _authorize_existing(device, payload, authenticated)

    # Phase 2 — quotas. After authorization, so an unauthorized client can
    # neither consume the owner's allowance nor learn about its sensors.
    if creating:
        creation = limits.check_device_creation(policy, ip_hash)
        if not creation:
            raise _from_decision(429, creation)
    write_rate = limits.check_device_write_rate(policy, payload.device_id)
    if not write_rate:
        raise _from_decision(429, write_rate)
    cardinality = limits.check_sensor_cardinality(
        policy, payload.device_id, payload.sensor_names
    )
    if not cardinality:
        raise _from_decision(429, cardinality)

    # Phase 3 — write. Nothing in here touches the limiter.
    created = _commit(payload, policy, key_hash, ip_hash, now, authenticated, creating)
    log["action"] = "created" if created else "updated"

    if created:
        limits.record_device_creation(ip_hash)
    return created


def _commit(payload, policy, key_hash, ip_hash, now, authenticated, creating) -> bool:
    """The single transaction that stores a device and its measurements (§5, §16)."""
    with get_session() as session:
        device = None
        if creating:
            device = _new_device(payload, policy, key_hash, ip_hash, now, authenticated)
            session.add(device)
            try:
                session.flush()
            except IntegrityError:
                # Another request claimed this ID since phase 1. The unique
                # index is what makes that safe: exactly one of the two wins,
                # and the loser is now writing to somebody else's device, so it
                # must satisfy that device's credentials like any other
                # existing-device write (§5).
                session.rollback()
                creating = False
                device = None

        if device is None:
            device = session.exec(
                select(Device).where(Device.device_id == payload.device_id)
            ).first()
            if device is None:
                # Claimed and then expired between phases — vanishingly rare,
                # and retrying is the honest answer.
                raise Rejected(409, "device_conflict", "Device creation conflicted; retry.")
            if not creating:
                _authorize_existing(device, payload, authenticated)

        if creating:
            metrics.bump(session, "devices_total")
        if payload.project is not None and not session.exec(
            select(Reading.id).where(Reading.project == payload.project).limit(1)
        ).first():
            # A project exists exactly when some reading names it, so it is new
            # when none does yet.
            metrics.bump(session, "projects_total")
        metrics.bump(session, "measurements_total", len(payload.sensors))

        for sensor in payload.sensors:
            session.add(
                Reading(
                    project=payload.project,
                    device_id=payload.device_id,
                    device_name=payload.name,
                    timestamp=now,
                    sensor_type=sensor.sensor_type,
                    value=sensor.value,
                    value_text=sensor.value_text,
                    value_type=sensor.value_type,
                    unit=sensor.unit,
                    plot=sensor.plot,
                    api_key_hash=key_hash,
                )
            )

        # Only a write that got this far counts as activity (§3, §13).
        device.last_seen_at = now
        session.add(device)
        session.commit()
    return creating


def _require_claim_credentials(payload, authenticated: bool) -> None:
    """Check that a first write is entitled to claim the ID (§2).

    An anonymous first write must carry a write key — without one there would
    be nothing to prove ownership on the next write, and the ID would be free
    for anyone to take over. An API-key request may omit it, creating the
    keyless persistent device that the pre-0.2 model used.
    """
    if payload.write_key is None and not authenticated:
        raise Rejected(
            401,
            "missing_write_key",
            "A write_key is required to claim a new device.",
            hint="Choose a strong random key, save it, and send it with every "
                 "write. It cannot be recovered or reset.",
        )


def _new_device(payload, policy, key_hash, ip_hash, now, authenticated: bool) -> Device:
    return Device(
        device_id=payload.device_id,
        write_key_hash=hash_write_key(payload.write_key) if payload.write_key else None,
        persistent=authenticated and policy.persistent_devices,
        policy=policy.name,
        created_by_key_hash=key_hash,
        created_from_ip_hash=ip_hash,
        created_at=now,
        last_seen_at=now,
    )


def _authorize_existing(device: Device, payload, authenticated: bool) -> None:
    """Decide whether this request may write to an already-claimed device (§3).

    The two branches are not interchangeable. A device *with* a write key is
    owned by whoever holds that key, and no API key substitutes for it. A device
    *without* one can only be a keyless persistent device, which any valid API
    key may write to — the beta simplification recorded in §7 and §23.
    """
    if device.write_key_hash:
        if payload.write_key is None:
            raise Rejected(
                401,
                "missing_write_key",
                "This device requires its write_key.",
                hint="Include the write_key chosen when the device was first "
                     "claimed. There is no recovery for a lost key.",
            )
        if not matches(payload.write_key, device.write_key_hash):
            # Deliberately identical for a wrong key, a malformed key, and a
            # nearly-correct one: the response must not narrow a guess (§3).
            raise Rejected(403, "invalid_write_key", "Invalid write key.")
        return

    if not authenticated:
        raise Rejected(
            401,
            "api_key_required",
            "This device ID is already claimed and requires an API key.",
            hint="Pick a different device_id, or provide the X-API-Key header.",
        )
