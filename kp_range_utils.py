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


# ---------------------------------------------------------------------------
# Back-compat re-exports.
#
# These geometry helpers were moved to ``kp_geo_utils`` in 1.6 as part of
# consolidating the plugin's linear-referencing primitives. They remain
# importable from this module so existing call sites and any external scripts
# referring to the old paths keep working.
# ---------------------------------------------------------------------------

from .kp_geo_utils import (  # noqa: E402,F401
    iter_line_parts as _as_parts,
    measure_total_length_m,
    extract_line_segment,
)
