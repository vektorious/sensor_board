"""The public main page and the dashboard's expiry surfaces (plan §17, §26).

The page is asserted on content rather than markup: what matters is that a
newcomer can find the workflow, the limits, and the generators, and that the
warnings the plan requires are actually on the page.
"""
import itertools

from fastapi.testclient import TestClient
from sqlmodel import select

from app import metrics
from app.database import get_session
from app.main import app
from app.models import Device

client = TestClient(app)
URL = "/sensor/measurement"

_ids = itertools.count()


def device_id(label: str) -> str:
    return f"home-{label}-{next(_ids)}"


def page() -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.text


# --- what the main page must say (§26) --------------------------------------


def test_main_page_is_served_at_the_root():
    assert client.get("/").status_code == 200


def test_dashboard_keeps_its_own_prefix():
    assert client.get("/dashboard/").status_code == 200


def test_explains_the_workflow():
    body = page()
    for step in ("Microcontroller", "JSON", "API", "Dashboard"):
        assert step in body


def test_warns_that_this_is_a_beta_with_public_dashboards():
    body = page()
    assert "Beta" in body
    assert "readable by anyone" in body


def test_shows_live_platform_totals():
    d = device_id("totals")
    client.post(URL, json={"device_id": d, "write_key": "k", "sensors": {"t": 1}})
    total = metrics.lifetime()["measurements_total"]
    # Rendered server-side with thousands separators, so it is readable without
    # JavaScript and matches what the metrics module reports.
    assert f"{total:,}" in page()


def test_links_to_the_dashboard_and_to_feedback():
    body = page()
    assert 'href="/dashboard"' in body
    assert "Report a problem" in body


def test_documents_the_retention_window_and_the_limits():
    from app.policies import registry

    body = page()
    policy = registry.anonymous
    assert f"{policy.retention_hours} hours" in body
    assert "16 KB" in body            # payload cap
    # The published numbers must be the ones being enforced, not a copy that
    # can drift — so they are asserted against the live policy.
    assert str(policy.requests_per_minute_per_ip) in body
    assert str(policy.max_sensor_fields_per_request) in body


def test_documents_the_error_codes():
    body = page()
    for code in ("invalid_write_key", "payload_too_large", "storage_full"):
        assert code in body


def test_shows_a_copyable_curl_example_with_the_real_field_names():
    body = page()
    assert "curl -X POST" in body
    assert "device_id" in body
    assert "write_key" in body
    assert "/sensor/measurement" in body


def test_shows_python_and_microcontroller_examples():
    body = page()
    assert "requests.post" in body
    assert "HTTPClient" in body       # ESP32/Arduino


def test_explains_how_to_handle_keys():
    body = page()
    assert "over HTTPS" in body
    assert "out of your repository" in body
    assert "X-API-Key" in body


# --- generators (§26) -------------------------------------------------------


def test_offers_both_generators_with_copy_buttons():
    body = page()
    assert 'data-generator="write-key"' in body
    assert 'data-generator="device-id"' in body
    assert body.count("data-action='copy'") + body.count('data-action="copy"') >= 2


def test_generators_run_in_the_browser_and_say_so():
    body = page()
    assert "crypto.getRandomValues" in body
    assert "generated locally" in body.lower() or "in your browser" in body


def test_warns_that_a_lost_write_key_cannot_be_recovered():
    body = page()
    assert "Save it now" in body
    assert "recovered" in body


def test_says_the_device_id_is_public_not_secret():
    assert "Public, like a username" in page()


def test_examples_carry_slots_the_generators_fill_in():
    body = page()
    # Each example marks where the generated values go, so a reader copies a
    # snippet that already works instead of editing two placeholders in three
    # places. Three code blocks plus the dashboard URL reference each field.
    assert body.count('data-field="device-id"') >= 4
    assert body.count('data-field="write-key"') >= 3
    assert "YOUR-DEVICE-ID" in body      # the un-substituted default
    assert "YOUR-WRITE-KEY" in body


def test_offers_a_single_button_that_fills_everything():
    assert "data-action='generate-all'" in page() or 'data-action="generate-all"' in page()


def test_every_example_has_its_own_copy_button():
    body = page()
    assert body.count('data-action="copy-code"') == 3   # curl, Python, Arduino


def test_warns_once_the_examples_contain_a_real_key():
    body = page()
    assert "These examples now contain your write key" in body
    # Hidden until a key has actually been generated.
    assert 'id="examples-filled" hidden' in body


def test_limits_and_errors_come_after_the_quick_start():
    body = page()
    # Reference material belongs below the thing a first-time reader came for.
    assert body.index("Send a measurement") < body.index(">Limits<")
    assert body.index(">Limits<") < body.index(">Errors<")


def generator_code() -> str:
    """The generator script with comments stripped.

    The comments discuss the APIs these tests forbid — they explain *why* the
    key is never written to localStorage — so matching against the raw file
    would fail on its own documentation.
    """
    import re

    script = client.get("/dashboard/static/js/generators.js").text
    assert script  # served at all
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", script, flags=re.MULTILINE)


def test_substitution_never_builds_markup():
    # A generated value is inserted as text, so it can never become an element.
    code = generator_code()
    assert "innerHTML" not in code
    assert "textContent" in code


def test_generated_values_are_not_persisted():
    # Storing a write key would leave a secret on the machine long after the
    # tab closed, to save one click.
    code = generator_code()
    assert "localStorage" not in code
    assert "sessionStorage" not in code
    assert "document.cookie" not in code


def test_generator_script_is_served_and_self_contained():
    assert client.get("/dashboard/static/js/generators.js").status_code == 200
    code = generator_code()
    assert "crypto.getRandomValues" in code
    # No network calls: a generated key must not leave the browser.
    assert "fetch(" not in code
    assert "XMLHttpRequest" not in code


def test_main_page_never_carries_a_secret():
    body = page()
    with get_session() as s:
        hashes = [d.write_key_hash for d in s.exec(select(Device)).all() if d.write_key_hash]
    assert all(h not in body for h in hashes)


# --- device page expiry surfaces (§17) --------------------------------------


def test_device_page_shows_when_a_temporary_device_expires(monkeypatch):
    from app.config import settings

    # Retention is off in the test environment (conftest), which is also how a
    # deployment switches expiry off — so the countdown only exists with it on.
    monkeypatch.setattr(settings, "retention_hours", 48)
    d = device_id("expiry")
    client.post(URL, json={"device_id": d, "write_key": "k", "sensors": {"t": 1}})
    body = client.get(f"/dashboard/device/{d}").text
    assert "expires" in body
    assert "h left" in body


def test_device_page_shows_no_expiry_for_a_persistent_device(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "retention_hours", 48)
    d = device_id("forever")
    client.post(URL, json={"device_id": d, "sensors": {"t": 1}}, headers={"x-api-key": "testkey"})
    body = client.get(f"/dashboard/device/{d}").text
    assert "h left" not in body


def test_no_countdown_when_retention_is_disabled():
    d = device_id("noexpiry")
    client.post(URL, json={"device_id": d, "write_key": "k", "sensors": {"t": 1}})
    assert "h left" not in client.get(f"/dashboard/device/{d}").text


def test_unknown_or_expired_device_page_says_so_clearly():
    body = client.get("/dashboard/device/never-existed").text
    assert "No such device" in body
    assert "expired" in body


def test_device_page_never_exposes_the_write_key():
    d = device_id("secret")
    client.post(URL, json={"device_id": d, "write_key": "top-secret-key", "sensors": {"t": 1}})
    body = client.get(f"/dashboard/device/{d}").text
    assert "top-secret-key" not in body
    assert "write_key" not in body


def test_dashboard_overview_states_that_dashboards_are_public():
    assert "Every dashboard here is public" in client.get("/dashboard/").text
