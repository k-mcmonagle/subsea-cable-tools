"""New profile controls, manually sized tables and native multi-raster sampling."""
import gc
import os
import shutil
import tempfile
import unittest

import numpy as np
from osgeo import gdal, osr
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                       QgsProject, QgsRasterLayer, QgsRectangle)
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QTableWidgetItem

from ..burial import map_layers, schema
from ..burial.analysis_task import DepthSnapshot
from ..burial.cpt_log_view import ZoneStripItem
from ..burial.profile_widget import BurialProfileWidget
from ..burial.tabs.bathy_dialog import BathymetryDialog
from ..qgis_compat import HEADER_RESIZE_MODE_INTERACTIVE, HEADER_RESIZE_MODE_STRETCH
from ..table_widgets import ResizableTableWidget
from ..workbench.depth_service import DepthSourceConfig

REQUIRES_QGIS = True


def pump():
    for _ in range(4):
        QApplication.processEvents()


class UiImprovementsTests(unittest.TestCase):
    def test_missing_partial_and_excess_cpt_zones(self):
        item = ZoneStripItem()
        for zones in ([], [None, None, None], [None], [3, 3, 3, 4]):
            item.set_zones([0.0, 1.0, 2.0], zones)
        item.set_zones([0.0, 1.0, 2.0], [3])
        self.assertEqual([(a, b) for a, b, _colour in item._runs], [(0.0, 0.5)])
        self.assertEqual(item.boundingRect().height(), 2.0)
        item.set_zones([], [3])
        self.assertEqual(item._runs, [])

    def test_planned_events_stay_hidden_after_redraw_and_edit_mode(self):
        widget = BurialProfileWidget()
        events = [dict(event_id="down", event_type=schema.EVENT_BURIAL_START, kp=1),
                  dict(event_id="up", event_type=schema.EVENT_BURIAL_END, kp=2)]
        try:
            widget._overlay_visible["events"] = False
            widget.set_events(events, "plough")
            self.assertFalse(widget._event_markers.isVisible())
            widget.set_events(events, "plough", editable=True)
            self.assertEqual(len(widget._event_lines), 2)
            self.assertTrue(all(not line.isVisible() for line in widget._event_lines))
            self.assertTrue(all(not line.label.isVisible() for line in widget._event_lines))
            widget._overlay_visible["events"] = True
            widget._apply_overlay_visibility()
            self.assertTrue(all(line.isVisible() for line in widget._event_lines))
            self.assertTrue(all(line.label.isVisible() for line in widget._event_lines))
        finally:
            widget.close()
            widget.deleteLater()
            pump()

    def test_manual_column_width_survives_refresh(self):
        table = ResizableTableWidget(1, 2)
        table.setHorizontalHeaderLabels(["Criterion", "Notes"])
        table.setItem(0, 0, QTableWidgetItem("Slope"))
        table.setItem(0, 1, QTableWidgetItem("long path " * 100))
        header = table.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_STRETCH)
        table.resize(900, 240)
        table.show()
        pump()
        try:
            self.assertEqual(header.sectionResizeMode(0), HEADER_RESIZE_MODE_INTERACTIVE)
            self.assertLessEqual(table.columnWidth(1), 480)
            fitted = table.columnWidth(0)
            start = QPoint(fitted - 1, header.height() // 2)
            finish = start + QPoint(90, 0)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QApplication.sendEvent(header.viewport(), QMouseEvent(
                QEvent.Type.MouseMove, QPointF(finish), QPointF(header.viewport().mapToGlobal(finish)),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=finish)
            width = table.columnWidth(0)
            self.assertGreater(width, fitted + 50)
            table.setItem(0, 0, QTableWidgetItem("Water depth criterion with much longer text"))
            header.setSectionResizeMode(HEADER_RESIZE_MODE_STRETCH)
            pump()
            table.resizeColumnsToContents()
            table.setColumnWidth(0, 50)  # legacy refresh default must not override the drag
            pump()
            self.assertEqual(table.columnWidth(0), width)
            table._fit_column(0)
            self.assertGreater(table.columnWidth(0), width)
        finally:
            table.close()
            table.deleteLater()
            pump()


class MultiRasterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="burial_multi_")
        self.project = QgsProject.instance()
        self.layers = []
        self.snapshots = []
        self.coarse = self.make_raster("coarse", 20, 20)
        self.fine = self.make_raster("fine", 5, 10, gap=True)
        self.to_wgs = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:32630"),
                                             QgsCoordinateReferenceSystem("EPSG:4326"), self.project)

    def tearDown(self):
        self.snapshots.clear()
        self.project.removeMapLayers([layer.id() for layer in self.layers])
        self.layers.clear()
        self.coarse = self.fine = None
        gc.collect()
        shutil.rmtree(self.folder, ignore_errors=True)

    def make_raster(self, name, pixel, value, gap=False, origin=(499900, 6000100)):
        path = os.path.join(self.folder, name + ".tif")
        n = int(200 / pixel)
        ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((origin[0], pixel, 0, origin[1], 0, -pixel))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32630)
        ds.SetProjection(srs.ExportToWkt())
        values = np.full((n, n), value, dtype=np.float32)
        if gap:
            values[n//2-3:n//2+3, n//2-3:n//2+3] = -9999
        ds.GetRasterBand(1).WriteArray(values)
        ds.GetRasterBand(1).SetNoDataValue(-9999)
        ds = None
        layer = QgsRasterLayer(path, name, "gdal")
        self.assertTrue(layer.isValid())
        self.project.addMapLayer(layer, False)
        self.layers.append(layer)
        return layer

    def snapshot(self, priority="finest", layers=None, dates=None):
        config = DepthSourceConfig({"mode": 1, "raster_layer_ids": [l.id() for l in (layers or [self.coarse, self.fine])],
                                    "raster_priority": priority, "raster_acquired_dates": dates or {}})
        snap = DepthSnapshot(config, self.project)
        self.snapshots.append(snap)
        return snap

    def test_native_priority_fallback_and_spatial_candidates(self):
        outside_gap = self.to_wgs.transform(QgsPointXY(500060, 6000000))
        gap = self.to_wgs.transform(QgsPointXY(500000, 6000000))
        finest = self.snapshot()
        self.assertAlmostEqual(finest._sample_rasters(outside_gap), 10)
        self.assertEqual(finest.last_source, self.fine.source())
        self.assertAlmostEqual(finest._sample_rasters(gap), 20)
        self.assertEqual(finest.last_source, self.coarse.source())
        listed = self.snapshot("listed")
        self.assertAlmostEqual(listed._sample_rasters(outside_gap), 20)
        self.assertEqual(listed._raster_candidates(QgsRectangle(0, 0, 0.1, 0.1)), [])
        self.assertIsNone(listed._sample_rasters(QgsPointXY(0, 0)))

    def test_recency_breaks_resolution_ties_and_listed_order_overrides_it(self):
        newer_fine = self.make_raster("newer_fine", 5, 30)
        layers = [self.coarse, self.fine, newer_fine]
        dates = {self.coarse.source(): "2026-10-01", self.fine.source(): "2020-01-01",
                 newer_fine.source(): "2025-01-01"}
        point = self.to_wgs.transform(QgsPointXY(500060, 6000000))
        # Finest before recency: the newer but coarser survey does not win.
        self.assertAlmostEqual(self.snapshot(layers=layers, dates=dates)._sample_rasters(point), 30)
        self.assertAlmostEqual(self.snapshot("listed", layers, dates)._sample_rasters(point), 20)
        self.assertAlmostEqual(self.snapshot(layers=layers)._sample_rasters(point), 10)

    def test_indexed_cross_route_sampling_matches_individual_points(self):
        from qgis.core import QgsDistanceArea
        from ..burial.analysis_task import transect_points
        distance = QgsDistanceArea()
        distance.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:4326"), self.project.transformContext())
        distance.setEllipsoid("WGS84")
        center = self.to_wgs.transform(QgsPointXY(500000, 6000000))
        offsets = list(range(-120, 121, 10))
        snap = self.snapshot()
        values, sources, cells = snap._sample_raster_transect(center, 0.0, offsets, distance)
        points = transect_points(center, 0.0, offsets, distance, None)
        expected = [snap._sample_rasters(p) for p in points]
        self.assertEqual(values, expected)
        self.assertIn(None, values)
        self.assertIn(10, values)
        self.assertIn(20, values)

    def test_large_physical_merge_is_refused_before_writing_pixels(self):
        from qgis.core import QgsProcessingContext, QgsProcessingException, QgsProcessingFeedback
        from .test_mbes_xyz_tools import _init_processing
        from ..processing.merge_mbes_rasters_algorithm import MergeMBESRastersAlgorithm
        self.assertTrue(_init_processing())
        distant = self.make_raster("distant", 5, 30, origin=(699900, 6200100))
        algorithm = MergeMBESRastersAlgorithm()
        algorithm.initAlgorithm()
        context = QgsProcessingContext()
        context.setProject(self.project)
        output = os.path.join(self.folder, "huge.tif")
        with self.assertRaisesRegex(QgsProcessingException, "exceeds one billion cells"):
            algorithm.processAlgorithm({"INPUTS": [self.fine, distant], "OUTPUT": output},
                                       context, QgsProcessingFeedback())
        self.assertFalse(os.path.exists(output))

    def test_total_tile_cache_is_bounded(self):
        layers = [self.make_raster(f"survey{i}", 20, i + 1) for i in range(10)]
        snap = self.snapshot(layers=layers)
        self.assertLessEqual(sum(s.cache_bytes for s, _ in snap._native_samplers), 128 * 1024 * 1024)

    def test_dialog_roundtrip_priority_fingerprint_and_relink(self):
        import json
        dialog = BathymetryDialog()
        again = None
        try:
            dialog.use_layers([self.coarse, self.fine, self.coarse])
            self.assertEqual(len(dialog._rasters), 2)
            dialog.raster_priority.setCurrentIndex(1)
            item = dialog.raster_tree.topLevelItem(0)
            item.setText(2, "not a date")
            self.assertTrue(dialog.problems())
            item.setText(2, "2026-10-01")
            self.assertFalse(dialog.problems())
            saved = dialog.result_row()
            data = json.loads(saved["config_json"])
            self.assertEqual(data["raster_layer_ids"], [self.coarse.id(), self.fine.id()])
            config = DepthSourceConfig(data)
            again = BathymetryDialog(saved, config)
            self.assertEqual(len(again._rasters), 2)
            self.assertEqual(again.raster_priority.currentData(), "listed")
            self.assertEqual(again.raster_tree.topLevelItem(0).text(2), "2026-10-01")
            self.assertEqual(config.raster_acquired_dates[self.coarse.source()], "2026-10-01")
            self.assertEqual(map_layers.legacy_depth_config_fingerprint(self.project, config), "")
            before = map_layers.depth_config_fingerprint(self.project, config)
            from ..burial.plan_model import PlanModel
            from unittest.mock import patch
            model = PlanModel(object())
            with patch.object(model, "depth_config", return_value=config):
                self.assertEqual(model.depth_fingerprint(), before)
            config.raster_layer_ids.reverse()
            self.assertNotEqual(before, map_layers.depth_config_fingerprint(self.project, config))
            config.raster_layer_ids.reverse()
            config.raster_priority = "finest"
            self.assertNotEqual(before, map_layers.depth_config_fingerprint(self.project, config))
            self.assertNotIn("raster_priority", config.to_dict())
            # A new QGIS layer ID is relinked by source, preserving its rank.
            relink_data = dict(data, raster_layer_ids=["old-coarse-id", "old-fine-id"])
            relinked, _ = map_layers.relink_depth_config(self.project, relink_data)
            self.assertEqual(relinked["raster_layer_ids"], data["raster_layer_ids"])
            self.assertEqual(relinked["raster_priority"], "listed")
        finally:
            for window in (dialog, again):
                if window is not None:
                    window.close()
                    window.deleteLater()
            pump()
