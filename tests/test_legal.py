"""Optional Impressum / privacy pages (app/legal.py).

The pages are published per instance and are not in the repository, so the
behaviour worth pinning down is what happens on an instance that has *not*
published them: the routes must 404 and the footer links must be absent, so a
fresh clone is a working site rather than one with two dead links in the footer.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture
def legal_dir(tmp_path, monkeypatch):
    """Point the app at an empty directory we control."""
    monkeypatch.setattr(settings, "legal_dir", str(tmp_path))
    return tmp_path


def footers(path: str) -> list[str]:
    html = client.get(path).text
    return [
        " ".join(re.sub(r"<[^>]+>", " ", f).split())
        for f in re.findall(r"<footer.*?</footer>", html, re.S)
    ]


def test_pages_404_when_not_published(legal_dir):
    assert client.get("/impressum").status_code == 404
    assert client.get("/privacy").status_code == 404


def test_no_footer_links_when_not_published(legal_dir):
    assert footers("/dashboard/") == []
    assert not any("Impressum" in f for f in footers("/"))


def test_published_page_is_served_and_linked(legal_dir):
    (legal_dir / "impressum.html").write_text("<p>Anbieter: Someone</p>", encoding="utf-8")

    resp = client.get("/impressum")
    assert resp.status_code == 200
    assert "Anbieter: Someone" in resp.text

    # Linked from both the dashboard and the main page...
    assert any("Impressum" in f for f in footers("/dashboard/"))
    assert any("Impressum" in f for f in footers("/"))
    # ...and the page that was not published stays absent from both.
    assert not any("Privacy" in f for f in footers("/dashboard/"))
    assert client.get("/privacy").status_code == 404


def test_unknown_slug_is_not_served(legal_dir):
    """The slug becomes a filename, so only the known set may reach the disk."""
    (legal_dir / "agb.html").write_text("<p>nope</p>", encoding="utf-8")
    assert client.get("/agb").status_code == 404


def test_slug_cannot_escape_the_legal_directory(legal_dir):
    assert client.get("/../config.py").status_code == 404
    assert client.get("/%2e%2e%2fconfig.py").status_code == 404


def test_one_footer_per_page(legal_dir):
    """The main page has its own footer; the inherited one must not stack."""
    (legal_dir / "impressum.html").write_text("<p>x</p>", encoding="utf-8")
    assert len(footers("/")) == 1
