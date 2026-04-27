"""Shared utilities for working with KP ranges.

KPs are in km.

These helpers are used by both Processing algorithms and UI tools
(e.g. SLD) to avoid drift in geometry extraction.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Union

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsGeometry,
    QgsProject,
)


def make_distance_area(
    source_crs: QgsCoordinateReferenceSystem,
    transform_context: Optional[QgsCoordinateTransformContext] = None,
    mode: str = "ellipsoidal",
    project: Optional["QgsProject"] = None,
) -> QgsDistanceArea:
    """Build a configured QgsDistanceArea.

    Centralises the previously-duplicated setup so every plugin tool measures
    distance the same way.

    Parameters
    ----------
    source_crs:
        CRS of the geometries that will be measured. Use the *layer* CRS, not
        the project CRS, unless the geometries have been transformed.
    transform_context:
        Project transform context. If omitted, a default one is used.
    mode:
        ``"ellipsoidal"`` (default) — measurements are geodesic on the project
        ellipsoid, falling back to WGS84 when the project ellipsoid is unset.
        ``"cartesian"`` — planar measurements in the source CRS units. Only
        meaningful when ``source_crs`` is projected; raises ``ValueError`` for
        a geographic CRS.
    project:
        Project to read the ellipsoid from. Defaults to ``QgsProject.instance()``.
    """

    if mode not in ("ellipsoidal", "cartesian"):
        raise ValueError(f"Unknown distance mode: {mode!r}")

    if mode == "cartesian" and source_crs is not None and source_crs.isGeographic():
        raise ValueError(
            "Cartesian distance mode requires a projected CRS; "
            f"'{source_crs.authid() or source_crs.description()}' is geographic."
        )

    if transform_context is None:
        transform_context = QgsCoordinateTransformContext()

    distance_area = QgsDistanceArea()
    if source_crs is not None:
        distance_area.setSourceCrs(source_crs, transform_context)

    if mode == "ellipsoidal":
        if project is None:
            project = QgsProject.instance()
        ellipsoid = (project.ellipsoid() if project is not None else "") or "WGS84"
        distance_area.setEllipsoid(ellipsoid)
    # In cartesian mode we deliberately leave the ellipsoid unset so
    # measurements stay planar in the source CRS units.

    return distance_area


# Shared distance-mode parameter helpers for KP-emitting processing algorithms.
DISTANCE_MODE_PARAM = "DISTANCE_MODE"
DISTANCE_MODE_OPTIONS = (
    "Ellipsoidal (geodesic, recommended)",
    "Cartesian (planar, projected CRS only)",
)
DISTANCE_MODE_VALUES = ("ellipsoidal", "cartesian")


def add_distance_mode_parameter(algorithm, name: str = DISTANCE_MODE_PARAM):
    """Add a standard Distance mode enum parameter to a processing algorithm.

    Default is Ellipsoidal so existing behaviour is preserved.
    """

    from qgis.core import QgsProcessingParameterEnum

    param = QgsProcessingParameterEnum(
        name,
        algorithm.tr("Distance mode"),
        options=list(DISTANCE_MODE_OPTIONS),
        defaultValue=0,
        optional=False,
    )
    algorithm.addParameter(param)


def read_distance_mode(
    algorithm, parameters, context, name: str = DISTANCE_MODE_PARAM
) -> str:
    """Return the distance mode string ('ellipsoidal' or 'cartesian')."""

    idx = algorithm.parameterAsEnum(parameters, name, context)
    try:
        return DISTANCE_MODE_VALUES[idx]
    except IndexError:
        return DISTANCE_MODE_VALUES[0]


def _as_parts(line_geometry: QgsGeometry):
    """Return a list of polyline parts.

    Each part is a sequence of points (QgsPointXY/QgsPoint).
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


def measure_total_length_m(line_geometry: QgsGeometry, distance: QgsDistanceArea) -> float:
    parts = _as_parts(line_geometry)
    total = 0.0
    for part in parts:
        for i in range(len(part) - 1):
            total += float(distance.measureLine(part[i], part[i + 1]))
    return float(total)


def extract_line_segment(
    line_geometry: QgsGeometry,
    start_kp_km: float,
    end_kp_km: float,
    distance: QgsDistanceArea,
) -> Optional[QgsGeometry]:
    """Extract a line segment between start and end KP along a (multi)polyline.

    Returns a LineString QgsGeometry in the same CRS as the input geometry.
    If extraction fails or the range is invalid/outside the line, returns None.
    """

    try:
        start_kp_km = float(start_kp_km)
        end_kp_km = float(end_kp_km)
    except Exception:
        return None

    if start_kp_km == end_kp_km:
        return None

    # Normalize ordering
    if start_kp_km > end_kp_km:
        start_kp_km, end_kp_km = end_kp_km, start_kp_km

    start_m = start_kp_km * 1000.0
    end_m = end_kp_km * 1000.0
    if start_m < 0 or end_m < 0:
        return None

    parts = _as_parts(line_geometry)
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
                ratio = (start_m - cumulative) / seg_len
                x = p1.x() + ratio * (p2.x() - p1.x())
                y = p1.y() + ratio * (p2.y() - p1.y())
                try:
                    segment_points.append(p1.__class__(x, y))
                except Exception:
                    # Fallback to QgsPointXY-like constructor
                    segment_points.append(type(p1)(x, y))
                started = True

            if started:
                if next_cum <= end_m:
                    segment_points.append(p2)
                else:
                    ratio = (end_m - cumulative) / seg_len
                    x = p1.x() + ratio * (p2.x() - p1.x())
                    y = p1.y() + ratio * (p2.y() - p1.y())
                    try:
                        segment_points.append(p1.__class__(x, y))
                    except Exception:
                        segment_points.append(type(p1)(x, y))
                    try:
                        return QgsGeometry.fromPolyline(segment_points)
                    except Exception:
                        try:
                            return QgsGeometry.fromPolylineXY(segment_points)
                        except Exception:
                            return None

            cumulative = next_cum

    # If we got here, end wasn't reached. Only return if we started.
    if not started or len(segment_points) < 2:
        return None

    try:
        return QgsGeometry.fromPolyline(segment_points)
    except Exception:
        try:
            return QgsGeometry.fromPolylineXY(segment_points)
        except Exception:
            return None
