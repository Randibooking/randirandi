# SPDX-License-Identifier: AGPL-3.0-only
"""Addresses for the log test: a login that fails the way a bug would."""

from django.contrib.auth.decorators import login_not_required
from django.http import HttpRequest, HttpResponse
from django.urls import path


@login_not_required
def login(request: HttpRequest) -> HttpResponse:
    email = request.POST["email"]
    phone = request.POST["phone"]
    raise RuntimeError(f"no member with {email} or {phone}")


urlpatterns = [path("login/", login)]
