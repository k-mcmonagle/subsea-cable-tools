# -*- coding: utf-8 -*-
"""Construction checks for widgets using QGIS 3/4 compatibility aliases."""

from __future__ import annotations

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import QApplication
from qgis.gui import QgsMapCanvas

from ..catenary.v3.ui.bu_lowering_dialog import BULoweringDialog
from ..catenary.v3.ui.dialog import LaySimulatorDialog
from ..burial.tabs.inputs_tab import InputsTab
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
