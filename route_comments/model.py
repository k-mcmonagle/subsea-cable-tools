# -*- coding: utf-8 -*-
"""Route Comments data rules (no QGIS, no Qt).

A comment belongs to a named *comment set*. It has free text, an author, a
date, a status and a priority (choices kept per set, no enforced
transitions), optional tags and optional per-set custom field values, and a
*reference*: a route point, a route KP range or a free map location.

Plain dicts are used throughout (the store's column names), so the store,
the import/export code, the UI and the tests share one shape.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- references
REF_ROUTE_POINT = "route_point"
REF_KP_RANGE = "kp_range"
REF_LOCATION = "location"
REF_KINDS = (REF_ROUTE_POINT, REF_KP_RANGE, REF_LOCATION)
REF_LABELS = {REF_ROUTE_POINT: "Route point", REF_KP_RANGE: "KP range", REF_LOCATION: "Map location"}

# Reference conditions (route_ref.ReferenceChecker sets them).
COND_OK = "ok"
COND_SUPERSEDED = "superseded"   # route unchanged, but the RPL has a newer revision
COND_CHANGED = "changed"         # route geometry, start KP or KP convention differ
COND_UNAVAILABLE = "unavailable"  # the route cannot be found or opened
COND_NONE = "none"               # no route reference (e.g. imported by position)
COND_LABELS = {COND_OK: "OK", COND_SUPERSEDED: "Newer revision", COND_CHANGED: "Route changed",
               COND_UNAVAILABLE: "Route unavailable", COND_NONE: "No route"}
ATTENTION_CONDITIONS = (COND_CHANGED, COND_UNAVAILABLE)

# --------------------------------------------------------------------------- choices
DEFAULT_STATUSES = [
    {"name": "Open"},
    {"name": "In progress"},
    {"name": "Resolved", "closed": True},
    {"name": "Closed", "closed": True},
]
DEFAULT_PRIORITIES = ["Low", "Normal", "High", "Urgent"]
DEFAULT_PRIORITY = "Normal"


def normalise_statuses(statuses) -> List[Dict]:
    """``[{"name", "closed"}]`` without blanks or duplicates (case-insensitive)."""
    out, seen = [], set()
    for item in statuses or []:
        if isinstance(item, dict):
            name, closed = str(item.get("name") or "").strip(), bool(item.get("closed"))
        else:
            name, closed = str(item or "").strip(), False
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append({"name": name, "closed": closed})
    return out or [dict(s, closed=bool(s.get("closed"))) for s in DEFAULT_STATUSES]


def normalise_priorities(priorities) -> List[str]:
    out, seen = [], set()
    for item in priorities or []:
        name = str(item or "").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out or list(DEFAULT_PRIORITIES)


def status_names(statuses) -> List[str]:
    return [s["name"] for s in normalise_statuses(statuses)]


def closed_statuses(statuses) -> set:
    return {s["name"].casefold() for s in normalise_statuses(statuses) if s.get("closed")}


def first_status(statuses, closed: bool) -> str:
    """The set's first closed (or first open) status: what *Send & close*
    (or *Send & reopen*) sets. '' when the set has none of that kind."""
    for status in normalise_statuses(statuses):
        if bool(status.get("closed")) == closed:
            return status["name"]
    return ""


def status_to_restore(reply: Dict, replies: Sequence[Dict], current_status: str, statuses) -> str:
    """The status deleting ``reply`` should put back, or '' when it set none or
    the status has moved on since (a later reply changed it, or it was changed
    by hand). Replies saved before the previous status was recorded fall back
    on the status an earlier reply set, else the set's first status of the
    other kind (an undone *Send & close* reopens)."""
    status_to = str(reply.get("status_to") or "")
    if not status_to or str(current_status or "").casefold() != status_to.casefold():
        return ""
    changes = [r for r in replies if r.get("status_to")]
    if changes and changes[-1].get("reply_id") != reply.get("reply_id"):
        return ""
    if reply.get("status_from"):
        return str(reply["status_from"])
    earlier = [r for r in changes if r.get("reply_id") != reply.get("reply_id")]
    if earlier:
        return str(earlier[-1]["status_to"])
    return first_status(statuses, closed=status_to.casefold() not in closed_statuses(statuses))


def default_priority(priorities) -> str:
    names = normalise_priorities(priorities)
    for name in names:
        if name.casefold() == DEFAULT_PRIORITY.casefold():
            return name
    return names[len(names) // 2 - (1 if len(names) % 2 == 0 else 0)] if names else ""


def match_choice(value, choices: Sequence[str]) -> Tuple[str, bool]:
    """``(value, known)``: ``value`` spelt as in ``choices`` when it matches one
    (case-insensitive), else ``value`` unchanged and ``known`` False."""
    text = str(value or "").strip()
    for choice in choices:
        if choice.casefold() == text.casefold():
            return choice, True
    return text, False


# --------------------------------------------------------------------------- images
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")


def is_image_name(name: str) -> bool:
    return str(name or "").lower().endswith(IMAGE_EXTENSIONS)


def split_image_names(cell) -> List[str]:
    """File names or paths listed in one cell (``;``, ``|`` or one per line)."""
    out = []
    for part in re.split(r"[;|\r\n]+", str(cell or "")):
        part = part.strip().strip('"').strip()
        if part:
            out.append(part)
    return out


def resolve_image(name: str, folder: str = "") -> str:
    """``name`` as a full path: absolute as given, else inside ``folder``."""
    path = os.path.expanduser(name)
    if not os.path.isabs(path) and folder:
        path = os.path.join(folder, path)
    return os.path.normpath(path)


# --------------------------------------------------------------------------- custom fields
FIELD_TEXT = "text"
FIELD_NUMBER = "number"
FIELD_DATE = "date"
FIELD_BOOLEAN = "boolean"
FIELD_CHOICE = "choice"
FIELD_TYPES = (FIELD_TEXT, FIELD_NUMBER, FIELD_DATE, FIELD_BOOLEAN, FIELD_CHOICE)
FIELD_TYPE_LABELS = {FIELD_TEXT: "Text", FIELD_NUMBER: "Number", FIELD_DATE: "Date",
                     FIELD_BOOLEAN: "Yes / no", FIELD_CHOICE: "Choice"}

_TRUE = {"1", "true", "yes", "y", "t", "x", "on"}
_FALSE = {"0", "false", "no", "n", "f", "off", ""}


@dataclass
class CustomField:
    field_id: str
    name: str
    kind: str = FIELD_TEXT
    choices: List[str] = field(default_factory=list)
    seq: int = 0

    @classmethod
    def from_row(cls, row: Dict) -> "CustomField":
        try:
            choices = json.loads(row.get("choices_json") or "[]")
        except (TypeError, ValueError):
            choices = []
        kind = row.get("kind") if row.get("kind") in FIELD_TYPES else FIELD_TEXT
        return cls(str(row.get("field_id") or ""), str(row.get("name") or ""), kind,
                   [str(c) for c in choices if str(c).strip()], int(row.get("seq") or 0))

    def to_row(self, set_id: str) -> Dict:
        return {"field_id": self.field_id, "set_id": set_id, "name": self.name, "kind": self.kind,
                "choices_json": json.dumps(list(self.choices)), "seq": int(self.seq)}


def coerce_custom(fld: CustomField, raw) -> Tuple[object, str]:
    """``(value, error)``: ``raw`` (typed or text) as stored for ``fld``.

    Empty input gives ``(None, "")``. Numbers are floats, dates ISO
    ``YYYY-MM-DD``, booleans ``True``/``False``; a choice outside the field's
    list is kept and reported (choices are a convenience, not a rule).
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, ""
    if fld.kind == FIELD_NUMBER:
        if isinstance(raw, bool):
            return None, f"{fld.name}: '{raw}' is not a number"
        value = parse_number(raw)
        return (value, "") if value is not None else (None, f"{fld.name}: '{raw}' is not a number")
    if fld.kind == FIELD_DATE:
        value = parse_date(raw)
        return (value, "") if value else (None, f"{fld.name}: '{raw}' is not a date")
    if fld.kind == FIELD_BOOLEAN:
        if isinstance(raw, bool):
            return raw, ""
        text = str(raw).strip().casefold()
        if text in _TRUE:
            return True, ""
        if text in _FALSE:
            return False, ""
        return None, f"{fld.name}: '{raw}' is not yes/no"
    text = str(raw).strip()
    if fld.kind == FIELD_CHOICE and fld.choices:
        value, known = match_choice(text, fld.choices)
        return value, ("" if known else f"{fld.name}: '{text}' is not one of its choices (kept)")
    return text, ""


def custom_text(fld: CustomField, value) -> str:
    """Display/export text of a stored custom value."""
    if value is None or value == "":
        return ""
    if fld.kind == FIELD_BOOLEAN:
        return "Yes" if bool(value) else "No"
    if fld.kind == FIELD_NUMBER:
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def load_custom(comment: Dict) -> Dict[str, object]:
    try:
        data = json.loads(comment.get("custom_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dump_custom(values: Dict[str, object]) -> str:
    return json.dumps({k: v for k, v in (values or {}).items() if v is not None and v != ""}, sort_keys=True)


# --------------------------------------------------------------------------- parsing
def parse_number(raw) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().replace(" ", "")
        if text.count(",") == 1 and "." not in text:
            text = text.replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return None
    return value if value == value and abs(value) != float("inf") else None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                 "%Y%m%d")


def parse_date(raw) -> str:
    """ISO ``YYYY-MM-DD`` from a date/datetime or text ('' when unreadable).

    Day-first for slashed dates (``03/04/2026`` is 3 April), as in most
    survey and project registers; ISO dates are unambiguous.
    """
    if raw is None:
        return ""
    if isinstance(raw, _dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, _dt.date):
        return raw.isoformat()
    text = str(raw).strip()
    if not text:
        return ""
    # Spreadsheet cells come as "2026-10-19 00:00:00"; ISO timestamps as "...T...".
    head = re.split(r"[ T]", text, maxsplit=1)[0] if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", text) else text
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


_DMS_RE = re.compile(
    r"^\s*([NSEW])?\s*(-?\d+(?:[.,]\d+)?)\s*(?:[°d:\s]\s*(\d+(?:[.,]\d+)?)\s*(?:['m:\s]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:\"|''|s)?)?\s*'?)?\s*([NSEW])?\s*$", re.IGNORECASE)


def parse_coordinate(raw, is_lat: bool) -> Optional[float]:
    """Decimal degrees from ``12.345``, ``-12,345``, ``12 20.7 N``, ``12°20'42"S``…

    Returns ``None`` for unreadable text or a value outside ±90 / ±180.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        match = _DMS_RE.match(text)
        if not match:
            return None
        hemi = (match.group(1) or match.group(5) or "").upper()
        deg = float(match.group(2).replace(",", "."))
        minutes = float(match.group(3).replace(",", ".")) if match.group(3) else 0.0
        seconds = float(match.group(4).replace(",", ".")) if match.group(4) else 0.0
        if minutes >= 60 or seconds >= 60:
            return None
        value = abs(deg) + minutes / 60.0 + seconds / 3600.0
        if deg < 0 or str(match.group(2)).startswith("-") or hemi in ("S", "W"):
            value = -value
        if hemi and (hemi in ("N", "S")) != is_lat:
            return None
    limit = 90.0 if is_lat else 180.0
    return value if -limit <= value <= limit else None


def parse_kp(raw) -> Optional[float]:
    """KP in km from a cell (``12.345``, ``KP 12.345``, ``12+345``)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return parse_number(raw)
    text = re.sub(r"(?i)^\s*r?kp\s*", "", str(raw)).replace(" ", "")
    match = re.fullmatch(r"(-?\d+)\+(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1)) + float(match.group(2)) / 1000.0
    return parse_number(re.sub(r"(?i)km$", "", text))


# --------------------------------------------------------------------------- display
def ref_label(ref_no) -> str:
    try:
        return f"#{int(ref_no)}"
    except (TypeError, ValueError):
        return ""


def summary(text: str, limit: int = 80) -> str:
    """First line of ``text``, shortened to ``limit`` characters."""
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: max(1, limit - 1)].rstrip() + "…"


def kp_text(comment: Dict, places: int = 3) -> str:
    """``12.345`` / ``12.345 – 13.000`` (``~`` marks a free location's nearest KP)."""
    kp, end = comment.get("kp_km"), comment.get("kp_end_km")
    if kp is None:
        return ""
    text = f"{float(kp):.{places}f}"
    if comment.get("ref_kind") == REF_KP_RANGE and end is not None:
        text += f" – {float(end):.{places}f}"
    elif comment.get("ref_kind") == REF_LOCATION:
        text = "~" + text
    return text


def parse_kp_edit(comment: Dict, text: str) -> Tuple[float, Optional[float]]:
    """Read a table KP or KP range in km, keeping untouched stored precision."""
    kind = comment.get("ref_kind")
    if kind not in (REF_ROUTE_POINT, REF_KP_RANGE):
        raise ValueError("A map location's nearest KP is calculated from its coordinates. "
                         "Use Edit position to change latitude and longitude.")
    number = r"[+-]?(?:[0-9]+(?:\.[0-9]{0,3})?|\.[0-9]{1,3})"
    pattern = f"({number})"
    if kind == REF_KP_RANGE:
        pattern += rf"\s*(?:–|—|-|to)\s*({number})"
    match = re.fullmatch(pattern, str(text).strip(), re.IGNORECASE)
    hint = "two KPs, for example 12.345 – 13.456" if kind == REF_KP_RANGE else "one KP, for example 12.345"
    if match is None:
        raise ValueError(f"Enter {hint}, in km with up to 3 decimal places (1 m).")
    values = []
    for key, raw in zip(("kp_km", "kp_end_km"), match.groups()):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("KPs must be finite numbers in km.")
        initial = comment.get(key)
        if initial is not None and value == float(f"{float(initial):.3f}"):
            value = float(initial)
        values.append(value)
    return values[0], values[1] if len(values) > 1 else None


def route_text(comment: Dict) -> str:
    """``RPL name rev B`` or the route layer name."""
    name = comment.get("rpl_name") or comment.get("route_name") or ""
    rev = comment.get("rpl_rev")
    return f"{name} rev {rev}" if name and rev else name


def location_text(comment: Dict) -> str:
    """One line describing where a comment is, as captured."""
    kind = comment.get("ref_kind")
    route = route_text(comment)
    on = f" on {route}" if route else ""
    kp, end = comment.get("kp_km"), comment.get("kp_end_km")
    if kind == REF_KP_RANGE and kp is not None and end is not None:
        length = abs(float(end) - float(kp)) * 1000.0
        return f"KP {float(kp):.3f} – {float(end):.3f} ({length:,.0f} m){on}"
    if kind == REF_ROUTE_POINT and kp is not None:
        return f"KP {float(kp):.3f}{on}"
    parts = []
    lat, lon = comment.get("lat"), comment.get("lon")
    if lat is not None and lon is not None:
        parts.append(f"{float(lat):.6f}, {float(lon):.6f}")
    if kp is not None:
        dcc = comment.get("dcc_m")
        near = f"nearest KP {float(kp):.3f}"
        if dcc is not None:
            near += f", {float(dcc):,.0f} m off route"
        parts.append(near + on)
    return "; ".join(parts) or "Map location"


# --------------------------------------------------------------------------- search & filters
@dataclass
class Filter:
    text: str = ""
    statuses: Optional[set] = None      # casefolded names; None = all
    priorities: Optional[set] = None
    conditions: Optional[set] = None    # COND_* codes; None = all
    kinds: Optional[set] = None         # REF_* kinds; None = all
    hide_closed: bool = False


def search_blob(comment: Dict, fields: Sequence[CustomField] = (), replies: Sequence[Dict] = ()) -> str:
    """Lower-case text a free-text search looks through."""
    values = load_custom(comment)
    parts = [ref_label(comment.get("ref_no")), comment.get("ext_id"), comment.get("text"),
             comment.get("author"), comment.get("tags"), comment.get("status"), comment.get("priority"),
             comment.get("comment_date"), route_text(comment), comment.get("route_name"), kp_text(comment)]
    parts += [custom_text(f, values.get(f.field_id)) for f in fields]
    parts += [f"{r.get('author') or ''} {r.get('text') or ''}" for r in replies]
    return "\n".join(str(p) for p in parts if p not in (None, "")).casefold()


def matches(comment: Dict, flt: Filter, blob: str = "", closed: Iterable[str] = (),
            condition: str = "") -> bool:
    """Whether ``comment`` passes ``flt``. All words of the search must appear."""
    status = str(comment.get("status") or "").casefold()
    if flt.hide_closed and status in set(closed):
        return False
    if flt.statuses is not None and status not in flt.statuses:
        return False
    if flt.priorities is not None and str(comment.get("priority") or "").casefold() not in flt.priorities:
        return False
    if flt.kinds is not None and comment.get("ref_kind") not in flt.kinds:
        return False
    if flt.conditions is not None and (condition or COND_NONE) not in flt.conditions:
        return False
    words = [w for w in str(flt.text or "").casefold().split() if w]
    if words:
        haystack = blob or search_blob(comment)
        return all(word in haystack for word in words)
    return True


# --------------------------------------------------------------------------- export
BASE_EXPORT_COLUMNS = [
    ("comment_id", "Comment ID"), ("set", "Comment set"), ("ref", "No."), ("ext_id", "External ID"),
    ("status", "Status"), ("priority", "Priority"), ("author", "Author"), ("comment_date", "Date"),
    ("text", "Comment"), ("tags", "Tags"), ("reference", "Reference"), ("kp_km", "KP"),
    ("kp_end_km", "KP to"), ("dcc_m", "Distance off route (m)"), ("lat", "Latitude"), ("lon", "Longitude"),
    ("route", "Route"), ("route_layer", "Route layer"), ("rpl_id", "RPL ID"), ("rpl_rev", "RPL revision"),
    ("route_source", "Route source"), ("start_kp_km", "Route start KP"), ("kp_basis", "KP convention"),
    ("condition", "Reference condition"), ("reply_count", "Replies"), ("replies", "Reply text (export only)"),
    ("attachment_count", "Images"), ("wkt", "Geometry (WKT, EPSG:4326)"),
    ("created_utc", "Created (UTC)"), ("updated_utc", "Updated (UTC)"),
]

EXPORT_NOTE = (
    "Included: every comment field, its location and route reference (KP, KP convention, route "
    "start KP, RPL and revision), the reference condition, all custom fields, the number of replies "
    "and images, and the reply text as one column. Not included: image files (they stay in the "
    "register's attachments folder). Reply text is for reading only: importing this file adds the "
    "comments, not their replies.")


def export_columns(fields: Sequence[CustomField]) -> List[Tuple[str, str]]:
    return list(BASE_EXPORT_COLUMNS) + [(f"cf:{f.field_id}", f.name) for f in fields]


def export_rows(comments: Sequence[Dict], set_names: Dict[str, str], fields_by_set: Dict[str, Sequence[CustomField]],
                conditions: Dict[str, str], replies: Dict[str, Sequence[Dict]]) -> Tuple[List[str], List[List]]:
    """``(headers, rows)`` for a spreadsheet of ``comments``.

    Custom fields of every set involved become columns (same-named fields of
    different sets share one column).
    """
    columns = list(BASE_EXPORT_COLUMNS)
    custom_cols: List[Tuple[str, str]] = []   # (name casefold, header)
    for set_id in dict.fromkeys(c.get("set_id") for c in comments):
        for fld in fields_by_set.get(set_id, ()):
            key = fld.name.casefold()
            if key not in {k for k, _h in custom_cols}:
                custom_cols.append((key, fld.name))
    headers = [label for _key, label in columns] + [h for _k, h in custom_cols]
    rows = []
    for c in comments:
        thread = sorted(replies.get(c.get("comment_id"), ()), key=lambda r: str(r.get("created_utc") or ""))
        values = {
            "comment_id": c.get("comment_id"), "set": set_names.get(c.get("set_id"), ""),
            "ref": c.get("ref_no"), "ext_id": c.get("ext_id") or "", "status": c.get("status") or "",
            "priority": c.get("priority") or "", "author": c.get("author") or "",
            "comment_date": c.get("comment_date") or "", "text": c.get("text") or "", "tags": c.get("tags") or "",
            "reference": REF_LABELS.get(c.get("ref_kind"), ""), "kp_km": c.get("kp_km"),
            "kp_end_km": c.get("kp_end_km") if c.get("ref_kind") == REF_KP_RANGE else None,
            "dcc_m": c.get("dcc_m"), "lat": c.get("lat"), "lon": c.get("lon"),
            "route": route_text(c), "route_layer": c.get("route_name") or "", "rpl_id": c.get("rpl_id") or "",
            "rpl_rev": c.get("rpl_rev") or "", "route_source": c.get("route_source") or "",
            "start_kp_km": c.get("start_kp_km"), "kp_basis": c.get("kp_basis") or "",
            "condition": COND_LABELS.get(conditions.get(c.get("comment_id"), ""), ""),
            "reply_count": len(thread),
            "replies": "\n".join(f"{(r.get('created_utc') or '')[:10]} {r.get('author') or ''}: {r.get('text') or ''}"
                                 + (f" [status: {r['status_to']}]" if r.get("status_to") else "")
                                 for r in thread),
            "attachment_count": int(c.get("attachment_count") or 0), "wkt": c.get("wkt") or "",
            "created_utc": c.get("created_utc") or "", "updated_utc": c.get("updated_utc") or "",
        }
        row = [values.get(key) for key, _label in columns]
        custom = load_custom(c)
        by_name = {f.name.casefold(): f for f in fields_by_set.get(c.get("set_id"), ())}
        for key, _header in custom_cols:
            fld = by_name.get(key)
            row.append(custom_text(fld, custom.get(fld.field_id)) if fld else "")
        rows.append(row)
    return headers, rows


# --------------------------------------------------------------------------- import
IMPORT_TARGETS = [
    ("ext_id", "ID"), ("text", "Comment"), ("status", "Status"), ("priority", "Priority"),
    ("author", "Author"), ("comment_date", "Date"), ("tags", "Tags"), ("kp_km", "KP"),
    ("kp_end_km", "KP to"), ("lat", "Latitude"), ("lon", "Longitude"), ("images", "Image file(s)"),
]
_GUESSES = {
    "ext_id": ("comment id", "id", "ref", "reference", "no", "number", "item", "comment no"),
    "text": ("comment", "comments", "text", "description", "remark", "remarks", "note", "notes", "issue",
             "observation"),
    "status": ("status", "state"),
    "priority": ("priority", "severity", "importance"),
    "author": ("author", "by", "raised by", "reviewer", "commenter", "name", "user"),
    "comment_date": ("date", "raised", "created", "date raised", "comment date"),
    "tags": ("tags", "tag", "category", "categories", "type", "discipline"),
    "kp_km": ("kp", "kp from", "start kp", "kp start", "from kp", "kp1", "chainage"),
    "kp_end_km": ("kp to", "end kp", "kp end", "to kp", "kp2"),
    "lat": ("lat", "latitude", "y lat"),
    "lon": ("lon", "long", "longitude", "lng", "x lon"),
    "images": ("image", "images", "image file", "image files", "picture", "pictures", "photo", "photos",
               "attachment", "attachments"),
}


def _norm_header(text: str) -> str:
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(text or "").casefold())
    return " ".join(re.sub(r"[_\-./:#]+", " ", text).split())


def guess_mapping(headers: Sequence[str], fields: Sequence[CustomField] = ()) -> Dict[str, int]:
    """``{target: column}`` from header names (each column used at most once).

    Targets are the ``IMPORT_TARGETS`` keys plus ``cf:<field_id>`` for the
    set's custom fields (matched by field name). Unmatched targets are left out.
    """
    normed = [_norm_header(h) for h in headers]
    mapping: Dict[str, int] = {}
    used = set()
    wanted = [(key, _GUESSES.get(key, ())) for key, _label in IMPORT_TARGETS]
    wanted = [(f"cf:{f.field_id}", (_norm_header(f.name),)) for f in fields] + wanted
    # Exact matches first, then a header that starts with a guess.
    for exact in (True, False):
        for key, guesses in wanted:
            if key in mapping:
                continue
            for col, name in enumerate(normed):
                if col in used or not name:
                    continue
                if (name in guesses) if exact else any(name.startswith(g + " ") for g in guesses):
                    mapping[key] = col
                    used.add(col)
                    break
    return mapping


@dataclass
class ImportRow:
    line: int                              # 1-based row in the file
    values: Dict[str, object] = field(default_factory=dict)
    kind: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _uuid_like(text: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                             str(text or "").strip()))


def validate_import(data_rows: Sequence[Sequence], mapping: Dict[str, int], fields: Sequence[CustomField] = (),
                    statuses=None, priorities=None, existing_ids: Iterable[str] = (),
                    kp_range: Optional[Tuple[float, float]] = None, first_line: int = 2,
                    default_author: str = "", default_date: str = "", image_folder: str = "") -> List[ImportRow]:
    """Check every data row and turn it into comment values.

    A row needs comment text and a location: latitude and longitude (a map
    location), a KP (a route point) or KP and KP to (a KP range). KPs need a
    route: ``kp_range`` is that route's ``(start KP, end KP)``; without one,
    KP-only rows are errors. An ID equal to a comment already in the
    register (``existing_ids``: comment IDs and external IDs), or repeated
    in the file, is a duplicate and the row is not imported. An ID that is a
    comment UUID (e.g. from an export) keeps that UUID; any other ID is kept
    as the comment's external ID. Unknown statuses/priorities are kept and
    reported. Image files named in the image column (absolute, or inside
    ``image_folder``; several separated by ``;``) go in ``attach_paths``; a
    missing or unsupported one is noted and the comment imported without it.
    """
    status_list = status_names(statuses)
    priority_list = normalise_priorities(priorities)
    existing = {str(i).strip().casefold() for i in existing_ids if str(i or "").strip()}
    seen: Dict[str, int] = {}
    fields_by_key = {f"cf:{f.field_id}": f for f in fields}
    out: List[ImportRow] = []

    def cell(row, key):
        col = mapping.get(key, -1)
        if col is None or col < 0 or col >= len(row):
            return None
        value = row[col]
        return None if isinstance(value, str) and not value.strip() else value

    for index, row in enumerate(data_rows):
        item = ImportRow(first_line + index)
        if not any(str(v).strip() for v in row if v is not None):
            continue
        values = item.values
        text = cell(row, "text")
        values["text"] = str(text).strip() if text is not None else ""
        if not values["text"]:
            item.errors.append("no comment text")

        ext = cell(row, "ext_id")
        ext = str(ext).strip() if ext is not None else ""
        if isinstance(cell(row, "ext_id"), float) and float(cell(row, "ext_id")).is_integer():
            ext = str(int(cell(row, "ext_id")))
        if ext:
            key = ext.casefold()
            if key in existing:
                item.errors.append(f"duplicate ID '{ext}': already in the register")
            elif key in seen:
                item.errors.append(f"duplicate ID '{ext}': also on row {seen[key]}")
            else:
                seen[key] = item.line
            if _uuid_like(ext):
                values["comment_id"] = ext.lower()
            else:
                values["ext_id"] = ext

        status = cell(row, "status")
        if status is not None:
            values["status"], known = match_choice(status, status_list)
            if not known:
                item.warnings.append(f"status '{status}' is not one of the set's statuses (kept)")
        else:
            values["status"] = status_list[0] if status_list else ""
        priority = cell(row, "priority")
        if priority is not None:
            values["priority"], known = match_choice(priority, priority_list)
            if not known:
                item.warnings.append(f"priority '{priority}' is not one of the set's priorities (kept)")
        else:
            values["priority"] = default_priority(priority_list)

        author = cell(row, "author")
        values["author"] = str(author).strip() if author is not None else default_author
        date = cell(row, "comment_date")
        if date is not None:
            values["comment_date"] = parse_date(date)
            if not values["comment_date"]:
                item.warnings.append(f"date '{date}' not understood (left empty)")
        else:
            values["comment_date"] = default_date
        tags = cell(row, "tags")
        values["tags"] = str(tags).strip() if tags is not None else ""

        # Location.
        lat_raw, lon_raw = cell(row, "lat"), cell(row, "lon")
        kp_raw, kp_end_raw = cell(row, "kp_km"), cell(row, "kp_end_km")
        lat = parse_coordinate(lat_raw, True) if lat_raw is not None else None
        lon = parse_coordinate(lon_raw, False) if lon_raw is not None else None
        kp = parse_kp(kp_raw) if kp_raw is not None else None
        kp_end = parse_kp(kp_end_raw) if kp_end_raw is not None else None
        if lat_raw is not None and lat is None:
            item.errors.append(f"latitude '{lat_raw}' not understood")
        if lon_raw is not None and lon is None:
            item.errors.append(f"longitude '{lon_raw}' not understood")
        if kp_raw is not None and kp is None:
            item.errors.append(f"KP '{kp_raw}' not understood")
        if kp_end_raw is not None and kp_end is None:
            item.errors.append(f"KP to '{kp_end_raw}' not understood")
        if lat is not None and lon is not None:
            item.kind = REF_LOCATION
            values["lat"], values["lon"] = lat, lon
            if kp is not None:
                item.warnings.append("KP ignored: the row has a position (its nearest KP is measured)")
        elif (lat_raw is None) != (lon_raw is None) and not item.errors:
            item.errors.append("a position needs both latitude and longitude")
        elif kp is not None:
            item.kind = REF_KP_RANGE if kp_end is not None and abs(kp_end - kp) > 1e-9 else REF_ROUTE_POINT
            values["kp_km"] = kp
            if item.kind == REF_KP_RANGE:
                values["kp_end_km"] = kp_end
            if kp_range is None:
                item.errors.append("KPs need a route: choose the route the KPs refer to")
            else:
                lo, hi = min(kp_range), max(kp_range)
                for label, value in (("KP", kp), ("KP to", kp_end if item.kind == REF_KP_RANGE else None)):
                    if value is not None and not (lo - 1e-9 <= value <= hi + 1e-9):
                        item.errors.append(f"{label} {value:.3f} is off the route (KP {lo:.3f} – {hi:.3f})")
        elif not any(e for e in item.errors if "not understood" in e):
            item.errors.append("no location: give latitude and longitude, or a KP")

        custom = {}
        for key, fld in fields_by_key.items():
            raw = cell(row, key)
            value, problem = coerce_custom(fld, raw)
            if problem:
                (item.warnings if value is not None else item.errors).append(problem)
            if value is not None:
                custom[fld.field_id] = value
        values["custom_json"] = dump_custom(custom)

        paths = []
        for name in split_image_names(cell(row, "images")):
            path = resolve_image(name, image_folder)
            if not is_image_name(path):
                item.warnings.append(f"'{name}' is not an image ({', '.join(IMAGE_EXTENSIONS)}); left out")
            elif not os.path.isfile(path):
                item.warnings.append(f"image '{name}' not found; left out")
            elif path not in paths:
                paths.append(path)
        if paths:
            values["attach_paths"] = paths
        out.append(item)
    return out


def import_summary(rows: Sequence[ImportRow]) -> str:
    good = sum(1 for r in rows if r.ok)
    bad = len(rows) - good
    warned = sum(1 for r in rows if r.ok and r.warnings)
    text = f"{good} of {len(rows)} rows will be imported"
    if bad:
        text += f"; {bad} with errors will be skipped"
    if warned:
        text += f"; {warned} with notes"
    return text + "."
