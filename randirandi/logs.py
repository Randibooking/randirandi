# SPDX-License-Identifier: AGPL-3.0-only
"""Logging with ids only.

A log line names a member, an order or a booking by its id. A name, an email
address or a phone number does not belong in a log (control C18). Code keeps
that rule by never handing such a value to a logger.

The formatter here is the net under the rule. Before a line is written,
whatever looks like an email address or a phone number is struck out. That
also covers what the code does not write itself: a traceback, or a message
from a library. The price is that a bare number of eight digits is struck
too, even when it was something else.
"""

import logging
import re
import time
from typing import TYPE_CHECKING, Any, TextIO

STRUCK = "[struck]"

# The "@" also in the form it takes inside an address or a form body.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+(?:@|%40)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# A number with a country code, eight digits standing alone, or eight digits
# grouped the way they are written in Norway. Not part of a longer id, a date
# or a time.
_PHONE = re.compile(
    r"(?<![\w.:\-])"
    r"(?:"
    r"(?:\+|00|%2B)\d{8,15}"
    r"|(?:(?:\+|00|%2B)\d{2} ?)?"
    r"(?:\d{8}|\d{3} \d{2} \d{3}|\d{2} \d{2} \d{2} \d{2})"
    r")"
    r"(?![\w.:\-])"
)

_FORMAT = "%(asctime)sZ %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def strike(text: str) -> str:
    """Strike out what looks like an email address or a phone number."""
    return _PHONE.sub(STRUCK, _EMAIL.sub(STRUCK, text))


class IdsOnlyFormatter(logging.Formatter):
    """Writes the time in UTC and strikes out personal data."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)
        self.converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        if record.name == "django.security.DisallowedHost":
            # An unrecognized hostname is an ordinary rejected request. Do not
            # repeat the caller-controlled host or a traceback for each one.
            record = logging.makeLogRecord(
                record.__dict__
                | {
                    "msg": "request rejected: unrecognized host",
                    "args": (),
                    "exc_info": None,
                    "exc_text": None,
                    "stack_info": None,
                }
            )
        return strike(super().format(record))


if TYPE_CHECKING:
    StreamHandler = logging.StreamHandler[TextIO]
else:
    StreamHandler = logging.StreamHandler


class RequiredLogError(RuntimeError):
    """A required local log write failed; no stream details are exposed."""


class RequiredWriteHandler(StreamHandler):
    """Keep explicitly durable records retryable after write or flush failure."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        if getattr(record, "require_write", False) is True:
            raise RequiredLogError("Required alert log could not be written.") from None
        super().handleError(record)


def config(*, gunicorn: bool = False) -> dict[str, Any]:
    """The logging setup. Every line goes through the formatter above."""
    handled = {"handlers": ["stderr"], "level": "INFO", "propagate": False}
    loggers = {"django": handled, "django.server": handled}
    if gunicorn:
        loggers |= {
            "gunicorn.error": handled | {"qualname": "gunicorn.error"},
            "gunicorn.access": handled | {"qualname": "gunicorn.access"},
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "ids_only": {
                "()": "randirandi.logs.IdsOnlyFormatter",
                "format": _FORMAT,
                "datefmt": _DATE_FORMAT,
            },
        },
        "handlers": {
            "stderr": {
                "class": "randirandi.logs.RequiredWriteHandler",
                "formatter": "ids_only",
            },
        },
        "root": {"handlers": ["stderr"], "level": "INFO"},
        "loggers": loggers,
    }
