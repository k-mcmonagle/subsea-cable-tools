# -*- coding: utf-8 -*-
"""QGIS checks for RPL segment reading, clipping, grouping, and preview drafts."""

from __future__ import annotations

import os
import tempfile

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import (
    QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer,
)

from ..planner import rpl_import as rpl_import_module
from ..planner.rpl_import import (
    DEFAULT_OPERATION_RULES, RplImportDialog, RplSource, _read_segments,
    _rule_operation,
)
from ..planner.store import PlannerStore
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _layers():
    points = QgsVectorLayer("Point?crs=EPSG:4326", "RPL Points", "memory")
    points.dataProvider().addAttributes([
        QgsField("PosNo", FIELD_TYPE_INT), QgsField("DistCumulative", FIELD_TYPE_DOUBLE)])
    points.updateFields()
    for pos, kp, x in ((1, 10.0, 0.0), (2, 11.0, 0.01),
                       (3, 12.0, 0.02), (4, 13.0, 0.03)):
        feature = QgsFeature(points.fields())
        feature.setAttributes([pos, kp])
        feature.setGeometry(QgsGeometry.fromWkt("POINT(%s 0)" % x))
        points.dataProvider().addFeature(feature)

    lines = QgsVectorLayer("LineString?crs=EPSG:4326", "RPL Lines", "memory")
    lines.dataProvider().addAttributes([
        QgsField("SeqNo", FIELD_TYPE_INT), QgsField("FromPos", FIELD_TYPE_INT),
        QgsField("ToPos", FIELD_TYPE_INT), QgsField("CableType", FIELD_TYPE_STRING),
        QgsField("CableCode", FIELD_TYPE_STRING),
        QgsField("ProtectionMethod", FIELD_TYPE_STRING),
        QgsField("LayVessel", FIELD_TYPE_STRING),
    ])
    lines.updateFields()
    rows = [
        (1, 1, 2, "LW", "A", "Surface lay", "V1", 0.0, 0.01),
        (2, 2, 3, "LW", "A", "Surface lay", "V1", 0.01, 0.02),
        (3, 3, 4, "DA", "B", "Plough", "V1", 0.02, 0.03),
    ]
    for row in rows:
        feature = QgsFeature(lines.fields())
        feature.setAttributes(list(row[:7]))
        feature.setGeometry(QgsGeometry.fromWkt(
            "LINESTRING(%s 0, %s 0)" % (row[7], row[8])))
        lines.dataProvider().addFeature(feature)
    return points, lines


def test_workbench_kp_reading():
    points, lines = _layers()
    segments = _read_segments(RplSource("Test", "workbench", lines, points, "r1"))
    ok = len(segments) == 3
    ok = ok and [segment.kp_start for segment in segments] == [10.0, 11.0, 12.0]
    ok = ok and [segment.kp_end for segment in segments] == [11.0, 12.0, 13.0]
    return _result("Workbench point KPs mapped to ordered line segments", ok)


def test_dialog_grouping_range_and_speeds():
    project = QgsProject.instance()
    project.removeAllMapLayers()
    points, lines = _layers()
    project.addMapLayer(points)
    project.addMapLayer(lines)
    folder = tempfile.mkdtemp(prefix="pow_rpl_import_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    resources = [{"resource_id": "v1", "name": "Vessel 1", "default_speed_kn": 1.2}]
    dialog = RplImportDialog(store, resources)
    index = next((i for i in range(dialog.source_combo.count())
                  if dialog.source_combo.itemData(i).line_layer is lines), -1)
    dialog.source_combo.setCurrentIndex(index)
    drafts = dialog.task_drafts()
    ok = len(drafts) == 2  # consecutive LW pair, then DA section
    ok = ok and all(abs(draft["speed_knots"] - 1.2) < 1e-9 for draft in drafts)
    ok = ok and all(draft["operation"] == "Lay" for draft in drafts)
    dialog.operation_combo.setCurrentIndex(dialog.operation_combo.findData("Plough"))
    ok = ok and all(draft["operation"] == "Plough" for draft in dialog.task_drafts())
    first_operation = dialog.preview.cellWidget(0, 1)
    first_operation.setCurrentIndex(first_operation.findData("ROV"))
    overridden = dialog.task_drafts()
    ok = ok and overridden[0]["operation"] == "ROV"
    ok = ok and overridden[1]["operation"] == "Plough"
    dialog.group_combo.setCurrentIndex(dialog.group_combo.findData("segment"))
    ok = ok and len(dialog.task_drafts()) == 3
    dialog.start_spin.setValue(dialog.start_spin.minimum() + 0.002)
    clipped = dialog.task_drafts()
    ok = ok and clipped and clipped[0]["length_m"] < drafts[0]["length_m"]
    dialog.close()
    return _result("RPL grouping + speed defaults + range clipping", ok)


def test_saved_operation_rule_matching():
    ok = _rule_operation("Post-lay ROV jet burial", DEFAULT_OPERATION_RULES) == "ROV"
    ok = ok and _rule_operation("Plough burial", DEFAULT_OPERATION_RULES) == "Plough"
    ok = ok and _rule_operation("Surface lay", DEFAULT_OPERATION_RULES) == "Lay"
    return _result("ProtectionMethod operation rules", ok)


def test_empty_state_and_source_refresh():
    points, lines = _layers()
    folder = tempfile.mkdtemp(prefix="pow_rpl_source_refresh_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    available = []
    original_discover = rpl_import_module._discover_sources
    rpl_import_module._discover_sources = lambda _store, errors=None: list(available)
    dialog = None
    try:
        dialog = RplImportDialog(store, [])
        ok = dialog.source_combo.count() == 1
        ok = ok and dialog.source_combo.currentData() is None
        ok = ok and not dialog.source_combo.isEnabled()
        ok = ok and not dialog.ok_button.isEnabled()
        ok = ok and "No route sources are available" in dialog.status.text()

        available.append(RplSource(
            "Project layer: RPL Lines", "project", lines, points))
        dialog._refresh_sources()
        ok = ok and dialog.source_combo.isEnabled()
        ok = ok and dialog.source_combo.currentData().line_layer is lines
        ok = ok and dialog.ok_button.isEnabled()
        ok = ok and len(dialog.task_drafts()) == 2
    finally:
        if dialog is not None:
            dialog.close()
        rpl_import_module._discover_sources = original_discover
    return _result("Planner RPL empty state + source refresh", ok)


def test_import_wizard_handoff_selects_new_rpl():
    class FakeWizard(QDialog):
        imported = pyqtSignal(str)

        def __init__(self, _store, _iface, parent=None):
            super().__init__(parent)

    points, lines = _layers()
    other_points, other_lines = _layers()
    other_lines.setName("New RPL Lines")
    folder = tempfile.mkdtemp(prefix="pow_rpl_wizard_handoff_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    available = [RplSource(
        "Workbench: Existing", "workbench", lines, points, "existing-rpl")]
    original_discover = rpl_import_module._discover_sources
    rpl_import_module._discover_sources = lambda _store, errors=None: list(available)

    def execute(wizard):
        available.append(RplSource(
            "Workbench: Newly imported", "workbench",
            other_lines, other_points, "new-rpl"))
        wizard.imported.emit("new-rpl")
        return 0

    dialog = None
    try:
        dialog = RplImportDialog(
            store, [], wizard_factory=FakeWizard, dialog_exec=execute)
        dialog._import_new_rpl()
        selected = dialog.source_combo.currentData()
        ok = selected is not None and selected.rpl_id == "new-rpl"
        ok = ok and selected.line_layer is other_lines
        ok = ok and len(dialog.task_drafts()) == 2
    finally:
        if dialog is not None:
            dialog.close()
        rpl_import_module._discover_sources = original_discover
    return _result("guided import refreshes and selects new Workbench RPL", ok)


def run_all():
    return [test_workbench_kp_reading(), test_dialog_grouping_range_and_speeds(),
            test_saved_operation_rule_matching(), test_empty_state_and_source_refresh(),
            test_import_wizard_handoff_selects_new_rpl()]
