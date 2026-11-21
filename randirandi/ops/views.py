# SPDX-License-Identifier: AGPL-3.0-only
import logging

from django.contrib.auth.decorators import login_not_required
from django.db import Error, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)


@require_safe
@never_cache
@login_not_required
def health(request: HttpRequest) -> JsonResponse:
    """The health page: the app answers and reaches its database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Error:
        logger.exception("health: the database does not answer")
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@require_safe
@login_not_required
def robots(request: HttpRequest) -> HttpResponse:
    """Search engines are told to index nothing (C26)."""
    return HttpResponse(
        "User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8"
    )
