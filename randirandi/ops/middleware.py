# SPDX-License-Identifier: AGPL-3.0-only
"""What a request passes on its way in, beside Django's own middleware."""

import base64
import binascii
import secrets
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from randirandi.ops import views

type Handler = Callable[[HttpRequest], HttpResponseBase]

HEALTH = "/health/"
ROBOTS = "/robots.txt"


class NoIndex:
    """Every response asks search engines to keep away (C26).

    Outermost, so the health answer, the gate's refusal, static files and
    error pages carry it too.
    """

    VALUE = "noindex, nofollow"

    def __init__(self, get_response: Handler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        response.headers["X-Robots-Tag"] = self.VALUE
        return response


class ContainerProbe:
    """Answers the platform when it asks the container whether the app is alive.

    The platform asks the container itself, past its own front door: over
    plain HTTP and under the container's own address, which is no hostname
    of ours. Every other request with that name is refused further in, and
    so was this one. Here that one question gets the health page's answer,
    and nothing else is opened.

    A request that came in through the front door carries the header the
    front door sets, and replaces when a visitor sends it. Such a request is
    not answered here.
    """

    HOST = "127.0.0.1:8000"
    FRONT_DOOR = "HTTP_X_ENVOY_EXTERNAL_ADDRESS"

    def __init__(self, get_response: Handler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if (
            request.path == HEALTH
            and request.META.get("HTTP_HOST") == self.HOST
            and self.FRONT_DOOR not in request.META
        ):
            return views.health(request)
        return self.get_response(request)


class PermissionsPolicy:
    """Camera, microphone and location are off for every page (C9)."""

    VALUE = "camera=(), geolocation=(), microphone=()"

    def __init__(self, get_response: Handler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        response.headers.setdefault("Permissions-Policy", self.VALUE)
        return response


class AccessGate:
    """A shared password before account login while the site is closed (C26).

    Settings resolve the intended gate state and its password. The browser
    asks for it once and then sends it with every
    request; any user name goes. The health page stays open, because the
    platform and the check from outside must reach it.

    It stands behind the redirect to HTTPS, so the password is never asked
    for over plain HTTP.
    """

    # Signed callbacks and cross-browser returns have their own narrow guards.
    OPEN = (HEALTH, ROBOTS, "/payments/webhook/", "/payments/return/")

    def __init__(self, get_response: Handler) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        password: str = settings.ACCESS_GATE_PASSWORD
        if not password or request.path in self.OPEN or self._has(request, password):
            return self.get_response(request)
        production = settings.ENVIRONMENT == "production"
        response = HttpResponse(
            "This site is not open yet.\n"
            if production
            else "This is a closed test site.\n",
            status=401,
            content_type="text/plain; charset=utf-8",
        )
        realm = "RandiRandi" if production else "RandiRandi sandbox"
        response["WWW-Authenticate"] = f'Basic realm="{realm}", charset="UTF-8"'
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _has(request: HttpRequest, password: str) -> bool:
        kind, _, given = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
        if kind.lower() != "basic":
            return False
        try:
            _, _, tried = base64.b64decode(given, validate=True).partition(b":")
        except binascii.Error:
            return False
        return secrets.compare_digest(tried, password.encode())
