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
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, NamedTuple, Optional, Sequence, Union

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeatureSource,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)


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
        except Exception:
            return []

    try:
        return [line_geometry.asPolyline()]
    except Exception:
        return []


def _normalise_geoms(geoms) -> List[QgsGeometry]:
    """Accept a single geometry, an iterable, or a feature source."""

    if geoms is None:
        return []
    if isinstance(geoms, QgsGeometry):
        return [geoms]
    if isinstance(geoms, QgsFeatureSource):
        out: List[QgsGeometry] = []
        for feat in geoms.getFeatures():
            g = feat.geometry()
            if g is not None and not g.isEmpty():
                out.append(QgsGeometry(g))
        return out
    out2: List[QgsGeometry] = []
    for g in geoms:
        if g is not None and not g.isEmpty():
            out2.append(g)
    return out2


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
        except Exception:
            pass

    ratio = (target_dist_m / seg_len_m) if seg_len_m > 0 else 0.0
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
    """

    try:
        target_m = float(kp_km) * 1000.0
    except Exception:
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
                try:
                    seg_len = float(distance.measureLine(p1, p2))
                except Exception:
                    continue
                if seg_len <= 0:
                    continue

                next_cum = cumulative + seg_len
                last_point = QgsPointXY(p2)

                if target_m <= next_cum:
                    return _interpolate_on_segment(
                        p1, p2, distance, target_m - cumulative, seg_len
                    )

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
                interp = _interpolate_on_segment(
                    p1, p2, distance, start_m - cumulative, seg_len
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
                    interp = _interpolate_on_segment(
                        p1, p2, distance, end_m - cumulative, seg_len
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
) -> Iterator[QgsGeometry]:
    """Yield copies of ``geoms`` reprojected from ``source_crs`` to ``target_crs``.

    Geometries are copied before transformation (the input is not mutated).
    When the two CRSes are equal, geometries are yielded unchanged (still as
    copies). Geometries that fail to transform are skipped silently — caller
    is responsible for any user feedback.
    """

    if project is None:
        project = QgsProject.instance()

    if source_crs == target_crs:
        for g in geoms:
            if g is not None and not g.isEmpty():
                yield QgsGeometry(g)
        return

    xform = QgsCoordinateTransform(source_crs, target_crs, project)
    for g in geoms:
        if g is None or g.isEmpty():
            continue
        copy = QgsGeometry(g)
        try:
            copy.transform(xform)
        except Exception:
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

    # ----- builders -----

    @classmethod
    def from_source(
        cls,
        source,
        distance: QgsDistanceArea,
        target_crs: Optional[QgsCoordinateReferenceSystem] = None,
        source_crs: Optional[QgsCoordinateReferenceSystem] = None,
        project: Optional[QgsProject] = None,
    ) -> "RouteFrame":
        """Build a ``RouteFrame`` from a feature source or iterable of geometries.

        When ``target_crs`` is given and differs from ``source_crs`` (inferred
        from the source when possible), geometries are reprojected up front.
        """

        # Resolve source CRS for reprojection, if any.
        if source_crs is None and isinstance(source, QgsFeatureSource):
            try:
                source_crs = source.sourceCrs()
            except Exception:
                source_crs = None

        raw_geoms = _normalise_geoms(source)

        if target_crs is not None and source_crs is not None and source_crs != target_crs:
            geoms = list(reproject_geoms_to(raw_geoms, source_crs, target_crs, project))
        else:
            geoms = raw_geoms

        lengths = [measure_total_length_m(g, distance) for g in geoms]
        return cls(geoms, lengths, distance)

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
    def feature_offsets_m(self) -> List[float]:
        return list(self._offsets_m)

    # ----- queries -----

    def point_at_kp(self, kp_km: float, *, clamp: bool = False) -> Optional[QgsPointXY]:
        return point_at_kp(self._geoms, kp_km, self._distance, clamp=clamp)

    def kp_at_point(self, point_xy: QgsPointXY) -> KPHit:
        return kp_at_point(self._geoms, point_xy, self._distance)

    def extract_segment(self, start_kp_km: float, end_kp_km: float) -> Optional[QgsGeometry]:
        """Extract a sub-line between two KPs across the whole route.

        Walks features in order, slicing the first feature at ``start_kp`` and
        the last feature at ``end_kp``, returning a single LineString. Returns
        ``None`` if the range is invalid or fully outside the route.
        """

        try:
            s = float(start_kp_km)
            e = float(end_kp_km)
        except Exception:
            return None
        if s == e:
            return None
        if s > e:
            s, e = e, s

        start_m = s * 1000.0
        end_m = e * 1000.0
        if end_m <= 0 or start_m >= self._total_m:
            return None
        start_m = max(0.0, start_m)
        end_m = min(self._total_m, end_m)

        # Single-feature fast path.
        if len(self._geoms) == 1:
            return extract_line_segment(self._geoms[0], start_m / 1000.0, end_m / 1000.0, self._distance)

        # Multi-feature: collect points across affected features.
        points: List = []
        for idx, geom in enumerate(self._geoms):
            f_start = self._offsets_m[idx]
            f_end = f_start + self._feature_lengths_m[idx]
            if f_end <= start_m or f_start >= end_m:
                continue
            local_start_km = max(0.0, start_m - f_start) / 1000.0
            local_end_km = min(self._feature_lengths_m[idx], end_m - f_start) / 1000.0
            sub = extract_line_segment(geom, local_start_km, local_end_km, self._distance)
            if sub is None or sub.isEmpty():
                continue
            for part in iter_line_parts(sub):
                if not part:
                    continue
                if points and part:
                    # Avoid duplicating the join vertex when consecutive
                    # features share an endpoint.
                    last = points[-1]
                    first = part[0]
                    if abs(float(last.x()) - float(first.x())) < 1e-12 and abs(float(last.y()) - float(first.y())) < 1e-12:
                        points.extend(part[1:])
                        continue
                points.extend(part)

        if len(points) < 2:
            return None

        try:
            return QgsGeometry.fromPolyline(points)
        except Exception:
            try:
                return QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in points])
            except Exception:
                return None
