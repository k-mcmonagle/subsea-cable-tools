# -*- coding: utf-8 -*-
"""Construction checks for widgets using QGIS 3/4 compatibility aliases."""

from __future__ import annotations

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsProject
from qgis.gui import QgsMapCanvas

from ..catenary.v3.ui.bu_lowering_dialog import BULoweringDialog
from ..catenary.v3.ui.dialog import LaySimulatorDialog
from ..burial.tabs.inputs_tab import InputsTab
from ..burial.tabs.paths_tab import (
    LaybackProfileDialog,
    PathsTab,
    RadiusRulesDialog,
    VesselDialog,
)
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..explorer import CableLayExplorerWindow
from ..workbench import schema
from ..workbench.assessment_panel import RuleEditorDialog
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
    assert widget.contour_combo2 is not None
    assert not hasattr(widget, "inherit_check")
    assert widget.search_radius.minimum() > 0
    widget.manual_source_combo.setCurrentIndex(
        widget.manual_source_combo.findData(2))
    assert widget.contour_combo.isEnabled()
    assert widget.contour_combo2.isEnabled()
    assert not widget.raster_combo.isEnabled()
    widget.close()
    widget.deleteLater()


def test_burial_installation_paths_widgets_construct():
    import os
    import tempfile
    import time

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


def run_all():
    if QApplication.instance() is None:
        print("[SKIP] compatibility widget checks need QApplication")
        return []
    failures = []
    for test in (
            test_burial_inputs_construct_and_switch_source_type,
            test_burial_installation_paths_widgets_construct,
            test_workbench_rule_layer_filters_construct,
            test_lay_simulator_tables_construct,
            test_bu_lowering_tool_constructs_and_builds_config,
            test_cable_lay_explorer_panels_construct):
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
