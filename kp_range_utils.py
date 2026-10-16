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
        ``"cartesian"`` — planar (grid) measurements in one projected CRS, chosen
        by :func:`resolve_kp_grid`; a geographic source is measured in that
        grid (:class:`GridDistanceArea`).
    project:
        Accepted for API compatibility; the ellipsoid is always WGS84.
    """

    if mode not in ("ellipsoidal", "cartesian"):
        raise ValueError(f"Unknown distance mode: {mode!r}")

    if mode == "cartesian":
        # Grid distances in one grid CRS, chosen by :func:`resolve_kp_grid`
        # (KP settings, else the source CRS when projected, else the project
        # CRS when projected, else the UTM zone at the route start).
        grid, how = resolve_kp_grid(kp_grid_crs_setting(), source_crs, project)
        if grid is None or source_crs is None or grid != source_crs:
            return GridDistanceArea(source_crs, transform_context, grid, how)

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
# KP is geodesic (WGS84) by default. Some RPLs are chained on a map
# projection instead, so the plugin offers one switch — Cartesian (grid) —
# honoured by every KP-producing tool. Grid distances are planar in one
# *grid CRS* (:func:`resolve_kp_grid`): the CRS set in KP settings, else the
# route's own CRS when projected, else the project CRS when projected, else
# the UTM zone at the route start. Grid KP is only valid inside that CRS's
# area of use; tools report where the route leaves it.

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


GRID_FROM_SETTING = "set in KP settings"
GRID_FROM_ROUTE = "the route's own CRS"
GRID_FROM_PROJECT = "the project CRS"
GRID_FROM_UTM = "the UTM zone at the route start (chosen automatically)"


def resolve_kp_grid(grid_crs: str = "", route_crs=None, project=None):
    """``(grid CRS or None, how)`` for Cartesian (grid) KP.

    One rule for every tool, most explicit first: a grid CRS given (KP
    settings or the caller), else the route's own CRS when it is projected
    (grid KP of a route drawn in a grid is chained in that grid), else the
    project CRS when projected, else ``None``: the UTM zone at the route
    start, chosen when the route is measured.
    """
    if grid_crs:
        crs = QgsCoordinateReferenceSystem(grid_crs)
        if crs.isValid() and not crs.isGeographic():
            return crs, GRID_FROM_SETTING
    if route_crs is not None and route_crs.isValid() and not route_crs.isGeographic():
        return route_crs, GRID_FROM_ROUTE
    try:
        from qgis.core import QgsProject
        project = project or QgsProject.instance()
        crs = project.crs()
        if crs.isValid() and not crs.isGeographic():
            return crs, GRID_FROM_PROJECT
    except Exception:
        pass
    return None, GRID_FROM_UTM


def resolve_grid_crs(grid_crs: str = "", project=None, route_crs=None):
    """The fixed grid CRS for Cartesian KP, or None (auto UTM per route)."""
    return resolve_kp_grid(grid_crs, route_crs, project)[0]


def describe_kp_mode(mode: Optional[str] = None, grid_crs: Optional[str] = None) -> str:
    """Short human description, e.g. 'Geodesic (WGS84)' / 'Cartesian (grid EPSG:32630)'."""
    mode = mode or kp_distance_mode()
    if mode != KP_MODE_CARTESIAN:
        return "Geodesic (WGS84)"
    grid = kp_grid_crs_setting() if grid_crs is None else grid_crs
    if grid:
        crs = QgsCoordinateReferenceSystem(grid)
        if crs.isValid() and not crs.isGeographic():
            return f"Cartesian (grid {crs.authid() or crs.description()}, valid inside its area of use)"
    project = resolve_kp_grid("", None, None)[0]
    rest = f"else the project CRS {project.authid()}" if project is not None else \
        "else the UTM zone at the route start"
    return f"Cartesian (grid: the route's own CRS when projected, {rest})"


class GridDistanceArea(QgsDistanceArea):
    """Planar (grid) distances in a projected CRS for geometries in any CRS.

    Behaves like a ``QgsDistanceArea`` for the plugin's KP code: only
    ``measureLine`` / ``measureLength`` are planar in the grid CRS; bearings
    and spheroid projections stay on WGS84. With no fixed grid CRS the UTM
    zone is chosen from the first point measured (the route start).
    """

    def __init__(self, source_crs, transform_context=None, grid_crs=None, how=""):
        super().__init__()
        if transform_context is None:
            transform_context = QgsCoordinateTransformContext()
        self._ctx = transform_context
        self._source = source_crs
        self._grid = grid_crs
        self._how = how or (GRID_FROM_UTM if grid_crs is None else "")
        self._xform = None
        self._back = None
        if source_crs is not None:
            self.setSourceCrs(source_crs, transform_context)
        self.setEllipsoid("WGS84")

    # -- identity --------------------------------------------------------
    def is_grid(self) -> bool:
        return True

    def grid_crs(self):
        return self._grid

    def grid_how(self) -> str:
        """How the grid CRS was chosen (see :func:`resolve_kp_grid`)."""
        return self._how

    def clone(self) -> "GridDistanceArea":
        other = GridDistanceArea(self._source, self._ctx, self._grid, self._how)
        other._xform = self._xform
        return other

    def to_grid(self, point):
        """``point`` (source CRS) in grid coordinates."""
        return self._transform(point)

    def from_grid(self, point):
        """Grid coordinates back to the source CRS."""
        from qgis.core import QgsCoordinateTransform, QgsPointXY
        if self._xform is None:
            raise ValueError("The grid is chosen when the route is first measured.")
        if self._source == self._grid:
            return QgsPointXY(point)
        if self._back is None:
            self._back = QgsCoordinateTransform(self._grid, self._source, self._ctx)
        return self._back.transform(QgsPointXY(point))

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
    route_crs: Optional[QgsCoordinateReferenceSystem] = None,
) -> QgsDistanceArea:
    """Distance area for **KP chainage**, honouring the plugin KP setting.

    ``mode`` / ``grid_crs`` override the plugin setting (a Burial Planner
    plan keeps its own so its KPs never change behind its back).
    ``route_crs`` is the CRS the route is stored in when the geometries
    measured are in another (e.g. the map CRS); it takes part in the grid
    choice (:func:`resolve_kp_grid`). By default it is ``source_crs``.
    """
    mode = mode or kp_distance_mode()
    if mode != KP_MODE_CARTESIAN:
        return make_distance_area(source_crs, transform_context, project=project)
    grid, how = resolve_kp_grid(kp_grid_crs_setting() if grid_crs is None else grid_crs,
                                source_crs if route_crs is None else route_crs, project)
    if grid is not None and source_crs is not None and grid == source_crs:
        return make_distance_area(source_crs, transform_context, mode=KP_MODE_CARTESIAN)
    return GridDistanceArea(source_crs, transform_context, grid, how)


def kp_basis_text(distance) -> str:
    """The KP distance model in words, e.g. 'geodesic on the WGS84 ellipsoid' or
    'grid (Cartesian) in EPSG:32630 WGS 84 / UTM zone 30N (the project CRS)'."""
    if isinstance(distance, GridDistanceArea):
        grid = distance.grid_crs()
        name = f"{grid.authid()} {grid.description()}".strip() if grid is not None else "a UTM zone"
        return f"grid (Cartesian) in {name} ({distance.grid_how()})"
    try:
        geodesic = distance.willUseEllipsoid()
    except Exception:
        geodesic = False
    if geodesic:
        return "geodesic on the WGS84 ellipsoid"
    try:
        crs = distance.sourceCrs()
        name = f"{crs.authid()} {crs.description()}".strip()
    except Exception:
        name = "the layer CRS"
    return f"grid (Cartesian) in {name} (the route's own CRS)"


# Shared distance-mode parameter helpers for KP-emitting processing algorithms.
DISTANCE_MODE_PARAM = "DISTANCE_MODE"
DISTANCE_MODE_OPTIONS = (
    "Geodesic (WGS84 ellipsoid)",
    "Cartesian (grid: KP-settings CRS, else the route's projected CRS, else the project CRS, else UTM zone)",
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
