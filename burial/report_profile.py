"""Render a plan report from saved samples, sections, ground units and targets."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

import pyqtgraph as pg
from qgis.PyQt.QtCore import QByteArray, QBuffer, QIODevice, Qt
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget

from ..kp_axis_item import KPAxisItem
from .ground_plot import GroundModelPlot
from .profile_panels import PlanEventLane
from .profile_widget import _event_color, _LEFT_AXIS_WIDTH, _STRIP_STYLES


def saved_profile_figure(model):
    """Return report PNG bytes (if available) and its scope and depth references."""
    lo = float(model.plan.get("scope_start_kp") or 0)
    hi = float(model.plan.get("scope_end_kp") or 0)
    scope_label = "Plan scope"
    if hi <= lo:
        if model.route is None:
            return None, "A plan scope or resolved route is needed to draw the profile."
        lo, hi = model.route.start_kp_km, model.route.end_kp_km
        scope_label = "Full route (no applied scope)"
    caption = [f"{scope_label} · KP {lo:.3f}–{hi:.3f}."]
    state = model.profile_state()
    xs, depths = [], []
    if state == "current":
        profile = model.bathy_profile
        a = max(0, bisect_left(profile.kps, lo) - 1)
        b = bisect_right(profile.kps, hi) + 1
        xs = profile.kps[a:b]
        depths = [float("nan") if v is None else v for v in profile.depths[a:b]]
    bathy = any(math.isfinite(v) for v in depths)
    if bathy:
        caption.append("Bathymetry shows sampled depth in metres, positive down, on the source survey datum.")
    elif state == "stale":
        caption.append("Saved bathymetry is out of date and is omitted.")
    else:
        caption.append("No saved bathymetry in this range.")
    units = [u for u in model.ground_units if u.get("start_kp") is not None
             and u.get("end_kp") is not None and u["end_kp"] > lo and u["start_kp"] < hi]
    targets = model.target_runs(lo, hi)
    below = bool(units or any(depth is not None and depth > 0 for _, _, depth in targets))
    if below:
        caption.append("The ground profile uses metres below seabed, with the seabed at 0 m.")
    label = " ".join(caption)
    if not bathy and not below:
        return None, label

    panel = QWidget()
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    try:
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        plots = []
        if bathy:
            axis = KPAxisItem()
            axis.set_linear()
            depth_plot = pg.PlotWidget(background="w", axisItems={"bottom": axis})
            depth_plot.setFixedHeight(260)
            depth_plot.setLabel("bottom", "KP", units="km")
            depth_plot.setLabel("left", "Depth", units="m")
            depth_plot.invertY(True)
            depth_plot.showGrid(x=True, y=True, alpha=0.25)
            curve = depth_plot.plot(xs, depths, pen=pg.mkPen("#1f77b4", width=2), connect="finite")
            curve.setDownsampling(auto=True, method="peak")
            depth_plot.enableAutoRange(y=True)
            layout.addWidget(depth_plot, 1)
            plots.append(depth_plot)
        if below:
            ground = GroundModelPlot()
            ground.setFixedHeight(290)
            ground.set_scope(lo, hi)
            ground.set_units(units, model.ground_classes)
            ground.set_target_runs(targets)
            layout.addWidget(ground, 1)
            plots.append(ground.plot)
        if model.events or model.sections:
            lane = PlanEventLane(plots[0])
            lane.set_rows(model.events, model.sections, model.event_labels(), model.label_method,
                          _event_color, _STRIP_STYLES)
            if bathy:
                lane.leaders.set_cable(curve.xData, curve.yData)
            layout.insertWidget(0, lane)
            plots.append(lane)
        for plot in plots:
            plot.getAxis("left").setWidth(_LEFT_AXIS_WIDTH)
            for axis_name in ("bottom", "left"):
                plot.getAxis(axis_name).enableAutoSIPrefix(False)
            plot.hideButtons()
            plot.setXRange(lo, hi, padding=0)
        panel.resize(1100, layout.sizeHint().height())
        panel.show()
        layout.activate()
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not panel.grab().save(buffer, "PNG"):
            raise RuntimeError("Could not render the saved profile.")
        buffer.close()
        return bytes(data), label
    finally:
        panel.close()
        panel.deleteLater()
