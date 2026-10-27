# -*- coding: utf-8 -*-
"""Route references for comments, on the plugin's KP services (no widgets).

A comment made on a route records, at capture time:

* **where**: the KP (or KP range) read from the same route model the KP
  Mouse Tool shows (``build_reference_route`` / ``RouteFrame``: route order,
  the RPL's start KP, the plugin-wide geodesic or grid KP convention), the
  geometry in EPSG:4326 and exactly as captured in its own CRS; a free map
  location keeps its own position and, separately, its nearest KP and
  distance off the route;
* **on what**: the route layer (id, name, source, provider, CRS), the
  Workbench RPL when the layer is one (id, name, revision, route, source
  file), a fingerprint of the route geometry, the route length, the start
  KP and the KP convention.

:class:`ReferenceChecker` later compares that record with the route as it
is now. It never recomputes a comment's KP: a comment whose route changed
or cannot be found is flagged, and keeps the position and KPs it was made with.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsGeometry,
                       QgsPointXY, QgsProject, QgsVectorLayer)

from .. import kp_datum
from ..kp_geo_utils import iter_line_parts, ordered_route_features
from ..kp_range_utils import (KP_MODE_CARTESIAN, KP_MODE_GEODESIC, is_grid_distance, kp_basis_text,
                              kp_distance_mode, make_kp_distance_area)
from ..plugin_log import log_exception
from ..qgis_compat import GEOMETRY_POINT
from . import model

WGS84 = "EPSG:4326"
# Start KPs differing by less than this are the same (KPs are shown to 1 m).
START_KP_TOL_KM = 0.0005
POSITION_COLUMNS = ("ref_kind", "kp_km", "kp_end_km", "dcc_m", "lat", "lon", "wkt", "geom_wkt", "geom_crs")


# --------------------------------------------------------------------------- identity
def kp_convention_token(distance) -> str:
    """A stable token for how KPs are measured: ``geodesic:WGS84``,
    ``grid:EPSG:32630`` or ``grid:utm-auto`` (UTM zone at the route start)."""
    if is_grid_distance(distance):
        grid = distance.grid_crs()
        return "grid:" + (grid.authid() if grid is not None and grid.isValid() else "utm-auto")
    try:
        if distance.willUseEllipsoid():
            return "geodesic:WGS84"
    except Exception:  # noqa: BLE001 - fall through to the planar description
        pass
    try:
        return "grid:" + (distance.sourceCrs().authid() or "layer")
    except Exception:  # noqa: BLE001
        return "grid:layer"


def describe_convention(token: str) -> str:
    if not token:
        return "unknown"
    if token.startswith("geodesic"):
        return "geodesic (WGS84)"
    grid = token.split(":", 1)[1] if ":" in token else token
    return "grid, UTM zone at the route start" if grid == "utm-auto" else f"grid ({grid})"


def route_fingerprint(layer) -> str:
    """SHA-1 of the route's vertices in route order (layer CRS, 10 significant
    digits: sub-millimetre), so any edit to the route geometry changes it."""
    digest = hashlib.sha1()
    count = 0
    for feature in ordered_route_features(layer.getFeatures()):
        digest.update(b"|f")
        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            continue
        for part in iter_line_parts(geom):
            digest.update(b"|p")
            for point in part:
                digest.update(("%.10g %.10g;" % (point.x(), point.y())).encode("ascii"))
                count += 1
    digest.update(f"|n{count}|{layer.crs().authid()}".encode("utf-8"))
    return digest.hexdigest()


def _norm_source(source: str) -> str:
    text = str(source or "").strip()
    path, sep, rest = text.partition("|")
    if os.path.isabs(path):
        path = os.path.normcase(os.path.abspath(path))
    return path + sep + rest.lower()


def _source_path(source: str) -> str:
    return str(source or "").split("|", 1)[0]


def workbench_rpl_info(layer, store=None) -> Dict:
    """``{rpl_id, rpl_name, rpl_rev, rpl_route_id, rpl_source_file}`` when
    ``layer`` is a Cable Route Workbench RPL lines layer, else ``{}``."""
    try:
        if layer is None or "rpl_id" not in layer.fields().names():
            return {}
        rpl_id = ""
        for feature in layer.getFeatures():
            value = feature["rpl_id"]
            if value not in (None, "") and str(value).upper() != "NULL":
                rpl_id = str(value)
                break
        if not rpl_id:
            return {}
        if store is None:
            from ..workbench.registry_access import open_store
            store = open_store()
        rpl = store.get_rpl(rpl_id) if store is not None else None
        if not rpl:
            return {"rpl_id": rpl_id}
        return {"rpl_id": rpl_id, "rpl_name": rpl.get("name") or "", "rpl_rev": rpl.get("rev_label") or "",
                "rpl_route_id": rpl.get("route_id") or "", "rpl_source_file": rpl.get("source_file") or ""}
    except Exception:  # noqa: BLE001 - identity is best effort; the layer identity stands
        log_exception("Route Comments: the Workbench RPL of the route could not be read")
        return {}


def kp_tool_distance(layer, crs, use_cartesian: Optional[bool] = None):
    """The KP distance the KP Mouse Tool uses for ``layer`` measured in ``crs``."""
    if use_cartesian is None:
        use_cartesian = kp_distance_mode() == KP_MODE_CARTESIAN
    return make_kp_distance_area(crs, QgsProject.instance().transformContext(),
                                 mode=KP_MODE_CARTESIAN if use_cartesian else KP_MODE_GEODESIC,
                                 route_crs=layer.crs())


def current_convention(layer) -> str:
    """The KP convention the KP tools would use for ``layer`` now. An
    automatic UTM grid is resolved from the route start, as measuring does."""
    distance = kp_tool_distance(layer, layer.crs())
    if is_grid_distance(distance) and distance.grid_crs() is None:
        for feature in ordered_route_features(layer.getFeatures()):
            parts = iter_line_parts(feature.geometry()) if feature.hasGeometry() else []
            if parts and parts[0]:
                distance.to_grid(parts[0][0])
                break
    return kp_convention_token(distance)


# --------------------------------------------------------------------------- capture
@dataclass
class CaptureRoute:
    """A route as the KP Mouse Tool measures it, ready to place comments on.

    ``route`` is a ``RouteFrame`` over ``layer``'s features in ``crs`` (the
    map CRS during capture); ``distance`` measures it.
    """
    layer: QgsVectorLayer
    route: object
    distance: object
    crs: QgsCoordinateReferenceSystem
    _identity: Optional[Dict] = field(default=None, repr=False)

    @classmethod
    def from_layer(cls, layer, crs=None, start_kp_km=None, use_cartesian: Optional[bool] = None,
                   *, grid_crs=None) -> "CaptureRoute":
        """Build the KP Mouse Tool's route model of ``layer`` (in ``crs``,
        default the layer CRS). Raises ``ValueError`` when it cannot be measured."""
        from ..maptools.kp_mouse_maptool import build_reference_route

        crs = crs or layer.crs()
        if use_cartesian is None:
            use_cartesian = kp_distance_mode() == KP_MODE_CARTESIAN
        try:
            route, distance, _geoms = build_reference_route(layer, crs, use_cartesian, start_kp_km=start_kp_km,
                                                           grid_crs=grid_crs)
        except QgsCsException as exc:
            raise ValueError(f"The route could not be transformed: {exc}") from exc
        if route.total_length_m <= 0:
            raise ValueError(f"The route '{layer.name()}' has no length.")
        return cls(layer, route, distance, QgsCoordinateReferenceSystem(crs))

    @property
    def start_kp_km(self) -> float:
        return float(self.route.start_kp_km)

    @property
    def end_kp_km(self) -> float:
        return float(self.route.end_kp_km)

    def identity(self) -> Dict:
        """The route's identity columns (computed once)."""
        if self._identity is None:
            layer = self.layer
            ident = {
                "route_name": layer.name(), "route_layer_id": layer.id(), "route_source": layer.source(),
                "route_provider": layer.providerType(), "route_crs": layer.crs().authid() or layer.crs().toWkt(),
                "route_hash": route_fingerprint(layer), "route_length_m": float(self.route.total_length_m),
                "start_kp_km": self.start_kp_km, "kp_convention": kp_convention_token(self.distance),
                "kp_basis": kp_basis_text(self.distance),
                "rpl_id": "", "rpl_name": "", "rpl_rev": "", "rpl_route_id": "", "rpl_source_file": "",
            }
            ident.update(workbench_rpl_info(layer))
            self._identity = ident
        return dict(self._identity)

    def _to_wgs84(self):
        return QgsCoordinateTransform(self.crs, QgsCoordinateReferenceSystem(WGS84), QgsProject.instance())

    def _geometry_columns(self, geom: QgsGeometry) -> Dict:
        """``wkt`` (EPSG:4326), ``lat``/``lon`` (the point, or a range's start)
        and the exact captured geometry with its CRS."""
        wgs = QgsGeometry(geom)
        if self.crs.authid() != WGS84:
            wgs.transform(self._to_wgs84())
        point = wgs.asPoint() if geom.type() == GEOMETRY_POINT else wgs.vertexAt(0)
        return {"wkt": wgs.asWkt(12), "lat": float(point.y()), "lon": float(point.x()),
                "geom_wkt": geom.asWkt(12), "geom_crs": self.crs.authid() or self.crs.toWkt()}

    def clamp(self, kp_km: float) -> float:
        return self.route.clamp_kp(float(kp_km))

    def point_values(self, kp_km: float) -> Dict:
        """A route point at ``kp_km`` (clamped to the route)."""
        kp = self.clamp(kp_km)
        point = self.route.point_at_kp(kp, clamp=True)
        if point is None:
            raise ValueError(f"No route position at KP {kp:.3f}.")
        values = self.identity()
        values.update(ref_kind=model.REF_ROUTE_POINT, kp_km=kp, kp_end_km=None, dcc_m=None)
        values.update(self._geometry_columns(QgsGeometry.fromPointXY(QgsPointXY(point))))
        return values

    def values_at_kps(self, kind: str, kp_km: float, kp_end_km: Optional[float] = None) -> Dict:
        """Validate user-entered KPs before calculating a point or range.

        An endpoint rounded just outside the route at display precision (1 m)
        is accepted at that endpoint. Other out-of-route values are rejected.
        """
        if kind not in (model.REF_ROUTE_POINT, model.REF_KP_RANGE):
            raise ValueError("Only route points and KP ranges can be positioned by KP.")
        kps = [kp_km, kp_end_km] if kind == model.REF_KP_RANGE else [kp_km]
        checked = []
        for value in kps:
            if value is None or not math.isfinite(float(value)):
                raise ValueError("Enter a finite KP for each end of the comment.")
            value = float(value)
            if not self.start_kp_km <= value <= self.end_kp_km:
                for bound in (self.start_kp_km, self.end_kp_km):
                    if value == float(f"{bound:.3f}"):
                        value = bound
                        break
                else:
                    raise ValueError(f"KPs must be between {self.start_kp_km:.3f} and {self.end_kp_km:.3f} km.")
            checked.append(value)
        return self.range_values(*checked) if kind == model.REF_KP_RANGE else self.point_values(checked[0])

    def nearest(self, point_xy) -> Tuple[float, float, Optional[QgsPointXY]]:
        """``(kp_km, dcc_m, snapped point)`` nearest ``point_xy`` (``crs``)."""
        hit = self.route.kp_at_point(QgsPointXY(point_xy))
        return float(hit.kp_km), float(hit.dcc_m), hit.snapped_xy

    def location_values(self, point_xy) -> Dict:
        """A free map location at ``point_xy`` (``crs``) with its nearest KP kept apart."""
        kp, dcc, snapped = self.nearest(point_xy)
        values = self.identity()
        values.update(ref_kind=model.REF_LOCATION, kp_end_km=None,
                      kp_km=kp if snapped is not None else None, dcc_m=dcc if snapped is not None else None)
        values.update(self._geometry_columns(QgsGeometry.fromPointXY(QgsPointXY(point_xy))))
        return values

    def range_values(self, kp_a: float, kp_b: float) -> Dict:
        """A KP range (ordered low to high, clamped to the route)."""
        lo, hi = sorted((self.clamp(kp_a), self.clamp(kp_b)))
        if hi - lo < 1e-6:
            raise ValueError("A KP range needs two different KPs.")
        geom = self.route.extract_segment(lo, hi)
        if geom is None or geom.isEmpty():
            raise ValueError(f"No route section between KP {lo:.3f} and {hi:.3f}.")
        if geom.isMultipart():
            parts = [pt for part in iter_line_parts(geom) for pt in part]
            geom = QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in parts])
        values = self.identity()
        values.update(ref_kind=model.REF_KP_RANGE, kp_km=lo, kp_end_km=hi, dcc_m=None)
        values.update(self._geometry_columns(geom))
        return values

    def location_from_wgs84(self, lat: float, lon: float) -> Dict:
        """A free location given in latitude/longitude."""
        point = QgsPointXY(float(lon), float(lat))
        if self.crs.authid() != WGS84:
            try:
                point = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), self.crs,
                                               QgsProject.instance()).transform(point)
            except QgsCsException as exc:
                raise ValueError(f"The position could not be transformed to the route CRS: {exc}") from exc
        if not (math.isfinite(point.x()) and math.isfinite(point.y())):
            raise ValueError("The position lies outside the route CRS.")
        return self.location_values(point)


def location_without_route(lat: float, lon: float) -> Dict:
    """A free location with no route reference (e.g. imported by position)."""
    wkt = f"POINT({float(lon)!r} {float(lat)!r})"
    return {"ref_kind": model.REF_LOCATION, "kp_km": None, "kp_end_km": None, "dcc_m": None, "lat": float(lat),
            "lon": float(lon), "wkt": wkt, "geom_wkt": wkt, "geom_crs": WGS84}


# --------------------------------------------------------------------------- conditions
@dataclass
class RouteState:
    """What was found for a route (the layer itself is not kept: a route
    opened from its file must not stay open while the dock is)."""
    found: bool = False
    how: str = ""                 # "project", "file", "workbench"
    route_hash: str = ""
    start_kp_km: Optional[float] = None
    convention: str = ""
    newer_revision: str = ""
    error: str = ""


class ReferenceChecker:
    """Reference condition of comments against the routes as they are now.

    ``start_kp_for(layer)`` gives the start KP the KP tools use for a layer
    (``None``: detect it from the Workbench, as for a new layer).
    Results are cached per route until :meth:`clear`.
    """

    def __init__(self, project: Optional[QgsProject] = None,
                 start_kp_for: Optional[Callable[[QgsVectorLayer], Optional[float]]] = None,
                 workbench_store: Optional[Callable[[], object]] = None):
        self.project = project or QgsProject.instance()
        self.start_kp_for = start_kp_for
        self._store_opener = workbench_store
        self._store = None
        self._store_loaded = False
        self._cache: Dict[Tuple, RouteState] = {}

    def clear(self) -> None:
        self._cache.clear()
        self._store = None
        self._store_loaded = False

    def _workbench(self):
        if not self._store_loaded:
            self._store_loaded = True
            try:
                if self._store_opener is not None:
                    self._store = self._store_opener()
                else:
                    from ..workbench.registry_access import open_store
                    self._store = open_store()
            except Exception:  # noqa: BLE001
                log_exception("Route Comments: the Workbench registry could not be opened")
                self._store = None
        return self._store

    # -- finding the route -------------------------------------------------------
    def _find_layer(self, comment: Dict) -> Tuple[Optional[QgsVectorLayer], str]:
        layer_id = comment.get("route_layer_id") or ""
        source = _norm_source(comment.get("route_source") or "")
        layer = self.project.mapLayer(layer_id) if layer_id else None
        if isinstance(layer, QgsVectorLayer) and layer.isValid() and \
                (not source or _norm_source(layer.source()) == source):
            return layer, "project"
        if source:
            for candidate in self.project.mapLayers().values():
                if isinstance(candidate, QgsVectorLayer) and candidate.isValid() \
                        and _norm_source(candidate.source()) == source:
                    return candidate, "project"
        rpl_id = comment.get("rpl_id") or ""
        if rpl_id:
            store = self._workbench()
            rpl = store.get_rpl(rpl_id) if store is not None else None
            if rpl and rpl.get("lines_layer"):
                layer = store.open_layer(rpl.get("lines_layer"))
                if layer is not None and layer.isValid():
                    return layer, "workbench"
        path = _source_path(comment.get("route_source") or "")
        if comment.get("route_provider") == "ogr" and path and os.path.isfile(path):
            layer = QgsVectorLayer(comment.get("route_source"), comment.get("route_name") or "route", "ogr")
            if layer.isValid():
                return layer, "file"
        return None, ""

    def capture_for_edit(self, comment: Dict) -> Optional[CaptureRoute]:
        """Resolve the original route and KP basis for an explicit position edit.

        Never transfer a comment to changed geometry or the current reference
        layer. Old, unreferenced map locations can still be edited by position.
        """
        linked = any(comment.get(k) for k in ("route_hash", "route_layer_id", "route_source", "rpl_id"))
        if not linked and comment.get("ref_kind") == model.REF_LOCATION:
            return None
        layer, _how = self._find_layer(comment)
        if layer is None:
            raise ValueError("The comment's route is unavailable. Restore its original route to edit the position.")
        if comment.get("route_hash") and route_fingerprint(layer) != comment["route_hash"]:
            raise ValueError("The comment's route geometry has changed. Restore its original route to edit the "
                             "position using the saved KPs.")
        token = comment.get("kp_convention") or ""
        use_cartesian, grid_crs = None, None
        if token == "geodesic:WGS84":
            use_cartesian = False
        elif token.startswith("grid:"):
            use_cartesian = True
            grid_crs = token.split(":", 1)[1]
            grid = QgsCoordinateReferenceSystem(grid_crs)
            if not grid.isValid() or grid.isGeographic():
                raise ValueError("The comment's saved KP grid cannot be restored: " + token)
        elif token:
            raise ValueError("The comment's saved KP convention cannot be restored: " + token)
        # Use the capture CRS too: nearest-point calculations must match capture.
        crs = QgsCoordinateReferenceSystem(comment.get("geom_crs") or "")
        if not crs.isValid():
            crs = layer.crs()
        capture = CaptureRoute.from_layer(layer, crs, start_kp_km=comment.get("start_kp_km"),
                                          use_cartesian=use_cartesian, grid_crs=grid_crs)
        if token and capture.identity()["kp_convention"] != token:
            raise ValueError("The comment's saved KP convention could not be restored.")
        return capture

    def route_state(self, comment: Dict) -> RouteState:
        key = (comment.get("route_layer_id") or "", _norm_source(comment.get("route_source") or ""),
               comment.get("rpl_id") or "")
        state = self._cache.get(key)
        if state is not None:
            return state
        state = RouteState()
        try:
            layer, how = self._find_layer(comment)
            if layer is not None:
                state.found, state.how = True, how
                state.route_hash = route_fingerprint(layer)
                start = self.start_kp_for(layer) if self.start_kp_for is not None and how == "project" else None
                state.start_kp_km = float(start) if start is not None else kp_datum.lines_layer_start_kp(
                    layer, self._workbench() if comment.get("rpl_id") else None)
                state.convention = current_convention(layer)
                state.newer_revision = self._newer_revision(comment)
        except Exception as exc:  # noqa: BLE001 - one broken route must not stop the register
            log_exception("Route Comments: checking a route reference failed")
            state = RouteState(error=str(exc))
        self._cache[key] = state
        return state

    def _newer_revision(self, comment: Dict) -> str:
        rpl_id, route_id = comment.get("rpl_id") or "", comment.get("rpl_route_id") or ""
        store = self._workbench() if rpl_id and route_id else None
        if store is None:
            return ""
        latest = store.latest_revision(route_id)
        if latest and latest.get("rpl_id") and latest.get("rpl_id") != rpl_id:
            return latest.get("rev_label") or latest.get("name") or "a newer revision"
        return ""

    # -- condition ----------------------------------------------------------------
    def check(self, comment: Dict) -> Tuple[str, List[str]]:
        """``(condition code, notes)`` for one comment (see ``model.COND_*``)."""
        if not (comment.get("route_hash") or comment.get("route_layer_id") or comment.get("rpl_id")):
            return model.COND_NONE, ["Not referenced to a route."]
        name = model.route_text(comment) or "the route"
        state = self.route_state(comment)
        if not state.found:
            why = f" ({state.error})" if state.error else ""
            return model.COND_UNAVAILABLE, [
                f"{name} is not available: it is not in this project and its source cannot be opened{why}. "
                "The comment keeps the position and KPs it was made with."]
        notes = []
        if comment.get("route_hash") and state.route_hash != comment.get("route_hash"):
            notes.append(f"The geometry of {name} has changed since the comment was made; its KPs refer to "
                         "the route as it was then.")
        stored_start = comment.get("start_kp_km")
        if stored_start is not None and state.start_kp_km is not None \
                and abs(float(stored_start) - float(state.start_kp_km)) > START_KP_TOL_KM:
            notes.append(f"The route now starts at KP {state.start_kp_km:.3f} (the comment was made with start "
                         f"KP {float(stored_start):.3f}).")
        stored_conv = comment.get("kp_convention") or ""
        if stored_conv and state.convention and stored_conv != state.convention:
            notes.append(f"KPs are now measured {describe_convention(state.convention)}; the comment's KPs are "
                         f"{describe_convention(stored_conv)}.")
        if notes:
            return model.COND_CHANGED, notes
        if state.newer_revision:
            return model.COND_SUPERSEDED, [f"A newer revision ({state.newer_revision}) of this route is in the "
                                           "Workbench. The comment stays on the revision it was made on."]
        where = {"workbench": " (opened from the Workbench)", "file": " (opened from its file)"}.get(state.how, "")
        return model.COND_OK, [f"{name} is unchanged{where}."]

    def check_all(self, comments) -> Dict[str, Tuple[str, List[str]]]:
        return {c.get("comment_id"): self.check(c) for c in comments}
