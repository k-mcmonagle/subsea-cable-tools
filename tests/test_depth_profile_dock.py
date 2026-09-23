"""End-to-end checks for the Depth Profile dock: generate -> plot -> interact.

Earlier tests exercised the dock's slope maths but never ``generate_profile``,
so a plotting call the pyqtgraph shim did not implement (``fill_between``)
crashed every raster profile in the field. These checks drive the full
path on a synthetic planar seabed with a known answer:

    depth = 100 + 0.05 * (x - X0)   (deepening eastward, 2.862°)

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import math
import os
import tempfile
from types import SimpleNamespace
from typing import List

_X0, _Y0 = 500000.0, 6000000.0
_GRAD = 0.05


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _depth_at(x_m: float) -> float:
    return 100.0 + _GRAD * x_m


def _make_raster(path: str, pixel: float) -> None:
    import numpy as np
    from osgeo import gdal, osr
    x_min, x_max, y_min, y_max = _X0 - 200, _X0 + 1200, _Y0 - 300, _Y0 + 300
    width, height = int((x_max - x_min) / pixel), int((y_max - y_min) / pixel)
    ds = gdal.GetDriverByName("GTiff").Create(path, width, height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x_min, pixel, 0.0, y_max, 0.0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32630)
    ds.SetProjection(srs.ExportToWkt())
    centres = x_min + (np.arange(width) + 0.5) * pixel
    band = ds.GetRasterBand(1)
    band.WriteArray(np.tile(100.0 + _GRAD * (centres - _X0), (height, 1)).astype(np.float32))
    band.SetNoDataValue(-9999)
    ds = None


class _Harness:
    def __init__(self, temp: str):
        from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
                               QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QApplication, QMainWindow
        from ..bathymetry_sampling import PREFIX
        from ..depth_profile_dockwidget import DepthProfileDockWidget

        self.project = QgsProject.instance()
        self.temp = temp
        self.layers = []
        rasters = []
        for name, pixel in (("fine", 10.0), ("coarse", 20.0)):
            path = os.path.join(temp, f"{name}.tif")
            _make_raster(path, pixel)
            layer = QgsRasterLayer(path, f"dp_{name}")
            assert layer.isValid(), path
            layer.setCustomProperty(PREFIX + "vertical", "depth")
            rasters.append(layer)
        self.fine, self.coarse = rasters

        def line_layer(name, lines):
            layer = QgsVectorLayer("LineString?crs=EPSG:32630&field=name:string", name, "memory")
            features = []
            for label, pts in lines:
                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts]))
                f.setAttributes([label])
                features.append(f)
            layer.dataProvider().addFeatures(features)
            layer.updateExtents()
            return layer

        main = ("main", [(_X0, _Y0), (_X0 + 1000, _Y0)])
        self.route = line_layer("dp_route", [main])
        self.routes = line_layer("dp_routes", [main, ("spur", [(_X0, _Y0 + 100), (_X0 + 500, _Y0 + 100)])])

        self.contours = QgsVectorLayer("LineString?crs=EPSG:32630&field=depth:double", "dp_contours", "memory")
        cfeatures = []
        for k in range(-2, 23):
            x = _X0 + 50.0 * k
            f = QgsFeature(self.contours.fields())
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, _Y0 - 300), QgsPointXY(x, _Y0 + 300)]))
            f.setAttributes([_depth_at(x - _X0)])
            cfeatures.append(f)
        self.contours.dataProvider().addFeatures(cfeatures)
        self.contours.updateExtents()
        self.contours.setCustomProperty(PREFIX + "vertical", "depth")

        self.layers = [self.fine, self.coarse, self.route, self.routes, self.contours]
        self.project.addMapLayers(self.layers)
        # KP is ellipsoidal; the synthetic seabed is planar UTM. Grid metres
        # per KP metre (the UTM scale factor, ~0.9996 here).
        from ..kp_range_utils import make_distance_area
        da = make_distance_area(self.route.crs(), self.project.transformContext(), project=self.project)
        self.scale = 1000.0 / da.measureLine(QgsPointXY(_X0, _Y0), QgsPointXY(_X0 + 1000, _Y0))

        self.root = QMainWindow()
        self.root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32630"))
        self.bar = QgsMessageBar()
        self.iface = SimpleNamespace(mainWindow=lambda: self.root, mapCanvas=lambda: self.canvas,
                                     messageBar=lambda: self.bar)
        self.dock = DepthProfileDockWidget(self.iface)
        self.root.resize(1400, 900)
        self.root.show()
        self.dock.show()
        self.dock.populate_layer_combos()
        QApplication.processEvents()

    def configure(self, *, source="Raster", rasters=("fine",), variable="Depth (m)", dual=False,
                  reverse=False, selected_only=False, per_raster=True, route=None):
        from qgis.PyQt.QtCore import Qt
        d = self.dock
        d.use_drawn_chk.setChecked(False)
        d.line_layer_combo.setCurrentIndex(d.line_layer_combo.findData((route or self.route).id()))
        d.selected_only_chk.setChecked(selected_only)
        d.source_type_combo.setCurrentText(source)
        wanted = {getattr(self, name).id() for name in rasters}
        for i in range(d.raster_layer_list.count()):
            item = d.raster_layer_list.item(i)
            checked = item.data(Qt.ItemDataRole.UserRole) in wanted
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        d.plot_rasters_separately_chk.setChecked(per_raster)
        d.contour_layer_combo.setCurrentIndex(d.contour_layer_combo.findData(self.contours.id()))
        d.populate_depth_fields_1()
        d.depth_field_combo.setCurrentText("depth")
        d.contour_layer_combo2.setCurrentIndex(0)
        d.interval_spin.setValue(10)
        d.adaptive_interval_chk.setChecked(False)
        d.slope_window_spin.setValue(0)
        d.side_slope_chk.setChecked(False)
        d.variable_combo.setCurrentText(variable)
        d.dual_plot_chk.setChecked(dual)
        d.reverse_kp_chk.setChecked(reverse)
        d.invert_kp_axis_chk.setChecked(False)
        d.invert_slope_chk.setChecked(False)
        d.ve_spin.setValue(0)

    def generate(self):
        from qgis.PyQt.QtWidgets import QApplication
        self.dock.generate_profile()
        QApplication.processEvents()

    def close(self):
        from qgis.PyQt.QtWidgets import QApplication
        self.dock.close()
        self.dock.deleteLater()
        self.root.close()
        self.canvas.close()
        for layer in self.layers:
            self.project.removeMapLayer(layer.id())
        QApplication.processEvents()


def test_shim_fill_between() -> bool:
    from ..plot_widget import Figure, FigureCanvas
    figure = Figure()
    FigureCanvas(figure)
    ax = figure.add_subplot(111)
    handles = ax.fill_between([0, 1, 2, 3, 4], [1, 2, None, 4, 5], 6, color="steelblue", alpha=0.2)
    path = handles[0].item.path() if handles else None
    # The gap at index 2 splits the shading into two closed polygons.
    ok = bool(handles) and path is not None and not path.isEmpty()
    ok = ok and ax.fill_between([0, 1], [None, None], 1) == []
    return _result("plot shim fill_between (gap-aware, named colours)", ok)


def test_raster_depth_profile(h: _Harness) -> bool:
    h.configure()
    h.generate()
    d = h.dock
    depths_ok = bool(d.kp_values) and all(
        v is not None and abs(v - _depth_at(kp * 1000 * h.scale)) < 1e-3 for kp, v in zip(d.kp_values, d.depth_values))
    slopes = [v for v in d.slope_deg if v is not None]
    expected_deg = math.degrees(math.atan(_GRAD * h.scale))
    slope_ok = bool(slopes) and all(abs(v + expected_deg) < 1e-3 for v in slopes)
    expected_seabed = math.hypot(d.line_length, _GRAD * 1000.0)
    seabed_ok = abs(d.seabed_length - expected_seabed) < 0.01
    plotted = len(d.figure.get_axes()) == 1 and d._depth_axis is not None
    summary = "max |slope| 2.86°" in d.plot_status_label.text()
    return _result("raster depth profile generates and plots (field crash regression)",
                   depths_ok and slope_ok and seabed_ok and plotted and summary,
                   f"stations={len(d.kp_values)} seabed={d.seabed_length:.3f} (expected {expected_seabed:.3f})")


def test_dual_and_slope_only(h: _Harness) -> bool:
    d = h.dock
    h.configure(dual=True)
    h.generate()
    dual_ok = len(d.figure.get_axes()) == 2 and d.measure.plot_item is d.figure.get_axes()[0].plot_item
    h.configure(variable="Slope (deg)")
    h.generate()
    slope_only_ok = d._depth_axis is None and not d.measure.action.isEnabled()
    return _result("dual plot measures on depth axis; slope-only plot disables Measure",
                   dual_ok and slope_only_ok, f"dual={dual_ok} slope_only={slope_only_ok}")


def test_reverse_kp_hover(h: _Harness) -> bool:
    from ..plot_widget import PlotMouseEvent
    d = h.dock
    h.configure(reverse=True)
    h.generate()
    axis = d.figure.get_axes()[0]
    idx = d._station_for_plot_x(0.0)
    d.on_mouse_move(PlotMouseEvent("motion_notify_event", axis, 0.0, 120.0))
    centre = d.marker.center() if d.marker else None
    # Displayed KP 0 with Reverse KP is the route END: marker must be there.
    ok = idx == len(d.kp_values) - 1 and centre is not None and abs(centre.x() - (_X0 + 1000)) < 1e-6
    ok = ok and abs(d.vertical_line.get_xdata()[0]) < 1e-9
    d.on_mouse_move(PlotMouseEvent("motion_notify_event", axis, 0.25, 120.0))
    idx = d._station_for_plot_x(0.25)
    # Displayed KP 0.25 from the end is ~750 m from the start.
    ok = ok and abs(d.kp_values[idx] * 1000 - 750) <= 5.0
    ok = ok and abs(d.marker.center().x() - (_X0 + d.kp_values[idx] * 1000 * h.scale)) < 1e-6
    d.clear_plot()
    from qgis.gui import QgsVertexMarker
    leftovers = [i for i in h.canvas.scene().items() if isinstance(i, QgsVertexMarker)]
    ok = ok and d.marker is None and not leftovers
    return _result("Reverse KP hover maps crosshair and map marker to the right station; marker removed on clear",
                   ok, f"idx={idx} centre={centre.x() if centre else None} leftovers={len(leftovers)}")


def test_measurements(h: _Harness) -> bool:
    from qgis.PyQt.QtCore import QPointF, Qt
    from qgis.PyQt.QtWidgets import QApplication, QFileDialog
    d = h.dock
    h.configure()
    h.generate()
    m = d.measure
    entry = m.add_measurement((100.0, 0.0), (600.0, 0.0))
    metrics = entry["metrics"] if entry else {}
    dz = 25.0 * h.scale
    ok = bool(entry) and abs(metrics["width_m"] - 500) < 1e-6 and abs(metrics["depth_change_m"] - dz) < 1e-3
    ok = ok and abs(metrics["seabed_distance_m"] - math.hypot(500, dz)) < 1e-3
    ok = ok and m.table.rowCount() == 1 and m.table.isVisibleTo(d)

    # Real clicks through the plot scene (plot X is KP km, Y depth m).
    vb = m.plot_item.vb
    QApplication.processEvents()

    class Click:
        def __init__(self, kp, z):
            self.p = vb.mapViewToScene(QPointF(kp, z))
        def scenePos(self): return self.p
        def button(self): return Qt.MouseButton.LeftButton
        def accept(self): pass

    m.action.setChecked(True)
    m._on_click(Click(0.2, 112.0))
    m._on_move(Click(0.35, 118.0).scenePos())
    preview_ok = m.preview is not None and m.preview[0].isVisible()
    m._on_click(Click(0.4, 121.0))
    clicked = m.measurements[-1]["metrics"] if len(m.measurements) == 2 else {}
    click_ok = preview_ok and bool(clicked) and abs(clicked["width_m"] - 200) < 0.5 \
        and abs(clicked["depth_change_m"] - 10) < 0.05

    d.ve_spin.setValue(1.0)
    ve_ok = abs(vb.state["aspectLocked"] - 1000.0) < 1e-6
    d.ve_spin.setValue(0)
    ve_ok = ve_ok and vb.state["aspectLocked"] is False

    saved = QFileDialog.getSaveFileName
    csv_path = os.path.join(h.temp, "measure.csv")
    png_path = os.path.join(h.temp, "profile.png")
    try:
        QFileDialog.getSaveFileName = lambda *a, **k: (csv_path, "")
        d.export_measurements_csv()
        QFileDialog.getSaveFileName = lambda *a, **k: (png_path, "")
        d.export_png()
    finally:
        QFileDialog.getSaveFileName = saved
    text = open(csv_path, encoding="utf-8-sig").read() if os.path.exists(csv_path) else ""
    export_ok = "from_kp_km" in text and len(text.strip().splitlines()) == 3 and os.path.getsize(png_path) > 0

    m.action.setChecked(False)
    d.generate_profile()  # new data discards old measurements
    reset_ok = not m.measurements and not m.table.isVisibleTo(d)
    return _result("measure on the depth plot: snapped metrics, clicks, VE lock, CSV/PNG export, reset",
                   ok and click_ok and ve_ok and export_ok and reset_ok,
                   f"api={ok} click={click_ok} ve={ve_ok} export={export_ok} reset={reset_ok}")


def test_multi_raster_series(h: _Harness) -> bool:
    d = h.dock
    h.configure(rasters=("fine", "coarse"), per_raster=True)
    h.generate()
    names = [s["name"] for s in d.measure.series]
    ok = len(d.raster_series) == 2 and len(names) == 3 and names[0].startswith("Composite")
    return _result("per-raster plotting offers composite + each raster as measurement lines", ok, str(names))


def test_contour_profile(h: _Harness) -> bool:
    d = h.dock
    h.configure(source="Contours")
    h.generate()
    ok = len(d.kp_values) == 21 and all(abs(v - _depth_at(kp * 1000 * h.scale)) < 1e-6
                                         for kp, v in zip(d.kp_values, d.depth_values))
    slopes = [v for v in d.slope_deg if v is not None]
    expected_deg = math.degrees(math.atan(_GRAD * h.scale))
    ok = ok and bool(slopes) and all(abs(v + expected_deg) < 1e-3 for v in slopes)
    ok = ok and d._depth_axis is not None
    return _result("contour profile: exact crossings, KP by vectorised projection, plotted", ok,
                   f"crossings={len(d.kp_values)}")


def test_selected_only(h: _Harness) -> bool:
    d = h.dock
    h.configure(route=h.routes)
    h.generate()
    both = len(d.line_parts) == 2  # unselected: both routes, flagged as disconnected parts
    spur = [f.id() for f in h.routes.getFeatures() if f["name"] == "spur"]
    h.routes.selectByIds(spur)
    h.configure(selected_only=True, route=h.routes)
    h.generate()
    ok = both and abs(d.line_length * h.scale - 500.0) < 0.05 and abs(d.kp_values[-1] * 1000 - d.line_length) < 1e-6
    h.routes.removeSelection()
    h.generate()
    empty_ok = d._status_msg == "No selected route features" and not d.kp_values
    h.configure(selected_only=False)
    return _result("'Selected only' profiles just the selected route feature", ok and empty_ok,
                   f"length={d.line_length}")


def test_csv_export(h: _Harness) -> bool:
    from qgis.PyQt.QtWidgets import QFileDialog
    d = h.dock
    h.configure()
    h.generate()
    path = os.path.join(h.temp, "profile.csv")
    saved = QFileDialog.getSaveFileName
    try:
        QFileDialog.getSaveFileName = lambda *a, **k: (path, "")
        d.export_csv()
    finally:
        QFileDialog.getSaveFileName = saved
    rows = open(path, encoding="utf-8").read().strip().splitlines()
    first = rows[1].split(",") if len(rows) > 1 else []
    seabed_sum = sum(d.segment_seabed_length)
    ok = len(rows) == len(d.kp_values) and first and first[2] and first[3]
    ok = ok and abs(seabed_sum - d.seabed_length) < 1e-6
    return _result("segment CSV: lat/lon computed at export, segment seabed sums to total", bool(ok),
                   f"rows={len(rows)} lat={first[2] if first else None}")


def run_all() -> List[bool]:
    from qgis.PyQt.QtCore import QSettings
    results = [test_shim_fill_between()]
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory() as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        harness = _Harness(temp)
        try:
            for test in (test_raster_depth_profile, test_dual_and_slope_only, test_reverse_kp_hover,
                         test_measurements, test_multi_raster_series, test_contour_profile,
                         test_selected_only, test_csv_export):
                try:
                    results.append(test(harness))
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(test.__name__, False, repr(exc)))
        finally:
            harness.close()
            QSettings.setDefaultFormat(old_format)
            import gc
            del harness
            gc.collect()
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
