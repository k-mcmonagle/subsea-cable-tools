# -*- coding: utf-8 -*-
"""User-defined Planner operation types with JSON import/export.

Operation types classify Planner tasks (Lay, PLGR, Plough, ROV, ...). They are
purely descriptive: no scheduling or fuel logic branches on a specific value, so
the list is fully user-configurable. Entries are stored per user (the dock keeps
them in QSettings as JSON), start blank, and can be shared inside an organisation
through the JSON round-trip in this module.

Each entry is ``{"value": <slug>, "label": <display>}``. ``value`` is the stable
key stored on a task; ``label`` is what the user sees in the dropdown. When a user
supplies only a label the slug is derived from it.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Sequence, Tuple

from . import schema

ENTRY_FIELDS = ("value", "label")

# The unspecified/blank choice always leads every dropdown and is not a
# user-editable entry.
UNSPECIFIED = ("", "(unspecified)")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Derive a stable lower-case key from a human label."""
    slug = _SLUG_RE.sub("_", str(text or "").strip().lower()).strip("_")
    return slug


def example_operation_types() -> List[Dict]:
    """The built-in starter list, offered on demand (never loaded by default)."""
    return [{"value": value, "label": label}
            for value, label in schema.OPERATION_TYPES if value]


def default_operation_types() -> List[Dict]:
    """New users start with a blank library."""
    return []


def normalize_entries(entries: Sequence[Dict]) -> List[Dict]:
    """Coerce arbitrary dicts to clean, de-duplicated entries.

    Rows without a usable label or value are dropped; a missing value is
    derived from the label, and a missing label falls back to the value. The
    first occurrence of each value wins.
    """
    normalized: List[Dict] = []
    seen = set()
    for item in entries or ():
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not value and label:
            value = slugify(label)
        if not value:
            continue
        if not label:
            label = value
        if value in seen:
            continue
        seen.add(value)
        normalized.append({"value": value, "label": label})
    return normalized


def as_choices(entries: Sequence[Dict], include=None) -> List[Tuple[str, str]]:
    """Build ``(value, label)`` pairs for a combo box.

    Always leads with the unspecified choice. ``include`` is an optional value
    (e.g. a task's stored operation type) appended when it is not otherwise in
    the list, so existing data is never hidden.
    """
    choices = [UNSPECIFIED]
    seen = {""}
    for entry in normalize_entries(entries):
        choices.append((entry["value"], entry["label"]))
        seen.add(entry["value"])
    extra = str(include or "").strip()
    if extra and extra not in seen:
        choices.append((extra, extra))
    return choices


def entries_to_json(entries: Sequence[Dict]) -> str:
    """Serialise for QSettings storage or file export."""
    return json.dumps(normalize_entries(entries))


def entries_from_json(raw) -> List[Dict]:
    """Parse QSettings/JSON text, tolerating empty or malformed input."""
    try:
        data = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return normalize_entries(data)


def entries_from_json_text(text: str) -> Tuple[List[Dict], List[str]]:
    """Parse an imported JSON file into entries plus human-readable warnings.

    Accepts either a list of ``{"value", "label"}`` objects or a plain list of
    strings (treated as labels).
    """
    raw = str(text or "").lstrip(chr(0xFEFF)).strip()
    if not raw:
        return [], ["The file is empty."]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], ["The file is not valid JSON: %s" % exc]
    if isinstance(data, dict):
        data = data.get("operation_types", data.get("entries", []))
    if not isinstance(data, list):
        return [], ["Expected a JSON list of operation types."]
    coerced: List[Dict] = []
    warnings: List[str] = []
    for index, item in enumerate(data, start=1):
        if isinstance(item, str):
            label = item.strip()
            if label:
                coerced.append({"value": slugify(label), "label": label})
            continue
        if isinstance(item, dict):
            coerced.append(item)
            continue
        warnings.append("Item %d was ignored (not an object or string)." % index)
    entries = normalize_entries(coerced)
    if not entries and not warnings:
        warnings.append("No operation types were found in the file.")
    return entries, warnings
