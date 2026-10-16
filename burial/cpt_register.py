# -*- coding: utf-8 -*-
"""CPTs in the Burial Planner: import, placement on a plan's route, map layer.

A *campaign* is one import: a set of AGS files, optionally joined by CPT
name to a position / summary table, with the user's choices (``config``):

``files``      ``[{"path", "fingerprint"}]``
``name_rule``  how each test is named (see ``geotech.cpt.NAME_RULES``)
``positions``  ``{path, sheet, header_row, columns: {role: header},
               coords: "latlon"|"grid", crs, loose, fingerprint}`` or ``{}``
``grid_crs``   CRS of AGS easting/northing (LOCA_NATE/NATN) when the file
               gives no EPSG code (LOCA_NSRI), also used for local x/y
``ags3_pore``  the channel an AGS 3 STCN_PWP1 is read as (u2 by default)

Positions are stored in WGS84. KP and cross-course offset are measured on
each plan's route whenever CPTs are shown, so moving a plan to another RPL
needs no re-referencing. Readings are stored as delivered (canonical
units); derived values are computed from the project's CPT settings when
shown (``geotech.cpt.derive``).
"""

from __future__ import annotations

import json
import math
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsPointXY,
    QgsProject,
)

from ..geotech import ags as ags_mod
from ..geotech import cpt as cpt_mod
from ..geotech import positions as pos_mod
from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_POINT
from . import schema
from .plan_import import parse_delimited, read_grid

MAX_TABLE_ROWS = 200002


# -- sources ------------------------------------------------------------------------
def file_fingerprint(path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{int(stat.st_mtime)}:{stat.st_size}"


def read_files(paths: Sequence[str], ags3_pore: str = "u2") -> Tuple[List[Dict], List[Dict]]:
    """``(tests, reports)`` for AGS files; one report per file.

    A report has ``path``, ``file``, ``version``, ``groups``, ``tests``,
    ``readings``, ``error`` and ``warnings``. A file that cannot be read is
    reported, not raised, so one bad file never blocks the others.
    """
    tests: List[Dict] = []
    reports: List[Dict] = []
    for path in paths:
        report = {"path": path, "file": os.path.basename(path), "version": "", "groups": "",
                  "tests": 0, "readings": 0, "error": "", "warnings": []}
        try:
            found, ags = cpt_mod.read_tests(path, ags3_pore)
        except (ags_mod.AgsError, OSError, ValueError) as exc:
            report["error"] = str(exc)
            reports.append(report)
            continue
        report.update(version=ags.version_label, groups=", ".join(ags.groups),
                      tests=len(found), readings=sum(len(t["depth"]) for t in found),
                      warnings=list(ags.warnings))
        if not found:
            report["error"] = ("no CPT readings (no CPTT, SCPT or STCN data rows) — groups: "
                               + (", ".join(ags.groups) or "none"))
        tests.extend(found)
        reports.append(report)
    return tests, reports


def read_table_grid(path: str, sheet: Optional[str] = None) -> Tuple[List[List[str]], List[str]]:
    """``(grid, sheets)`` of a CSV/XLSX table. A CSV that is not UTF-8 (an
    ANSI export with ° in its coordinates) is read as Windows-1252."""
    try:
        return read_grid(path, sheet or None, max_rows=MAX_TABLE_ROWS)
    except UnicodeDecodeError:
        with open(path, "r", encoding="cp1252", errors="replace", newline="") as handle:
            rows = parse_delimited(handle.read(), MAX_TABLE_ROWS)
        width = max((len(r) for r in rows), default=0)
        return [r + [""] * (width - len(r)) for r in rows], []


def read_positions_grid(positions: Dict) -> Tuple[List[List[str]], List[str]]:
    path = positions.get("path") or ""
    if not path or not os.path.exists(path):
        raise ValueError(f"The position table {path or '(none)'} no longer exists.")
    grid, sheets = read_table_grid(path, positions.get("sheet") or None)
    if len(grid) >= MAX_TABLE_ROWS:
        raise ValueError("The position table exceeds 200 000 rows; split it.")
    return grid, sheets


def wgs84_transform(crs_text: str) -> Optional[Callable[[float, float], Tuple[float, float]]]:
    """``(x, y) -> (lon, lat)`` from ``crs_text`` (an authid or WKT), else None."""
    crs = QgsCoordinateReferenceSystem(crs_text) if crs_text else QgsCoordinateReferenceSystem()
    if not crs.isValid():
        return None
    transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                       QgsProject.instance())

    def convert(x: float, y: float) -> Tuple[float, float]:
        point = transform.transform(QgsPointXY(float(x), float(y)))
        return point.x(), point.y()
    return convert


# -- assembling a campaign ------------------------------------------------------------
def build(config: Dict, tests: Optional[List[Dict]] = None, grid: Optional[List[List[str]]] = None,
          campaign_id: str = "") -> Dict:
    """CPT rows and readings for a campaign ``config``.

    ``tests`` / ``grid`` may be passed when already read (the import dialog
    reads them once while the user chooses); otherwise they are read from
    the files in ``config``. Returns ``{"cpts", "readings", "notes",
    "reports", "unmatched_table"}``. Nothing is raised for a single bad CPT:
    it is imported without a position and listed in ``notes``.
    """
    notes: List[str] = []
    reports: List[Dict] = []
    if tests is None:
        tests, reports = read_files([f.get("path") or "" for f in config.get("files") or []],
                                    config.get("ags3_pore") or "u2")
        for report in reports:
            if report["error"]:
                notes.append(f"{report['file']}: {report['error']}")
    notes += cpt_mod.assign_names(tests, config.get("name_rule") or cpt_mod.NAME_AUTO)
    positions = config.get("positions") or {}
    table: Dict[str, Dict] = {}
    if positions.get("path") and (positions.get("columns") or {}).get("id"):
        if grid is None:
            grid, _sheets = read_positions_grid(positions)
        table, problems = pos_mod.read_table(grid, int(positions.get("header_row") or 1),
                                             positions.get("columns") or {})
        notes += [f"Position table {p}" for p in problems]
    names = [t["name"] for t in tests]
    matches, _unmatched, unused = pos_mod.match_names(names, list(table), bool(positions.get("loose", True)))
    loose = [f"{name} ≈ {key}" for name, (key, how) in matches.items() if how == "loose"]
    if loose:
        notes.append(f"{len(loose)} CPT name(s) matched the table ignoring case, spaces or "
                     "punctuation: " + ", ".join(loose[:8]) + (" …" if len(loose) > 8 else ""))
    table_transform = None
    if positions.get("coords") == "grid":
        table_transform = wgs84_transform(positions.get("crs") or "")
        if table_transform is None and any(r.get("x") is not None for r in table.values()):
            notes.append("The position table's easting/northing CRS is not set; those positions "
                         "were not used.")
    grid_transform = wgs84_transform(config.get("grid_crs") or "") if config.get("grid_crs") else None
    file_transforms: Dict[str, object] = {}

    cpts: List[Dict] = []
    readings: Dict[str, Dict] = {}
    for test in tests:
        name = test["name"]
        record = table.get(matches[name][0]) if name in matches else None
        location = test.get("location") or {}
        lat = lon = None
        source = ""
        if record is not None:
            if record.get("lat") is not None:
                lat, lon, source = record["lat"], record["lon"], "table"
            elif record.get("x") is not None and table_transform is not None:
                try:
                    lon, lat = table_transform(record["x"], record["y"])
                    source = "table"
                except QgsCsException as exc:
                    notes.append(f"{name}: the table position could not be transformed ({exc})")
        if source == "" and location.get("lat") is not None:
            lat, lon, source = location["lat"], location["lon"], "ags"
        if source == "":
            # The file's own EPSG code (AGS 4.2 LOCA_NSRI) wins over the choice
            # made for files that give none.
            crs = location.get("grid_crs") or ""
            transform = grid_transform
            if crs:
                if crs not in file_transforms:
                    file_transforms[crs] = wgs84_transform(crs)
                transform = file_transforms[crs] or grid_transform
            for x_key, y_key, label in (("easting", "northing", "ags"),
                                        ("local_x", "local_y", "ags local grid")):
                if location.get(x_key) is None or location.get(y_key) is None or transform is None:
                    continue
                try:
                    lon, lat = transform(location[x_key], location[y_key])
                    source = label
                except QgsCsException as exc:
                    notes.append(f"{name}: the AGS position could not be transformed ({exc})")
                break
        if lat is not None and not (math.isfinite(lat) and math.isfinite(lon)
                                    and abs(lat) <= 90 and abs(lon) <= 180):
            notes.append(f"{name}: the position is outside the valid latitude/longitude range")
            lat = lon = None
            source = ""
        water = record.get("water_depth") if record else None
        water_source = "table" if water is not None else ""
        if water is None and location.get("water_depth") is not None:
            water, water_source = abs(location["water_depth"]), location.get("water_depth_source") or "ags"
        for key, label in (("ground_level", "AGS ground level"), ("local_z", "AGS local level")):
            if water is None and location.get(key) is not None and location[key] < 0:
                water, water_source = abs(location[key]), label
        final = record.get("penetration") if record else None
        if final is None:
            final = location.get("final_depth")
        layers = list(record.get("layers") or []) if record else []
        layer_source = "table" if layers else ""
        if not layers and test.get("geology"):
            layers, layer_source = list(test["geology"]), "ags"
        tested = " ".join(v for v in (_tidy_date(record.get("date")) if record else "",
                                      record.get("time") if record else "") if v) \
            or location.get("date") or ""
        depth = test["depth"]
        qc = test["channels"].get("qc") or []
        qcs = [v for v in qc if v is not None]
        cpt_id = cpt_mod.cpt_id(campaign_id, name)
        cpts.append({
            "cpt_id": cpt_id, "name": name, "location_id": test["location_id"],
            "test_ref": test["test_ref"], "source_file": test["file"],
            "ags_version": test["ags_version"], "lat": lat, "lon": lon,
            "position_source": source, "water_depth_m": water,
            "final_depth_m": final, "max_depth_m": max(depth) if depth else None,
            "readings": len(depth), "max_qc_mpa": max(qcs) if qcs else None,
            "tested": tested, "summary": (record.get("summary") if record else "") or "",
            "termination": test.get("termination") or "", "layers": layers,
            "meta": {"path": test["path"], "location": location, "general": test["general"],
                     "project": test["project"], "warnings": test["warnings"],
                     "headings": test["headings"], "area_ratio": test.get("area_ratio"),
                     "zero_reference": test.get("zero_reference") or "",
                     "predrilled_m": test.get("predrilled_m"),
                     "interpreted": test.get("interpreted") or [], "group": test.get("group"),
                     "table_row": record.get("row") if record else None,
                     "water_depth_source": water_source, "layer_source": layer_source,
                     "name_rule": test.get("name_rule", "")},
        })
        readings[cpt_id] = {"depth": depth, "channels": test["channels"], "units": test["units"]}
    unplaced = [c["name"] for c in cpts if c["lat"] is None]
    if unplaced:
        notes.append(f"{len(unplaced)} CPT(s) have no position: " + ", ".join(unplaced[:10])
                     + (" …" if len(unplaced) > 10 else ""))
    return {"cpts": cpts, "readings": readings, "notes": notes, "reports": reports,
            "unmatched_table": unused}


def _tidy_date(text) -> str:
    value = str(text or "").strip()
    return value[:-9] if value.endswith(" 00:00:00") else value


def changed_sources(campaign: Dict) -> List[str]:
    """Notes for files (or the position table) changed since the import."""
    config = campaign.get("config") or {}
    out = []
    for entry in config.get("files") or []:
        path = entry.get("path") or ""
        now = file_fingerprint(path)
        if not now:
            out.append(f"{os.path.basename(path)} cannot be found")
        elif entry.get("fingerprint") and now != entry["fingerprint"]:
            out.append(f"{os.path.basename(path)} has changed")
    positions = config.get("positions") or {}
    if positions.get("path"):
        now = file_fingerprint(positions["path"])
        if not now:
            out.append(f"{os.path.basename(positions['path'])} cannot be found")
        elif positions.get("fingerprint") and now != positions["fingerprint"]:
            out.append(f"{os.path.basename(positions['path'])} has changed")
    return out


def stamp_fingerprints(config: Dict) -> Dict:
    """``config`` with each source's current fingerprint recorded."""
    out = dict(config)
    out["files"] = [dict(f, fingerprint=file_fingerprint(f.get("path") or ""))
                    for f in config.get("files") or []]
    if (config.get("positions") or {}).get("path"):
        out["positions"] = dict(config["positions"],
                                fingerprint=file_fingerprint(config["positions"]["path"]))
    return out


# -- the register -------------------------------------------------------------------
class CptRegister:
    """Cached CPT headers and readings of one store (reloaded on change)."""

    def __init__(self, store):
        self.store = store
        self._key = None
        self.campaigns: List[Dict] = []
        self.cpts: List[Dict] = []
        self._readings: Dict[str, Dict] = {}
        self._derived: Dict[Tuple, Dict] = {}

    def refresh(self, force: bool = False) -> bool:
        """Re-read when the campaigns changed; True when something was re-read."""
        campaigns = self.store.list_cpt_campaigns()
        key = tuple((c.get("campaign_id"), c.get("imported_utc"), c.get("name"),
                     c.get("status")) for c in campaigns)
        if not force and key == self._key:
            self.campaigns = campaigns
            return False
        self.campaigns = campaigns
        self.cpts = self.store.list_cpts()
        self._readings = {}
        self._derived = {}
        self._key = key
        return True

    def invalidate(self) -> None:
        self._key = None

    def campaign(self, campaign_id: str) -> Optional[Dict]:
        return next((c for c in self.campaigns if c.get("campaign_id") == campaign_id), None)

    def by_id(self, cpt_id: str) -> Optional[Dict]:
        return next((c for c in self.cpts if c.get("cpt_id") == cpt_id), None)

    def readings(self, cpt_ids: Sequence[str]) -> Dict[str, Dict]:
        missing = [i for i in cpt_ids if i not in self._readings]
        if missing:
            self._readings.update(self.store.cpt_readings(missing))
        return {i: self._readings.get(i) or {} for i in cpt_ids}

    def superseded_ids(self) -> set:
        return {c.get("campaign_id") for c in self.campaigns if c.get("status") == "superseded"}

    def active_cpts(self) -> List[Dict]:
        """CPTs of imports not marked superseded (those drawn and placed)."""
        superseded = self.superseded_ids()
        return [c for c in self.cpts if c.get("campaign_id") not in superseded]

    def unique_names(self, cpts: Sequence[Dict]) -> Dict[str, str]:
        """``{cpt_id: name}``; a name shared by CPTs of different imports
        gains the import's name, e.g. "CPT01 [Rev 1 CPTs]"."""
        counts: Dict[str, int] = {}
        for cpt in cpts:
            counts[cpt.get("name") or ""] = counts.get(cpt.get("name") or "", 0) + 1
        out = {}
        for cpt in cpts:
            name = cpt.get("name") or ""
            if counts[name] > 1:
                campaign = self.campaign(cpt.get("campaign_id") or "") or {}
                name = f"{name} [{campaign.get('name') or cpt.get('campaign_id')}]"
            out[cpt["cpt_id"]] = name
        return out

    def channels(self, cpt: Dict, settings: Dict) -> Dict[str, List]:
        """Measured and derived channels (plus ``depth``), cached per settings."""
        key = (cpt.get("cpt_id"), cpt.get("water_depth_m"),
               json.dumps(settings, sort_keys=True, default=str))
        cached = self._derived.get(key)
        if cached is None:
            test = self.test(cpt)
            cached = cpt_mod.all_channels(test, settings, cpt.get("water_depth_m"))
            cached["depth"] = list(test["depth"])
            if len(self._derived) > 4000:
                self._derived.clear()
            self._derived[key] = cached
        return cached

    def test(self, cpt: Dict) -> Dict:
        """A CPT header and its readings in the shape ``geotech.cpt`` expects."""
        data = self.readings([cpt["cpt_id"]])[cpt["cpt_id"]]
        meta = cpt.get("meta") or {}
        return {"name": cpt.get("name"), "depth": data.get("depth") or [],
                "channels": data.get("channels") or {}, "units": data.get("units") or {},
                "area_ratio": meta.get("area_ratio"), "zero_reference": meta.get("zero_reference") or ""}


# -- placement along a plan's route ---------------------------------------------------
def _route_crs(route):
    try:
        return route._distance.sourceCrs()
    except AttributeError:
        return QgsCoordinateReferenceSystem("EPSG:4326")


def place(route, cpts: Sequence[Dict], max_offset_m: float, half_range_m: float
          ) -> Tuple[List[Dict], List[str]]:
    """KP, signed offset (+ starboard of increasing KP) and the KP range each
    CPT represents on ``route``.

    Every CPT gets a placement row with ``status``: ``placed`` (within
    ``max_offset_m`` of the route), ``too far`` or ``no position``. Placed
    CPTs apply up to ``half_range_m`` either side of their KP, never past
    halfway to the next placed CPT, so ranges never overlap.
    """
    if route is None:
        return [], ["Set the plan's route to place CPTs along it."]
    crs = _route_crs(route)
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                                       QgsProject.instance())
    geographic = crs.isGeographic()
    rows: List[Dict] = []
    for cpt in cpts:
        row = {"cpt_id": cpt.get("cpt_id"), "name": cpt.get("name"), "kp": None,
               "offset_m": None, "start_kp": None, "end_kp": None, "status": "no position"}
        rows.append(row)
        if cpt.get("lat") is None or cpt.get("lon") is None:
            continue
        try:
            point = transform.transform(QgsPointXY(float(cpt["lon"]), float(cpt["lat"])))
        except QgsCsException:
            row["status"] = "no position"
            continue
        hit = route.kp_at_point(point)
        if hit.snapped_xy is None or not math.isfinite(hit.dcc_m):
            continue
        row["kp"] = hit.kp_km
        row["offset_m"] = hit.dcc_m * _side(route, hit.kp_km, hit.snapped_xy, point, geographic)
        row["status"] = "placed" if hit.dcc_m <= max_offset_m + 1e-9 else "too far"
    notes: List[str] = []
    placed = sorted((r for r in rows if r["status"] == "placed"),
                    key=lambda r: (r["kp"], abs(r["offset_m"] or 0.0)))
    half = max(0.0, float(half_range_m)) / 1000.0
    lo_limit, hi_limit = route.start_kp_km, route.end_kp_km
    for i, row in enumerate(placed):
        lo = row["kp"] - half
        hi = row["kp"] + half
        if i > 0:
            lo = max(lo, (placed[i - 1]["kp"] + row["kp"]) / 2.0)
        if i + 1 < len(placed):
            hi = min(hi, (placed[i + 1]["kp"] + row["kp"]) / 2.0)
        row["start_kp"] = max(lo, lo_limit)
        row["end_kp"] = min(hi, hi_limit)
    close = [f"{a['name']} / {b['name']}" for a, b in zip(placed, placed[1:])
             if (b["kp"] - a["kp"]) * 1000.0 < 1.0]
    if close:
        notes.append("CPTs less than 1 m apart along the route share their KP: "
                     + ", ".join(close[:6]) + (" …" if len(close) > 6 else ""))
    far = [r["name"] for r in rows if r["status"] == "too far"]
    if far:
        notes.append(f"{len(far)} CPT(s) are more than {max_offset_m:g} m from the route")
    return rows, notes


def _side(route, kp: float, snapped: QgsPointXY, point: QgsPointXY, geographic: bool) -> float:
    """+1 when ``point`` is starboard of the route at ``kp`` (right of
    increasing KP), -1 port, +1 when on the line."""
    step = 0.001  # km
    a = route.point_at_kp(max(route.start_kp_km, kp - step), clamp=True)
    b = route.point_at_kp(min(route.end_kp_km, kp + step), clamp=True)
    if a is None or b is None:
        return 1.0
    scale = math.cos(math.radians(snapped.y())) if geographic else 1.0
    dx, dy = (b.x() - a.x()) * scale, b.y() - a.y()
    px, py = (point.x() - snapped.x()) * scale, point.y() - snapped.y()
    cross = dx * py - dy * px
    return -1.0 if cross > 0 else 1.0


def side_text(offset_m: Optional[float]) -> str:
    if offset_m is None:
        return ""
    if abs(offset_m) < 0.5:
        return "on route"
    return f"{abs(offset_m):.0f} m {'stbd' if offset_m > 0 else 'port'}"


# -- map layer -------------------------------------------------------------------------
def write_layer(store, campaigns: Sequence[Dict], cpts: Sequence[Dict]) -> int:
    names = {c.get("campaign_id"): c.get("name") or "" for c in campaigns}
    rows = []
    for cpt in cpts:
        if cpt.get("lat") is None or cpt.get("lon") is None:
            continue
        row = {key: cpt.get(key) for key, _type in schema.CPT_LAYER_FIELDS}
        row["campaign"] = names.get(cpt.get("campaign_id"), "")
        row[WKT_KEY] = f"POINT ({float(cpt['lon'])!r} {float(cpt['lat'])!r})"
        rows.append(row)
    return store.write_spatial_layer(schema.CPT_LAYER_NAME, schema.CPT_LAYER_FIELDS, WKB_POINT, rows)
