# SPDX-License-Identifier: AGPL-3.0-only
"""The pages can be put on a phone's home screen : every page names the icon and the web
app manifest, and
the manifest's icons are the approved brand copies."""

import json
import struct
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse
from django.test import Client
from whitenoise.middleware import (  # type: ignore[import-untyped]
    WhiteNoiseMiddleware,
)

from randirandi.core.models import Organization
from tests.test_login_page import HOST
from tests.test_login_page import page_settings as page_settings

ROOT = Path(__file__).resolve().parents[1]


class Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, dict[str, str]] = {}
        self.metas: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        if tag == "link":
            self.links[found["rel"]] = found
        elif tag == "meta" and "name" in found:
            self.metas[found["name"]] = found.get("content", "")


def signed_out_page() -> Head:
    with mock.patch(
        "randirandi.core.login.organization_for_request",
        return_value=Organization(
            name="Fictional organization", contact_email="post@example.com"
        ),
    ):
        response = Client().get("/login/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 200
    head = Head()
    head.feed(response.content.decode())
    return head


def png_size(path: str) -> tuple[int, int]:
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_a_page_names_the_home_screen_icon_and_the_manifest() -> None:
    head = signed_out_page()
    touch, manifest = head.links["apple-touch-icon"], head.links["manifest"]
    assert touch["href"] == "/static/core/brand/apple-touch-icon.png"
    assert manifest["href"] == "/static/core/manifest.webmanifest"
    # A manifest is fetched without the login or the sandbox's access gate
    # password unless it asks for them.
    assert manifest["crossorigin"] == "use-credentials"
    assert head.metas["apple-mobile-web-app-title"] == "RandiRandi"
    # The dark page's own background (themePrimitives, scheme C's surface.page).
    assert head.metas["theme-color"] == "#222221"
    icon = finders.find("core/brand/apple-touch-icon.png")
    assert isinstance(icon, str) and png_size(icon) == (180, 180)


def test_the_manifest_opens_the_pages_in_the_browser_with_the_brand_icons() -> None:
    path = finders.find("core/manifest.webmanifest")
    assert isinstance(path, str)
    manifest = json.loads(Path(path).read_text())
    assert manifest["name"] == manifest["short_name"] == "RandiRandi"
    assert manifest["start_url"] == manifest["scope"] == manifest["id"] == "/"
    # Asked for in the browser, not standalone: the return from Vipps comes
    # back to the browser, and a standalone app on an iPhone keeps its own
    # login. Whether a phone honours it is to be tried on the phone.
    assert manifest["display"] == "browser"
    sizes = set()
    for icon in manifest["icons"]:
        found = finders.find(f"core/{icon['src']}")
        assert isinstance(found, str), icon["src"]
        width, height = png_size(found)
        assert icon["sizes"] == f"{width}x{height}" and icon["type"] == "image/png"
        sizes.add((icon["purpose"], width))
    assert sizes == {("any", 192), ("any", 512), ("maskable", 192), ("maskable", 512)}
    # Each icon is an approved brand copy (tools/brand_assets checks the bytes).
    exports = json.loads((ROOT / "frontend/brand/asset-sources.json").read_text())
    for icon in manifest["icons"]:
        copy = f"randirandi/core/static/core/{icon['src']}"
        assert copy in exports["runtime_exports"], copy


def test_the_icons_keep_the_names_the_manifest_gives_them() -> None:
    # The manifest storage hashes the manifest's own name but not the icon
    # names inside it; the unhashed copies are served while WhiteNoise keeps
    # them.
    assert not getattr(settings, "WHITENOISE_KEEP_ONLY_HASHED_FILES", False)


def test_the_manifest_is_served_as_one() -> None:
    def page(request: HttpRequest) -> HttpResponse:
        return HttpResponse()

    path = finders.find("core/manifest.webmanifest")
    served = WhiteNoiseMiddleware(page).media_types.get_type(path)
    assert served == "application/manifest+json"
