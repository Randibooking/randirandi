# SPDX-License-Identifier: AGPL-3.0-only
"""`{% word "key" %}`: a catalogue text in a server-drawn page, in the language of
the organization the view put in the context as `word_organization` (none: the
product's own English). The text is plain text: it is escaped, and a
placeholder, `{% word "key" name=value %}`, is filled after the escaping."""

from typing import Any

from django import template
from django.core.exceptions import ImproperlyConfigured
from django.template import Context

from randirandi import labels

register = template.Library()


@register.simple_tag(takes_context=True)
def word(context: Context, key: str, **values: Any) -> str:
    # A view names its organization, or None for the product's own English.
    # A view that forgets must not render English by default.
    if "word_organization" not in context:
        raise ImproperlyConfigured(
            f'{{% word "{key}" %}} needs `word_organization` in the context: '
            "the view passes the organization, or None for English."
        )
    organization = context["word_organization"]
    if context.autoescape:
        return labels.word_html(organization, key, **values)
    return labels.word(organization, key, **values)
