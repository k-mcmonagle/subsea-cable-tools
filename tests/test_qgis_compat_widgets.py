# -*- coding: utf-8 -*-
"""Construction checks for widgets using QGIS 3/4 compatibility aliases."""

from __future__ import annotations

from qgis.PyQt.QtCore import QObject, QSettings, pyqtSignal
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsProject
from qgis.gui import QgsMapCanvas

from ..catenary.v3.ui.bu_lowering_dialog import BULoweringDialog
from ..catenary.v3.ui.dialog import LaySimulatorDialog
from ..burial.tabs.attribute_widgets import AttributeRulesTable
from ..burial.tabs.inputs_tab import InputsTab
from ..burial.tabs.risk_tab import CheckEditorDialog
from ..burial.tabs.rules_tab import RuleEditorDialog as BurialRuleEditorDialog
from ..burial.profile_widget import BurialProfileWidget
from ..burial.tabs.paths_tab import (
    LaybackProfileDialog,
    PathsTab,
    RadiusRulesDialog,
    VesselDialog,
)
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..explorer import CableLayExplorerWindow
from ..workbench import layer_style, schema
from ..workbench.assessment_panel import RuleEditorDialog
from ..workbench.cable_type_dialog import CableTypeColourDialog
from ..workbench.compare_panel import RevisionComparePanel
from ..workbench.depth_service import DepthSourceConfig


def test_burial_inputs_construct_and_switch_source_type():
    class _Model(QObject):
        planChanged = pyqtSignal()
        inputsChanged = pyqtSignal()

        def __init__(self):
            super().__init__()
            self.plan = {}
            self.inputs = []
            self.route = None
            self.route_notice = ""
            self.route_error = ""

        def depth_config(self):
            return DepthSourceConfig({})

    widget = InputsTab(_Model(), lambda: None)
    assert not hasattr(widget, "inherit_check")
    # No Workbench: the route source opens on the line-layer picker, and
    # only the chosen source's picker is shown.
    assert widget.route_layer_radio.isChecked()
    assert widget.route_pages.currentIndex() == 1
    widget.route_workbench_radio.click()
    assert widget.route_pages.currentIndex() == 0
    assert "Configure bathymetry" in widget.apply_bathy_button.text()
    widget.close()
    widget.deleteLater()

    from ..burial.tabs.bathy_dialog import BathymetryDialog

    dialog = BathymetryDialog(None, DepthSourceConfig({}))
    assert dialog.raster_radio.isChecked()
    assert not dialog.ok_button.isEnabled()
    assert dialog.search_radius.minimum() > 0
    dialog.set_mode(2)
    assert dialog.pages.currentIndex() == 1
    dialog.deleteLater()


def test_burial_installation_paths_widgets_construct():
    import os
    import tempfile
    import time

    visibility_key = "SubseaCableTools/BurialPlanner/dcc_plot_visible"
    settings = QSettings()
    had_visibility = settings.contains(visibility_key)
    saved_visibility = settings.value(visibility_key)
    settings.remove(visibility_key)
    path = os.path.join(
        tempfile.gettempdir(),
        f"bp_paths_widget_{os.getpid()}_{int(time.time() * 1000)}.gpkg")
    store = BurialStore(path, QgsProject.instance().transformContext())
    store.migrate()
    widget = PathsTab(PlanModel(store))
    assert widget.mode_combo.count() == 2
    assert widget.mode_combo.itemData(1) == "through_ac"
    assert "course change" in widget.mode_combo.itemText(1).lower()
    assert widget.vessel_combo.count() == 1  # placeholder only
    assert "Constant tool radius" in widget.radius_rules_label.text()
    assert not widget.show_dcc.isChecked()  # first-use default is off
    assert widget.results_splitter.count() == 2
    assert widget.dcc_plot.maximumHeight() > 230  # user-sized, not capped
    # Laptop-height layout: settings scroll above results on a splitter,
    # with collapsible (checkable) settings groups.
    assert widget.tab_splitter.count() == 2
    assert not widget.tab_splitter.childrenCollapsible() \
        or widget.tab_splitter.widget(1) is not None
    from qgis.PyQt.QtWidgets import QGroupBox, QScrollArea
    assert isinstance(widget.tab_splitter.widget(0), QScrollArea)
    boxes = widget.tab_splitter.widget(0).findChildren(QGroupBox)
    assert len(boxes) >= 3 and all(box.isCheckable() for box in boxes)
    dialog = LaybackProfileDialog({
        "name": "Test", "points_json": "[[0,50],[100,150]]",
        "outside_mode": "hold"})
    assert dialog.table.rowCount() == 2
    assert dialog.outside_combo.currentData() == "hold"
    dialog.close()
    dialog.deleteLater()
    rules_dialog = RadiusRulesDialog([
        {"max_depth_m": 1000.0, "radius_m": 1150.0},
        {"max_depth_m": 100.0, "radius_m": 950.0}])
    assert rules_dialog.table.rowCount() == 2
    assert rules_dialog.rules()[0] == {"max_depth_m": 100.0,
                                       "radius_m": 950.0}
    rules_dialog.close()
    rules_dialog.deleteLater()
    vessel_dialog = VesselDialog({
        "name": "CLV Test", "min_turn_radius_m": 950.0,
        "footprint_wkt": "LINESTRING (0 -50, 0 50)",
        "footprint_source": "test.dxf", "length_m": 100.0, "width_m": 20.0})
    assert vessel_dialog.name_edit.text() == "CLV Test"
    assert abs(vessel_dialog.radius_spin.value() - 950.0) < 1e-9
    assert "test.dxf" in vessel_dialog.outline_label.text()
    payload = vessel_dialog.payload()
    assert payload["footprint_wkt"].startswith("LINESTRING")
    vessel_dialog.close()
    vessel_dialog.deleteLater()
    widget.shutdown()
    widget.close()
    widget.deleteLater()
    store.close()
    if had_visibility:
        settings.setValue(visibility_key, saved_visibility)
    else:
        settings.remove(visibility_key)


def test_workbench_rule_layer_filters_construct():
    for kind in (
            schema.RULE_KIND_PROXIMITY,
            schema.RULE_KIND_POLYGON,
            schema.RULE_KIND_KP_TABLE):
        dialog = RuleEditorDialog(
            {"kind": kind, "name": kind, "config_json": "{}"},
            ["plough"],
        )
        assert dialog.layer_combo is not None
        dialog.close()
        dialog.deleteLater()


def test_burial_attribute_rule_editors_round_trip():
    import json

    # Risk check: value / range (explicit bounds) / expression rows survive
    # the editor unchanged, and the field pickers accept free text with no
    # layer loaded.
    rules = [
        {"min": 0.0, "max": 5.0, "min_inclusive": True,
         "max_inclusive": False, "risk": "high"},
        {"match": "WRECK", "risk": "high"},
        {"expression": '"Height_m" > 2', "risk": "medium"},
        {"min": 5.0, "risk": "low"},   # legacy: no flags = inclusive
    ]
    check = {"name": "Boulders", "config_json": json.dumps({
        "kind": "features", "input_id": "", "distance_m": 50.0,
        "attribute": "Height_m", "attribute_rules": rules,
        "filter_expression": '"Status" = \'live\'',
        "label_attribute": "Name", "default_risk": "low"})}
    dialog = CheckEditorDialog(check, [])
    assert dialog.attribute_edit.text() == "Height_m"
    assert dialog.label_edit.text() == "Name"
    assert dialog.filter_edit.text() == '"Status" = \'live\''
    assert dialog.rules_table.row_count() == 4
    assert not dialog.rules_table.invalid_rows()
    out = json.loads(dialog.result_check()["config_json"])
    assert out["attribute"] == "Height_m"
    assert out["attribute_rules"] == [
        {"min": 0.0, "min_inclusive": True, "max": 5.0,
         "max_inclusive": False, "risk": "high"},
        {"match": "WRECK", "risk": "high"},
        {"expression": '"Height_m" > 2', "risk": "medium"},
        {"min": 5.0, "min_inclusive": True, "risk": "low"},
    ], out["attribute_rules"]
    assert out["filter_expression"] == '"Status" = \'live\''
    assert dialog.rules_table.describe()[0] == "0 \u2264 Height_m < 5"
    dialog.close()
    dialog.deleteLater()

    # Structured table: a bad expression and an inverted range are
    # reported (not silently dropped); a new range row defaults to [a, b).
    table = AttributeRulesTable(with_kind=True, with_risk=True)
    table.add_row({"expression": "this is not (an expression"})
    table.add_row({"min": 9, "max": 1})
    problems = table.invalid_rows()
    assert len(problems) == 2, problems
    table.set_rules([{"min": 1, "max": 2}])
    table.add_row({})
    table.table.item(1, table.col_value).setText("3")
    table.table.item(1, table.col_to).setText("4")
    kind_combo = table.table.cellWidget(1, table.col_kind)
    kind_combo.setCurrentIndex(kind_combo.findData("range"))
    got = table.rules()
    assert got[0] == {"min": 1.0, "min_inclusive": True, "max": 2.0,
                      "max_inclusive": True, "risk": "low"}, got
    assert got[1] == {"min": 3.0, "min_inclusive": True, "max": 4.0,
                      "max_inclusive": False, "risk": "low"}, got
    table.deleteLater()

    # Exclusion polygon rule: values + ranges + expression round trip; the
    # legacy exact-values path is unchanged and an empty expression is
    # not stored.
    rule = {"kind": schema.RULE_KIND_POLYGON, "name": "Soils",
            "criterion_class": "project",
            "config_json": json.dumps({
                "input_id": "", "attribute": "GRADE",
                "match_values": ["ROCK"],
                "match_rules": [{"min": 5.0, "min_inclusive": True}]})}
    dialog = BurialRuleEditorDialog(rule, [], "plough")
    assert dialog.attribute_field.text() == "GRADE"
    assert dialog.ranges_table.row_count() == 1
    assert dialog.attribute_field.isEnabled()
    out = json.loads(dialog.result_rule()["config_json"])
    assert out["attribute"] == "GRADE"
    assert out["match_values"] == ["ROCK"]
    assert out["match_rules"] == [{"min": 5.0, "min_inclusive": True}], out
    assert "match_expression" not in out
    dialog.match_expression_edit.setText('"GRADE" >= 5')
    assert not dialog.attribute_field.isEnabled()
    out = json.loads(dialog.result_rule()["config_json"])
    assert out["match_expression"] == '"GRADE" >= 5'
    dialog.close()
    dialog.deleteLater()

    for kind in (schema.RULE_KIND_PROXIMITY, schema.RULE_KIND_KP_TABLE):
        dialog = BurialRuleEditorDialog(
            {"kind": kind, "name": kind, "config_json": "{}"}, [], "plough")
        assert dialog.filter_edit.text() == ""
        dialog.close()
        dialog.deleteLater()


def test_burial_profile_true_scale_toggle():
    widget = BurialProfileWidget()
    widget.set_scope(0.0, 10.0)
    widget.set_profile([(0.0, 100.0), (5.0, 1500.0), (10.0, 200.0)])
    vb = widget.plot.getPlotItem().vb
    assert vb.state["aspectLocked"] is False
    widget.set_true_scale(True)
    assert widget.true_scale()
    assert widget._true_scale_action.isChecked()
    assert float(vb.state["aspectLocked"]) == 1000.0
    widget.reset_scope_view()            # keeps the lock
    assert float(vb.state["aspectLocked"]) == 1000.0
    widget._true_scale_action.trigger()  # menu entry toggles it back off
    assert not widget.true_scale()
    assert vb.state["aspectLocked"] is False
    assert vb.state["autoRange"][1]
    widget.deleteLater()


def test_lay_simulator_tables_construct():
    dialog = LaySimulatorDialog()
    assert dialog.windowTitle()
    dialog.close()
    dialog.deleteLater()


def test_bu_lowering_tool_constructs_and_builds_config():
    dialog = BULoweringDialog()
    try:
        assert dialog.windowTitle()
        # Own settings scope — never the main simulator's.
        assert dialog.settings.applicationName() == "BULoweringTool"
        cfg = dialog.build_config("quick")
        assert cfg.mode == "operation"
        assert cfg.scenario == "bu_deployment"
        assert cfg.op["quality"] == "quick"
        assert "integration" in cfg.op and cfg.op["integration"]["trunk"]["items"]
        assert cfg.current_layers == []          # no drag inputs in this tool
        assert cfg.chute_radius_m == float(dialog.sheave_radius.value())
        assert dialog.build_config("full").op["quality"] == "full"
    finally:
        dialog._save_settings = lambda: None     # don't write user settings
        dialog.close()
        dialog.deleteLater()


def test_cable_lay_explorer_panels_construct():
    class _Iface:
        def __init__(self):
            self.canvas = QgsMapCanvas()

        def mapCanvas(self):
            return self.canvas

    window = CableLayExplorerWindow(_Iface())
    assert window.table_panel is not None
    assert window.qc_panel is not None
    assert window.inspection_panel is not None
    window.shutdown()


def test_workbench_cable_type_dialog_constructs():
    """The colour editor builds with no registry and lists the known types."""
    previous = layer_style.user_cable_type_colours()
    try:
        layer_style.set_user_cable_type_colours({"XYZ": "#010203"})
        dialog = CableTypeColourDialog(None)
        assert dialog.table.rowCount() >= len(layer_style.KNOWN_CABLE_TYPE_COLOURS)
        tokens = {dialog.table.item(row, 0).text()
                  for row in range(dialog.table.rowCount())}
        assert "XYZ" in tokens and "DA" in tokens
        sources = {dialog.table.item(row, 0).text(): dialog.table.item(row, 2).text()
                   for row in range(dialog.table.rowCount())}
        assert sources["XYZ"] == "Custom"
        assert sources["DA"] == "Standard"
        assert dialog.palette() == {"xyz".upper(): "#010203"}
        dialog.deleteLater()
    finally:
        layer_style.set_user_cable_type_colours(previous)


def test_workbench_compare_panel_constructs():
    panel = RevisionComparePanel()
    panel.load_segment(None, "")
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == [
        "Statistics", "Positions", "Legs"]
    assert not panel.export_btn.isEnabled()
    panel.set_visible_tab(True)          # nothing selected: must not raise
    panel.deleteLater()


def run_all():
    if QApplication.instance() is None:
        print("[SKIP] compatibility widget checks need QApplication")
        return []
    failures = []
    for test in (
            test_burial_inputs_construct_and_switch_source_type,
            test_burial_installation_paths_widgets_construct,
            test_workbench_rule_layer_filters_construct,
            test_burial_attribute_rule_editors_round_trip,
            test_burial_profile_true_scale_toggle,
            test_lay_simulator_tables_construct,
            test_bu_lowering_tool_constructs_and_builds_config,
            test_cable_lay_explorer_panels_construct,
            test_workbench_cable_type_dialog_constructs,
            test_workbench_compare_panel_constructs):
        try:
            test()
            QApplication.processEvents()
            print("[PASS] %s" % test.__name__)
        except Exception as exc:
            print("[FAIL] %s - %r" % (test.__name__, exc))
            failures.append(test.__name__)
    return failures


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
