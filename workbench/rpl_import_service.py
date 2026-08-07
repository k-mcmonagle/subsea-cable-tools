# -*- coding: utf-8 -*-
"""QGIS-side services for the RPL import core: CRS transform, geodesy,
stated-vs-derived reconciliation, and the rollback-safe Workbench commit.

The pure core (``rpl_import``) knows nothing about QGIS; this module injects
QGIS geodesy into validation, transforms projected coordinates, converts the
neutral :class:`ImportedRpl` into a Workbench :class:`RplModel`, and registers
the revision with an atomic observable outcome:

    Either the ``wb_rpl`` registry row AND both canonical layers exist
    consistently, or none of the artefacts created by the attempt remain.

The registry row is written *last* (it is the commit point — nothing reads
the spatial layers except through the registry), and staged layers are
dropped from the GeoPackage if any later step fails. A durable audit record
(source fingerprint, confirmed profile, parser version, accepted diagnostics,
user decisions, derivation policy) is stored in ``wb_meta`` under
``import_audit_<rpl_id>`` — a key/value row, so no schema migration and no
change to ``RPL_FIELDS`` that downstream readers would notice.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
)

from ..rpl_import.model import (
    COORD_PROJECTED, Diagnostic, ImportedRpl, ImportProfile,
    SEVERITY_ERROR, SEVERITY_INFO,
)
from . import schema
from .rpl_engine import RplModel, RplPoint, RplSegment, recompute, SlackMode
from .rpl_layer_io import model_rows_for_layers
from .store import WorkbenchStore

WORKBENCH_GROUP = "Cable Route Workbench"
IMPORT_AUDIT_META_PREFIX = "import_audit_"


# ---------------------------------------------------------------------------
# Geodesy injection
# ---------------------------------------------------------------------------
def make_wgs84_distance_area(
        transform_context: Optional[QgsCoordinateTransformContext] = None
) -> QgsDistanceArea:
    da = QgsDistanceArea()
    crs = QgsCoordinateReferenceSystem("EPSG:4326")
    if transform_context is not None:
        da.setSourceCrs(crs, transform_context)
    else:
        from qgis.core import QgsProject
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
    da.setEllipsoid("WGS84")
    return da


def geodesy_fns(da: QgsDistanceArea):
    """(distance_km_fn, bearing_deg_fn) for :func:`rpl_import.validate.validate`."""
    from qgis.core import QgsPointXY
    import math

    def distance_km(lat1, lon1, lat2, lon2):
        return float(da.measureLine(QgsPointXY(lon1, lat1),
                                    QgsPointXY(lon2, lat2))) / 1000.0

    def bearing_deg(lat1, lon1, lat2, lon2):
        return math.degrees(da.bearing(QgsPointXY(lon1, lat1),
                                       QgsPointXY(lon2, lat2))) % 360.0

    return distance_km, bearing_deg


def measurement_config(da: QgsDistanceArea) -> Dict[str, str]:
    """What was measured with, for the audit record."""
    return {
        "ellipsoid": da.ellipsoid(),
        "source_crs": da.sourceCrs().authid(),
        "library": "QgsDistanceArea",
    }


# ---------------------------------------------------------------------------
# Projected coordinate transform
# ---------------------------------------------------------------------------
def transform_projected(doc: ImportedRpl, profile: ImportProfile,
                        transform_context: QgsCoordinateTransformContext
                        ) -> List[Diagnostic]:
    """Fill point lat/lon from staged easting/northing via the profile CRS."""
    diagnostics: List[Diagnostic] = []
    if profile.coord_encoding != COORD_PROJECTED:
        return diagnostics
    source = QgsCoordinateReferenceSystem(profile.source_crs or "")
    if not source.isValid():
        diagnostics.append(Diagnostic(
            rule_id="rpl_import.crs.invalid", severity=SEVERITY_ERROR,
            message=f"Source CRS '{profile.source_crs}' is not valid.",
            sheet=doc.sheet))
        return diagnostics
    if source.isGeographic():
        diagnostics.append(Diagnostic(
            rule_id="rpl_import.crs.not_projected", severity=SEVERITY_ERROR,
            message=(f"CRS {source.authid()} is geographic; easting/northing "
                     "columns need a projected CRS."), sheet=doc.sheet))
        return diagnostics
    transform = QgsCoordinateTransform(
        source, QgsCoordinateReferenceSystem("EPSG:4326"), transform_context)
    from qgis.core import QgsPointXY

    for point in doc.points:
        east = point.extras.pop("_easting", None)
        north = point.extras.pop("_northing", None)
        if east is None or north is None:
            continue  # already reported by the parser
        try:
            out = transform.transform(QgsPointXY(float(east), float(north)))
            point.lat, point.lon = out.y(), out.x()
        except Exception as exc:
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.crs.transform_failed",
                severity=SEVERITY_ERROR,
                message=f"Could not transform ({east}, {north}) from "
                        f"{source.authid()}: {exc}",
                sheet=doc.sheet, row=point.source_row))
    return diagnostics


# ---------------------------------------------------------------------------
# Neutral model -> Workbench RplModel (stated values preserved)
# ---------------------------------------------------------------------------
def to_rpl_model(doc: ImportedRpl, source_file: str = ""
                 ) -> Tuple[RplModel, List[Diagnostic]]:
    """Build the engine model. Stated values pass through verbatim; text
    fields ride in ``attrs`` using the canonical Workbench field names.

    ``ChartNo`` stays canonically integer for downstream compatibility;
    alphanumeric chart references keep their text in a ``ChartNoText`` extra.
    """
    diagnostics: List[Diagnostic] = []
    points: List[RplPoint] = []
    for point in doc.points:
        if point.lat is None or point.lon is None:
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.point.unresolved_coordinates",
                severity=SEVERITY_ERROR,
                message="Position still has no usable coordinates.",
                sheet=doc.sheet, row=point.source_row))
            continue
        attrs: Dict[str, object] = {"Remarks": point.remarks or ""}
        chart_int: Optional[int] = None
        if point.chart_no:
            try:
                chart_int = int(str(point.chart_no).strip())
            except (TypeError, ValueError):
                attrs["ChartNoText"] = point.chart_no
                diagnostics.append(Diagnostic(
                    rule_id="rpl_import.point.chart_no_text",
                    severity=SEVERITY_INFO,
                    message=(f"Chart reference '{point.chart_no}' is not "
                             "numeric; kept in the ChartNoText column."),
                    sheet=doc.sheet, row=point.source_row))
        attrs["ChartNo"] = chart_int
        if point.pos_no_raw:
            attrs["PosNoText"] = point.pos_no_raw
        if source_file:
            attrs["SourceFile"] = source_file
        attrs.update(point.extras)
        points.append(RplPoint(
            seq=len(points), pos_no=point.pos_no, event=point.event or "",
            lat=point.lat, lon=point.lon,
            dist_cum_km=point.dist_cum_km,
            cable_dist_cum_km=point.cable_dist_cum_km,
            depth_m=point.depth_m, attrs=attrs))

    segments: List[RplSegment] = []
    for seg in doc.segments[:max(0, len(points) - 1)]:
        attrs = {
            "CableCode": seg.cable_code or "",
            "FiberPair": seg.fiber_pair or "",
            "CableType": seg.cable_type or "",
            "LayDirection": seg.lay_direction or "",
            "LayVessel": seg.lay_vessel or "",
            "ProtectionMethod": seg.protection_method or "",
            "DateInstalled": seg.date_installed or "",
            "TargetBurialDepth": seg.target_burial_depth_m,
            "BurialDepth": seg.burial_depth_m,
            "TerritorialWater": seg.territorial_water or "",
            "EEZ": seg.eez or "",
        }
        if source_file:
            attrs["SourceFile"] = source_file
        attrs.update(seg.extras)
        segments.append(RplSegment(
            seq=len(segments), bearing_deg=seg.bearing_deg,
            dist_km=seg.dist_km, slack_pct=seg.slack_pct,
            cable_dist_km=seg.cable_dist_km, attrs=attrs))

    if points and len(segments) != len(points) - 1:
        # pad with empty spans so the engine invariant holds; the missing
        # data was already diagnosed at parse/validate time
        while len(segments) < len(points) - 1:
            segments.append(RplSegment(seq=len(segments)))
        del segments[len(points) - 1:]
    if not points:
        segments = []
    return RplModel(points=points, segments=segments), diagnostics


# ---------------------------------------------------------------------------
# Stated-versus-derived reconciliation
# ---------------------------------------------------------------------------
@dataclass
class DerivationReport:
    """What was filled in from geometry (per explicit policy), for audit."""
    derived_dist: int = 0
    derived_bearing: int = 0
    derived_slack: int = 0
    derived_cable: int = 0
    derived_cumulative: bool = False

    def to_dict(self) -> Dict:
        return {
            "derived_span_distances": self.derived_dist,
            "derived_bearings": self.derived_bearing,
            "derived_slack_segments": self.derived_slack,
            "derived_cable_distances": self.derived_cable,
            "derived_cumulatives": self.derived_cumulative,
        }


def reconcile_model(model: RplModel, da: QgsDistanceArea,
                    derive_missing: bool = True) -> DerivationReport:
    """Fill *missing* engineering values from geometry; never overwrite
    stated ones. Per-segment, not all-or-nothing.

    - missing span distance -> geodesic distance
    - missing bearing       -> geodesic bearing
    - missing slack with stated cable+route -> implied slack
    - missing cable distance with stated slack (or zero) -> implied cable
    - missing cumulative KP / cable cumulative -> cascade from the first
      stated value (anchored, stated cumulatives kept)
    """
    from .rpl_engine import segment_bearing_deg, segment_distance_km

    report = DerivationReport()
    for i, seg in enumerate(model.segments):
        a, b = model.points[i], model.points[i + 1]
        if seg.dist_km is None and derive_missing:
            seg.dist_km = segment_distance_km(a, b, da)
            report.derived_dist += 1
        if seg.bearing_deg is None and derive_missing:
            seg.bearing_deg = segment_bearing_deg(a, b, da)
            report.derived_bearing += 1
        if (seg.slack_pct is None and seg.cable_dist_km is not None
                and seg.dist_km and seg.dist_km > 0):
            seg.slack_pct = (seg.cable_dist_km / seg.dist_km - 1.0) * 100.0
            report.derived_slack += 1
        if (seg.cable_dist_km is None and seg.dist_km is not None
                and derive_missing):
            slack = seg.slack_pct if seg.slack_pct is not None else 0.0
            seg.cable_dist_km = seg.dist_km * (1.0 + slack / 100.0)
            report.derived_cable += 1

    # cumulative cascade only where the document left gaps
    if derive_missing and model.points:
        anchor_kp = model.points[0].dist_cum_km
        anchor_cable = model.points[0].cable_dist_cum_km
        need_kp = any(p.dist_cum_km is None for p in model.points)
        need_cable = any(p.cable_dist_cum_km is None for p in model.points)
        if need_kp or need_cable:
            report.derived_cumulative = True
            kp = anchor_kp if anchor_kp is not None else 0.0
            cable = anchor_cable if anchor_cable is not None else 0.0
            if model.points[0].dist_cum_km is None:
                model.points[0].dist_cum_km = kp
            if model.points[0].cable_dist_cum_km is None:
                model.points[0].cable_dist_cum_km = cable
            kp = model.points[0].dist_cum_km
            cable = model.points[0].cable_dist_cum_km
            for i, seg in enumerate(model.segments):
                kp += seg.dist_km or 0.0
                cable += seg.cable_dist_km or 0.0
                target = model.points[i + 1]
                if target.dist_cum_km is None and need_kp:
                    target.dist_cum_km = kp
                else:
                    kp = target.dist_cum_km if target.dist_cum_km is not None else kp
                if target.cable_dist_cum_km is None and need_cable:
                    target.cable_dist_cum_km = cable
                else:
                    cable = (target.cable_dist_cum_km
                             if target.cable_dist_cum_km is not None else cable)
    return report


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------
class CommitError(Exception):
    """Import commit failed; the message is user-facing. No artefacts remain."""


@dataclass
class CommitRequest:
    route_name: str
    kind: str = "planned"                    # planned | as_laid
    rev_label: str = ""                      # blank = next Rev N
    slack_mode: str = ""                     # blank = default from kind
    source_file: str = ""
    audit: Dict = field(default_factory=dict)
    notes: str = ""


@dataclass
class CommitResult:
    rpl_id: str
    route_id: str
    rev_label: str
    registered_name: str
    points_layer: str
    lines_layer: str
    gpkg_path: str


def default_slack_mode(kind: str) -> str:
    return "hold_cable" if kind == "as_laid" else "hold_slack"


def delete_gpkg_layer(gpkg_path: str, layer_name: str) -> bool:
    """Drop one layer from a GeoPackage (cleanup of staged artefacts)."""
    try:
        from osgeo import ogr
    except ImportError:
        return False
    try:
        ds = ogr.Open(gpkg_path, update=1)
        if ds is None:
            return False
        try:
            for index in range(ds.GetLayerCount()):
                if ds.GetLayerByIndex(index).GetName() == layer_name:
                    ds.DeleteLayer(index)
                    return True
        finally:
            ds = None
    except Exception:
        return False
    return False


def commit_import(store: WorkbenchStore, model: RplModel,
                  request: CommitRequest) -> CommitResult:
    """Register the imported model as a new Workbench RPL revision.

    Write order stages everything reversible first and makes the registry row
    the commit point:

    1. spatial layers (unique staged names)
    2. wb_meta audit row
    3. topology component + ports
    4. wb_rpl registry row  <- the revision "exists" only after this

    Any exception before step 4 completes triggers cleanup of steps 1-3.
    Never overwrites an issued revision (labels are checked up front).
    """
    if len(model.points) < 2 or len(model.segments) != len(model.points) - 1:
        raise CommitError("Model is not consistent (points/segments mismatch).")

    kind = request.kind if request.kind in ("planned", "as_laid") else "planned"
    slack_mode = request.slack_mode or default_slack_mode(kind)
    route_name = (request.route_name or "").strip() or "Segment"

    store.migrate()

    existing_route = next(
        (r for r in store.list_routes()
         if (r.get("name") or "").strip().lower() == route_name.lower()), None)
    supersedes_id = ""
    if existing_route:
        route_id = existing_route["route_id"]
        revisions = store.revisions_of_route(route_id)
        rev_label = (request.rev_label or "").strip() or schema.next_rev_label(revisions)
        needle = rev_label.strip().lower()
        if any((r.get("rev_label") or "").strip().lower() == needle
               for r in revisions):
            raise CommitError(
                f"Segment '{route_name}' already has revision '{rev_label}'. "
                "Choose a new label or leave it blank for the next revision.")
        latest = store.latest_revision(route_id)
        supersedes_id = latest.get("rpl_id") if latest else ""
        created_route = False
    else:
        route_id = store.create_route(route_name)
        rev_label = (request.rev_label or "").strip() or "Rev 1"
        created_route = True

    rpl_id = schema.new_id()
    registered_name = f"{route_name} {rev_label}".strip()
    existing_layers = set()
    for row in store.list_rpls():
        for key in ("points_layer", "lines_layer"):
            if row.get(key):
                existing_layers.add(row[key])
    points_layer = schema.unique_layer_name(
        existing_layers, schema.rpl_points_layer_name(registered_name))
    existing_layers.add(points_layer)
    lines_layer = schema.unique_layer_name(
        existing_layers, schema.rpl_lines_layer_name(registered_name))

    staged_layers: List[str] = []
    staged_meta_key: Optional[str] = None
    staged_component: Optional[str] = None
    try:
        rows = model_rows_for_layers(model, rpl_id, request.source_file)
        point_specs = _specs_with_extras(schema.RPL_POINT_FIELDS, rows["points"])
        line_specs = _specs_with_extras(schema.RPL_LINE_FIELDS, rows["lines"])
        from ..qgis_compat import WKB_LINESTRING, WKB_POINT
        store.write_spatial_layer(points_layer, point_specs, WKB_POINT,
                                  rows["points"])
        staged_layers.append(points_layer)
        store.write_spatial_layer(lines_layer, line_specs, WKB_LINESTRING,
                                  rows["lines"])
        staged_layers.append(lines_layer)

        audit = dict(request.audit or {})
        audit.setdefault("imported_utc", schema.utc_now_iso())
        audit["registered_name"] = registered_name
        audit["route_id"] = route_id
        audit["rev_label"] = rev_label
        staged_meta_key = IMPORT_AUDIT_META_PREFIX + rpl_id
        store.write_meta(staged_meta_key, json.dumps(audit, sort_keys=True))

        staged_component = store.save_component(
            {"component_id": schema.new_id(), "kind": "rpl",
             "subject_id": rpl_id, "name": registered_name},
            port_labels=["A", "B"])

        store.save_rpl({
            "rpl_id": rpl_id,
            "name": registered_name,
            "kind": kind,
            "points_layer": points_layer,
            "lines_layer": lines_layer,
            "source_file": request.source_file or "",
            "slack_mode": slack_mode,
            "depth_source_config": "",
            "route_id": route_id,
            "rev_label": rev_label,
            "status": schema.STATUS_DRAFT,
            "supersedes_id": supersedes_id,
            "issued_utc": "",
            "notes": request.notes or "",
        })
    except Exception as exc:
        _cleanup_staged(store, staged_layers, staged_meta_key,
                        staged_component,
                        route_id if created_route else None)
        if isinstance(exc, CommitError):
            raise
        raise CommitError(f"Import failed and was rolled back: {exc}") from exc

    return CommitResult(
        rpl_id=rpl_id, route_id=route_id, rev_label=rev_label,
        registered_name=registered_name, points_layer=points_layer,
        lines_layer=lines_layer, gpkg_path=store.gpkg_path)


def _cleanup_staged(store: WorkbenchStore, staged_layers: List[str],
                    meta_key: Optional[str], component_id: Optional[str],
                    created_route_id: Optional[str]) -> None:
    """Best-effort removal of every artefact staged by a failed commit."""
    for layer_name in staged_layers:
        try:
            delete_gpkg_layer(store.gpkg_path, layer_name)
        except Exception:
            pass
    if meta_key:
        try:
            rows = [r for r in store.read_table(schema.TABLE_META)
                    if r.get("key") != meta_key]
            store._write_table_rows(schema.TABLE_META, schema.META_FIELDS, rows)
        except Exception:
            pass
    if component_id:
        try:
            store.delete_component(component_id)
        except Exception:
            pass
    if created_route_id:
        try:
            store.delete_route(created_route_id)
        except Exception:
            pass


def read_import_audit(store: WorkbenchStore, rpl_id: str) -> Dict:
    raw = store.read_meta().get(IMPORT_AUDIT_META_PREFIX + rpl_id, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _specs_with_extras(base_specs, rows: List[Dict]):
    """Base field specs plus typed specs for extra keys found in the rows.

    Unlike the legacy register path, the type of an extra column is decided
    from the first *non-null* value, so a leading empty cell doesn't force a
    numeric column to text.
    """
    from ..processing.cable_lay_parsers import WKT_KEY

    specs = list(base_specs)
    known = {name for name, _ in specs}
    extra_types: Dict[str, str] = {}
    order: List[str] = []
    for row in rows:
        for key, value in row.items():
            if key in known or key == WKT_KEY:
                continue
            if key not in extra_types:
                order.append(key)
            current = extra_types.get(key)
            if value is None:
                extra_types.setdefault(key, "")
                continue
            if isinstance(value, bool):
                candidate = "str"
            elif isinstance(value, int):
                candidate = "int"
            elif isinstance(value, float):
                candidate = "float"
            else:
                candidate = "str"
            if not current:
                extra_types[key] = candidate
            elif current != candidate:
                if {current, candidate} == {"int", "float"}:
                    extra_types[key] = "float"
                else:
                    extra_types[key] = "str"
    for key in order:
        specs.append((key, extra_types.get(key) or "str"))
    return specs
