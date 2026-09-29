"""Linear-referencing primitives for the Subsea Cable Tools plugin.

This module owns the **geometry ↔ KP** conversions that were previously
re-implemented across processing algorithms, dock widgets and map tools:

* KP (km along a line) → point on line
* Point → nearest KP on line (with cross-track distance)
* Cumulative-length walk across multi-feature **and** multi-part line layers
* KP-range → sub-line geometry extraction
* CRS-mismatch reprojection of a route into a target CRS

Distance measurement itself stays in :mod:`kp_range_utils` — callers build a
configured :class:`QgsDistanceArea` via
:func:`kp_range_utils.make_distance_area` and pass it in. This module never
reads project settings.

KP semantics
------------

* KP units at the API surface are **kilometres**; metres are internal.
* For multi-feature line layers, KP is **continuous**: it accumulates across
  features in iteration order. This matches the existing KP Mouse Tool and
  Find Nearest KP behaviour for multi-feature RPLs.
* Out-of-range KPs return ``None`` by default. Pass ``clamp=True`` to clamp
  to the route start/end.
* One KP definition plugin-wide: geodesic (WGS84) segment lengths, with
  positions interpolated along each segment as stored
  (``follow_stored_geometry=True``), so point→KP→point round-trips exactly.
  Routes are assembled with :func:`ordered_route_geometry` (SeqNo / layer
  order, no noding). A :class:`RouteFrame` may start at a non-zero KP
  (``start_kp_km``, an RPL's first position).
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from typing import Iterable, Iterator, List, NamedTuple, Optional, Sequence, Union

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsFeatureSource,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
)

from .plugin_log import log_exception, log_warning


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class KPHit(NamedTuple):
    """Result of :func:`kp_at_point`.

    Attributes
    ----------
    kp_km:
        KP value in kilometres along the route, measured from the start of the
        first feature. ``0.0`` when no usable geometry was found.
    dcc_m:
        Distance Cross Course — perpendicular distance in metres from the
        input point to the snapped point on the route. ``inf`` when no usable
        geometry was found.
    snapped_xy:
        Snapped point on the route in the route's CRS, or ``None`` when no
        usable geometry was found.
    feature_index:
        Index (within the iterable passed to ``kp_at_point``) of the feature
        containing the snapped point. ``-1`` when no usable geometry was found.
    """

    kp_km: float
    dcc_m: float
    snapped_xy: Optional[QgsPointXY]
    feature_index: int


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def iter_line_parts(line_geometry: QgsGeometry) -> List[List]:
    """Return a list of polyline parts from a (multi)line geometry.

    Each part is a sequence of points (``QgsPointXY`` for 2D, ``QgsPoint`` for
    geometries with Z/M). Returns ``[]`` for empty / non-line geometries.
    """

    if line_geometry is None or line_geometry.isEmpty():
        return []

    if line_geometry.isMultipart():
        try:
            return list(line_geometry.asMultiPolyline())
        except (TypeError, ValueError):  # not a line geometry
            return []

    try:
        return [line_geometry.asPolyline()]
    except (TypeError, ValueError):  # not a line geometry
        return []


def get_features_skip_invalid(source, request=None):
    """Iterate a feature source without Processing's invalid-geometry filter.

    ``parameterAsSource`` wraps the input layer in a
    ``QgsProcessingFeatureSource`` that applies the user's Processing
    "Invalid features filtering" setting. The QGIS default aborts the whole
    algorithm on the first GEOS-invalid feature (e.g. a zero-length line
    left behind by digitising), and the "skip" setting silently drops such
    features — which shortens the route and shifts every KP after the gap.
    The plugin's tools measure along linework and never rely on OGC
    validity, so they opt out of the check the same way QGIS core
    algorithms do, and handle null/empty geometries explicitly instead.

    Safe to call with a plain ``QgsVectorLayer``/``QgsFeatureSource``,
    where it behaves exactly like ``source.getFeatures()``.
    """
    from qgis.core import Qgis, QgsFeatureRequest, QgsProcessingFeatureSource

    if request is None:
        request = QgsFeatureRequest()
    if isinstance(source, QgsProcessingFeatureSource):
        flag_scope = getattr(Qgis, "ProcessingFeatureSourceFlag", None)
        flag = getattr(flag_scope, "SkipGeometryValidityChecks", None)
        if flag is None:
            flag = getattr(
                QgsProcessingFeatureSource, "FlagSkipGeometryValidityChecks", None
            )
        if flag is not None:
            try:
                return source.getFeatures(request, flag)
            except TypeError:
                pass
    return source.getFeatures(request)


def _normalise_geoms(geoms) -> List[QgsGeometry]:
    """Accept a single geometry, an iterable, or a feature source."""

    if geoms is None:
        return []
    if isinstance(geoms, QgsGeometry):
        return [geoms]
    if isinstance(geoms, QgsFeatureSource):
        out: List[QgsGeometry] = []
        for feat in get_features_skip_invalid(geoms):
            g = feat.geometry()
            if g is not None and not g.isEmpty():
                out.append(QgsGeometry(g))
        return out
    out2: List[QgsGeometry] = []
    for g in geoms:
        if g is not None and not g.isEmpty():
            out2.append(g)
    return out2


ORDER_FIELDS = ("SeqNo", "seqno", "SEQNO", "Seq", "seq")


def ordered_route_features(features) -> list:
    """Route features in chainage order: by an RPL ``SeqNo`` field when every
    feature has one, else as given (layer order). Empty geometries dropped."""
    feats = [f for f in features
             if f.hasGeometry() and f.geometry() is not None and not f.geometry().isEmpty()]
    if not feats:
        return []
    names = feats[0].fields().names() if hasattr(feats[0], "fields") else []
    field = next((n for n in ORDER_FIELDS if n in names), None)
    if field is None:
        return feats
    keyed = []
    for index, feat in enumerate(feats):
        try:
            keyed.append((float(feat[field]), index, feat))
        except (TypeError, ValueError, KeyError):
            return feats   # incomplete numbering: keep layer order
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [f for _k, _i, f in keyed]


def ordered_route_geometry(items, join_tolerance: float = 1e-9) -> QgsGeometry:
    """The plugin's one way to turn a multi-feature line layer into a route.

    ``items`` are features (ordered via :func:`ordered_route_features`) or
    geometries (kept in the given order). Parts are concatenated in that
    order; a part that starts where the previous one ended is joined to it,
    so RPL legs become one continuous line. Nothing is noded, re-ordered or
    reversed — unlike ``unaryUnion`` / ``mergeLines`` — so KP along the
    result equals :class:`RouteFrame` KP over the same features.
    """
    items = list(items or [])
    if items and not isinstance(items[0], QgsGeometry):
        geoms = [QgsGeometry(f.geometry()) for f in ordered_route_features(items)]
    else:
        geoms = _normalise_geoms(items)
    lines: List[List[QgsPointXY]] = []
    for geom in geoms:
        for part in iter_line_parts(geom):
            pts = [QgsPointXY(p) for p in part]
            if len(pts) < 2:
                continue
            if lines:
                last = lines[-1][-1]
                if (abs(last.x() - pts[0].x()) <= join_tolerance
                        and abs(last.y() - pts[0].y()) <= join_tolerance):
                    lines[-1].extend(pts[1:])
                    continue
            lines.append(pts)
    if not lines:
        return QgsGeometry()
    if len(lines) == 1:
        return QgsGeometry.fromPolylineXY(lines[0])
    return QgsGeometry.fromMultiPolylineXY(lines)


def geometry_is_finite(geometry: QgsGeometry) -> bool:
    """True when every coordinate of ``geometry`` is finite.

    ``QgsGeometry.transform`` does not raise for points outside the target
    CRS's domain (QGIS 3.40 / 4.0): it reports success and writes ``inf``.
    Check the result with this before trusting a transformed geometry.
    """
    box = geometry.boundingBox()
    return all(math.isfinite(v) for v in (box.xMinimum(), box.yMinimum(),
                                          box.xMaximum(), box.yMaximum()))


def stored_geometry_index(features=None) -> QgsSpatialIndex:
    """A ``QgsSpatialIndex`` that keeps each feature's geometry.

    A plain index only holds bounding boxes, so ``nearestNeighbor`` ranks by
    *bounding-box* distance: a long diagonal line whose box contains the
    query point beats a much closer short line, and a "nearest contour" or
    "nearest route segment" can be the wrong one. With stored geometries the
    ranking uses the true geometry distance. ``features`` may be a feature
    iterator (e.g. ``layer.getFeatures()``) or ``None`` for an empty index
    to fill with ``addFeature``.
    """
    scope = getattr(QgsSpatialIndex, "Flag", QgsSpatialIndex)
    flag = getattr(scope, "FlagStoreFeatureGeometries")
    if features is None:
        return QgsSpatialIndex(flag)
    return QgsSpatialIndex(features, None, flag)


_UNMEASURABLE_WARNED: set = set()


def _warn_unmeasurable_segment(p1, p2, detail: str) -> None:
    """Log (once per segment) that a route segment could not be measured.

    Chainage after such a segment is unknowable: skipping it would silently
    shift every later KP, so callers stop there and report instead.
    """
    try:
        key = (round(float(p1.x()), 9), round(float(p1.y()), 9),
               round(float(p2.x()), 9), round(float(p2.y()), 9))
    except (TypeError, ValueError, AttributeError):
        key = (repr(p1), repr(p2))
    if key in _UNMEASURABLE_WARNED:
        return
    if len(_UNMEASURABLE_WARNED) > 1000:
        _UNMEASURABLE_WARNED.clear()
    _UNMEASURABLE_WARNED.add(key)
    log_warning(
        "KP: route segment (%s) -> (%s) could not be measured (%s); KPs beyond "
        "it are undefined and are not reported." % (
            _xy_text(p1), _xy_text(p2), detail))


def _xy_text(point) -> str:
    try:
        return "%.8f, %.8f" % (float(point.x()), float(point.y()))
    except (TypeError, ValueError, AttributeError):
        return repr(point)


def _measure_segment_m(distance: QgsDistanceArea, p1, p2) -> Optional[float]:
    """Length (m) of ``p1 -> p2``, or ``None`` (warned) if it can't be measured."""
    try:
        seg_len = float(distance.measureLine(p1, p2))
    except Exception as exc:  # noqa: BLE001 - QgsCsException et al.
        _warn_unmeasurable_segment(p1, p2, "%s: %s" % (type(exc).__name__, exc))
        return None
    if not math.isfinite(seg_len):
        _warn_unmeasurable_segment(p1, p2, "length %r" % seg_len)
        return None
    return seg_len


def crosses_antimeridian(geoms_or_geom) -> bool:
    """True when any segment jumps more than 180° of longitude.

    Every planar operation in the plugin (GEOS predicates, search
    rectangles, stored-segment interpolation) assumes longitudes vary
    continuously; a route straddling ±180° silently produces positions and
    intersections on the wrong side of the planet. Callers detect and
    refuse such routes with a clear message instead.
    """
    for geom in _normalise_geoms(geoms_or_geom):
        for part in iter_line_parts(geom):
            for i in range(len(part) - 1):
                try:
                    if abs(float(part[i + 1].x()) - float(part[i].x())) > 180.0:
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def measure_total_length_m(
    geoms_or_geom, distance: QgsDistanceArea
) -> float:
    """Return total length in metres of one or more line geometries.

    Accepts a single ``QgsGeometry``, an iterable of geometries, or a
    ``QgsFeatureSource``.
    """

    geoms = _normalise_geoms(geoms_or_geom)
    total = 0.0
    for geom in geoms:
        for part in iter_line_parts(geom):
            for i in range(len(part) - 1):
                total += float(distance.measureLine(part[i], part[i + 1]))
    return float(total)


def _interpolate_on_segment(
    p1, p2, distance: QgsDistanceArea, target_dist_m: float, seg_len_m: float
) -> QgsPointXY:
    """Return the point at ``target_dist_m`` along the segment ``p1 -> p2``.

    On geographic CRSes the point is forward-projected on the spheroid using
    ``QgsDistanceArea.computeSpheroidProject`` (so the result lies on the
    geodesic, matching the ``measureLine`` distance). On projected CRSes the
    fast linear interpolation is used, which is exact in the segment's own
    plane.

    Falls back to linear interpolation if anything goes wrong.
    """

    src_crs = None
    try:
        src_crs = distance.sourceCrs()
    except Exception:
        src_crs = None

    if (
        src_crs is not None
        and src_crs.isGeographic()
        and hasattr(distance, "computeSpheroidProject")
        and hasattr(distance, "bearing")
        and seg_len_m > 0
    ):
        try:
            p1_xy = QgsPointXY(p1)
            p2_xy = QgsPointXY(p2)
            az = float(distance.bearing(p1_xy, p2_xy))
            pt = distance.computeSpheroidProject(p1_xy, float(target_dist_m), az)
            return QgsPointXY(pt)
        except Exception:  # noqa: BLE001 - documented linear fallback
            log_exception("KP: spheroid interpolation failed; interpolating linearly",
                          level=logging.DEBUG)

    ratio = (target_dist_m / seg_len_m) if seg_len_m > 0 else 0.0
    x = float(p1.x()) + ratio * (float(p2.x()) - float(p1.x()))
    y = float(p1.y()) + ratio * (float(p2.y()) - float(p1.y()))
    return QgsPointXY(x, y)


def _interpolate_on_stored_segment(
    p1, p2, target_dist_m: float, seg_len_m: float
) -> QgsPointXY:
    """Interpolate along the segment as stored/rendered in its current CRS.

    The segment's total chainage may still be ellipsoidal; this function only
    controls the visual path followed between its vertices. It is useful for
    canvas playback, where replacing a geographic line edge with its spheroid
    arc would visibly depart from the QGIS feature the user drew.
    """
    ratio = (target_dist_m / seg_len_m) if seg_len_m > 0 else 0.0
    ratio = min(1.0, max(0.0, ratio))
    x = float(p1.x()) + ratio * (float(p2.x()) - float(p1.x()))
    y = float(p1.y()) + ratio * (float(p2.y()) - float(p1.y()))
    return QgsPointXY(x, y)


# ---------------------------------------------------------------------------
# KP ↔ point primitives
# ---------------------------------------------------------------------------


def point_at_kp(
    geoms_or_geom,
    kp_km: float,
    distance: QgsDistanceArea,
    *,
    clamp: bool = False,
    follow_stored_geometry: bool = True,
) -> Optional[QgsPointXY]:
    """Return the point on the route at the given KP.

    Parameters
    ----------
    geoms_or_geom:
        Single ``QgsGeometry``, iterable of geometries, or feature source.
        For multi-feature inputs the KP is continuous across features in
        iteration order.
    kp_km:
        KP in kilometres.
    distance:
        Configured distance calculator (built via
        ``kp_range_utils.make_distance_area``).
    clamp:
        When ``True``, KPs outside ``[0, total_length_km]`` are clamped to the
        route start / end. When ``False`` (default), out-of-range returns
        ``None``.
    follow_stored_geometry:
        ``True`` (default): interpolate along each segment as stored/drawn
        (fraction of the segment x its geodesic length). This is the exact
        inverse of ``kp_at_point`` and keeps points on the line QGIS draws.
        ``False`` follows the spheroid arc instead; on a long geographic
        segment that point is then off the drawn line and ``kp_at_point``
        of it can differ by tens of metres (60 km leg: ~57 m).

    A segment that cannot be measured (transform failure, NaN length) makes
    every KP at or beyond it undefined: ``None`` is returned (even with
    ``clamp``) and a warning is logged, instead of skipping the segment and
    silently shifting every later position.
    """

    try:
        target_m = float(kp_km) * 1000.0
    except (TypeError, ValueError):
        return None
    if math.isnan(target_m):
        return None

    geoms = _normalise_geoms(geoms_or_geom)
    if not geoms:
        return None

    if target_m < 0.0:
        if not clamp:
            return None
        target_m = 0.0

    cumulative = 0.0
    first_point: Optional[QgsPointXY] = None
    last_point: Optional[QgsPointXY] = None

    for geom in geoms:
        for part in iter_line_parts(geom):
            if len(part) < 2:
                continue
            if first_point is None:
                first_point = QgsPointXY(part[0])
            for i in range(len(part) - 1):
                p1 = part[i]
                p2 = part[i + 1]
                seg_len = _measure_segment_m(distance, p1, p2)
                if seg_len is None:
                    return None
                if seg_len <= 0:
                    continue

                next_cum = cumulative + seg_len
                last_point = QgsPointXY(p2)

                if target_m <= next_cum:
                    if follow_stored_geometry:
                        return _interpolate_on_stored_segment(
                            p1, p2, target_m - cumulative, seg_len)
                    return _interpolate_on_segment(
                        p1, p2, distance, target_m - cumulative, seg_len)

                cumulative = next_cum

    # Past the end of the route.
    if clamp:
        return last_point if last_point is not None else first_point
    return None


def kp_at_point(
    geoms_or_geom,
    point_xy: QgsPointXY,
    distance: QgsDistanceArea,
) -> KPHit:
    """Return the nearest KP on the route to ``point_xy``.

    Walks every feature, finds the global nearest point (by ellipsoidal /
    cartesian distance per ``distance``'s configuration), then re-walks that
    feature to compute the cumulative KP up to the snapped point.

    Coordinates of ``point_xy`` and the route geometries must be in the same
    CRS — reproject beforehand with :func:`reproject_geoms_to` if not.
    """

    geoms = _normalise_geoms(geoms_or_geom)
    if not geoms or point_xy is None:
        return KPHit(0.0, float("inf"), None, -1)

    point_geom = QgsGeometry.fromPointXY(QgsPointXY(point_xy))

    # First pass: feature-level cumulative offsets and pick the closest feature.
    offsets_m: List[float] = []
    cumulative = 0.0
    for geom in geoms:
        offsets_m.append(cumulative)
        cumulative += measure_total_length_m(geom, distance)

    best_feature = -1
    best_dist = float("inf")
    best_snapped: Optional[QgsPointXY] = None
    for idx, geom in enumerate(geoms):
        nearest_geom = geom.nearestPoint(point_geom)
        if nearest_geom is None or nearest_geom.isEmpty():
            continue
        try:
            snapped = QgsPointXY(nearest_geom.asPoint())
        except Exception:
            continue
        try:
            d = float(distance.measureLine(QgsPointXY(point_xy), snapped))
        except Exception:
            continue
        if d < best_dist:
            best_dist = d
            best_feature = idx
            best_snapped = snapped

    if best_feature < 0 or best_snapped is None:
        return KPHit(0.0, float("inf"), None, -1)

    # Second pass: walk the chosen feature to compute KP up to the snapped point.
    feature_kp_m = _kp_along_geometry_m(geoms[best_feature], best_snapped, distance)
    total_m = offsets_m[best_feature] + feature_kp_m
    return KPHit(total_m / 1000.0, best_dist, best_snapped, best_feature)


def _kp_along_geometry_m(
    geom: QgsGeometry, snapped: QgsPointXY, distance: QgsDistanceArea
) -> float:
    """Return the distance (m) from the start of ``geom`` to ``snapped``.

    ``snapped`` is assumed to lie on (or very near) ``geom``. Picks the segment
    whose perpendicular projection of ``snapped`` is closest, then sums prior
    segment lengths plus the partial length to the projection.

    The per-segment projection is computed in planar coordinates of the
    geometry's CRS. When ``snapped`` is the output of
    ``QgsGeometry.nearestPoint`` (also planar in the geometry CRS) this is
    consistent. The partial length is then scaled by the ellipsoidal
    ``measureLine`` length, which is accurate in the small for short segments
    even on geographic CRSes.
    """

    cumulative = 0.0
    best_kp_m = 0.0
    best_dist = float("inf")
    sx, sy = float(snapped.x()), float(snapped.y())

    for part in iter_line_parts(geom):
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            x1, y1 = float(p1.x()), float(p1.y())
            x2, y2 = float(p2.x()), float(p2.y())
            dx = x2 - x1
            dy = y2 - y1
            seg_len_planar_sq = dx * dx + dy * dy
            try:
                seg_len = float(distance.measureLine(p1, p2))
            except Exception:
                seg_len = 0.0

            if seg_len_planar_sq <= 0.0 or seg_len <= 0.0:
                continue

            # Project snapped onto segment in planar coords.
            t = ((sx - x1) * dx + (sy - y1) * dy) / seg_len_planar_sq
            t_clamped = max(0.0, min(1.0, t))
            px = x1 + t_clamped * dx
            py = y1 + t_clamped * dy
            ddx = sx - px
            ddy = sy - py
            d2 = ddx * ddx + ddy * ddy

            if d2 < best_dist:
                best_dist = d2
                best_kp_m = cumulative + t_clamped * seg_len

            cumulative += seg_len

    return best_kp_m


def extract_line_segment(
    line_geometry: QgsGeometry,
    start_kp_km: float,
    end_kp_km: float,
    distance: QgsDistanceArea,
    *,
    follow_stored_geometry: bool = True,
) -> Optional[QgsGeometry]:
    """Extract a line segment between two KPs along a single (multi)polyline.

    Returns a LineString ``QgsGeometry`` in the same CRS as the input. Returns
    ``None`` for invalid / out-of-range / zero-length ranges.

    Note: this primitive operates on a **single** geometry, not on a route
    composed of multiple features. Use :class:`RouteFrame` for multi-feature
    routes.
    """

    try:
        start_kp_km = float(start_kp_km)
        end_kp_km = float(end_kp_km)
    except Exception:
        return None

    if start_kp_km == end_kp_km:
        return None

    if start_kp_km > end_kp_km:
        start_kp_km, end_kp_km = end_kp_km, start_kp_km

    start_m = start_kp_km * 1000.0
    end_m = end_kp_km * 1000.0
    if start_m < 0 or end_m < 0:
        return None

    parts = iter_line_parts(line_geometry)
    if not parts:
        return None

    segment_points: List = []
    cumulative = 0.0
    started = False

    for part in parts:
        if len(part) < 2:
            continue
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            seg_len = float(distance.measureLine(p1, p2))
            if seg_len <= 0:
                continue

            next_cum = cumulative + seg_len

            if not started and next_cum >= start_m:
                interp = (
                    _interpolate_on_stored_segment(
                        p1, p2, start_m - cumulative, seg_len)
                    if follow_stored_geometry else
                    _interpolate_on_segment(
                        p1, p2, distance, start_m - cumulative, seg_len)
                )
                try:
                    segment_points.append(p1.__class__(interp.x(), interp.y()))
                except Exception:
                    segment_points.append(type(p1)(interp.x(), interp.y()))
                started = True

            if started:
                if next_cum <= end_m:
                    segment_points.append(p2)
                else:
                    interp = (
                        _interpolate_on_stored_segment(
                            p1, p2, end_m - cumulative, seg_len)
                        if follow_stored_geometry else
                        _interpolate_on_segment(
                            p1, p2, distance, end_m - cumulative, seg_len)
                    )
                    try:
                        segment_points.append(p1.__class__(interp.x(), interp.y()))
                    except Exception:
                        segment_points.append(type(p1)(interp.x(), interp.y()))
                    try:
                        return QgsGeometry.fromPolyline(segment_points)
                    except Exception:
                        try:
                            return QgsGeometry.fromPolylineXY(segment_points)
                        except Exception:
                            return None

            cumulative = next_cum

    if not started or len(segment_points) < 2:
        return None

    try:
        return QgsGeometry.fromPolyline(segment_points)
    except Exception:
        try:
            return QgsGeometry.fromPolylineXY(segment_points)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# CRS reprojection helper
# ---------------------------------------------------------------------------


def reproject_geoms_to(
    geoms: Iterable[QgsGeometry],
    source_crs: QgsCoordinateReferenceSystem,
    target_crs: QgsCoordinateReferenceSystem,
    project: Optional[QgsProject] = None,
    transform_context: Optional[QgsCoordinateTransformContext] = None,
    strict: bool = False,
) -> Iterator[QgsGeometry]:
    """Yield copies of ``geoms`` reprojected from ``source_crs`` to ``target_crs``.

    Geometries are copied before transformation (the input is not mutated).
    When the two CRSes are equal, geometries are yielded unchanged (still as
    copies). The transform uses ``transform_context`` when given (callers on
    worker threads, e.g. a Processing context's), else ``project``'s, else
    the current project's. A geometry that fails to transform (an exception,
    or non-finite coordinates — QGIS writes ``inf`` rather than raising)
    raises ``ValueError`` with ``strict``; otherwise it is skipped with a
    logged warning (in a route, every KP after a skipped feature shifts) —
    the caller is responsible for any user feedback.
    """

    if source_crs == target_crs:
        for g in geoms:
            if g is not None and not g.isEmpty():
                yield QgsGeometry(g)
        return

    if transform_context is not None:
        xform = QgsCoordinateTransform(source_crs, target_crs, transform_context)
    else:
        xform = QgsCoordinateTransform(source_crs, target_crs,
                                       project if project is not None else QgsProject.instance())
    for index, g in enumerate(geoms):
        if g is None or g.isEmpty():
            continue
        copy = QgsGeometry(g)
        message = "KP: geometry %d could not be transformed from %s to %s" % (
            index, source_crs.authid(), target_crs.authid())
        try:
            copy.transform(xform)
            failed = not geometry_is_finite(copy)
        except Exception:  # noqa: BLE001 - QgsCsException
            if strict:
                raise ValueError(message)
            log_exception(message + " and is left out; KPs after it are shifted")
            continue
        if failed:
            if strict:
                raise ValueError(message + " (coordinates outside the target CRS)")
            log_warning(message + " (coordinates outside the target CRS) and is left "
                        "out; KPs after it are shifted")
            continue
        yield copy


# ---------------------------------------------------------------------------
# RouteFrame
# ---------------------------------------------------------------------------


class RouteFrame:
    """A cached view of a multi-feature line layer for KP lookups.

    Builds once from a feature source (or iterable of geometries), then serves
    repeated ``point_at_kp`` / ``kp_at_point`` / ``extract_segment`` calls
    without re-iterating the provider.

    The cached geometries are stored in the **route CRS** (the CRS of the
    incoming features unless ``target_crs`` is supplied, in which case they
    are reprojected up front). All KP and DCC measurements use the supplied
    ``QgsDistanceArea``; the caller is responsible for building it with a
    source CRS that matches the cached geometries.

    Typical usage::

        from .kp_range_utils import make_distance_area
        from .kp_geo_utils import RouteFrame

        distance = make_distance_area(layer.crs(), context.transformContext())
        route = RouteFrame.from_source(layer, distance)
        pt = route.point_at_kp(12.345)
    """

    def __init__(
        self,
        geoms: Sequence[QgsGeometry],
        feature_lengths_m: Sequence[float],
        distance: QgsDistanceArea,
        follow_stored_geometry: bool = True,
        start_kp_km: float = 0.0,
    ) -> None:
        self._geoms: List[QgsGeometry] = list(geoms)
        self._feature_lengths_m: List[float] = list(feature_lengths_m)
        # Cumulative offsets at the *start* of each feature.
        self._offsets_m: List[float] = []
        running = 0.0
        for length in self._feature_lengths_m:
            self._offsets_m.append(running)
            running += float(length)
        self._total_m: float = running
        self._distance = distance
        self._follow_stored_geometry = bool(follow_stored_geometry)
        # KP of the route's first vertex: an RPL may start at a non-zero KP.
        # Chainage is still measured from the start; every KP in or out of
        # this frame is ``start_kp_km + chainage``.
        try:
            self._start_kp_km = float(start_kp_km or 0.0)
        except (TypeError, ValueError):
            self._start_kp_km = 0.0
        # Lazy chainage/KP indexes may be built from either the main thread
        # or a background task; the lock closes the half-initialised window.
        self._chain_lock = threading.Lock()
        self._kp_index = None
        self._seg_feature: List[int] = []

    # ----- builders -----

    @classmethod
    def from_source(
        cls,
        source,
        distance: QgsDistanceArea,
        target_crs: Optional[QgsCoordinateReferenceSystem] = None,
        source_crs: Optional[QgsCoordinateReferenceSystem] = None,
        project: Optional[QgsProject] = None,
        follow_stored_geometry: bool = True,
        start_kp_km: float = 0.0,
        transform_context: Optional[QgsCoordinateTransformContext] = None,
    ) -> "RouteFrame":
        """Build a ``RouteFrame`` from a feature source or iterable of geometries.

        When ``target_crs`` is given and differs from ``source_crs`` (inferred
        from the source when possible), geometries are reprojected up front,
        with ``transform_context`` when given (see :func:`reproject_geoms_to`).
        """

        # Resolve source CRS for reprojection, if any.
        if source_crs is None and isinstance(source, QgsFeatureSource):
            try:
                source_crs = source.sourceCrs()
            except Exception:
                source_crs = None

        raw_geoms = _normalise_geoms(source)

        if target_crs is not None and source_crs is not None and source_crs != target_crs:
            geoms = list(reproject_geoms_to(raw_geoms, source_crs, target_crs, project,
                                            transform_context=transform_context))
        else:
            geoms = raw_geoms

        lengths = [measure_total_length_m(g, distance) for g in geoms]
        return cls(geoms, lengths, distance, follow_stored_geometry, start_kp_km)

    # ----- properties -----

    @property
    def geometries(self) -> List[QgsGeometry]:
        return list(self._geoms)

    @property
    def total_length_m(self) -> float:
        return self._total_m

    @property
    def total_length_km(self) -> float:
        return self._total_m / 1000.0

    @property
    def start_kp_km(self) -> float:
        """KP of the route start (0 unless the RPL starts at another KP)."""
        return self._start_kp_km

    @property
    def end_kp_km(self) -> float:
        """KP of the route end: ``start_kp_km + total_length_km``."""
        return self._start_kp_km + self._total_m / 1000.0

    def clamp_kp(self, kp_km: float) -> float:
        """``kp_km`` limited to ``[start_kp_km, end_kp_km]``."""
        return min(max(float(kp_km), self.start_kp_km), self.end_kp_km)

    @property
    def feature_offsets_m(self) -> List[float]:
        """Chainage (m from the route start) where each feature begins."""
        return list(self._offsets_m)

    # ----- queries -----

    def _ensure_chainage(self) -> None:
        """Build the per-segment chainage index once (lazily).

        ``point_at_kp`` used to re-walk every vertex with one geodesic
        measurement per segment on *every* call — O(vertices) per lookup,
        which made dense sampling of long routes quadratic. The index costs
        one full walk, after which each lookup is a bisect. Same segments,
        same ``measureLine`` calls in the same order, so cumulative chainage
        is identical to the walking implementation.

        A segment that cannot be measured ends the index there (warned):
        chainage beyond it is unknowable, and skipping it would shift every
        later KP. KPs past that point are then out of range.
        """
        if getattr(self, "_seg_end_m", None) is not None:
            return
        with self._chain_lock:
            if getattr(self, "_seg_end_m", None) is not None:
                return
            seg_end: List[float] = []
            segs: List[tuple] = []  # (p1, p2, seg_len_m, cumulative_at_p1_m)
            seg_feature: List[int] = []
            cumulative = 0.0
            first_point: Optional[QgsPointXY] = None
            last_point: Optional[QgsPointXY] = None
            broken = False
            for feature_index, geom in enumerate(self._geoms):
                for part in iter_line_parts(geom):
                    if len(part) < 2:
                        continue
                    if first_point is None:
                        first_point = QgsPointXY(part[0])
                    for i in range(len(part) - 1):
                        p1 = part[i]
                        p2 = part[i + 1]
                        seg_len = _measure_segment_m(self._distance, p1, p2)
                        if seg_len is None:
                            broken = True
                            break
                        if seg_len <= 0:
                            continue
                        segs.append((p1, p2, seg_len, cumulative))
                        seg_feature.append(feature_index)
                        cumulative += seg_len
                        seg_end.append(cumulative)
                        last_point = QgsPointXY(p2)
                    if broken:
                        break
                if broken:
                    break
            self._segs = segs
            self._seg_feature = seg_feature
            self._chain_total_m = cumulative
            self._chain_broken = broken
            self._chain_first = first_point
            self._chain_last = last_point
            # The guard attribute is assigned last so a concurrent reader
            # that passes the unlocked fast check sees a complete index.
            self._seg_end_m = seg_end

    def point_at_kp(self, kp_km: float, *, clamp: bool = False) -> Optional[QgsPointXY]:
        try:
            target_m = (float(kp_km) - self._start_kp_km) * 1000.0
        except (TypeError, ValueError):
            return None
        if math.isnan(target_m):
            # NaN used to bisect to index 0 and come back as the route start.
            return None
        if target_m < 0.0:
            if not clamp:
                return None
            target_m = 0.0
        self._ensure_chainage()
        if not self._segs:
            return None
        if target_m > self._chain_total_m:
            # Past an unmeasurable segment the KP has no position; clamping
            # to the last measured vertex would report a wrong one.
            if clamp and not self._chain_broken:
                return (self._chain_last if self._chain_last is not None
                        else self._chain_first)
            return None
        index = bisect.bisect_left(self._seg_end_m, target_m)
        if index >= len(self._segs):
            index = len(self._segs) - 1
        p1, p2, seg_len, cum_start = self._segs[index]
        if self._follow_stored_geometry:
            return _interpolate_on_stored_segment(
                p1, p2, target_m - cum_start, seg_len)
        return _interpolate_on_segment(
            p1, p2, self._distance, target_m - cum_start, seg_len)

    def _ensure_kp_index(self) -> None:
        """Spatial index over route segments for nearest-KP queries.

        The free-function ``kp_at_point`` re-measures the whole route
        geodesically on every call — O(route vertices) per query, which made
        contour profiles and risk scans quadratic. One segment index turns
        each query into a k-nearest lookup plus a handful of exact
        projections; KP still comes from the same ellipsoidal chainage.

        The index stores segment geometries so ``nearestNeighbor`` ranks by
        true (planar) segment distance. A bounding-box index ranked long
        diagonal legs whose box contains the query first, and with enough
        of them the truly nearest segment fell outside the 12 candidates.
        """
        if self._kp_index is not None:
            return
        self._ensure_chainage()
        with self._chain_lock:
            if self._kp_index is not None:
                return
            from qgis.core import QgsFeature

            index = stored_geometry_index()
            for seg_id, (p1, p2, _len, _cum) in enumerate(self._segs):
                feat = QgsFeature()
                feat.setId(seg_id)
                feat.setGeometry(QgsGeometry.fromPolylineXY(
                    [QgsPointXY(p1), QgsPointXY(p2)]))
                index.addFeature(feat)
            # Metric-scale cells for exact nearest searches: 1/256 of the
            # route extent (the scale changes little across one).
            xs = [float(p.x()) for seg in self._segs for p in seg[:2]]
            ys = [float(p.y()) for seg in self._segs for p in seg[:2]]
            span = max(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
            self._scale_cell = span / 256.0 if span > 0 else 1.0
            self._scale_cache = {}
            self._kp_index = index

    def build_indexes(self) -> "RouteFrame":
        """Build the chainage and segment indexes now instead of on the
        first query (e.g. before interactive use, so the first mouse move
        does not pay for a whole-route walk). Returns ``self``."""
        self._ensure_kp_index()
        return self

    def kp_at_point(self, point_xy: QgsPointXY) -> KPHit:
        """Nearest KP on the route (indexed; same chainage as point_at_kp).

        Candidate segments come from the spatial index; each candidate gets
        the exact planar projection and a geodesic ``measureLine`` DCC, and
        the geodesically closest wins — the same snapped point and partial
        chainage (planar fraction × ellipsoidal segment length) the walking
        implementation produced, without walking every vertex per call.

        The search is exact under the measured metric: the index ranks by
        planar distance, but the DCC is measured (geodesic on a geographic
        CRS, where a degree of longitude is short at high latitude), so a
        planar-farther segment can be nearer. When the planar candidates do
        not already settle it, every segment within the planar radius that
        could still be nearer (best distance / the smallest local metres per
        map unit) is examined too.
        """
        if point_xy is None:
            return KPHit(0.0, float("inf"), None, -1)
        self._ensure_kp_index()
        if not self._segs:
            return KPHit(0.0, float("inf"), None, -1)
        query = QgsPointXY(point_xy)
        qx, qy = float(query.x()), float(query.y())
        total = len(self._segs)
        try:
            candidate_ids = self._kp_index.nearestNeighbor(query, 12)
        except Exception:  # noqa: BLE001 - the walk below is exact
            candidate_ids = []
        if not candidate_ids:
            return self._offset_hit(kp_at_point(self._geoms, point_xy, self._distance))
        # best = [dist, kp_m, snapped, feature]
        best = [float("inf"), 0.0, None, -1]

        def consider(seg_id, planar_limit=None):
            """Planar distance to segment ``seg_id``; measure it if it can win."""
            if seg_id < 0 or seg_id >= total:
                return 0.0
            p1, p2, seg_len, cum_start = self._segs[seg_id]
            x1, y1 = float(p1.x()), float(p1.y())
            dx, dy = float(p2.x()) - x1, float(p2.y()) - y1
            planar_sq = dx * dx + dy * dy
            if planar_sq <= 0.0:
                return 0.0
            t = max(0.0, min(1.0, ((qx - x1) * dx + (qy - y1) * dy) / planar_sq))
            sx, sy = x1 + t * dx, y1 + t * dy
            planar = math.hypot(qx - sx, qy - sy)
            if planar_limit is not None and planar > planar_limit:
                return planar
            snapped = QgsPointXY(sx, sy)
            try:
                dist = float(self._distance.measureLine(query, snapped))
            except Exception:  # noqa: BLE001 - unmeasurable candidate
                return planar
            if dist < best[0]:
                feature = self._seg_feature[seg_id] if seg_id < len(self._seg_feature) else -1
                best[:] = [dist, cum_start + t * seg_len, snapped, feature]
            return planar

        farthest_planar = max(consider(seg_id) for seg_id in candidate_ids)
        if best[2] is not None and len(candidate_ids) < total:
            scale = self._local_metres_per_unit(query)
            if scale is not None and scale * farthest_planar < best[0]:
                # Any segment measured nearer than the best so far lies
                # within this planar radius: examine all of them once.
                radius = best[0] / scale
                seen = set(candidate_ids)
                rect = QgsRectangle(qx - radius, qy - radius, qx + radius, qy + radius)
                for seg_id in self._kp_index.intersects(rect):
                    if seg_id not in seen:
                        consider(seg_id, radius)
        if best[2] is None:
            return self._offset_hit(kp_at_point(self._geoms, point_xy, self._distance))
        return KPHit(self._start_kp_km + best[1] / 1000.0, best[0], best[2], best[3])

    def _local_metres_per_unit(self, query: QgsPointXY) -> Optional[float]:
        """Smallest measured metres per map unit near ``query`` (eight
        directions, 10 % margin), cached per small cell of the route extent
        so repeated queries (mouse moves, scans along the route) reuse it.
        ``None`` when it cannot be measured (the planar candidates stand)."""
        cell = self._scale_cell
        key = (math.floor(query.x() / cell), math.floor(query.y() / cell))
        if key in self._scale_cache:
            return self._scale_cache[key]
        centre = QgsPointXY((key[0] + 0.5) * cell, (key[1] + 0.5) * cell)
        scales = []
        for i in range(8):
            angle = math.pi * i / 4.0
            probe = QgsPointXY(centre.x() + cell * math.cos(angle),
                               centre.y() + cell * math.sin(angle))
            try:
                metres = float(self._distance.measureLine(centre, probe))
            except Exception:  # noqa: BLE001 - e.g. probe beyond the CRS bounds
                metres = float("nan")
            if not (math.isfinite(metres) and metres > 0.0):
                scales = []
                break
            scales.append(metres / cell)
        scale = 0.9 * min(scales) if scales else None
        if len(self._scale_cache) > 4096:
            self._scale_cache.clear()
        self._scale_cache[key] = scale
        return scale

    def _offset_hit(self, hit: KPHit) -> KPHit:
        if not self._start_kp_km:
            return hit
        return KPHit(hit.kp_km + self._start_kp_km, hit.dcc_m, hit.snapped_xy,
                     hit.feature_index)

    def extract_segment(self, start_kp_km: float, end_kp_km: float) -> Optional[QgsGeometry]:
        """Extract a sub-line between two KPs across the whole route.

        Uses the same per-segment chainage index as ``point_at_kp`` (built
        once, then a bisect per call) instead of re-walking every vertex with
        a geodesic measurement on each call — highlighting a section near the
        end of a long dense route used to cost a full-route walk, twice per
        double-click. Returns a single LineString, or ``None`` if the range
        is invalid or fully outside the route.
        """

        try:
            s = float(start_kp_km)
            e = float(end_kp_km)
        except Exception:
            return None
        if not (math.isfinite(s) and math.isfinite(e)):
            return None
        if s == e:
            return None
        if s > e:
            s, e = e, s

        start_m = (s - self._start_kp_km) * 1000.0
        end_m = (e - self._start_kp_km) * 1000.0
        if end_m <= 0 or start_m >= self._total_m:
            return None
        self._ensure_chainage()
        segs = self._segs
        if not segs:
            return None
        start_m = max(0.0, start_m)
        end_m = min(self._total_m, self._chain_total_m, end_m)
        if end_m <= start_m:
            return None

        seg_end = self._seg_end_m
        first = bisect.bisect_left(seg_end, start_m)
        last = bisect.bisect_left(seg_end, end_m)
        first = min(first, len(segs) - 1)
        last = min(last, len(segs) - 1)

        def interp(p1, p2, along_m, seg_len):
            point = (_interpolate_on_stored_segment(p1, p2, along_m, seg_len)
                     if self._follow_stored_geometry else
                     _interpolate_on_segment(p1, p2, self._distance,
                                             along_m, seg_len))
            try:
                return p1.__class__(point.x(), point.y())
            except Exception:
                return type(p1)(point.x(), point.y())

        points: List = []
        p1, p2, seg_len, cum_start = segs[first]
        points.append(interp(p1, p2, start_m - cum_start, seg_len))
        last_vertex = p2
        if first == last:
            points.append(interp(p1, p2, end_m - cum_start, seg_len))
        else:
            points.append(p2)
            for index in range(first + 1, last):
                p1, p2, _seg_len, _cum = segs[index]
                if (abs(float(p1.x()) - float(last_vertex.x())) >= 1e-12
                        or abs(float(p1.y()) - float(last_vertex.y())) >= 1e-12):
                    # Discontinuity between features/parts: keep the jump
                    # vertex so the slice mirrors the stored route.
                    points.append(p1)
                points.append(p2)
                last_vertex = p2
            p1, p2, seg_len, cum_start = segs[last]
            if (abs(float(p1.x()) - float(last_vertex.x())) >= 1e-12
                    or abs(float(p1.y()) - float(last_vertex.y())) >= 1e-12):
                points.append(p1)
            points.append(interp(p1, p2, end_m - cum_start, seg_len))

        if len(points) < 2:
            return None
        try:
            return QgsGeometry.fromPolyline(points)
        except Exception:
            try:
                return QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in points])
            except Exception:
                return None
