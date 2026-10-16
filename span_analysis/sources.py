# -*- coding: utf-8 -*-
"""QGIS-side inputs for Span & Slack Analysis.

* :class:`RouteGeo`: the route as a KP-referenced line (a Cable Route
  Workbench RPL or a line layer), with KP <-> map position and the alter
  courses along it.
* Seabed sampling along the route with the Depth Profile engine (rasters,
  finest first, or contour crossings), the same as the Lay Assessment.
* Cable types from the cable type library, and a Workbench assembly fitted
  to an RPL (cable sections and bodies by KP).

Everything that touches layers runs on the main thread; the sampling
request is then run in a background task (:func:`sample_profile`).
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from .. import depth_profile_core as dpc
from ..cable_library import store as library
from ..cable_seabed import planning as pl
from ..cable_seabed.core import G
from ..cable_seabed.sampling import SEABED_CONTOURS, SEABED_RASTER, seabed_sampling_notes
from ..kp_range_utils import make_kp_distance_area
from ..plugin_log import log_exception

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

ROUTE_RPL = "rpl"
ROUTE_LINE = "line"
ROUTE_CSV = "csv"
ROUTE_SYNTHETIC = "synthetic"
ROUTE_DXF = "dxf"
ROUTE_CHOICES = (
    (ROUTE_RPL, "Cable Route Workbench RPL"),
    (ROUTE_LINE, "Line layer"),
    (ROUTE_CSV, "Profile from a CSV file (no map)"),
    (ROUTE_DXF, "Profile from a DXF drawing (no map)"),
    (ROUTE_SYNTHETIC, "Synthetic seabed (no map)"),
)
SEABED_RPL_DEPTH = "rpl_depth"
SEABED_CHOICES = (
    (SEABED_RASTER, "Raster bathymetry (MBES grid)"),
    (SEABED_CONTOURS, "Depth contours"),
    (SEABED_RPL_DEPTH, "RPL approximate depth (coarse)"),
)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
class RouteGeo:
    """A route line with KP: ``kp = kp0 + chainage / 1000``."""

    def __init__(self, stationing: "dpc.RouteStationing", kp0_km: float, name: str = ""):
        self.stationing = stationing
        self.kp0 = float(kp0_km)
        self.name = name
        crs = stationing.crs
        self._to_wgs = None
        if crs is not None and crs.isValid() and crs != WGS84:
            self._to_wgs = QgsCoordinateTransform(crs, WGS84, QgsProject.instance())

    @property
    def length_m(self) -> float:
        return float(self.stationing.line_length)

    @property
    def kp_range(self) -> Tuple[float, float]:
        return self.kp0, self.kp0 + self.length_m / 1000.0

    def point_at_kp(self, kp_km: float) -> Optional[QgsPointXY]:
        return self.stationing.point_at((float(kp_km) - self.kp0) * 1000.0)

    def lonlat_at_kp(self, kp_km: float) -> Optional[Tuple[float, float]]:
        point = self.point_at_kp(kp_km)
        if point is None:
            return None
        if self._to_wgs is not None:
            try:
                point = self._to_wgs.transform(point)
            except Exception:
                return None
        return float(point.x()), float(point.y())

    def polyline_lonlat(self, kp_a: float, kp_b: float, max_points: int = 400) -> List[Tuple[float, float]]:
        """The route between two KPs, with its vertices (so alter courses are not cut)."""
        lo, hi = sorted((float(kp_a), float(kp_b)))
        lo, hi = max(lo, self.kp_range[0]), min(hi, self.kp_range[1])
        if hi < lo:
            return []
        marks = np.linspace(lo, hi, max(2, min(max_points, int((hi - lo) * 1000.0) + 2)))
        vertices = self.kp0 + np.asarray(self.stationing.vertex_chainages(), dtype=float) / 1000.0
        marks = np.unique(np.concatenate((marks, vertices[(vertices > lo) & (vertices < hi)])))
        out = [self.lonlat_at_kp(k) for k in marks]
        return [p for p in out if p is not None]

    def kp_at(self, point: QgsPointXY, point_crs) -> Optional[float]:
        """KP of the route position nearest to ``point`` (in ``point_crs``)."""
        crs = self.stationing.crs
        try:
            if point_crs is not None and crs is not None and point_crs != crs:
                point = QgsCoordinateTransform(point_crs, crs, QgsProject.instance()).transform(point)
        except Exception:
            return None
        chainage = self.stationing.measure_along(point)
        return None if chainage is None else self.kp0 + float(chainage) / 1000.0

    def turns(self, min_deg: float = 0.5) -> List[Tuple[float, float]]:
        """``[(kp, heading change deg)]`` at the route's vertices (alter courses)."""
        da = self.stationing.distance_area
        out: List[Tuple[float, float]] = []
        chainage = 0.0
        for part in self.stationing.line_parts:
            pts = [QgsPointXY(p) for p in part]
            lengths = []
            for p1, p2 in zip(pts[:-1], pts[1:]):
                try:
                    lengths.append(float(da.measureLine(p1, p2)))
                except Exception:
                    lengths.append(math.hypot(p2.x() - p1.x(), p2.y() - p1.y()))
            for i in range(1, len(pts) - 1):
                if lengths[i - 1] <= 0 or lengths[i] <= 0:
                    continue
                try:
                    b_in = da.bearing(pts[i - 1], pts[i])
                    b_out = da.bearing(pts[i], pts[i + 1])
                except Exception:
                    continue
                turn = math.degrees(b_out - b_in)
                turn = (turn + 180.0) % 360.0 - 180.0
                if abs(turn) >= min_deg:
                    out.append((self.kp0 + (chainage + sum(lengths[:i])) / 1000.0, turn))
            chainage += sum(lengths)
        return out

    def sub_route(self, kp_a: float, kp_b: float) -> "RouteGeo":
        """The route between two KPs (for sampling a KP window).

        Cut from this route's own chainage, not measured again, so a KP in
        the window is the same position as on the whole route; separate line
        parts stay separate.
        """
        lo, hi = sorted((float(kp_a), float(kp_b)))
        lo, hi = max(lo, self.kp_range[0]), min(hi, self.kp_range[1])
        if hi - lo < 0.001:
            raise ValueError("The KP window is shorter than 1 m.")
        route = self.stationing.clip((lo - self.kp0) * 1000.0, (hi - self.kp0) * 1000.0)
        if route is None:
            raise ValueError("The KP window could not be cut from the route.")
        return RouteGeo(route, lo, self.name)


def route_from_rpl_model(model, name: str = "") -> RouteGeo:
    """A RouteGeo through the RPL's positions (WGS84), KP from the RPL's start KP.

    KP is measured (the plugin's KP setting: geodesic on WGS84 by default),
    never read from the RPL: only its start KP is taken (see ``kp_datum``).
    Between positions the route follows the KP path (the geodesic, or the grid
    line; ``kp_geo_utils.path_points``), as in every KP tool.
    """
    points = [QgsPointXY(float(p.lon), float(p.lat)) for p in model.points]
    if len(points) < 2:
        raise ValueError("The RPL has fewer than two positions.")
    distance = make_kp_distance_area(WGS84, QgsProject.instance().transformContext())
    route, status = dpc.route_from_points(points, WGS84, distance)
    if route is None:
        raise ValueError(status or "The RPL route could not be built.")
    return RouteGeo(route, model.start_kp_km(), name)


def route_from_layer(layer: QgsVectorLayer, selected_only: bool, start_kp: float) -> RouteGeo:
    if layer is None:
        raise ValueError("Choose a route line layer.")
    features = list(layer.getSelectedFeatures()) if selected_only else list(layer.getFeatures())
    features = [f for f in features if f.hasGeometry()]
    if not features:
        raise ValueError("The route layer has no " + ("selected " if selected_only else "") + "line features.")
    # KP along a line layer follows the plugin's KP setting, as in the Depth Profile tool.
    distance = make_kp_distance_area(layer.crs(), QgsProject.instance().transformContext())
    route, status = dpc.route_from_features(features, layer.crs(), distance)
    if route is None or status:
        raise ValueError(status or "The route line could not be read.")
    return RouteGeo(route, start_kp, layer.name())


# ---------------------------------------------------------------------------
# Seabed
# ---------------------------------------------------------------------------
def build_seabed_request(route: RouteGeo, mode: str, rasters: Sequence[QgsRasterLayer],
                         contour: Optional[Tuple[QgsVectorLayer, str]], interval_m: int):
    """Main thread: the Depth Profile request (``interval_m`` 0 = each raster at its cell size)."""
    params = dpc.ProfileParams(mode=dpc.RASTER if mode == SEABED_RASTER else dpc.CONTOURS,
                               interval_m=int(interval_m) or 1, adaptive=int(interval_m) == 0, adaptive_factor=1.0,
                               max_samples=400000, per_raster=False)
    context = QgsProject.instance().transformContext()
    if mode == SEABED_RASTER:
        layers = [layer for layer in rasters if isinstance(layer, QgsRasterLayer)]
        if not layers:
            raise ValueError("Tick one or more raster layers.")
        request = dpc.build_request(route.stationing, params, context, raster_layers=layers)
    else:
        if contour is None or contour[0] is None or not contour[1]:
            raise ValueError("Choose a contour layer and its depth field.")
        request = dpc.build_request(route.stationing, params, context, contour_layers=[contour])
    if request.status:
        raise ValueError(request.status)
    return request


def sample_profile(request, kp0: float, mode: str, auto: bool, cancel=None, progress=None) -> Dict:
    """Worker: run the Depth Profile request; returns the profile as plain lists."""
    result = dpc.run_profile(request, cancel=cancel, progress=progress)
    x = np.asarray(result.kp_values, dtype=float) * 1000.0
    depth = np.array([np.nan if v is None else float(v) for v in result.depth_values])
    if not np.isfinite(depth).any():
        raise ValueError(result.status or "No seabed depths along the route (check the bathymetry covers it).")
    notes, _skip = seabed_sampling_notes(mode, x, depth, kp0 + x / 1000.0,
                                         cells=list(result.depth_cell_m or []) or None,
                                         sources=list(result.depth_source_ids or []) or None,
                                         messages=result.messages, auto=auto)
    return {"x_m": x.tolist(), "kp_km": (kp0 + x / 1000.0).tolist(),
            "depth_m": [None if not math.isfinite(v) else float(v) for v in depth], "notes": notes}


def rpl_measured_kps(model) -> List[float]:
    """Measured KP of each RPL position: the start KP plus the chainage along the
    positions (as :func:`route_from_rpl_model` measures it)."""
    points = [QgsPointXY(float(p.lon), float(p.lat)) for p in model.points]
    distance = make_kp_distance_area(WGS84, QgsProject.instance().transformContext())
    out, running = [model.start_kp_km()], 0.0
    for a, b in zip(points[:-1], points[1:]):
        try:
            running += float(distance.measureLine(a, b))
        except Exception:
            log_exception("Span & Slack Analysis: an RPL leg could not be measured")
        out.append(model.start_kp_km() + running / 1000.0)
    return out


def rpl_kp_mapper(model):
    """``f(stated KP) -> measured KP``: the RPL's printed KPs (the datum of its legs and of
    a fitted assembly) mapped through its positions, keeping the fraction along each leg."""
    measured = np.asarray(rpl_measured_kps(model), dtype=float)
    stated = np.array([np.nan if p.dist_cum_km is None else float(p.dist_cum_km) for p in model.points])
    ok = np.isfinite(stated) & np.isfinite(measured)
    ok &= np.concatenate(([True], np.diff(np.where(ok, stated, -np.inf)) > 0))   # increasing only
    if ok.sum() < 2:
        return lambda kp: kp
    xs, ys = stated[ok], measured[ok]

    def mapper(kp):
        return float(np.interp(float(kp), xs, ys, left=ys[0] + (float(kp) - xs[0]),
                               right=ys[-1] + (float(kp) - xs[-1])))

    return mapper


def rpl_kp_note(model) -> Optional[str]:
    """A note when the RPL's printed KPs differ from the measured KPs by a metre or more."""
    from ..kp_datum import compare_stated_kps

    stated = [None if p.dist_cum_km is None else float(p.dist_cum_km) for p in model.points]
    check = compare_stated_kps(rpl_measured_kps(model), stated)
    if check.level == "ok":
        return None
    return (check.summary() + " Legs, cable types and an assembly fit are placed at the measured KPs of "
            "the RPL positions.")


def rpl_depth_profile(model, kp_lo: float, kp_hi: float, step_m: float = 5.0) -> Dict:
    """The RPL's ApproxDepth interpolated by (measured) KP (a coarse fallback seabed)."""
    kp = np.asarray(rpl_measured_kps(model), dtype=float)
    depth = np.array([p.depth_m if p.depth_m is not None else np.nan for p in model.points], dtype=float)
    ok = np.isfinite(kp) & np.isfinite(depth)
    if ok.sum() < 2:
        raise ValueError("The RPL has fewer than two positions with an ApproxDepth.")
    kp, depth = kp[ok], np.abs(depth[ok])
    lo, hi = max(kp_lo, kp[0]), min(kp_hi, kp[-1])
    count = int(max(3, math.ceil((hi - lo) * 1000.0 / max(step_m, 0.5)) + 1))
    marks = np.linspace(lo, hi, count)
    values = np.interp(marks, kp, depth)
    x = (marks - lo) * 1000.0
    notes = ["Seabed from the RPL's approximate depths (straight lines between RPL positions): it holds no "
             "seabed relief between positions, so it cannot show spans. Use it only to check the setup."]
    return {"x_m": x.tolist(), "kp_km": marks.tolist(), "depth_m": values.tolist(), "notes": notes}


# ---------------------------------------------------------------------------
# Cable types and the Workbench
# ---------------------------------------------------------------------------
def cable_type_from_row(row: Dict) -> pl.CableType:
    def val(key):
        value = library.to_float(row.get(key))
        return math.nan if value is None else float(value)

    weight = val("weight_water_kg_m")
    diameter = val("diameter_mm")
    return pl.CableType(name=row.get("name") or "Cable", w_npm=weight * G if math.isfinite(weight) else math.nan,
                        ei_knm2=val("bending_stiffness_knm2"), mbr_m=val("mbr_m"),
                        diameter_m=diameter / 1000.0 if math.isfinite(diameter) else math.nan,
                        npts_kn=val("npts_kn"))


def library_rows() -> List[Dict]:
    try:
        return library.load_current()
    except Exception:
        log_exception("Span & Slack Analysis: reading the cable type library failed")
        return []


def open_workbench():
    from ..workbench.registry_access import open_store

    return open_store()


def rpl_choices(store) -> List[Tuple[str, str]]:
    from ..workbench.registry_access import rpl_choices as choices

    return choices(store)


def fit_choices(store, rpl_id: str) -> List[Tuple[str, str]]:
    from ..workbench.registry_access import fit_choices as choices

    return choices(store, rpl_id)


def load_rpl_model(store, rpl_id: str):
    from ..workbench.registry_access import load_model

    return load_model(store, rpl_id)


def rpl_legs(model) -> List[Tuple[float, float, float]]:
    """``[(kp0, kp1, slack %)]`` per RPL leg at measured KPs (legs without slack are left out)."""
    kps = rpl_measured_kps(model)
    out = []
    for i, segment in enumerate(model.segments):
        if i + 1 >= len(kps) or segment.slack_pct is None:
            continue
        out.append((float(kps[i]), float(kps[i + 1]), float(segment.slack_pct)))
    return out


_BODY_KINDS = (("Repeater", r"\bREP|REPEATER|RPT"), ("Branching unit", r"\bBU\b|BRANCH"),
               ("Joint", r"JOINT|\bUJ\b|\bJB\b|SPLICE"), ("Equaliser", r"EQUALI[SZ]"))


def body_kind(name: str) -> str:
    text = str(name or "").upper()
    for kind, pattern in _BODY_KINDS:
        if re.search(pattern, text):
            return kind
    return "Body"


def fit_makeup(store, fit_id: str, rows: Sequence[Dict]) -> Tuple[List[pl.CableSection], List[pl.Body], List[str]]:
    """Cable sections and bodies of a Workbench assembly fitted to an RPL, by KP.

    A section's cable type resolves through the cable library (project
    mapping, name, alias, generic type); unresolved sections use the
    assembly's own properties.
    """
    from ..workbench import assembly_model as am
    from ..workbench.fit import FitAnchor, fit_assembly

    fit = next((f for f in store.list_fits() if f.get("fit_id") == fit_id), None)
    if fit is None:
        raise ValueError("The assembly fit is no longer in the Workbench.")
    header, items = store.get_assembly(fit.get("assembly_id") or "")
    if header is None:
        raise ValueError("The fitted assembly is no longer in the Workbench.")
    assembly = am.assembly_from_rows(header, items)
    model = load_rpl_model(store, fit.get("rpl_id") or "")
    anchor = FitAnchor(float(fit.get("anchor_kp_km") or 0.0), float(fit.get("anchor_cable_dist_m") or 0.0),
                       1 if int(fit.get("direction") or 1) >= 0 else -1)
    result = fit_assembly(assembly, model, anchor)
    measured = rpl_kp_mapper(model)      # the fit works in the RPL's printed KPs
    mapping = {}
    try:
        mapping = library.project_mapping()
    except Exception:
        pass
    warnings = list(result.warnings)
    sections: List[pl.CableSection] = []
    for span in result.sections:
        if span.kp_start_km is None or span.kp_end_km is None:
            continue
        item = span.item
        label = item.cable_type or item.name
        row, how = library.resolve(rows, label, mapping)[:2] if rows else (None, "none")
        if row is not None:
            cable = cable_type_from_row(row)
        else:
            cable = pl.CableType(
                name=label or "Cable",
                w_npm=float(item.q_water_npm) if item.q_water_npm is not None else math.nan,
                ei_knm2=float(item.bending_stiffness_kNm2) if item.bending_stiffness_kNm2 is not None else math.nan,
                mbr_m=float(item.min_bend_radius_m) if item.min_bend_radius_m is not None else math.nan,
                diameter_m=float(item.diameter_m) if item.diameter_m is not None else math.nan)
            if label:
                reason = "matches several library types: map it in step 3" if how == "ambiguous" \
                    else "is not in the library"
                warnings.append(f"Assembly cable type '{label}' {reason}; its own weight and stiffness are used, "
                                "with no NPTS.")
        sections.append(pl.CableSection(measured(span.kp_start_km), measured(span.kp_end_km), cable))
    bodies: List[pl.Body] = []
    for landing in result.bodies:
        if landing.kp_km is None:
            continue
        item = landing.item
        bodies.append(pl.Body(item.name or "Body", measured(landing.kp_km),
                              length_m=float(item.length_m or 0.0),
                              weight_kn=float(item.point_load_kN or 0.0),
                              diameter_m=float(item.diameter_m) if item.diameter_m else math.nan,
                              kind=body_kind(item.name)))
    return sections, bodies, warnings


# ---------------------------------------------------------------------------
# Cable type labels (RPL legs, assembly sections) -> library types
# ---------------------------------------------------------------------------
def project_mapping() -> Dict[str, str]:
    try:
        return library.project_mapping()
    except Exception:
        return {}


def set_mapping(label: str, library_name: Optional[str]) -> None:
    """Map a label to a library type in this QGIS project (None: resolve automatically).

    The same project mapping the Lay Assessment uses.
    """
    mapping = project_mapping()
    token = library.type_token(label)
    if not token:
        return
    if library_name:
        mapping[token] = library_name
    else:
        mapping.pop(token, None)
    library.set_project_mapping(mapping)


def resolve_label(rows: Sequence[Dict], label: str) -> Tuple[Optional[Dict], str]:
    return library.resolve(rows, label, project_mapping())


def candidates(rows: Sequence[Dict], label: str) -> List[str]:
    return library.candidates(rows, label)


def rpl_leg_labels(model) -> List[Tuple[float, float, str]]:
    """``[(kp0, kp1, cable type label)]`` per RPL leg with a ``CableType``, at measured KPs."""
    kps = rpl_measured_kps(model)
    out = []
    for i, segment in enumerate(model.segments):
        label = (segment.attrs or {}).get("CableType")
        text = None if label is None else str(label).strip()
        if text and text.upper() != "NULL" and i + 1 < len(kps):
            out.append((float(kps[i]), float(kps[i + 1]), text))
    return out


def fit_labels(store, fit_id: str) -> List[str]:
    """Cable type labels of the sections of a fitted assembly, in order, without repeats."""
    from ..workbench import assembly_model as am

    fit = next((f for f in store.list_fits() if f.get("fit_id") == fit_id), None)
    if fit is None:
        return []
    header, items = store.get_assembly(fit.get("assembly_id") or "")
    if header is None:
        return []
    assembly = am.assembly_from_rows(header, items)
    labels: List[str] = []
    for item in assembly.items:
        label = (item.cable_type or item.name or "").strip() if item.is_section else ""
        if label and label not in labels:
            labels.append(label)
    return labels


def rpl_makeup(model, rows: Sequence[Dict]) -> Tuple[List[pl.CableSection], List[str]]:
    """Cable sections from the RPL's ``CableType`` per leg, resolved through the library.

    Neighbouring legs of one label become one section. A label the library
    cannot resolve (unknown, or ambiguous without a project mapping) is left
    to the default cable type, with a note.
    """
    sections: List[pl.CableSection] = []
    notes: List[str] = []
    merged: List[List] = []
    for k0, k1, label in rpl_leg_labels(model):
        lo, hi = sorted((k0, k1))
        if merged and merged[-1][2] == label and abs(merged[-1][1] - lo) < 1e-6:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi, label])
    unresolved: Dict[str, str] = {}
    for lo, hi, label in merged:
        row, how = resolve_label(rows, label)
        if row is None:
            unresolved[label] = how
            continue
        sections.append(pl.CableSection(lo, hi, cable_type_from_row(row)))
    for label, how in unresolved.items():
        reason = "matches several library types: map it" if how == "ambiguous" else "is not in the library"
        notes.append(f"RPL cable type '{label}' {reason}; the default cable type is used on its legs.")
    return sections, notes
