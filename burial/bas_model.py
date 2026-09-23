# -*- coding: utf-8 -*-
"""Burial Assessment Study (BAS) register for the Burial Planner (pure).

A BAS delivers its conclusions as KP-range tables — required depth of
lowering, achievable depth per tool, BPI/CBRA category, residual risk,
soil province — whose column set differs between consultancies and
projects. The register therefore fixes only the KP range and the
re-referencing provenance as real columns and keeps every other field as
a named value in ``values_json``; the plan-level column list (key, label,
kind, order) lives in ``params_json["bas"]["columns"]`` so the table can
be reshaped without a schema change.

Rows are plain dicts matching ``schema.BAS_ROW_FIELDS`` with the decoded
values attached under ``"values"`` (a ``{column_key: str}`` dict) while
in memory; ``encode_row`` / ``decode_row`` move between the two forms.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import ground_model, schema

KIND_TEXT = "text"
KIND_NUMBER = "number"
KINDS: List[str] = [KIND_TEXT, KIND_NUMBER]
KIND_LABELS: Dict[str, str] = {KIND_TEXT: "Text", KIND_NUMBER: "Number"}
# Number columns may carry "decimals": the places shown in the table and
# written to the export, report and map layer. The stored value keeps what
# was imported or typed; absent = show values as entered.
MAX_DECIMALS = 6

# Fixed (real) columns shown before the flexible ones.
FIXED_KEYS: List[str] = ["start_kp", "end_kp"]
PROVENANCE_KEYS: List[str] = ["src_start_kp", "src_end_kp", "src_rpl",
                              "rereference_flags"]
_RESERVED = set(FIXED_KEYS) | set(PROVENANCE_KEYS) | {
    "row_id", "plan_id", "seq", "values_json", "values", "notes"}


# -- columns -----------------------------------------------------------------
def column_key(label: str, taken: Iterable[str] = ()) -> str:
    """A stable JSON key from a header label (unique among ``taken``)."""
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().casefold()).strip("_")
    base = base or "col"
    if base in _RESERVED:
        base = f"f_{base}"
    key = base
    used = set(taken)
    n = 2
    while key in used:
        key = f"{base}_{n}"
        n += 1
    return key


def make_column(label: str, kind: str = KIND_TEXT,
                taken: Iterable[str] = ()) -> Dict:
    return {"key": column_key(label, taken), "label": (label or "").strip() or "Column",
            "kind": kind if kind in KINDS else KIND_TEXT}


def normalise_columns(columns: Optional[Sequence[Dict]]) -> List[Dict]:
    out: List[Dict] = []
    seen: set = set()
    for col in columns or []:
        if not isinstance(col, dict):
            continue
        key = str(col.get("key") or "").strip()
        label = str(col.get("label") or key).strip()
        if not key:
            key = column_key(label, seen)
        if key in seen or key in _RESERVED:
            continue
        seen.add(key)
        kind = str(col.get("kind") or KIND_TEXT)
        clean = {"key": key, "label": label or key,
                 "kind": kind if kind in KINDS else KIND_TEXT}
        decimals = column_decimals(col)
        if decimals is not None and clean["kind"] == KIND_NUMBER:
            clean["decimals"] = decimals
        out.append(clean)
    return out


def column_decimals(column: Dict) -> Optional[int]:
    """The column's decimal places (None = as entered)."""
    value = column.get("decimals") if isinstance(column, dict) else None
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        places = int(value)
    except (TypeError, ValueError):
        return None
    return places if 0 <= places <= MAX_DECIMALS else None


def decimals_label(places: Optional[int]) -> str:
    """'As entered' / '3 (0.001)' for the decimal-places choices."""
    if places is None:
        return "As entered"
    if places == 0:
        return "0 (1)"
    return f"{places} (0.{'0' * (places - 1)}1)"


def format_value(text, column: Dict) -> str:
    """A cell value at the column's decimal places. Text columns, blank
    cells and non-numeric text come back unchanged."""
    raw = "" if text is None else str(text)
    if column.get("kind") != KIND_NUMBER:
        return raw
    places = column_decimals(column)
    if places is None:
        return raw
    value = _to_number(raw)
    if value is None:
        return raw
    out = f"{value:.{places}f}"
    return out[1:] if out.startswith("-") and float(out) == 0 else out


def guess_kind(values: Iterable[str]) -> str:
    seen = 0
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        seen += 1
        if _to_number(text) is None:
            return KIND_TEXT
    return KIND_NUMBER if seen else KIND_TEXT


# -- rows --------------------------------------------------------------------
def _to_number(text) -> Optional[float]:
    if text is None:
        return None
    cell = str(text).strip().replace(",", ".")
    if not cell:
        return None
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def decode_row(row: Dict) -> Dict:
    """Store row → in-memory row with ``values`` dict and typed KPs."""
    out = dict(row)
    values = out.get("values")
    if not isinstance(values, dict):
        try:
            values = json.loads(out.get("values_json") or "{}")
        except (TypeError, ValueError):
            values = {}
        if not isinstance(values, dict):
            values = {}
    out["values"] = {str(k): ("" if v is None else str(v)) for k, v in values.items()}
    start = _float_or_none(out.get("start_kp"))
    end = _float_or_none(out.get("end_kp"))
    if start is not None and end is not None and end < start:
        start, end = end, start
    out["start_kp"], out["end_kp"] = start, end
    for key in ("src_start_kp", "src_end_kp"):
        out[key] = _float_or_none(out.get(key))
    for key in ("src_rpl", "rereference_flags", "notes"):
        out[key] = str(out.get(key) or "")
    try:
        out["seq"] = int(out.get("seq") or 0)
    except (TypeError, ValueError):
        out["seq"] = 0
    return out


def encode_row(row: Dict) -> Dict:
    """In-memory row → store row (``values`` folded into ``values_json``)."""
    out = decode_row(row)
    values = out.pop("values", {})
    out["values_json"] = json.dumps(values, ensure_ascii=False)
    out.pop("_map_start", None)
    out.pop("_map_end", None)
    return {k: out.get(k) for k, _t in schema.BAS_ROW_FIELDS}


def sort_rows(rows: Iterable[Dict]) -> List[Dict]:
    out = [decode_row(r) for r in rows or []]
    out.sort(key=lambda r: (r["start_kp"] if r["start_kp"] is not None else 1e12,
                            r["end_kp"] if r["end_kp"] is not None else 1e12))
    for i, row in enumerate(out):
        row["seq"] = i
    return out


def validate_rows(rows: Iterable[Dict], columns: Sequence[Dict] = ()
                  ) -> List[str]:
    """Problems: missing/inverted KPs (blocking) and overlapping ranges,
    non-numeric values in number columns (notes)."""
    issues: List[str] = []
    decoded = [decode_row(r) for r in rows or []]
    number_keys = [c["key"] for c in normalise_columns(columns)
                   if c.get("kind") == KIND_NUMBER]
    labels = {c["key"]: c["label"] for c in normalise_columns(columns)}
    for i, row in enumerate(decoded):
        tag = f"Row {i + 1}"
        if row["start_kp"] is None or row["end_kp"] is None:
            issues.append(f"{tag}: start and end KP are required.")
            continue
        if row["end_kp"] - row["start_kp"] <= 1e-9:
            issues.append(f"{tag}: end KP must be greater than start KP.")
        for key in number_keys:
            text = row["values"].get(key, "")
            if text.strip() and _to_number(text) is None:
                issues.append(f"{tag}: '{labels.get(key, key)}' is not a number "
                              f"({text.strip()[:20]}).")
    ranged = sorted((r for r in decoded if r["start_kp"] is not None
                     and r["end_kp"] is not None), key=lambda r: r["start_kp"])
    for a, b in zip(ranged, ranged[1:]):
        if b["start_kp"] < a["end_kp"] - 1e-6:
            issues.append(f"Rows overlap around KP {schema.format_kp(b['start_kp'])} "
                          f"({schema.format_kp(a['start_kp'])}–{schema.format_kp(a['end_kp'])} "
                          f"and {schema.format_kp(b['start_kp'])}–{schema.format_kp(b['end_kp'])}).")
    return issues


def rows_at_kp(rows: Iterable[Dict], kp: float) -> List[Dict]:
    out = []
    for row in rows or []:
        start, end = row.get("start_kp"), row.get("end_kp")
        if start is None or end is None:
            continue
        if float(start) - 1e-9 <= kp <= float(end) + 1e-9:
            out.append(row)
    return out


def coverage_gaps(rows: Iterable[Dict], start_kp: float, end_kp: float
                  ) -> List[Tuple[float, float]]:
    """Scope stretches no BAS row covers (exact interval arithmetic)."""
    lo, hi = sorted((float(start_kp), float(end_kp)))
    if hi - lo <= 1e-9:
        return []
    spans = sorted((max(lo, float(r["start_kp"])), min(hi, float(r["end_kp"])))
                   for r in (decode_row(x) for x in rows or [])
                   if r["start_kp"] is not None and r["end_kp"] is not None)
    gaps: List[Tuple[float, float]] = []
    cursor = lo
    for a, b in spans:
        if b <= cursor:
            continue
        if a > cursor + 1e-9:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi - 1e-9:
        gaps.append((cursor, hi))
    return gaps


def rereference_rows(rows: Iterable[Dict], kp_map, source_label: str = "",
                     use_source_kps: bool = True, stretch_tol: float = 0.10
                     ) -> Tuple[List[Dict], Dict[str, int]]:
    from . import kp_rereference as kr

    mapped, tally = kr.rereference_rows(
        [decode_row(r) for r in rows or []], kp_map, source_label=source_label,
        use_source_kps=use_source_kps, stretch_tol=stretch_tol)
    return [decode_row(r) for r in mapped], tally


# -- import ------------------------------------------------------------------
def guess_kp_columns(headers: Sequence[str]) -> Tuple[Optional[int], Optional[int]]:
    mapping = ground_model.guess_mapping(headers)
    return (mapping.get(ground_model.TARGET_START),
            mapping.get(ground_model.TARGET_END))


def table_to_rows(headers: Sequence[str], table: Sequence[Sequence[str]],
                  start_index: int, end_index: int,
                  include: Optional[Sequence[int]] = None,
                  kp_in_metres: bool = False,
                  existing_columns: Sequence[Dict] = ()
                  ) -> Tuple[List[Dict], List[Dict], List[str]]:
    """Imported table → ``(rows, columns, problems)``.

    Every header other than the two KP columns becomes a flexible column
    (restricted to ``include`` indexes when given); headers matching an
    existing column label (case-insensitive) reuse its key so an
    appended import lands in the same columns. Kinds are guessed from
    the data.
    """
    existing = normalise_columns(existing_columns)
    by_label = {c["label"].casefold(): c for c in existing}
    taken = {c["key"] for c in existing}
    columns: List[Dict] = list(existing)
    col_map: Dict[int, str] = {}
    wanted = set(include) if include is not None else None
    for i, header in enumerate(headers):
        if i in (start_index, end_index):
            continue
        if wanted is not None and i not in wanted:
            continue
        label = (header or "").strip() or f"Column {i + 1}"
        found = by_label.get(label.casefold())
        if found is not None:
            col_map[i] = found["key"]
            continue
        sample = [row[i] if i < len(row) else "" for row in table]
        col = make_column(label, guess_kind(sample), taken)
        taken.add(col["key"])
        columns.append(col)
        by_label[label.casefold()] = col
        col_map[i] = col["key"]
    rows: List[Dict] = []
    problems: List[str] = []
    for n, raw in enumerate(table, start=2):
        start = _to_number(_strip_units(raw[start_index] if start_index < len(raw) else ""))
        end = _to_number(_strip_units(raw[end_index] if end_index < len(raw) else ""))
        if start is None and end is None and not any(str(c).strip() for c in raw):
            continue
        if start is None or end is None:
            problems.append(f"Row {n}: start/end KP missing or not numeric.")
            continue
        if kp_in_metres:
            start, end = start / 1000.0, end / 1000.0
        values = {key: (str(raw[i]).strip() if i < len(raw) else "")
                  for i, key in col_map.items()}
        rows.append(decode_row({"row_id": schema.new_id(), "start_kp": start,
                                "end_kp": end, "values": values}))
    return rows, columns, problems


def _strip_units(text) -> str:
    cell = str(text or "").strip()
    for unit in ("km", "m"):
        if cell.lower().endswith(unit):
            cell = cell[:-len(unit)].strip()
    return cell


# -- map layer ---------------------------------------------------------------
def layer_fields(columns: Sequence[Dict]) -> List[Tuple[str, str]]:
    """Field specs for the BAS map layer: fixed head, one field per
    register column (number → float, text → str), provenance tail."""
    specs: List[Tuple[str, str]] = list(schema.BAS_LAYER_BASE_FIELDS)
    taken = {name for name, _t in specs} | {name for name, _t in schema.BAS_LAYER_TAIL_FIELDS}
    for col in normalise_columns(columns):
        key = col["key"]
        if key in taken:
            continue
        taken.add(key)
        specs.append((key, "float" if col.get("kind") == KIND_NUMBER else "str"))
    specs.extend(schema.BAS_LAYER_TAIL_FIELDS)
    return specs


def layer_values(row: Dict, columns: Sequence[Dict]) -> Dict[str, object]:
    """The flexible-column attribute values of one row for the map layer."""
    out: Dict[str, object] = {}
    values = decode_row(row)["values"]
    for col in normalise_columns(columns):
        text = values.get(col["key"], "")
        if col.get("kind") == KIND_NUMBER:
            value = _to_number(text)
            places = column_decimals(col)
            if value is not None and places is not None:
                value = round(value, places)
            out[col["key"]] = value
        else:
            out[col["key"]] = text
    return out


# -- export ------------------------------------------------------------------
def rows_csv(plan: Dict, rows: Sequence[Dict], columns: Sequence[Dict]) -> str:
    cols = normalise_columns(columns)
    lines = [
        f"# plan: {plan.get('name') or ''}",
        f"# rpl: {plan.get('rpl_name') or ''} {plan.get('rpl_revision') or ''}".rstrip(),
        f"# exported_utc: {schema.utc_now_iso()}",
        "# kp: km on the plan's current route",
    ]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["start_kp", "end_kp"] + [c["label"] for c in cols]
                    + ["src_start_kp", "src_end_kp", "src_rpl",
                       "rereference_flags", "notes"])
    for row in sort_rows(rows):
        writer.writerow(
            [_fmt(row.get("start_kp")), _fmt(row.get("end_kp"))]
            + [format_value(row["values"].get(c["key"], ""), c) for c in cols]
            + [_fmt(row.get("src_start_kp")), _fmt(row.get("src_end_kp")),
               row.get("src_rpl") or "", row.get("rereference_flags") or "",
               row.get("notes") or ""])
    return "\n".join(lines) + "\n" + out.getvalue()


def _fmt(value, places: int = 3) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return str(value)
