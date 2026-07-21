# -*- coding: utf-8 -*-
"""QGIS checks for RPL segment reading, clipping, grouping, and preview drafts."""

from __future__ import annotations

import os
import tempfile

from qgis.core import (
    QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer,
)

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


def run_all():
    return [test_workbench_kp_reading(), test_dialog_grouping_range_and_speeds(),
            test_saved_operation_rule_matching()]
