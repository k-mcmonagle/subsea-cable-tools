# -*- coding: utf-8 -*-
"""CSV export/import for the Burial Planner (pure python).

Exports carry a metadata header block (``# key: value`` lines) so an events
file is never a bare KP list: plan, RPL, method, direction, scope and
generation id travel with every export. KPs are formatted to 3 decimal
places (1 m); lat/lon to 7 places.

Imports accepted (spec §10, Review & Export tab):
- round-trip events CSV (this module's own export format);
- KP-range list ``start_kp,end_kp[,note]`` -> start/end event pairs;
- events list ``kp,event_type[,note]``.
Rows are returned as plain event dicts; the caller validates the invariants
(``events.validate_events``) and logs the import as one change.
"""

from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional, Sequence, Tuple

from . import events as ev
from . import schema

EVENT_COLUMNS = ["seq", "event_type", "label", "kp", "lat", "lon", "depth_m",
                 "source", "status", "locked", "notes"]
SECTION_COLUMNS = ["kind", "start_kp", "end_kp", "length_km", "state",
                   "conclusion", "confidence", "reasons", "notes"]
INPUT_COLUMNS = ["role", "layer_name", "layer_source", "originator", "revision",
                 "status", "received_utc", "quality", "notes"]


def _fmt(value, places: int) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return ""


def metadata_lines(plan: Dict, generation_id: str = "") -> List[str]:
    return [
        f"# plan: {plan.get('name') or ''}",
        f"# plan_id: {plan.get('plan_id') or ''}",
        f"# rpl: {plan.get('rpl_name') or ''}",
        f"# rpl_id: {plan.get('rpl_id') or ''}",
        f"# method: {plan.get('method') or ''}",
        f"# direction: {'A-B' if int(plan.get('direction') or 1) >= 0 else 'B-A'}",
        f"# scope_kp: {schema.format_kp(plan.get('scope_start_kp'))}-"
        f"{schema.format_kp(plan.get('scope_end_kp'))}",
        f"# generation_id: {generation_id or ''}",
        f"# exported_utc: {schema.utc_now_iso()}",
    ]


def events_csv(plan: Dict, events: Sequence[Dict], generation_id: str = "") -> str:
    method = plan.get("method") or ""
    buf = io.StringIO()
    for line in metadata_lines(plan, generation_id):
        buf.write(line + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(EVENT_COLUMNS)
    for event in events:
        writer.writerow([
            int(event.get("seq") or 0),
            event.get("event_type") or "",
            ev.event_label(event.get("event_type") or "", method),
            schema.format_kp(event.get("kp")),
            _fmt(event.get("lat"), 7),
            _fmt(event.get("lon"), 7),
            _fmt(event.get("depth_m"), 1),
            event.get("source") or "",
            event.get("status") or "",
            int(event.get("locked") or 0),
            event.get("notes") or "",
        ])
    return buf.getvalue()


def sections_csv(plan: Dict, sections: Sequence[Dict], generation_id: str = "") -> str:
    buf = io.StringIO()
    for line in metadata_lines(plan, generation_id):
        buf.write(line + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(SECTION_COLUMNS)
    for section in sections:
        writer.writerow([
            section.get("kind") or "",
            schema.format_kp(section.get("start_kp")),
            schema.format_kp(section.get("end_kp")),
            schema.format_kp(section.get("length_km")),
            section.get("state") or "",
            schema.CONCLUSION_LABELS.get(section.get("conclusion") or "",
                                         section.get("conclusion") or ""),
            section.get("confidence") or "",
            section.get("reason_json") or "",
            section.get("notes") or "",
        ])
    return buf.getvalue()


def inputs_csv(plan: Dict, inputs: Sequence[Dict]) -> str:
    buf = io.StringIO()
    for line in metadata_lines(plan):
        buf.write(line + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(INPUT_COLUMNS)
    for row in inputs:
        writer.writerow([row.get(col) if row.get(col) is not None else ""
                         for col in INPUT_COLUMNS])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class ImportError_(ValueError):
    """Raised when an import file cannot be understood."""


def _data_rows(text: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or (row[0] or "").lstrip().startswith("#"):
            continue
        if all(not cell.strip() for cell in row):
            continue
        rows.append([cell.strip() for cell in row])
    return rows


def _float(cell: str) -> Optional[float]:
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def _event(kp: float, event_type: str, source: str, note: str = "",
           status: str = schema.EVENT_STATUS_CANDIDATE, locked: int = 0) -> Dict:
    return {
        "event_id": schema.new_id(),
        "plan_id": "",
        "generation_id": "",
        "seq": 0,
        "event_type": event_type,
        "kp": kp,
        "end_kp": None,
        "lat": None,
        "lon": None,
        "depth_m": None,
        "source": source,
        "status": status,
        "locked": int(locked or 0),
        "notes": note or "",
    }


_TYPE_ALIASES = {
    "burial_start": schema.EVENT_BURIAL_START,
    "burial_end": schema.EVENT_BURIAL_END,
    "pldn": schema.EVENT_BURIAL_START,
    "plup": schema.EVENT_BURIAL_END,
    "jet_start": schema.EVENT_BURIAL_START,
    "jet_stop": schema.EVENT_BURIAL_END,
    "jet_end": schema.EVENT_BURIAL_END,
    "start": schema.EVENT_BURIAL_START,
    "end": schema.EVENT_BURIAL_END,
}


def normalise_event_type(text: str) -> Optional[str]:
    return _TYPE_ALIASES.get((text or "").strip().lower())


def parse_events_csv(text: str, client_proposal: bool = False) -> List[Dict]:
    """Round-trip events CSV (this module's export format) -> event dicts."""
    rows = _data_rows(text)
    if not rows:
        raise ImportError_("The file contains no data rows.")
    header = [cell.lower() for cell in rows[0]]
    if "kp" not in header or "event_type" not in header:
        raise ImportError_("Expected an events CSV with 'event_type' and 'kp' columns.")
    idx = {name: header.index(name) for name in header}
    source = schema.EVENT_SOURCE_CLIENT if client_proposal else schema.EVENT_SOURCE_IMPORT
    out: List[Dict] = []
    for row in rows[1:]:
        def cell(name: str) -> str:
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else ""
        event_type = normalise_event_type(cell("event_type")) \
            or normalise_event_type(cell("label"))
        kp = _float(cell("kp"))
        if event_type is None or kp is None:
            raise ImportError_(
                f"Row {len(out) + 2}: unrecognised event type or KP "
                f"({cell('event_type')!r}, {cell('kp')!r}).")
        event = _event(kp, event_type, source, cell("notes"))
        if (cell("status") or "").lower() == schema.EVENT_STATUS_CONFIRMED:
            event["status"] = schema.EVENT_STATUS_CONFIRMED
        if cell("locked") in ("1", "true", "yes"):
            event["locked"] = 1
        out.append(event)
    return out


def parse_kp_ranges_csv(text: str, client_proposal: bool = False) -> List[Dict]:
    """``start_kp,end_kp[,note]`` rows -> BURIAL_START/END event pairs."""
    rows = _data_rows(text)
    if rows and _float(rows[0][0]) is None:
        rows = rows[1:]  # header row
    source = schema.EVENT_SOURCE_CLIENT if client_proposal else schema.EVENT_SOURCE_IMPORT
    out: List[Dict] = []
    for n, row in enumerate(rows, start=1):
        start = _float(row[0]) if len(row) > 0 else None
        end = _float(row[1]) if len(row) > 1 else None
        note = row[2] if len(row) > 2 else ""
        if start is None or end is None:
            raise ImportError_(f"Row {n}: expected numeric start_kp and end_kp.")
        lo, hi = min(start, end), max(start, end)
        out.append(_event(lo, schema.EVENT_BURIAL_START, source, note))
        out.append(_event(hi, schema.EVENT_BURIAL_END, source, note))
    if not out:
        raise ImportError_("The file contains no KP ranges.")
    return out


def parse_events_list_csv(text: str, client_proposal: bool = False) -> List[Dict]:
    """``kp,event_type[,note]`` rows -> event dicts."""
    rows = _data_rows(text)
    if rows and _float(rows[0][0]) is None:
        rows = rows[1:]
    source = schema.EVENT_SOURCE_CLIENT if client_proposal else schema.EVENT_SOURCE_IMPORT
    out: List[Dict] = []
    for n, row in enumerate(rows, start=1):
        kp = _float(row[0]) if len(row) > 0 else None
        event_type = normalise_event_type(row[1]) if len(row) > 1 else None
        note = row[2] if len(row) > 2 else ""
        if kp is None or event_type is None:
            raise ImportError_(f"Row {n}: expected numeric KP and an event type "
                               "(PLDN/PLUP, JET_START/JET_STOP, START/END).")
        out.append(_event(kp, event_type, source, note))
    if not out:
        raise ImportError_("The file contains no events.")
    return out


def detect_and_parse(text: str, client_proposal: bool = False
                     ) -> Tuple[str, List[Dict]]:
    """Best-effort format detection: returns (format_name, events)."""
    rows = _data_rows(text)
    if not rows:
        raise ImportError_("The file contains no data rows.")
    header = [cell.lower() for cell in rows[0]]
    if "event_type" in header and "kp" in header:
        return "events_csv", parse_events_csv(text, client_proposal)
    first = rows[0] if _float(rows[0][0]) is not None else (rows[1] if len(rows) > 1 else [])
    if len(first) > 1 and _float(first[0]) is not None and _float(first[1]) is not None:
        return "kp_ranges", parse_kp_ranges_csv(text, client_proposal)
    return "events_list", parse_events_list_csv(text, client_proposal)
