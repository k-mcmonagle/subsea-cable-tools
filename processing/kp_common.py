# -*- coding: utf-8 -*-
"""Shared KP handling for the KP Processing algorithms (see docs/KP_METHOD.md).

* A **Start KP** parameter (default 0): the KP at the route's first vertex.
  Processing algorithms never read it from the data; the user states it.
* One route model: :class:`kp_geo_utils.RouteFrame` (KP path between
  vertices, nearest points, slices), built with the start KP.
* The KP basis written to the log: the distance model, the start KP, and for
  grid KP the grid's validity (warnings when the route leaves its area of use).
"""

from __future__ import annotations

import math

from qgis.core import QgsProcessingException, QgsProcessingParameterNumber

from ..kp_geo_utils import AntimeridianError, RouteFrame
from ..qgis_compat import PROCESSING_NUMBER_DOUBLE

START_KP_PARAM = "START_KP"
START_KP_HELP = (
    "<b>Start KP (km)</b> is the KP at the route's first vertex: 0 for most routes. Set it when the route's KPs "
    "start elsewhere (for example an RPL that starts at KP 12.345). Every KP the tool reads or writes is "
    "start KP + chainage along the route. KP follows the plugin's KP method (docs/KP_METHOD.md): geodesic "
    "on WGS84 by default, between vertices along the geodesic; the log states the basis used.")


def add_start_kp_parameter(algorithm, name: str = START_KP_PARAM) -> None:
    """Add the standard optional Start KP (km) parameter (default 0)."""
    param = QgsProcessingParameterNumber(
        name, algorithm.tr("Start KP (km) — the KP at the route's first vertex"),
        type=PROCESSING_NUMBER_DOUBLE,
        defaultValue=0.0, optional=True)
    algorithm.addParameter(param)


def read_start_kp(algorithm, parameters, context, name: str = START_KP_PARAM) -> float:
    """The Start KP parameter in km (0 when absent or not a number)."""
    if parameters.get(name) in (None, ""):
        return 0.0
    try:
        value = float(algorithm.parameterAsDouble(parameters, name, context))
    except Exception:  # noqa: BLE001
        return 0.0
    return value if math.isfinite(value) else 0.0


def build_route(geoms, distance, start_kp_km: float = 0.0, **kwargs) -> RouteFrame:
    """A RouteFrame over ``geoms`` with the start KP; a route the KP tools cannot
    handle (across +/-180 deg) stops the algorithm with the reason."""
    try:
        return RouteFrame.from_source(geoms, distance, start_kp_km=start_kp_km, **kwargs)
    except AntimeridianError as exc:
        raise QgsProcessingException(str(exc)) from exc


def report_kp_basis(feedback, route: RouteFrame, start_kp_km: float) -> None:
    """Write the KP basis to the log: model, start KP, and grid validity."""
    if feedback is None or route is None:
        return
    start = f"Start KP {start_kp_km:.3f} km"
    if start_kp_km:
        start += " (not 0: KPs are start KP + chainage; check it is the KP of the route's first vertex)"
    feedback.pushInfo(start + ".")
    try:
        notes = route.kp_basis_notes()
    except Exception:  # noqa: BLE001 - a report, never a failure
        return
    for level, text in notes:
        if level == "warn":
            feedback.pushWarning(text)
        else:
            feedback.pushInfo(text)
