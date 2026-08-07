"""Small typed boundary for BeautifulSoup's scalar-or-list attributes."""

from __future__ import annotations

from typing import Any


def string_attr(tag: Any, name: str, default: str = "") -> str:
    """Read an HTML attribute as text without leaking BeautifulSoup unions."""
    value = tag.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return default


def string_list_attr(tag: Any, name: str) -> list[str]:
    """Read a token-list attribute such as ``class`` as strings."""
    value = tag.get(name)
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(part) for part in value]
    return []
