"""Behavioural regressions for rings, exclusion reuse, plot cursors and tool direction."""
import json
import math
import os
import tempfile

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication

from ..burial import generation
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..workbench.rules_engine import Interval
from .test_burial_dock_lifecycle import _Harness
from .test_kp_mouse_tool import _Iface, _layer, _tool, _FEATURES

REQUIRES_QGIS = True


def test_range_ring_touches_pointer_at_close_zoom_in_each_distance_mode():
    for crs_id in ('EPSG:3857', 'EPSG:32631', 'EPSG:4326'):
        for cartesian in (False, True):
            iface = _Iface(crs_id)
            tool = _tool(iface, _layer(_FEATURES), cartesian)
            try:
                transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),
                    QgsCoordinateReferenceSystem(crs_id), QgsProject.instance())
                origin = transform.transform(QgsPointXY(3.05, 55.03))
                mouse = transform.transform(QgsPointXY(3.08013, 55.05017))
                size = 0.00001 if crs_id == 'EPSG:4326' else 1.0
                iface.canvas.setExtent(QgsRectangle(mouse.x()-size, mouse.y()-size,
                                                   mouse.x()+size, mouse.y()+size))
                tool.range_bearing_origin = origin
                radius, _ = tool._compute_range_bearing(origin, mouse)
                tool._update_range_bearing_graphics(mouse, radius)
                geom = tool.rangeBearingCircle.asGeometry()
                vertex = next(geom.vertices())
                error = math.hypot(vertex.x()-mouse.x(), vertex.y()-mouse.y())
                assert error < iface.canvas.mapUnitsPerPixel() * 0.5, (crs_id, cartesian, error)
            finally:
                tool.cleanup_resources()
                iface.close()


def test_exclusion_acquisitions_survive_reload_and_input_edits_mark_stale():
    with tempfile.TemporaryDirectory() as folder:
        store = BurialStore(os.path.join(folder, 'plans.gpkg'))
        store.migrate()
        model = PlanModel(store)
        plan_id = model.create_plan('Reuse', 'plough')
        layer = QgsVectorLayer('Point?crs=EPSG:4326&field=value:integer', 'constraint', 'memory')
        feature = QgsFeature(layer.fields())
        feature.setAttributes([1])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(3, 55)))
        layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        again = None
        try:
            model.update_plan({'scope_start_kp': 0.0, 'scope_end_kp': 10.0})
            model.save_rules([{'rule_id': 'r', 'name': 'Constraint', 'enabled': 1,
                'kind': 'proximity', 'action': 'exclude', 'config_json': json.dumps({'layer_id': layer.id()})}])
            ctx = generation.ResolutionContext(rule_hits={'r': [Interval(1, 2)]})
            assert model.save_analysis(ctx, [], model.gen_params(), model.rules,
                                       acquisitions={'cached': ([Interval(1, 2)], [])})
            again = PlanModel(store)
            assert again.load_plan(plan_id)
            assert again.analysis_status()['rule_state'] == {'r': 'current'}
            assert again.acq_cache['cached'][0][0] == Interval(1, 2)
            assert again.display_context().rule_hits['r'] == [Interval(1, 2)]
            layer.startEditing()
            fid = next(layer.getFeatures()).id()
            layer.changeAttributeValue(fid, 0, 2)
            assert again.analysis_status()['rule_state']['r'] == 'changed'
            assert again.analysis_status()['reasons']
            layer.rollBack()
        finally:
            model.close_plan()
            if again is not None:
                again.close_plan()
            QgsProject.instance().removeMapLayer(layer.id())
            store.close()


def test_bidirectional_bar_cursor_sync_does_not_change_auto_fit():
    with _Harness() as h:
        dock = h.dock
        dock.model.plan['scope_end_kp'] = 10
        dock.profile.set_scope(0, 10)
        dock.profile.set_profile([(0, 10), (10, 20)])
        dock.profile.plot.enableAutoRange()
        for _ in range(3):
            QApplication.processEvents()
        before = dock.profile.plot.viewRange()
        dock.rules_tab._fire_hover.kpHovered.emit(4.0)
        assert dock.profile._vline.value() == 4
        assert dock.risk_tab._fire_delegate.hover_kp == 4
        dock.risk_tab.overview.kpHovered.emit(7.0)
        assert dock.profile._vline.value() == 7
        assert dock.rules_tab._fire_delegate.hover_kp == 7
        dock.profile.kpHovered.emit(3.0)
        assert dock.rules_tab._fire_delegate.hover_kp == dock.risk_tab._fire_delegate.hover_kp == 3
        for kp in (-2, 12, -2, 12):
            dock.profile._show_kp_readout(kp)
            for _ in range(3):
                QApplication.processEvents()
        after = dock.profile.plot.viewRange()
        assert before == after, (before, after)


def test_tool_outline_follows_installation_direction_on_route_and_saved_path():
    from ..burial import path_data
    from .test_burial_task import _route
    with _Harness() as h:
        dock = h.dock
        model = dock.model
        model.route, model.distance = _route()
        model.plan['params_json'] = json.dumps({'tool_id': 'tool'})
        model.tools = [{'tool_id': 'tool', 'footprint_wkt': 'LINESTRING(0 0, 0 20)'}]
        model.sections = []
        dock.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
        for direction in (1, -1):
            model.plan['direction'] = direction
            dock._draw_outlines(5.0, None, None)
            geom = dock._footprint_band.asGeometry()
            points = list(geom.vertices())
            assert (points[-1].y() - points[0].y()) * direction > 0
            # Saved path may predate a direction change; the displayed pose
            # still follows the current plan without changing stored geometry.
            model.path_result = {'path_id': 'path', 'tool_path_wkt':
                path_data.linestring_wkt([(0, 50.01), (0, 50.1)]),
                'barge_track_wkt': path_data.linestring_wkt([(0, 50.02), (0, 50.11)])}
            dock._clear_footprint_cache()
            tool, barge = dock._path_display_points()
            assert (tool[-1][1] - tool[0][1]) * direction > 0
            assert (barge[-1][1] - barge[0][1]) * direction > 0


def test_shared_plot_cursors_do_not_expand_data_bounds():
    from ..plot_widget import Figure, FigureCanvas
    widget = FigureCanvas(Figure())
    try:
        axis = widget.figure.add_subplot(111)
        axis.plot([0, 10], [10, 20])
        vertical = axis.axvline(5)
        horizontal = axis.axhline(15, ignore_bounds=True)
        view = axis.plot_item.vb
        view.enableAutoRange()
        view.updateAutoRange()
        before = view.viewRange()
        vertical.set_xdata([1000, 1000])
        horizontal.set_ydata([1000, 1000])
        view.updateAutoRange()
        assert before == view.viewRange()
    finally:
        widget.close()


def test_native_conversion_rejects_unsafe_geometry_before_host_opens_it():
    from qgis.core import QgsApplication, QgsProcessingException, QgsProcessingFeedback
    from ..processing.import_mdb_algorithm import ImportMdbAlgorithm
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, 'unsafe.geojson')
        with open(source, 'w') as handle:
            json.dump({'type': 'FeatureCollection', 'features': [{'type': 'Feature',
                'properties': {}, 'geometry': {'type': 'Point', 'coordinates': [1e100, 1]}}]}, handle)
        try:
            ImportMdbAlgorithm()._run_worker(['--input', source, '--output', os.path.join(folder, 'out.gpkg'),
                '--table', 'unsafe', '--crs', 'EPSG:32631', '--prefix', QgsApplication.prefixPath()],
                QgsProcessingFeedback(), worker_script='mdb_convert_worker.py')
        except QgsProcessingException as exc:
            assert 'unsafe coordinates' in str(exc)
        else:
            raise AssertionError('Unsafe output reached the parent process')


def test_mdb_geojson_attributes_remain_finite():
    from ..processing.mdb_odbc_worker import _coerce_json_value
    for value in (float('nan'), float('inf'), -float('inf')):
        assert _coerce_json_value(value) is None
    assert _coerce_json_value(42.5) == 42.5


def test_depth_changes_during_analysis_are_not_recorded_as_current():
    with tempfile.TemporaryDirectory() as folder:
        store = BurialStore(os.path.join(folder, 'plans.gpkg'))
        store.migrate()
        model = PlanModel(store)
        model.create_plan('Depth extensions', 'plough')
        try:
            depth = ['old']
            model.depth_fingerprint = lambda: depth[0]
            model.save_rules([{'rule_id': 'wd', 'name': 'Water depth extension', 'enabled': 1,
                'kind': 'manual', 'action': 'exclude', 'config_json': json.dumps({'extend_mode': 'wd'})}])
            basis = model.analysis_basis()
            depth[0] = 'changed during analysis'
            assert model.save_analysis(generation.ResolutionContext(), [], model.gen_params(),
                                       model.rules, basis=basis)
            status = model.analysis_status()
            assert status['rule_state']['wd'] == 'changed'
            assert status['reasons']
        finally:
            model.close_plan()
            store.close()
