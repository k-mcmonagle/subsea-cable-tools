"""Optional profile views share KP and preserve the active plan's editing state."""
import unittest
from unittest.mock import Mock, patch

from qgis.core import QgsPointXY
from qgis.PyQt.QtCore import QPoint, QPointF, QSettings, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QSplitter, QWidget

from ..burial import schema
from .test_burial_dock_lifecycle import _Harness

REQUIRES_QGIS = True


class ProfileLayoutTests(unittest.TestCase):
    def setUp(self):
        self.settings = QSettings()
        self.keys = ["SubseaCableTools/BurialPlanner/" + k for k in (
            "profile_layout", "event_lane_visible", "navigator_visible", "dock_splitter_state",
            "profile_splitter_state", "ground_splitter_state", "profile_drag_enabled",
            "profile_overlay_events", "profile_overlay_sections", "slope_panel_visible")]
        self.previous = {k: self.settings.value(k) for k in self.keys if self.settings.contains(k)}
        for key in self.keys:
            self.settings.remove(key)
        self.addCleanup(self.restore_settings)
        harness = _Harness()
        self.dock = harness.__enter__().dock
        self.addCleanup(harness.__exit__, None, None, None)
        self.dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.dock.resize(900, 800)
        self.dock.show()
        QApplication.processEvents()
        self.profile = self.dock.profile
        self.profile.set_route_extent(10, 30)
        self.profile.set_scope(12, 22)
        self.profile.set_profile([(13, 45), (14, 46), (15, None), (16, 49), (17, 48)])

    def restore_settings(self):
        for key in self.keys:
            if key in self.previous:
                self.settings.setValue(key, self.previous[key])
            else:
                self.settings.remove(key)

    def test_window_sync_and_modes_use_one_ground_plot(self):
        p, d = self.profile, self.dock
        ground = d.ground_tab.plot
        self.assertIsNone(p._navigator)
        self.assertIsNone(p._event_lane)
        before = dict(d.model.plan)
        d.navigator_toggle.trigger()
        nav = p._navigator
        self.assertEqual(nav.viewRange()[0], [10, 30])
        nav.region.setRegion((13, 18))
        self.assertEqual(p.plot.viewRange()[0], [13, 18])
        for mode in ("stacked", "ground", "bathy"):
            d._profile_layout_actions[mode].trigger()
            # Showing a splitter child posts layout requests through the
            # profile, the outer splitter and the dock on successive turns.
            QTest.qWait(50)
            self.assertEqual(p._depth_pane.isHidden(), mode == "ground")
            self.assertEqual(d.ground_tab.plot_box.isHidden(), mode != "bathy")
            self.assertEqual(p.ground_layout.indexOf(ground) >= 0, mode != "bathy")
            if mode == "stacked":
                self.assertGreaterEqual(p._depth_pane.height(), p._depth_pane.minimumSizeHint().height())
                self.assertGreaterEqual(p._ground_pane.height(), p._ground_pane.minimumSizeHint().height())
            self.assertAlmostEqual(p._active_plot.viewRange()[0][0], 13, places=4)
            self.assertAlmostEqual(p._active_plot.viewRange()[0][1], 18, places=4)
            visible_plot = ground.plot if mode == "stacked" else p._active_plot
            visible_plot.setXRange(14, 19, padding=0)
            self.assertEqual(nav.region.getRegion(), (14, 19))
            nav.region.setRegion((13, 18))
        self.assertEqual(d.model.plan, before)
        self.assertEqual(p._scope, (12, 22))
        self.assertEqual(nav.viewRange()[0], [10, 30])
        d.navigator_toggle.trigger()
        self.assertTrue(nav.isHidden())
        self.assertIsNone(nav._view)
        self.assertFalse(nav.region.isVisible())

    def test_navigator_pan_edges_and_missing_bathymetry(self):
        d, p = self.dock, self.profile
        d.navigator_toggle.trigger()
        region = p._navigator.region
        region.setRegion((13, 18))
        # Exercise the drag handler used by the view, including releasing
        # the mouse beyond an endpoint and resizing a single edge.
        drag = Mock()
        drag.button.return_value = Qt.MouseButton.LeftButton
        drag.buttonDownPos.return_value = QPointF(15, 0)
        drag.isStart.return_value = True
        drag.isFinish.return_value = False
        drag.pos.return_value = QPointF(4, 0)
        region.mouseDragEvent(drag)
        self.assertEqual(region.getRegion(), (10, 15))
        drag.isStart.return_value = False
        drag.isFinish.return_value = True
        drag.pos.return_value = QPointF(32, 0)
        region.mouseDragEvent(drag)
        self.assertEqual(region.getRegion(), (25, 30))
        self.assertEqual(p.plot.viewRange()[0], [25, 30])
        region.lines[0].setValue(27)
        self.assertEqual(p.plot.viewRange()[0], [27, 30])
        for kp in (15, 15.5, 28):
            p.focus_kp(kp)
            self.assertEqual(p._vline.value(), kp)
            self.assertNotIn("Depth", p._readout.toPlainText())
        p.focus_kp(13.5)
        self.assertIn("Depth 45.5 m", p._readout.toPlainText())
        self.assertEqual(p._scope, (12, 22))

    def test_compact_planning_keeps_controls_and_restores_the_table(self):
        d, p = self.dock, self.profile
        d.refresh_plans()
        d.tabs.setCurrentWidget(d.bas_tab)
        d.resize(720, 620)
        d._profile_layout_actions["stacked"].trigger()
        d.slope_toggle.setChecked(True)
        d.navigator_toggle.setChecked(True)
        p.set_route_extent(10, 30)
        p._active_plot.setXRange(13, 18, padding=0)
        d.profile_status.setText("The route has changed. Rebuild the bathymetry profile. " * 2)
        QTest.qWait(60)
        self.assertEqual(d.width(), 720)
        tab_bar_pos = d.tabs.tabBar().mapTo(d, QPoint())
        profile_button_pos = d.profile_fold.mapTo(d, QPoint())
        page_viewport = d.bas_tab.parentWidget()
        self.assertLess(d.bas_tab.table.mapTo(page_viewport, QPoint()).y(), page_viewport.height())
        bar = d.profile_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.assertEqual(d.tabs.tabBar().mapTo(d, QPoint()), tab_bar_pos)
        self.assertEqual(d.profile_fold.mapTo(d, QPoint()), profile_button_pos)
        before = d.splitter.sizes()
        d._save_dock_splitter_state()
        split_key = "SubseaCableTools/BurialPlanner/dock_splitter_state"
        saved = self.settings.value(split_key)
        d.profile_fold.click()
        QTest.qWait(50)
        self.assertFalse(d.profile_scroll.isVisible())
        self.assertGreater(d.splitter.sizes()[0], before[0])
        d.profile_fold.click()
        QTest.qWait(50)
        self.assertEqual(d.splitter.sizes(), before)
        d.profile_expand.click()
        self.assertFalse(d.tabs.isVisible())
        self.assertTrue(d.workflow_tabs.tabBar().isVisible())
        d.workflow_tabs.setCurrentWidget(d.reporting_tab)
        self.assertTrue(d.reporting_tab.isVisible())
        self.assertFalse(d.profile_pane.isVisible())
        d.workflow_tabs.setCurrentIndex(0)
        d.profile_expand.click()
        QTest.qWait(50)
        self.assertEqual(d.splitter.sizes(), before)
        self.assertIs(d.tabs.currentWidget(), d.bas_tab)
        self.assertAlmostEqual(p._active_plot.viewRange()[0][0], 13, places=4)
        self.assertEqual(self.settings.value(split_key), saved)
        d.profile_expand.click()
        d._on_profile_double_clicked(15)
        self.assertTrue(d.builder_tab.isVisible())
        self.assertTrue(d.profile_scroll.isVisible())

    def test_layout_preference_restores_in_another_dock(self):
        d = self.dock
        d._profile_layout_actions["ground"].trigger()
        d.event_lane_toggle.trigger()
        d.navigator_toggle.trigger()
        legacy = QSplitter(Qt.Orientation.Vertical)
        legacy.addWidget(QWidget())
        legacy.addWidget(QWidget())
        legacy.setSizes([300, 100])
        self.settings.setValue("SubseaCableTools/BurialPlanner/profile_splitter_state", legacy.saveState())
        with _Harness() as reopened:
            other = reopened.dock
            self.assertTrue(other._profile_layout_actions["ground"].isChecked())
            self.assertIs(other.profile._active_plot, other.ground_tab.plot.plot)
            self.assertFalse(other.profile_drag_toggle.isEnabled())
            self.assertTrue(other.event_lane_toggle.isChecked())
            self.assertTrue(other.navigator_toggle.isChecked())
            self.assertTrue(other.profile._navigator.isHidden())
            other.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            other.show()
            QTest.qWait(50)
            self.assertGreaterEqual(other.profile._ground_pane.height(),
                                    other.profile._ground_pane.minimumSizeHint().height())

    def test_lane_preserves_coincident_events_locked_handles_and_cleanup(self):
        d, p = self.dock, self.profile
        d.event_lane_toggle.trigger()
        lane = p._event_lane
        rows = [{"kp": 14., "event_id": "down", "event_type": schema.EVENT_BURIAL_START, "locked": 1},
                {"kp": 14., "event_id": "up", "event_type": schema.EVENT_BURIAL_END, "status": "conflict"}]
        p.set_events(rows, "plough")
        p.set_sections([{"kind": schema.SECTION_BURIAL, "start_kp": 12., "end_kp": 14., "state": "final"}])
        QApplication.processEvents()
        self.assertFalse(lane.isHidden())
        self.assertEqual({e.label for e in lane.layer.events}, {"PLDN", "PLUP", "Plough"})
        self.assertEqual(sum("14.000" in lane.layer.text_of(e) for e in lane.layer.events), 2)
        self.assertFalse(p._event_markers.isVisible())
        self.assertFalse(p._strip_vb.isVisible())
        pos = lane.getPlotItem().vb.mapViewToScene(QPointF(14, 0.5))
        click = Mock()
        click.scenePos.return_value = pos
        click.button.return_value = Qt.MouseButton.LeftButton
        with patch.object(d, "_canvas_point", return_value=QgsPointXY(2, 3)) as locate:
            lane._click(click)
            self.assertAlmostEqual(locate.call_args.args[0], 14.)
            self.assertEqual(d._marker.center(), QgsPointXY(2, 3))
            self.assertAlmostEqual(p._vline.value(), 14.)
        p.set_events(rows, "plough", editable=True)
        self.assertEqual([line.movable for line in p._event_lines], [False, True])
        with patch.object(d.builder_tab, "confirm_move_event", return_value=False) as confirm:
            line = p._event_lines[1]
            line.setValue(14.5)
            line.sigPositionChangeFinished.emit(line)
            confirm.assert_called_once_with("up", 14.5)
            self.assertAlmostEqual(line.value(), 14.)
        d.event_lane_toggle.trigger()
        self.assertTrue(lane.isHidden())
        self.assertFalse(lane.leaders.isVisible())
        d.event_lane_toggle.trigger()
        d.navigator_toggle.trigger()
        p.clear()
        self.assertFalse(lane.layer.events)
        self.assertTrue(lane.isHidden())
        self.assertFalse(p._navigator.region.isVisible())
        p._hover_at_kp(14)
        d.close()
        QTest.qWait(50)
        self.assertIsNone(d._marker)
