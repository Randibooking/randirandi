# SPDX-License-Identifier: AGPL-3.0-only
"""All routes default to authenticated; only the reviewed entry points are open."""

import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import Client, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, path
from pytest_django.fixtures import Settings

from randirandi import urls
from tests import made_up
from tests.test_login_sessions import (
    HOST,
    sign_in,
)
from tests.test_login_sessions import (
    made as made,
)
from tests.test_login_sessions import (
    page_settings as page_settings,
)

pytestmark = pytest.mark.django_db(transaction=True)


def routes(patterns: Any, prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for route in patterns:
        current = prefix + str(route.pattern)
        if isinstance(route, URLResolver):
            yield from routes(route.url_patterns, current)
        else:
            assert isinstance(route, URLPattern)
            yield current, route


def test_public_routes_are_exactly_the_reviewed_list() -> None:
    public = {
        route: entry.name
        for route, entry in routes(get_resolver().url_patterns)
        if not getattr(entry.callback, "login_required", True)
    }
    assert public == {
        "health/": "health",
        "robots.txt": "robots",
        "payments/return/": "payment-return",
        "payments/webhook/": "payment-webhook",
        "login/": "login",
        "login/code/": "email-code",
        "login/authenticator/": "authenticator",
        "login/authenticator/qr/<int:device_id>/": "authenticator-qr",
        # The price list and the booking conditions, public .
        "prices/": "public-prices",
        "prices/choices/": "public-prices-choices",
        "prices/calendar/": "public-prices-calendar",
        "conditions/": "conditions",
    }


def future_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("private future endpoint")


@pytest.mark.parametrize("method", ["get", "head", "post", "put", "delete"])
def test_future_unannotated_endpoint_cannot_be_called_anonymously(method: str) -> None:
    urlconf = ModuleType("future_routes")
    urlconf.urlpatterns = [*urls.urlpatterns, path("future/", future_view)]  # type: ignore[attr-defined]
    with override_settings(ROOT_URLCONF=urlconf):
        response = getattr(Client(), method)("/future/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 302
    assert response.headers["Location"] == "/login/?next=/future/"
    assert b"private future endpoint" not in response.content


def test_future_route_requires_current_host_bound_session(made: made_up.MadeUp) -> None:
    urlconf = ModuleType("future_routes")
    urlconf.urlpatterns = [*urls.urlpatterns, path("future/", future_view)]  # type: ignore[attr-defined]
    client = Client(enforce_csrf_checks=True)
    assert sign_in(client, made.member).status_code == 302
    with override_settings(ROOT_URLCONF=urlconf):
        response = client.get("/future/", HTTP_HOST=HOST, secure=True)
        assert response.status_code == 200
        assert response.content == b"private future endpoint"
        made.member.status = "disabled"
        made.member.save(update_fields=["status"])
        response = client.get("/future/", HTTP_HOST=HOST, secure=True)
        assert response.status_code == 302
        assert response.headers["Location"] == "/login/?next=/future/"


FILLERS = {
    "int": "1",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "str": "x",
    "slug": "x",
}
NOINDEX = "noindex, nofollow"


def concrete(route: str) -> str:
    """The route with every converter replaced by a value of its kind."""
    return re.sub(
        r"<(\w+):\w+>", lambda found: FILLERS[found.group(1)], "/" + route.lstrip("^")
    )


def test_without_the_gate_only_the_reviewed_public_paths_answer_anonymously(
    settings: Settings,
) -> None:
    """the access gate gives way to the login, nothing else opens."""
    settings.ACCESS_GATE_PASSWORD = ""
    public = {
        route
        for route, entry in routes(get_resolver().url_patterns)
        if not getattr(entry.callback, "login_required", True)
    }
    for route, _ in routes(get_resolver().url_patterns):
        path_ = concrete(route)
        for method in ("get", "head", "post", "put", "patch", "delete"):
            response = getattr(Client(), method)(path_, HTTP_HOST=HOST, secure=True)
            assert response.headers["X-Robots-Tag"] == NOINDEX, (method, path_)
            if route in public:
                continue
            # Private: the login, or refused. Never a page.
            if response.status_code == 302:
                assert response.headers["Location"].startswith("/login/?next="), (
                    method,
                    path_,
                )
            else:
                assert response.status_code in (403, 404, 405), (method, path_)
            assert response.status_code != 200, (method, path_)
            assert not response.content or response.status_code != 200
    # A path no route owns is not found.
    response = Client().get("/nothing-here/", HTTP_HOST=HOST, secure=True)
    assert response.status_code == 404
    assert response.headers["X-Robots-Tag"] == NOINDEX


def test_without_the_gate_a_real_static_file_is_served_with_noindex(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "real.css").write_text("body{}\n")
    settings.STATIC_ROOT = tmp_path
    settings.ACCESS_GATE_PASSWORD = ""
    response = Client().get("/static/real.css", HTTP_HOST=HOST, secure=True)
    response.close()  # the file WhiteNoise opened

    assert response.status_code == 200
    assert response.headers["X-Robots-Tag"] == NOINDEX
