"""Minecraft IDs are Windows-safe filenames, including historical names with spaces."""

import re

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+ -]{0,149}\Z")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def valid(value) -> bool:
    return (
        isinstance(value, str)
        and bool(_ID.fullmatch(value))
        and ".." not in value
        and not value.endswith((".", " "))
        and not _DEVICE.match(value.split(".", 1)[0].rstrip(" "))
    )


def validate(value: str) -> str:
    if not valid(value):
        raise ValueError("Недопустимый идентификатор версии Minecraft или загрузчика.")
    return value
