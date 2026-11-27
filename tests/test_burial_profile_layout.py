"""Optional profile views share KP and preserve the active plan's editing state."""
import unittest
from unittest.mock import Mock, patch

from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer
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
            "profile_layout", "event_lane_visible", "cable_type_bar_visible", "navigator_visible", "dock_splitter_state",
            "profile_splitter_state", "ground_splitter_state", "profile_drag_enabled",
            "profile_overlay_events", "profile_overlay_sections", "slope_panel_visible",
            "lane_rpl_repeater", "lane_rpl_bodies", "lane_rpl_crossings", "lane_rpl_alter_courses", "lane_rpl_other")]
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
        d.slope_toggle.setChecked(True)
        for mode in ("stacked", "ground", "bathy"):
            if mode == "bathy":
                d.bathy_toggle.trigger()
                d.ground_toggle.trigger()
            elif mode == "ground":
                d.bathy_toggle.trigger()
            else:
                d.ground_toggle.trigger()
            # Showing a splitter child posts layout requests through the
            # profile, the outer splitter and the dock on successive turns.
            QTest.qWait(50)
            self.assertEqual(p._depth_pane.isHidden(), mode == "ground")
            self.assertEqual(d.ground_tab.plot_box.isHidden(), mode != "bathy")
            self.assertEqual(p.ground_layout.indexOf(ground) >= 0, mode != "bathy")
            self.assertTrue(ground.plot.getAxis("bottom").label.isVisible())
            self.assertEqual(p.slope_plot.getAxis("bottom").label.isVisible(), mode == "bathy")
            self.assertFalse(p.plot.getAxis("bottom").label.isVisible())
            self.assertTrue(p.plot.getAxis("bottom").style["showValues"])
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
        d.slope_toggle.setChecked(False)
        self.assertTrue(p.plot.getAxis("bottom").label.isVisible())
        d.navigator_toggle.trigger()
        self.assertTrue(nav.isHidden())
        self.assertIsNone(nav._view)
        self.assertFalse(nav.region.isVisible())

    def test_view_menu_allows_several_plot_changes_while_open(self):
        d = self.dock
        menu = d.view_button.menu()
        menu.popup(d.view_button.mapToGlobal(QPoint(0, 0)))
        self.addCleanup(menu.close)
        QTest.qWait(30)
        for action in (d.slope_toggle, d.ground_toggle):
            pos = menu.actionGeometry(action).center()
            QTest.mouseMove(menu, pos)
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=pos)
            self.assertTrue(action.isChecked())
            self.assertTrue(menu.isVisible())
        self.assertFalse(self.profile._depth_pane.isHidden())

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

    def test_hiding_middle_plot_keeps_a_movable_divider_and_kp_master(self):
        d, p = self.dock, self.profile
        d.resize(760, 640)
        d.slope_toggle.trigger()
        d.ground_toggle.trigger()
        d.navigator_toggle.trigger()
        d.slope_toggle.trigger()
        QTest.qWait(60)
        before = p._splitter.sizes()
        self.assertEqual(before[1], 0)
        low, high = p._splitter.getRange(2)
        self.assertGreater(high - low, 20)
        self.assertTrue(p._splitter.handle(2).isVisible())
        p._splitter.moveSplitter(low if before[0] > (low + high) / 2 else high, 2)
        after = p._splitter.sizes()
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[2], after[2])
        self.assertGreater(min(after[0], after[2]), 0)
        p._navigator.region.setRegion((13, 16))
        p.set_measuring(True)
        d.slope_toggle.trigger()
        d.bathy_toggle.trigger()
        d.ground_toggle.trigger()
        QTest.qWait(50)
        self.assertIs(p._active_plot, p.slope_plot)
        self.assertFalse(p.measuring())
        self.assertFalse(d.slope_toggle.isEnabled())
        self.assertEqual(p.slope_plot.viewRange()[0], [13, 16])
        p.slope_plot.setXRange(14, 17, padding=0)
        self.assertEqual(p._navigator.region.getRegion(), (14, 17))
        d.bathy_toggle.trigger()
        d.slope_toggle.trigger()
        QTest.qWait(50)
        self.assertEqual(p.plot.viewRange()[0], [14, 17])

    def test_find_seabed_only_translates_the_vertical_axis(self):
        p = self.profile
        view = p.plot.getViewBox()
        for locked in (False, True):
            p.set_true_scale(locked)
            p.plot.setXRange(13.1, 13.9, padding=0)
            view.translateBy(y=100)
            x, y = view.viewRange()
            p._find_seabed.click()
            after_x, after_y = view.viewRange()
            for old, new in zip(x, after_x):
                self.assertAlmostEqual(old, new, places=6)
            self.assertAlmostEqual(after_y[1] - after_y[0], y[1] - y[0], places=6)
            self.assertAlmostEqual(sum(after_y)/2, 45.5, places=6)
        p.set_true_scale(False)
        p.plot.setXRange(14, 16, padding=0)
        p._find_seabed.click()  # No sounding at KP 15. Centre on a visible neighbour.
        self.assertIn(round(sum(view.viewRange()[1])/2), (46, 49))
        p.plot.setXRange(19, 20, padding=0)
        before = view.viewRange()
        with patch("qgis.PyQt.QtWidgets.QToolTip.showText"):
            p._find_seabed.click()
        self.assertEqual(view.viewRange(), before)

    def test_rpl_lane_uses_plan_chainage_and_filters_without_editing(self):
        from ..burial.analysis_task import build_route_frame
        from ..burial.profile_panels import read_rpl_lane_events
        from ..workbench.schema import CATEGORY_BODY

        d, p = self.dock, self.profile
        points = QgsVectorLayer("Point?crs=EPSG:4326&field=Event:string&field=DistCumulative:double", "positions", "memory")
        positions = [QgsPointXY(0.01*i, 0) for i in range(3)]
        for i, (pos, label) in enumerate(zip(positions, ("Unit 1", "A/C 2", "Joint"))):
            feature = QgsFeature(points.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(pos))
            feature.setAttributes([label, 100 + i])
            points.dataProvider().addFeatures([feature])
        lines = QgsVectorLayer("LineString?crs=EPSG:4326", "route", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPolylineXY(positions))
        lines.dataProvider().addFeatures([feature])
        route, _ = build_route_frame(lines, QgsProject.instance(), start_kp_km=12.)
        store = Mock()
        store.get_rpl.return_value = {"points_layer": "points"}
        store.open_layer.return_value = points
        store.list_event_rules.return_value = [dict(pattern="^Unit", category=CATEGORY_BODY, body_type="repeater")]
        with patch.object(d.model, "route", route), patch.object(d.model, "resolved_rpl_id", "active"), \
                patch.object(d.model, "workbench_store", store), \
                patch("subsea_cable_tools.burial.profile_panels.read_rpl_lane_events", wraps=read_rpl_lane_events) as read:
            d.event_lane_toggle.trigger()
            lane = p._event_lane
            self.assertEqual([e.label for e in lane.layer.events], ["Unit 1", "Joint"])
            d._rpl_filters["alter_courses"].trigger()
            self.assertEqual([e.label for e in lane.layer.events], ["Unit 1", "A/C 2", "Joint"])
            event = lane.layer.events[1]
            self.assertAlmostEqual(event.kp, route.kp_at_point(positions[1]).kp_km, places=5)
            self.assertIn("printed KP 101.000", event.detail)
            d._rpl_filters["bodies"].trigger()
            self.assertEqual([e.label for e in lane.layer.events], ["Unit 1", "A/C 2"])
            self.assertEqual(read.call_count, 1)
            store.get_rpl.assert_called_once_with("active")
        d._refresh_rpl_lane()
        self.assertFalse(lane.layer.events)
        self.assertTrue(lane.isHidden())

    def test_cable_type_bar_follows_measured_legs_view_and_map(self):
        from ..qgis_compat import FIELD_TYPE_STRING
        from .test_kp_reference import _FakeWorkbench, _plan_on

        d, p = self.dock, self.profile
        wb = _FakeWorkbench()
        vertices = [(0, 0), (.01, 0), (.02, 0), (.01, 0), (.01, .01), (.02, .01)]
        wb.add("typed", "1", vertices, start_kp=12, bias=200)
        wb.add("untyped", "2", vertices, start_kp=12)
        lines = wb.open_layer("typed_l")
        provider = lines.dataProvider()
        provider.addAttributes([QgsField("CableType", FIELD_TYPE_STRING)])
        lines.updateFields()
        labels = ["Type A", "Type A", None, "Type A", "Type B"]
        provider.changeAttributeValues({
            f.id(): {lines.fields().indexOf("CableType"): labels[f["SeqNo"] - 1]} for f in lines.getFeatures()})
        _plan_on(d.model, wb, "typed")
        self.assertTrue(d.cable_type_toggle.isEnabled())
        self.assertIsNone(p._cable_type_bar)
        route = d.model.route
        edges = [12 + m / 1000 for m in route.feature_offsets_m] + [route.end_kp_km]
        expected = [(edges[0], edges[2], "Type A"), (edges[3], edges[4], "Type A"), (edges[4], edges[5], "Type B")]
        self.assertEqual(d.model.cable_type_ranges, expected)
        saved = dict(d.model.plan)
        with patch("subsea_cable_tools.workbench.layer_style.user_cable_type_colours", return_value={"TYPEA": "#123456"}):
            d.cable_type_toggle.trigger()
        bar = p._cable_type_bar
        self.assertTrue(bar.isVisible())
        self.assertFalse(d.event_lane_toggle.isChecked())
        self.assertEqual(bar.layer.events[0].colour, (18, 52, 86))
        d.ground_toggle.trigger()
        d.bathy_toggle.trigger()
        d.navigator_toggle.trigger()
        p._navigator.region.setRegion((edges[1], edges[4]))
        QTest.qWait(40)
        for actual, wanted in zip(bar.viewRange()[0], (edges[1], edges[4])):
            self.assertAlmostEqual(actual, wanted, places=4)
        vb = bar.getViewBox()
        middle = (edges[1] + edges[2]) / 2
        pos = vb.mapViewToScene(QPointF(middle, 0.5))
        bar._hover(pos)
        self.assertIn("Type A", bar.toolTip())
        self.assertIn(f"{edges[0]:.3f}–{edges[2]:.3f}", bar.toolTip())
        self.assertAlmostEqual(bar.cursor.value(), middle, places=6)
        click = Mock()
        click.button.return_value = Qt.MouseButton.LeftButton
        click.scenePos.return_value = pos
        with patch.object(d, "highlight_kp") as highlight:
            bar._click(click)
        self.assertAlmostEqual(highlight.call_args[0][0], middle, places=6)
        gap = (edges[2] + edges[3]) / 2
        bar._hover(vb.mapViewToScene(QPointF(gap, .5)))
        self.assertEqual(bar.toolTip(), "")
        p.focus_kp(edges[3])
        self.assertAlmostEqual(bar.cursor.value(), edges[3])
        self.assertEqual(d.model.plan, saved)
        d.cable_type_toggle.trigger()
        self.assertTrue(bar.isHidden())
        self.assertFalse(bar.layer.events)
        d.cable_type_toggle.trigger()
        _plan_on(d.model, wb, "untyped")
        self.assertTrue(bar.isHidden())
        self.assertFalse(d.cable_type_toggle.isEnabled())
        self.assertFalse(bar.layer.events)
        _plan_on(d.model, wb, "typed")
        self.assertTrue(bar.isVisible())

    def test_crowded_labels_keep_all_ticks_and_exact_kps(self):
        d, p = self.dock, self.profile
        d.profile_expand.click()
        d.event_lane_toggle.trigger()
        rows = [dict(kp=12.2 + i*.4, event_id=str(i),
                     event_type=schema.EVENT_BURIAL_START if i%2 else schema.EVENT_BURIAL_END) for i in range(24)]
        p.set_events(rows, "plough")
        lane = p._event_lane
        QTest.qWait(50)
        # Deterministic device width for both displays and font scales.
        with patch.object(lane.layer, "x_mapping", return_value=(12, 22, 0, 420, 0)):
            layout = lane.layer.layout()
            self.assertTrue(layout["compact"])
            self.assertEqual(len(layout["slots"]), len(rows))
            i = next(i for i, slot in enumerate(layout["slots"]) if slot.tier < 0)
            hits = lane.layer.events_at(layout["x"][i], lane.layer.lane_height() - 1, 0)
            self.assertIn(lane.layer.events[i], hits)
            self.assertIn(f"{lane.layer.events[i].kp:.3f}", lane.layer.events[i].detail)
        with patch.object(lane.layer, "x_mapping", return_value=(12, 13, 0, 420, 0)):
            self.assertFalse(lane.layer.layout()["compact"])

    def test_cable_bar_uses_active_layer_categories_and_shared_editor(self):
        from qgis.core import QgsCategorizedSymbolRenderer, QgsLineSymbol, QgsRendererCategory
        from ..workbench import layer_style, project_layers
        from ..workbench.cable_type_dialog import palette_updates
        from .test_workbench_project_layers import _store_with_rpl

        d = self.dock
        store, rpl = _store_with_rpl()
        project = QgsProject.instance()
        lines = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
        layer_id = lines.id()
        self.addCleanup(lambda: project.removeMapLayer(layer_id) if project.mapLayer(layer_id) else None)
        previous = layer_style.user_cable_type_colours()
        self.addCleanup(layer_style.set_user_cable_type_colours, previous)
        layer_style.set_user_cable_type_colours({"LW": "#333333"})
        with patch.object(d.model, "workbench_store", store), patch.object(d.model, "resolved_rpl_id", rpl["rpl_id"]), \
                patch.object(d.model, "cable_type_ranges", [(12., 13., "DA"), (13., 14., "LW")]):
            d._refresh_cable_type_bar(False)
            d.cable_type_toggle.trigger()
            bar = self.profile._cable_type_bar
            def colours():
                return [e.colour for e in bar.layer.events]

            symbol = QgsLineSymbol.createSimple({"color": "#112233"})
            lines.setRenderer(QgsCategorizedSymbolRenderer("CableType", [QgsRendererCategory(["DA", "SA"], symbol, "Armour")]))
            self.assertEqual(colours(), [(17, 34, 51), (51, 51, 51)])
            layer_style.refresh_line_categories(lines)
            self.assertEqual(len(lines.renderer().categories()), 2)

            def accept_editor(dialog):
                dialog._palette["LW"] = "#556677"
                return 1

            action = next(a for a in d.view_button.menu().actions() if a.text() == "Cable type colours…")
            with patch("subsea_cable_tools.workbench.cable_type_dialog.qt_exec", side_effect=accept_editor):
                action.trigger()
            self.assertEqual(colours(), [(17, 34, 51), (85, 102, 119)])
            project.removeMapLayer(layer_id)
            d._refresh_cable_type_bar(True)
            palette_updates.changed.emit()
            self.assertEqual(colours(), [(211, 47, 53), (85, 102, 119)])
            self.assertIsNone(d._cable_style_layer)
            runs = [(12., 13., "DA"), (13., 14., "da")]
            bar.set_ranges(runs)
            self.assertEqual([(e.kp, e.kp_end) for e in bar.layer.events], [(12., 14.)])
            bar.set_ranges(runs, {"da": "#334455"})
            self.assertEqual(colours(), [(211, 47, 53), (51, 68, 85)])

    def test_compact_planning_keeps_controls_and_restores_the_table(self):
        d, p = self.dock, self.profile
        d.refresh_plans()
        d.tabs.setCurrentWidget(d.bas_tab)
        d.resize(720, 620)
        d.ground_toggle.trigger()
        d.slope_toggle.setChecked(True)
        d.navigator_toggle.setChecked(True)
        p.set_route_extent(10, 30)
        p._active_plot.setXRange(13, 18, padding=0)
        message = "Out of date — the route has changed. Rebuild the bathymetry profile. " * 3
        d.profile_status.setText(message)
        QTest.qWait(60)
        self.assertEqual(d.width(), 720)
        self.assertIs(d.profile_status.parentWidget(), d.profile_header)
        self.assertEqual(d.profile_status.toolTip(), message)
        self.assertEqual(d.profile_status.accessibleName(), message)
        self.assertLess(d.profile_scroll.y() - d.profile_header.geometry().bottom(),
                        d.profile_status.fontMetrics().height())
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

    def test_bas_coverage_and_scrollbars_in_a_short_pane(self):
        d, tab = self.dock, self.dock.bas_tab
        d.refresh_plans()
        d.tabs.setCurrentWidget(tab)
        self.assertTrue(tab.strip.isHidden())
        d.model.update_plan({"scope_start_kp": 10., "scope_end_kp": 20.})
        tab.refresh()
        self.assertFalse(tab.strip.isHidden())
        tab._insert_rows(None, 20)
        QTest.qWait(50)
        # Allocate space for a few rows between the table's native minimum
        # and its preferred height. The vertical scrollbar belongs to the table.
        page = tab.parentWidget()
        height = (tab.table.minimumHeight() + tab.table.sizeHint().height()) // 2
        top = tab.table.mapTo(d.tabs, QPoint()).y()
        bottom = d.tabs.height() - page.mapTo(d.tabs, QPoint(0, page.height())).y()
        desired = top + height + tab.layout().contentsMargins().bottom() + bottom
        d.splitter.setSizes([desired, sum(d.splitter.sizes()) - desired])
        QTest.qWait(50)
        self.assertEqual(page.parentWidget().verticalScrollBar().maximum(), 0)
        self.assertGreater(tab.table.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(tab.table.height(), tab.table.minimumHeight())

    def test_layout_preference_restores_in_another_dock(self):
        d = self.dock
        d.ground_toggle.trigger()
        d.bathy_toggle.trigger()
        d.event_lane_toggle.trigger()
        d.navigator_toggle.trigger()
        legacy = QSplitter(Qt.Orientation.Vertical)
        legacy.addWidget(QWidget())
        legacy.addWidget(QWidget())
        legacy.setSizes([300, 100])
        self.settings.setValue("SubseaCableTools/BurialPlanner/profile_splitter_state", legacy.saveState())
        with _Harness() as reopened:
            other = reopened.dock
            self.assertTrue(other.ground_toggle.isChecked())
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
