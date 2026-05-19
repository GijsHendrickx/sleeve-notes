"""Template filters for dict / list lookup by variable key.

Django's `{{ obj.key }}` syntax doesn't work when `key` is itself a
variable — it tries `obj.attr_key` literally. `get_item` closes that gap.
"""
from django import template

register = template.Library()


@register.filter(name="get_item")
def get_item(value, key):
    if value is None:
        return None
    try:
        return value[key]
    except (KeyError, TypeError, IndexError):
        return None
