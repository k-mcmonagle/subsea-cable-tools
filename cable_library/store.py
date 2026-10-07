# -*- coding: utf-8 -*-
"""Read and write the cable type library GeoPackage.

The library is one attribute-only table, ``cable_types``, in a GeoPackage
the user picks (remembered in QSettings). The table is created with the
QGIS vector-file writer (so it is a valid GeoPackage layer that QGIS can
open and edit too); rows are read and replaced with plain ``sqlite3`` in a
single transaction. Matching a lay-data cable type to a library row uses
the row's name and its comma-separated aliases, case-insensitively.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from ..burial.gpkg_sql import _readonly_uri, has_table
from ..laydata.lay_assessment import CableProps

TABLE = "cable_types"
SETTINGS_KEY = "SubseaCableTools/CableLibrary/path"
CATEGORIES = ("cable", "rope")


@dataclass(frozen=True)
class Column:
    name: str
    kind: str   # "str" | "float"
    label: str
    help: str = ""


COLUMNS: Sequence[Column] = (
    Column("name", "str", "Name", "Unique type name."),
    Column("category", "str", "Category", "cable or rope."),
    Column("aliases", "str", "Aliases", "Other names used for this type in lay data or RPLs, comma separated."),
    Column("description", "str", "Description"),
    Column("diameter_mm", "float", "Diameter (mm)", "Outer diameter."),
    Column("weight_air_kg_m", "float", "Weight in air (kg/m)"),
    Column("weight_water_kg_m", "float", "Weight in water (kg/m)", "Submerged weight."),
    Column("cbl_kn", "float", "CBL (kN)", "Cable breaking load."),
    Column("ntts_kn", "float", "NTTS (kN)", "Nominal transient tensile strength: short, accidental loads "
                                            "(e.g. during recovery)."),
    Column("nots_kn", "float", "NOTS (kN)", "Nominal operating tensile strength: sustained operations "
                                            "(e.g. holding during jointing / repair)."),
    Column("npts_kn", "float", "NPTS (kN)", "Nominal permanent tensile strength: the cable's state after "
                                            "laying (residual tension on the seabed)."),
    Column("mbr_m", "float", "MBR (m)", "Minimum bend radius."),
    Column("notes", "str", "Notes"),
)
COLUMN_NAMES = [column.name for column in COLUMNS]
FLOAT_COLUMNS = {column.name for column in COLUMNS if column.kind == "float"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def to_float(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text or text.upper() == "NULL":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_row(row: Dict) -> Dict:
    """A row with every column present, numbers as floats or None, text stripped."""
    out = {}
    for column in COLUMNS:
        value = row.get(column.name)
        if column.kind == "float":
            out[column.name] = to_float(value)
        else:
            text = "" if value is None else str(value).strip()
            out[column.name] = "" if text.upper() == "NULL" else text
    if not out["category"]:
        out["category"] = "cable"
    return out


def validate(rows: Sequence[Dict]) -> List[str]:
    """Blocking problems: missing / duplicate names, bad categories, negatives."""
    problems: List[str] = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        name = (row.get("name") or "").strip()
        if not name:
            problems.append(f"Row {index}: a name is required.")
            continue
        if name.lower() in seen:
            problems.append(f"Row {index}: the name '{name}' is used twice.")
        seen.add(name.lower())
        if row.get("category") not in CATEGORIES:
            problems.append(f"'{name}': category must be one of {', '.join(CATEGORIES)}.")
        for column in FLOAT_COLUMNS:
            value = row.get(column)
            if value is not None and value < 0 and column != "weight_water_kg_m":
                problems.append(f"'{name}': {column} cannot be negative.")
    return problems


def warnings_for(rows: Sequence[Dict]) -> List[str]:
    """Non-blocking sanity checks (limits normally rise NPTS < NOTS < NTTS < CBL)."""
    notes: List[str] = []
    order = ("npts_kn", "nots_kn", "ntts_kn", "cbl_kn")
    for row in rows:
        values = [(key, row.get(key)) for key in order if row.get(key) is not None]
        for (k1, v1), (k2, v2) in zip(values[:-1], values[1:]):
            if v1 > v2:
                notes.append(f"'{row.get('name')}': {k1[:-3].upper()} ({v1:g} kN) is above "
                             f"{k2[:-3].upper()} ({v2:g} kN).")
        air, water = row.get("weight_air_kg_m"), row.get("weight_water_kg_m")
        if air is not None and water is not None and water > air:
            notes.append(f"'{row.get('name')}': weight in water is above weight in air.")
    return notes


def find_row(rows: Sequence[Dict], text: Optional[str]) -> Optional[Dict]:
    """The row whose name or alias matches ``text`` (case-insensitive)."""
    if not text:
        return None
    wanted = str(text).strip().lower()
    for row in rows:
        if (row.get("name") or "").strip().lower() == wanted:
            return row
    for row in rows:
        aliases = [a.strip().lower() for a in (row.get("aliases") or "").split(",")]
        if wanted in aliases:
            return row
    return None


def to_props(row: Optional[Dict]) -> Optional[CableProps]:
    if row is None:
        return None
    return CableProps(name=row.get("name") or "", weight_water_kg_m=row.get("weight_water_kg_m"),
                      weight_air_kg_m=row.get("weight_air_kg_m"), cbl_kn=row.get("cbl_kn"),
                      ntts_kn=row.get("ntts_kn"), nots_kn=row.get("nots_kn"), npts_kn=row.get("npts_kn"),
                      mbr_m=row.get("mbr_m"))


def read_csv(path: str) -> List[Dict]:
    """Rows from a CSV whose header uses the column names or labels."""
    by_label = {normal(column.label): column.name for column in COLUMNS}
    by_label.update({normal(column.name): column.name for column in COLUMNS})
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            row = {}
            for key, value in raw.items():
                target = by_label.get(normal(key or ""))
                if target:
                    row[target] = value
            if any(str(v).strip() for v in row.values() if v is not None):
                rows.append(clean_row(row))
    return rows


def write_csv(path: str, rows: Sequence[Dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMN_NAMES)
        for row in rows:
            writer.writerow(["" if row.get(name) is None else row.get(name) for name in COLUMN_NAMES])


def normal(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# GeoPackage I/O
# ---------------------------------------------------------------------------
def is_library(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        return has_table(path, TABLE)
    except sqlite3.Error:
        return False


def create_library(path: str, transform_context=None) -> None:
    """Create ``path`` (or add the table to an existing GeoPackage)."""
    from qgis.core import QgsCoordinateTransformContext

    from ..processing import cable_lay_parsers as clp
    from ..qgis_compat import WKB_NO_GEOMETRY

    if is_library(path):
        return
    specs = [(column.name, column.kind) for column in COLUMNS]
    clp.write_layer_to_gpkg(path, TABLE, clp.fields_from_specs(specs), WKB_NO_GEOMETRY, [],
                            transform_context or QgsCoordinateTransformContext())


def read_rows(path: str) -> List[Dict]:
    if not is_library(path):
        raise ValueError(f"{path} is not a cable library (no '{TABLE}' table).")
    with closing(sqlite3.connect(_readonly_uri(path), uri=True)) as conn:
        present = [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")')]
        wanted = [name for name in COLUMN_NAMES if name in present]
        order = "fid" if "fid" in present else "rowid"
        cursor = conn.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in wanted)} '
                              f'FROM "{TABLE}" ORDER BY {order}')
        return [clean_row(dict(zip(wanted, values))) for values in cursor.fetchall()]


def write_rows(path: str, rows: Sequence[Dict]) -> None:
    """Replace every row in one transaction (columns added to old files first)."""
    rows = [clean_row(row) for row in rows]
    problems = validate(rows)
    if problems:
        raise ValueError("\n".join(problems))
    with closing(sqlite3.connect(path)) as conn:
        present = [r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")')]
        if not present:
            raise ValueError(f"{path} has no '{TABLE}' table.")
        with conn:
            for column in COLUMNS:
                if column.name not in present:
                    kind = "REAL" if column.kind == "float" else "TEXT"
                    conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{column.name}" {kind}')
            conn.execute(f'DELETE FROM "{TABLE}"')
            placeholders = ", ".join("?" for _ in COLUMN_NAMES)
            names = ", ".join(f'"{name}"' for name in COLUMN_NAMES)
            conn.executemany(f'INSERT INTO "{TABLE}" ({names}) VALUES ({placeholders})',
                             [[row.get(name) for name in COLUMN_NAMES] for row in rows])


# ---------------------------------------------------------------------------
# The current library (path in QSettings)
# ---------------------------------------------------------------------------
def current_path() -> str:
    from qgis.PyQt.QtCore import QSettings

    return str(QSettings().value(SETTINGS_KEY, "") or "")


def set_current_path(path: str) -> None:
    from qgis.PyQt.QtCore import QSettings

    QSettings().setValue(SETTINGS_KEY, path or "")


def load_current() -> List[Dict]:
    """Rows of the current library, or ``[]`` when none is set / readable."""
    path = current_path()
    if not is_library(path):
        return []
    try:
        return read_rows(path)
    except (ValueError, sqlite3.Error):
        return []
