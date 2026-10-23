# -*- coding: utf-8 -*-
"""Files written by a newer version of the plugin are opened by nobody older.

Every tool's GeoPackage stamps ``schema_version`` in its meta table. A build
that meets a higher stamp must not write the file: its row and table
rewrites would drop what the newer version added, and the newer version
would never add it back (the stamp says it is there). Such a file is
refused before anything is written, with a message asking for the plugin
update; the rest of the QGIS project keeps working.

A newer version whose change older builds handle safely (for example a
nullable column they carry through untouched) can say so by also writing
``min_compatible_version``: the oldest schema version that may still open
and write the file. Without it the file's own ``schema_version`` is the
limit. See DECISIONS.md, "Files from a newer plugin version".
"""

from __future__ import annotations

from typing import Mapping, Optional

MIN_COMPATIBLE_KEY = "min_compatible_version"


class NewerVersionError(RuntimeError):
    """The file was written by a newer version of the plugin."""


def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def newer_version_message(meta: Mapping, known: int, what: str) -> str:
    """Why this build must not open a file with ``meta`` ('' when it may).

    ``known`` is this build's schema version; ``what`` names the file in
    the message, e.g. "The plan file plans.gpkg". A file never stamped (an
    old one, or one being created) may be opened; an unreadable stamp may
    not.
    """
    stamp = meta.get("schema_version")
    limit = meta.get(MIN_COMPATIBLE_KEY)
    if limit in (None, ""):
        limit = stamp
    if limit in (None, ""):
        return ""
    needed = _as_int(limit)
    if needed is not None and needed <= known:
        return ""
    number = _as_int(stamp)
    version = f"schema {number}" if number is not None else f"schema '{stamp}'"
    return (f"{what} was written by a newer version of Subsea Cable Tools ({version}; this version "
            f"reads up to schema {known}). Update the plugin to open it. Nothing in it has been changed.")


def check(meta: Mapping, known: int, what: str) -> None:
    """Raise :class:`NewerVersionError` when this build must not open the file."""
    message = newer_version_message(meta, known, what)
    if message:
        raise NewerVersionError(message)
