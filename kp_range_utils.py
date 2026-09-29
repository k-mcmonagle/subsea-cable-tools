"""Shared utilities for working with KP ranges.

KPs are in km.

These helpers are used by both Processing algorithms and UI tools
(e.g. SLD) to avoid drift in geometry extraction.
"""

from __future__ import annotations

import math
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
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
        ``"ellipsoidal"`` (default) — measurements are geodesic on the
        **WGS84** ellipsoid, always. KP is the plugin's shared reference
        frame (RPLs, the Workbench, the Burial Planner and every KP tool),
        so it must not change with a project measurement setting.
        ``"cartesian"`` — planar measurements in the source CRS units. Only
        meaningful when ``source_crs`` is projected; raises ``ValueError`` for
        a geographic CRS.
    project:
        Accepted for API compatibility; the ellipsoid is always WGS84.
    """

    if mode not in ("ellipsoidal", "cartesian"):
        raise ValueError(f"Unknown distance mode: {mode!r}")

    if mode == "cartesian":
        # Grid distances: the KP-settings grid CRS when one is chosen, else
        # the source CRS when projected, else the project CRS / UTM zone.
        # (Geographic sources used to raise here.)
        fixed = resolve_grid_crs(kp_grid_crs_setting(), None) if kp_grid_crs_setting() else None
        if fixed is not None and source_crs is not None and fixed != source_crs:
            return GridDistanceArea(source_crs, transform_context, fixed)
        if source_crs is not None and source_crs.isGeographic():
            return GridDistanceArea(source_crs, transform_context, resolve_grid_crs("", project))

    if transform_context is None:
        transform_context = QgsCoordinateTransformContext()

    distance_area = QgsDistanceArea()
    if source_crs is not None:
        distance_area.setSourceCrs(source_crs, transform_context)

    if mode == "ellipsoidal":
        # One KP definition plugin-wide: geodesic on WGS84, the RPL / survey
        # convention the Workbench already fixes. Honouring the project
        # ellipsoid made the same route measure differently per project
        # (and "NONE" meant planar degrees). Planar measurement remains
        # available via the explicit cartesian mode.
        distance_area.setEllipsoid("WGS84")
    # In cartesian mode we deliberately leave the ellipsoid unset so
    # measurements stay planar in the source CRS units.

    return distance_area


# ---------------------------------------------------------------------------
# Plugin-wide KP distance setting
# ---------------------------------------------------------------------------
#
# KP is geodesic (WGS84) by default. Some RPLs are chained on the map
# projection instead, so the plugin offers one switch — Cartesian (grid) —
# honoured by every KP-producing tool. Grid distances are planar in the
# *grid CRS*: the chosen projected CRS, else the project CRS when it is
# projected, else the UTM zone at the start of the route being measured.

KP_SETTINGS_GROUP = "SubseaCableTools/KP"
KP_MODE_GEODESIC = "ellipsoidal"
KP_MODE_CARTESIAN = "cartesian"


def _settings():
    from qgis.PyQt.QtCore import QSettings
    return QSettings()


def kp_distance_mode() -> str:
    """The plugin-wide KP distance mode (``ellipsoidal`` default)."""
    try:
        value = str(_settings().value(f"{KP_SETTINGS_GROUP}/distance_mode", "") or "")
    except Exception:
        value = ""
    if not value:
        # First use: carry over the KP Mouse Tool's old per-tool option.
        try:
            from qgis.PyQt.QtCore import QSettings
            legacy = QSettings("SubseaCableTools", "KPMouseTool").value(
                "useCartesian", False, type=bool)
        except Exception:
            legacy = False
        return KP_MODE_CARTESIAN if legacy else KP_MODE_GEODESIC
    return KP_MODE_CARTESIAN if value == KP_MODE_CARTESIAN else KP_MODE_GEODESIC


def kp_grid_crs_setting() -> str:
    """Grid CRS authid for Cartesian KP ("" = project CRS if projected, else UTM)."""
    try:
        return str(_settings().value(f"{KP_SETTINGS_GROUP}/grid_crs", "") or "")
    except Exception:
        return ""


def set_kp_distance_settings(mode: str, grid_crs: str = "") -> None:
    settings = _settings()
    settings.setValue(f"{KP_SETTINGS_GROUP}/distance_mode",
                      KP_MODE_CARTESIAN if mode == KP_MODE_CARTESIAN else KP_MODE_GEODESIC)
    settings.setValue(f"{KP_SETTINGS_GROUP}/grid_crs", grid_crs or "")


def resolve_grid_crs(grid_crs: str = "", project=None):
    """The fixed grid CRS for Cartesian KP, or None (auto UTM per route)."""
    if grid_crs:
        crs = QgsCoordinateReferenceSystem(grid_crs)
        if crs.isValid() and not crs.isGeographic():
            return crs
    try:
        from qgis.core import QgsProject
        project = project or QgsProject.instance()
        crs = project.crs()
        if crs.isValid() and not crs.isGeographic():
            return crs
    except Exception:
        pass
    return None


def describe_kp_mode(mode: Optional[str] = None, grid_crs: Optional[str] = None) -> str:
    """Short human description, e.g. 'Geodesic (WGS84)' / 'Cartesian (EPSG:32630)'."""
    mode = mode or kp_distance_mode()
    if mode != KP_MODE_CARTESIAN:
        return "Geodesic (WGS84)"
    crs = resolve_grid_crs(kp_grid_crs_setting() if grid_crs is None else grid_crs)
    if crs is None:
        return "Cartesian (grid, UTM zone of the route)"
    return f"Cartesian (grid, {crs.authid() or crs.description()})"


class GridDistanceArea(QgsDistanceArea):
    """Planar (grid) distances in a projected CRS for geometries in any CRS.

    Behaves like a ``QgsDistanceArea`` for the plugin's KP code: only
    ``measureLine`` / ``measureLength`` are planar in the grid CRS; bearings
    and spheroid projections stay on WGS84. With no fixed grid CRS the UTM
    zone is chosen from the first point measured (the route start).
    """

    def __init__(self, source_crs, transform_context=None, grid_crs=None):
        super().__init__()
        if transform_context is None:
            transform_context = QgsCoordinateTransformContext()
        self._ctx = transform_context
        self._source = source_crs
        self._grid = grid_crs
        self._xform = None
        if source_crs is not None:
            self.setSourceCrs(source_crs, transform_context)
        self.setEllipsoid("WGS84")

    # -- identity --------------------------------------------------------
    def is_grid(self) -> bool:
        return True

    def grid_crs(self):
        return self._grid

    def clone(self) -> "GridDistanceArea":
        other = GridDistanceArea(self._source, self._ctx, self._grid)
        other._xform = self._xform
        return other

    # -- measuring -------------------------------------------------------
    def _transform(self, point):
        from qgis.core import QgsCoordinateTransform, QgsPointXY
        p = QgsPointXY(point)
        if self._xform is None:
            if self._grid is None:
                from .kp_datum import utm_epsg_for
                wgs = QgsCoordinateReferenceSystem("EPSG:4326")
                to_wgs = QgsCoordinateTransform(self._source, wgs, self._ctx)
                ll = to_wgs.transform(p) if self._source != wgs else p
                self._grid = QgsCoordinateReferenceSystem(f"EPSG:{utm_epsg_for(ll.x(), ll.y())}")
            self._xform = QgsCoordinateTransform(self._source, self._grid, self._ctx)
        if self._source == self._grid:
            return p
        return self._xform.transform(p)

    def measureLine(self, *args):  # noqa: N802 (Qt API name)
        if len(args) == 2:
            a, b = self._transform(args[0]), self._transform(args[1])
            return math.hypot(b.x() - a.x(), b.y() - a.y())
        points = [self._transform(p) for p in (args[0] if args else [])]
        return sum(math.hypot(q.x() - p.x(), q.y() - p.y())
                   for p, q in zip(points, points[1:]))

    def measureLength(self, geometry):  # noqa: N802
        return sum(self.measureLine(list(part)) for part in iter_line_parts(geometry))


def is_grid_distance(distance) -> bool:
    return isinstance(distance, GridDistanceArea)


def clone_distance_area(distance):
    """Worker-owned copy of a distance area, preserving grid mode."""
    if isinstance(distance, GridDistanceArea):
        return distance.clone()
    try:
        return QgsDistanceArea(distance)
    except Exception:
        return distance


def make_kp_distance_area(
    source_crs: QgsCoordinateReferenceSystem,
    transform_context: Optional[QgsCoordinateTransformContext] = None,
    project: Optional["QgsProject"] = None,
    mode: Optional[str] = None,
    grid_crs: Optional[str] = None,
) -> QgsDistanceArea:
    """Distance area for **KP chainage**, honouring the plugin KP setting.

    ``mode`` / ``grid_crs`` override the plugin setting (a Burial Planner
    plan keeps its own so its KPs never change behind its back).
    """
    mode = mode or kp_distance_mode()
    if mode != KP_MODE_CARTESIAN:
        return make_distance_area(source_crs, transform_context, project=project)
    grid = resolve_grid_crs(kp_grid_crs_setting() if grid_crs is None else grid_crs, project)
    if grid is not None and source_crs is not None and grid == source_crs:
        return make_distance_area(source_crs, transform_context, mode=KP_MODE_CARTESIAN)
    return GridDistanceArea(source_crs, transform_context, grid)


# Shared distance-mode parameter helpers for KP-emitting processing algorithms.
DISTANCE_MODE_PARAM = "DISTANCE_MODE"
DISTANCE_MODE_OPTIONS = (
    "Geodesic (WGS84 ellipsoid)",
    "Cartesian (grid: KP-settings CRS, projected layer CRS, or UTM zone)",
)
DISTANCE_MODE_VALUES = ("ellipsoidal", "cartesian")


def add_distance_mode_parameter(algorithm, name: str = DISTANCE_MODE_PARAM):
    """Add a standard Distance mode enum parameter to a processing algorithm.

    The default follows the plugin-wide KP setting (Geodesic unless the
    user chose Cartesian in KP settings).
    """

    from qgis.core import QgsProcessingParameterEnum

    param = QgsProcessingParameterEnum(
        name,
        algorithm.tr("Distance mode"),
        options=list(DISTANCE_MODE_OPTIONS),
        defaultValue=1 if kp_distance_mode() == KP_MODE_CARTESIAN else 0,
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
    iter_line_parts,
    measure_total_length_m,
    extract_line_segment,
)
