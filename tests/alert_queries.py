# SPDX-License-Identifier: AGPL-3.0-only
"""Reads a log alert rule's expression the way Scaleway's log store does, for
the stages the service's alert rules use: `|=`, `|~`, `| regexp` (a "..."
string, or a `...` raw string, which keeps its backslashes as written), a label
filter and `sum by (...)`. A test feeds it a line as the store keeps it (the
worker's line inside a JSON object) and sees the labels the alert would carry.
"""

import json
import re

# The expressions of two alert rules as the service installs them. The product's
# payment tests run against these copies; the service's tests/test_alerts.py
# fails when a copy and the rule file differ.
PAYMENT_STUCK = (
    "sum by (organization, payment, name, reference, step, kind, "
    "since, when, link) "
    '(count_over_time({resource_id="41c2a0b3-584c-4f28-ac87-2e7b2'
    'c2dbf16"} |= "payment trouble lasting:" | regexp `payment '
    "trouble lasting: organization=org-(?P<organization>[0-9]+) "
    "payment=payment-(?P<payment>[0-9]+) "
    "reference=(?P<reference>[2-9A-HJKMNP-Z]{6}) "
    'name=(?P<name>[A-Za-z0-9-]+) step=\\\\?"(?P<step>[^"\\\\]+)\\\\?" '
    'kind=\\\\?"(?P<kind>[^"\\\\]+)\\\\?" tries=[0-9]+ '
    'since=(?P<since>[0-9TZ:-]+) when=\\\\?"(?P<when>[^"\\\\]+)\\\\?" '
    "link=(?P<link>https://[a-z0-9.-]+/admin/bookings/[0-9]+/)` | "
    'payment != "" [10m])) > 0'
)
REFUND_DONE = (
    "sum by (organization, refund, payment, automatic, name, "
    "reference, link) "
    '(count_over_time({resource_id="41c2a0b3-584c-4f28-ac87-2e7b2'
    'c2dbf16"} |= "payment refund completed:" | regexp `payment '
    "refund completed: organization=org-(?P<organization>[0-9]+) "
    "refund=refund-(?P<refund>[0-9]+) "
    "payment=payment-(?P<payment>[0-9]+) "
    "automatic=(?P<automatic>True|False) "
    "name=(?P<name>[A-Za-z0-9-]+) "
    "reference=(?P<reference>[2-9A-HJKMNP-Z]{6}) "
    "link=(?P<link>https://[a-z0-9.-]+/admin/bookings/[0-9]+/)` | "
    'refund != "" [1h])) > 0'
)
_STRING = r'"((?:\\.|[^"\\])*)"'
_STAGE = re.compile(
    rf"\|=\s*{_STRING}|\|~\s*{_STRING}|\|\s*regexp\s*{_STRING}"
    rf"|\|\s*(\w+)\s*!=\s*{_STRING}"
)
_BY = re.compile(r"sum\s+by\s*\(([^)]*)\)")
_RAW = re.compile(r"`([^`]*)`")


def quoted(expr: str) -> str:
    """The expression with each raw string written as a "..." string whose
    regular expression is the same."""
    return _RAW.sub(lambda m: '"' + m[1].replace('"', '\\"') + '"', expr)


def grouping(expr: str) -> list[str]:
    match = _BY.search(expr)
    return [name.strip() for name in match[1].split(",")] if match else []


def stored(line: str) -> str:
    """A worker line as Scaleway keeps it: inside a JSON object."""
    return json.dumps({"message": line})


def labels(expr: str, line: str) -> dict[str, str] | None:
    """The labels an alert instance gets from one stored line, None when the
    line is not selected by the expression's filters."""
    found: dict[str, str] = {}
    for match in _STAGE.finditer(quoted(expr)):
        contains, pattern, parser, name, blank = match.groups()
        if contains is not None and contains not in line:
            return None
        if pattern is not None and not re.search(pattern, line):
            return None
        if parser is not None:
            parsed = re.search(parser, line)
            if parsed:
                found |= {k: v for k, v in parsed.groupdict().items() if v}
        if name is not None and found.get(name, "") == blank:
            return None
    return {name: found.get(name, "") for name in grouping(expr)}
