"""Optional Impressum / privacy pages.

The pages are *not* in the repository. They carry an operator's real name and
postal address, which every clone of a public repo would otherwise ship, and
which differ for every instance anyway. Instead the operator drops HTML
fragments into LEGAL_DIR on the server; the routes and the footer links appear
only for the files that exist, so an instance without them is a working site
with no dead links.

Existence is checked per request rather than cached at startup, so adding a
page on the server takes effect without a restart.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings

# Fixed slug -> heading. A closed set, not a directory listing: the slug becomes
# a filesystem path, and anything user-supplied there is a traversal waiting to
# happen.
PAGES: dict[str, str] = {
    "impressum": "Impressum",
    "privacy": "Privacy",
}


def _path(slug: str) -> Path | None:
    if slug not in PAGES:
        return None
    return Path(settings.legal_dir) / f"{slug}.html"


def available() -> dict[str, str]:
    """Slugs that have a file, in PAGES order. Used by the footer."""
    return {
        slug: title
        for slug, title in PAGES.items()
        if (p := _path(slug)) is not None and p.is_file()
    }


def render(slug: str) -> str | None:
    """The fragment's HTML, or None if this instance has no such page."""
    path = _path(slug)
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
